import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from check_delay import check as check_delay
from lab_common import CANCEL_OBSERVE, CANCEL_WRITE_DELAY, EXPECTED, PROMPT, SYSTEM, save_json
from summarize import summarize


ROOT = Path(__file__).resolve().parent
WEIGHTS = Path.home() / ".lmstudio/models/lmstudio-community/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf"
ENGINE = Path.home() / ".lmstudio/extensions/backends/llama.cpp-win-x86_64-avx2-2.69.0/llama-server.exe"
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FRAMES = ["pydantic", "nanobot", "langgraph"]
FOLDERS = ["direct", *FRAMES]


def send(url, body=None, timeout=3):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with CLIENT.open(request, timeout=timeout) as response:
        return json.load(response)


def build_plan(pilot, restart_only=None):
    if pilot:
        return [(candidate, "draft", 0) for candidate in pilot]
    if restart_only:
        return [(candidate, mode, 1) for candidate in restart_only for mode in ("prepare", "resume")]
    plan = []
    for i in range(4):
        plan.extend((candidate, "draft", i + 1) for candidate in FOLDERS[i:] + FOLDERS[:i])
    for i in range(3):
        plan.extend((candidate, "cancel", i + 1) for candidate in FRAMES[i:] + FRAMES[:i])
    for candidate in FRAMES:
        plan.extend((candidate, mode, 1) for mode in ["prepare", "resume"])
    return plan


def run_one(folder, env, candidate, mode, index):
    script = ROOT / candidate / "bench.py" if candidate != "direct" else ROOT / "direct.py"
    python = ROOT / candidate / ".venv/Scripts/python.exe" if candidate != "direct" else Path(sys.executable)
    phase = "restart" if mode in ("prepare", "resume") else mode
    out = folder / candidate / f"{phase}-{index:02}"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(python), str(script), mode, "--out", str(out)]
    started = time.perf_counter()
    try:
        limit = 480 if mode in ("prepare", "resume") else 240
        result = subprocess.run(cmd, cwd=script.parent, env=env, timeout=limit,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        log, code = result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        log = f"{limit} 秒超时\n{error.stdout!r}\n{error.stderr!r}"
        code = 124
    (out / f"{mode}.log").write_text(log, encoding="utf-8")
    filename = f"recovery-{mode}.json" if mode in ("prepare", "resume") else "result.json"
    evidence = out / filename
    valid = evidence.is_file() and json.loads(evidence.read_text(encoding="utf-8")).get("passed", mode == "prepare") is True
    return {"candidate": candidate, "mode": mode, "index": index, "exit": code, "verified": valid, "dir": str(out.relative_to(folder)),
            "process_s": round(time.perf_counter() - started, 4)}


def run(args):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    prefix = "pilot" if args.pilot else ("restart" if args.restart_only else "benchmark")
    folder = ROOT / "runs" / f"{prefix}-{stamp}"
    for path in [args.engine, args.weights]:
        if not path.is_file():
            raise FileNotFoundError(path)
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", args.port))
    folder.mkdir(parents=True)
    slots = folder / "slots"
    slots.mkdir()
    url = f"http://127.0.0.1:{args.port}"
    model = "moss-agent-lab-qwen"
    cmd = [str(args.engine), "-m", str(args.weights), "--alias", model, "--host", "127.0.0.1",
           "--port", str(args.port), "--n-gpu-layers", "0", "--parallel", "1", "--ctx-size", "4096",
           "--threads", "8", "--threads-batch", "8", "--reasoning", "off", "--jinja",
           "--cache-prompt", "--cache-ram", "0", "--slot-save-path", str(slots)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "MOSS_BASE_URL": url + "/v1", "MOSS_MODEL": model, "NO_PROXY": "127.0.0.1,localhost",
           "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false"}
    info = {"start": dt.datetime.now().astimezone().isoformat(), "os": platform.platform(),
            "python": sys.version, "model": model, "weights_bytes": args.weights.stat().st_size,
            "server_command": cmd, "port": args.port, "prompt": PROMPT, "system": SYSTEM, "expected": EXPECTED,
            "cancel_write_delay_s": CANCEL_WRITE_DELAY, "cancel_observe_s": CANCEL_OBSERVE,
            "plan": build_plan(args.pilot, args.restart_only), "executions": []}
    with args.weights.open("rb") as fp:
        info["weights_sha256"] = hashlib.file_digest(fp, "sha256").hexdigest()
    console = folder / "server.log"
    asyncio.run(check_delay(folder / "delay-control"))
    start = time.perf_counter()
    print(folder, flush=True)
    with console.open("wb") as log, subprocess.Popen(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
              env=env, creationflags=subprocess.CREATE_NO_WINDOW) as server:
        info["server_pid"] = server.pid
        try:
            for _ in range(180):
                if server.poll() is not None:
                    raise RuntimeError(f"推理进程退出 {server.returncode}，详见 {console}")
                try:
                    if send(url + "/health").get("status") == "ok":
                        break
                except (OSError, TimeoutError):
                    pass
                time.sleep(0.5)
            else:
                raise TimeoutError("模型启动超时")
            info["server_ready_s"] = round(time.perf_counter() - start, 3)
            info["models"] = send(url + "/v1/models")
            warm_start = time.perf_counter()
            info["warmup"] = send(url + "/v1/chat/completions", {"model": model, "temperature": 0,
                                  "messages": [{"role": "user", "content": "只回复：好"}], "max_tokens": 12}, timeout=90)
            info["warmup_s"] = round(time.perf_counter() - warm_start, 3)
            save_json(folder / "manifest.json", info)
            for candidate, mode, index in info["plan"]:
                reset = send(url + "/slots/0?action=erase", {})
                if reset.get("id_slot") != 0:
                    raise RuntimeError(f"无法清除前一条样本的模型上下文 {reset}")
                offset = console.stat().st_size
                item = run_one(folder, env, candidate, mode, index)
                item["slot_reset"] = reset
                with console.open("rb") as engine_log:
                    engine_log.seek(offset)
                    (folder / item["dir"] / f"{mode}-server.log").write_bytes(engine_log.read())
                info["executions"].append(item)
                save_json(folder / "manifest.json", info)
                print(candidate, mode, index, "exit", item["exit"], flush=True)
        finally:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=20)
            info["server_terminated"] = server.poll() is not None
            info["finish"] = dt.datetime.now().astimezone().isoformat()
            save_json(folder / "manifest.json", info)
    print("结果目录", folder, flush=True)
    return int(not summarize(folder))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, default=ENGINE)
    parser.add_argument("--weights", type=Path, default=WEIGHTS)
    parser.add_argument("--port", type=int, default=18176)
    subset = parser.add_mutually_exclusive_group()
    subset.add_argument("--pilot", nargs="+", choices=FOLDERS)
    subset.add_argument("--restart-only", nargs="+", choices=FRAMES)
    sys.exit(run(parser.parse_args()))
