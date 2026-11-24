import argparse
from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
from statistics import median
from time import perf_counter_ns
from unittest.mock import patch
from urllib.request import Request

import psutil

from local_media.library import MediaLibrary
from memory_store import install_schema
import windows_reminders
from windows_reminders import LOCAL_HTTP, WindowsReminders


ROOT = Path(__file__).resolve().parent
MODULES = ("core", "model_catalog", "holo_kernel", "window_image")
ORDER = ("original", "current", "current", "original")
GREETING = "你在吗？"

# The two arms execute the same serve.py and use the same import loader. It takes
# four source files from the selected tree while preserving their deployed paths.
ENTRYPOINT = """
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
from time import perf_counter_ns

root = Path({root!r})
source = Path({source!r})
trace = Path({trace!r})
modules = {modules!r}
model_port = {model_port}
sys.path.insert(0, str(root))

with Path({log!r}).open("w", encoding="utf-8", buffering=1) as output:
    sys.stdout = sys.stderr = output
    events = []
    unexpected_connections = []

    def record(stage):
        watched = ("openai", "httpx2", "pydantic_ai", "pydantic_ai.models.openai")
        events.append(dict(stage=stage, ns=perf_counter_ns(),
                           imports={{name:name in sys.modules for name in watched}}))

    def publish():
        trace.write_text(json.dumps(dict(pid=os.getpid(), sources=loaded, stages=events,
                                         unexpected_connections=unexpected_connections)), encoding="utf-8")

    def network_guard(event, args):
        if event != "socket.connect" or not isinstance(args[1], tuple):
            return
        host, port = args[1][:2]
        if host not in ("127.0.0.1", "::1") or port == model_port:
            unexpected_connections.append("model" if port == model_port else "nonloopback")
            raise OSError("模型启动对照禁止连接模型和外网")

    loaded = {{}}

    class SelectedSource(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            file = source / (fullname + ".py")
            loaded[fullname] = str(file)
            return self.source_to_code(file.read_bytes(), self.path)

        def exec_module(self, module):
            super().exec_module(module)
            record(module.__name__ + "_imported")

    class CompareSource:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in modules:
                path = str(root / (fullname + ".py"))
                return importlib.util.spec_from_file_location(fullname, path, loader=SelectedSource(fullname, path))

    record("entry")
    sys.addaudithook(network_guard)
    sys.meta_path.insert(0, CompareSource())
    from aiohttp import web
    record("aiohttp_imported")
    site_start = web.TCPSite.start
    app_handle = web.Application._handle

    async def measured_site(site):
        await site_start(site)
        record("site_bound")
        publish()

    async def measured_request(app, request):
        response = await app_handle(app, request)
        if request.path == "/api/message":
            record("greeting_complete")
            publish()
        return response

    web.TCPSite.start = measured_site
    web.Application._handle = measured_request
    runpy.run_path(str(root / "serve.py"), run_name="__main__")
"""


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(profile, path, data=None):
    message = Request(f"http://127.0.0.1:{profile.port}{path}",
                      data=None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with LOCAL_HTTP.open(message, timeout=5) as response:
        return json.load(response)


def shutdown(profile, spawned):
    try:
        if owner := profile.owner():
            request(profile, "/api/shutdown", {"token": owner["token"]})
            psutil.Process(owner["pid"]).wait(10)
    except psutil.NoSuchProcess:
        pass
    finally:
        # ensure_server can fail before writing instance.json. Only target the
        # process launched in this isolated trial and the children we can trace.
        for launcher in spawned:
            if launcher.poll() is None:
                try:
                    launcher.wait(2)
                except windows_reminders.subprocess.TimeoutExpired:
                    process = psutil.Process(launcher.pid)
                    children = process.children(recursive=True)
                    for child in reversed(children):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    launcher.terminate()
                    launcher.wait(5)
                    psutil.wait_procs(children, timeout=5)
    assert not profile.owner() and not profile.shortcut.exists()


def trial(folder, source, seed, row):
    folder.mkdir()
    data = folder / "data"
    data.mkdir()
    shutil.copyfile(seed, data / "session.sqlite3")
    port, model_port = free_port(), free_port()
    while model_port == port:
        model_port = free_port()
    catalog = folder / "media.sqlite3"
    MediaLibrary(catalog)
    profile = WindowsReminders(data, port)
    profile.launch_file.write_text(json.dumps({
        "port": port, "model_url": f"http://127.0.0.1:{model_port}/v1",
        "model": "isolated-startup-unused", "media_db": str(catalog), "speech_prepare": "0",
    }), encoding="utf-8")
    trace = folder / "trace.json"
    (folder / "serve.py").write_text(ENTRYPOINT.format(root=str(ROOT), source=str(source), trace=str(trace),
        modules=MODULES, log=str(folder / "server.log"), model_port=model_port), encoding="utf-8")
    spawned = []
    popen = windows_reminders.subprocess.Popen

    def spawn(*args, **kwargs):
        launcher = popen(*args, **kwargs)
        spawned.append(launcher)
        return launcher

    assert not profile.owner()
    trial_env = {"LOCALAPPDATA": str(folder / "appdata"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                 "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0", "MOSS_KEV_ENABLED": "0",
                 "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        with (patch.dict(os.environ, trial_env), patch.object(windows_reminders, "ROOT", folder),
              patch.object(windows_reminders.subprocess, "Popen", spawn)):
            began = perf_counter_ns()
            pid = profile.ensure_server(startup_timeout=32)
            ready = perf_counter_ns()
        row.update(pid=pid, port=port, owner_ready_ms=round((ready-began)/1e6, 2))
        owner = profile.owner()
        assert owner and owner["pid"] == pid
        assert request(profile, "/api/health")["model"] == "isolated-startup-unused"
        state = request(profile, "/api/state")
        assert not state["turns"]
        started = perf_counter_ns()
        greeted = request(profile, "/api/message", {"chat_id": state["chat_id"], "text": GREETING})
        finished = perf_counter_ns()
        turn, = greeted["turns"]
        reply = row["reply"] = {key:turn[key] for key in ("status", "source", "reply", "cloud_requests")}
        assert (reply["status"], reply["source"], reply["reply"]) == ("replied", "smalltalk", "我在。"), reply
        assert reply["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, reply
        row["greeting_ms"] = round((finished-started)/1e6, 2)
        row["startup_through_greeting_ms"] = round((finished-began)/1e6, 2)
        measured = json.loads(trace.read_text(encoding="utf-8"))
        assert measured["pid"] == pid and not measured["unexpected_connections"], measured
        assert measured["sources"].get("core") == str(source / "core.py"), measured["sources"]
        assert all(Path(file).parent == source for file in measured["sources"].values()), measured["sources"]
        row["stages"] = {event["stage"]: {
            "from_start_ms": round((event["ns"]-began)/1e6, 2), "imports": event["imports"],
        } for event in measured["stages"]}
        assert "site_bound" in row["stages"] and "greeting_complete" in row["stages"]
        row["source_paths"] = measured["sources"]
        row["working_set_mib"] = round(psutil.Process(pid).memory_info().rss / 2**20, 1)
    finally:
        shutdown(profile, spawned)


def run(baseline):
    for root in (baseline, ROOT):
        for module in MODULES:
            assert (root / (module + ".py")).is_file(), root / (module + ".py")
    assert baseline != ROOT
    folder = ROOT / "runs" / datetime.now().strftime("lazy-model-startup-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    sources = {"original": baseline, "current": ROOT}
    report = {"passed": False, "order": ORDER, "isolated_browser": False, "real_model": False, "trials": [],
              "source_sha256": {arm:{name:sha256((root / (name + ".py")).read_bytes()).hexdigest()
                                 for name in MODULES} for arm, root in sources.items()}}
    try:
        seed = folder / "seed.sqlite3"
        with closing(sqlite3.connect(seed)) as db:
            report["seed_chat"] = install_schema(db)
        for index, arm in enumerate(ORDER, 1):
            row = {"arm": arm}
            report["trials"].append(row)
            trial(folder / f"{index}-{arm}", sources[arm], seed, row)
            print(f"{arm} {row['owner_ready_ms']:.2f} ms, greeting {row['greeting_ms']:.2f} ms", flush=True)
        timing = ("owner_ready_ms", "greeting_ms", "startup_through_greeting_ms", "working_set_mib")
        report["medians"] = {arm: {key:round(median(row[key] for row in report["trials"] if row["arm"] == arm), 2)
                                    for key in timing} for arm in sources}
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="比较两版源码的 Windows 后台服务启动与本地问候耗时。")
    parser.add_argument("--baseline", required=True, type=lambda value:Path(value).resolve())
    run(parser.parse_args().baseline)
