import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from time import perf_counter

import aiohttp
import psutil

from browser_lab import ROOT
from windows_reminders import WindowsReminders


CHAT = "用中文写四句解释潮水涨落的基础知识。每句话不超过二十字，不要编号。"
DEVICE_LINE = re.compile(r"using device\s+(Vulkan[0-9]+)\s+([^\r\n]+)")
LAYER_LINE = re.compile(r"offloaded\s+([0-9]+)/([0-9]+)\s+layers to GPU")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def json_response(client, base, path, payload=None):
    request = client.get(base + path) if payload is None else client.post(base + path, json=payload)
    async with request as response:
        body = await response.json()
        assert response.status == 200, (path, response.status, body)
        return body


@asynccontextmanager
async def server(client, data, port, engine_port, device, serial):
    base = f"http://127.0.0.1:{port}"
    owner_path = data / "instance.json"
    options = (sys.executable, "-u", str(ROOT / "serve.py"), "--port", str(port),
               "--engine-port", str(engine_port), "--data", str(data),
               "--qwen-backend", "vulkan", "--vulkan-device", device)
    env = {**os.environ, "MOSS_WINDOWS_REMINDERS":"0", "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0",
           "MOSS_MEDIA_DB":str(data.parent / "catalog.sqlite3")}
    with (data.parent / f"serve-{serial}.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(*options, cwd=ROOT, env=env, stdout=log,
                                                       stderr=subprocess.STDOUT,
                                                       creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        async with asyncio.timeout(45):
            while True:
                assert process.returncode is None, f"进程启动失败，见 serve-{serial}.log"
                try:
                    health = await json_response(client, base, "/api/health")
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                    assert health["pid"] == owner["pid"] and owner["port"] == port
                    assert health["app"] == "moss-local-chat"
                    break
                except (aiohttp.ClientConnectionError, FileNotFoundError, TimeoutError):
                    await asyncio.sleep(.1)
        yield base, health, owner["pid"]
    finally:
        if process.returncode is None:
            if owner_path.is_file():
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
                health = await json_response(client, base, "/api/health")
                assert owner["pid"] == health["pid"] and owner["port"] == port
                assert health["app"] == "moss-local-chat"
                await json_response(client, base, "/api/shutdown", {"token":owner["token"]})
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), 25)


@asynccontextmanager
async def reminder_restart(client, data, port, engine_port, device):
    args = argparse.Namespace(engine_port=engine_port, qwen_backend="vulkan", vulkan_device=device)
    reminders = WindowsReminders(data, port, startup=args)
    reminders.launch_file.write_text(json.dumps(reminders.startup), encoding="utf-8")
    pid = await asyncio.to_thread(reminders.ensure_server)
    process = psutil.Process(pid)
    base = f"http://127.0.0.1:{port}"
    try:
        health = await json_response(client, base, "/api/health")
        assert health["pid"] == pid and health["app"] == "moss-local-chat"
        yield base, health
    finally:
        owner = json.loads((data / "instance.json").read_text(encoding="utf-8"))
        assert owner["pid"] == pid and owner["port"] == port
        await json_response(client, base, "/api/shutdown", {"token":owner["token"]})
        await asyncio.to_thread(process.wait, 25)


async def sample_resources(pid, measurements):
    service = psutil.Process(pid)
    model_process = None
    psutil.cpu_percent()
    while True:
        if model_process is None:
            model_process = next((child for child in service.children() if child.name() == "llama-server.exe"), None)
            if model_process is not None:
                model_process.cpu_percent()
        measurements.append({
            "availableMiB": round(psutil.virtual_memory().available / 2**20),
            "systemCpuPercent": psutil.cpu_percent(),
            "modelCpuPercent": model_process.cpu_percent() if model_process else 0,
            "modelRssMiB": round(model_process.memory_info().rss / 2**20) if model_process else 0,
        })
        await asyncio.sleep(.25)


async def sse_chat(client, base):
    async with client.get(base + "/api/events") as response:
        assert response.status == 200
        started = perf_counter()
        state = await json_response(client, base, "/api/message", {"text": CHAT})
        key = state["turns"][-1]["id"]
        first = None
        async with asyncio.timeout(180):
            async for line in response.content:
                if not line.startswith(b"data: "):
                    continue
                state = json.loads(line[6:])
                turn = next((item for item in state["turns"] if item["id"] == key), None)
                if turn is None:
                    continue
                if first is None and turn.get("partial_reply"):
                    first = perf_counter()
                if turn["status"] != "thinking":
                    finished = perf_counter()
                    assert turn["status"] == "replied" and turn["source"] == "holo" and turn["reply"], turn
                    assert first is not None, "本轮没有收到流式文字首段"
                    return {"reply":turn["reply"], "firstTextMs":round((first-started)*1000),
                            "fullTextMs":round((finished-started)*1000)}


async def preview(client, base, content):
    started = perf_counter()
    state = await json_response(client, base, "/api/message", {"text":"保存一份本地草稿，内容逐字写：" + content})
    async with asyncio.timeout(150):
        while state["turns"][-1]["status"] == "thinking":
            await asyncio.sleep(.08)
            state = await json_response(client, base, "/api/state")
    turn = state["turns"][-1]
    assert turn["status"] == "pending" and turn["preview"] == content, turn
    return turn["id"], round((perf_counter()-started)*1000)


async def approve(client, base, data, key, expected):
    state = await json_response(client, base, "/api/approve", {"turn_id":key})
    async with asyncio.timeout(10):
        while state["turns"][-1]["status"] == "saving":
            await asyncio.sleep(.05)
            state = await json_response(client, base, "/api/state")
    turn = state["turns"][-1]
    assert turn["id"] == key and turn["status"] in {"replied", "saved"} and turn["receipt"] and not turn["error"], turn
    filename = turn["receipt"]["filename"]
    assert filename == key + ".txt"
    content = (data / "drafts" / filename).read_bytes()
    assert content.decode("utf-8") == expected and hashlib.sha256(content).hexdigest() == turn["receipt"]["sha256"]
    return {"saved":True, "bytes":len(content), "receiptMs":turn["receipt_ms"]}


async def verify(folder, device):
    folder.mkdir(parents=True)
    data = folder / "data"
    port, engine_port = free_port(), free_port()
    while engine_port == port:
        engine_port = free_port()
    memory = psutil.virtual_memory().available / 2**30
    assert memory >= 9, f"可用内存 {memory:.2f} GiB，低于本轮测量要求。"
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0",
                      MOSS_MEDIA_DB=str(folder / "catalog.sqlite3"))
    result = {"passed":False, "requestedDevice":device, "availableGiBBefore":round(memory, 2)}
    samples = []
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=185)) as client:
        async with server(client, data, port, engine_port, device, 1) as (base, initial, pid):
            assert initial["local_engine"] == "standby" and initial["model_id"] == "deepseek", initial
            assert initial["qwen_backend"] == "vulkan" and initial["vulkan_device"] == device
            assert not (data / "qwen.log").exists()
            result["initialProvider"] = initial["model_id"]
            worker = asyncio.create_task(sample_resources(pid, samples))
            try:
                await json_response(client, base, "/api/select-model", {"id":"local"})
                result["ordinarySSE"] = await sse_chat(client, base)
                live = await json_response(client, base, "/api/health")
                assert live["model_id"] == "local" and live["local_engine"] == "vulkan", live
                result["actualBackend"] = live["local_engine"]
                first = "核对 Intel Vulkan 草稿确认。"
                key, delay = await preview(client, base, first)
                result["confirm"] = dict(await approve(client, base, data, key, first), previewMs=delay)
                key, delay = await preview(client, base, "核对 Intel Vulkan 草稿取消。")
                state = await json_response(client, base, "/api/cancel", {"turn_id":key})
                assert state["turns"][-1]["id"] == key and state["turns"][-1]["status"] == "cancelled"
                assert not (data/"drafts"/(key + ".txt")).exists()
                result["cancel"] = {"cancelled":True, "previewMs":delay}
                saved = "核对 Intel Vulkan 草稿在重启后继续。"
                recovery_key, delay = await preview(client, base, saved)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        async with reminder_restart(client, data, port, engine_port, device) as (base, initial):
            state = await json_response(client, base, "/api/state")
            assert state["turns"][-1]["id"] == recovery_key and state["turns"][-1]["status"] == "pending"
            assert initial["model_id"] == "local" and initial["local_engine"] == "standby"
            assert initial["qwen_backend"] == "vulkan" and initial["vulkan_device"] == device
            result["restart"] = dict(await approve(client, base, data, recovery_key, saved), previewMs=delay,
                                     provider=initial["model_id"], backendBeforeApprove=initial["local_engine"],
                                     entry="WindowsReminders.ensure_server")
            assert (await json_response(client, base, "/api/health"))["local_engine"] == "standby"

    log = (data / "qwen.log").read_text(encoding="utf-8", errors="replace")
    selected = DEVICE_LINE.search(log)
    layers = LAYER_LINE.search(log)
    assert selected and selected[1] == device and "Intel" in selected[2] and layers and int(layers[1]) > 0
    result.update(passed=True, actualDevice=selected[0], layersOffloaded=int(layers[1]), totalLayers=int(layers[2]),
                  resourceSample={
                      "minAvailableGiB":round(min(m["availableMiB"] for m in samples)/1024, 2),
                      "peakSystemCpuPercent":max(m["systemCpuPercent"] for m in samples),
                      "peakModelCpuPercent":max(m["modelCpuPercent"] for m in samples),
                      "peakModelWorkingSetMiB":max(m["modelRssMiB"] for m in samples),
                  })
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="Vulkan1")
    args = parser.parse_args()
    folder = ROOT / "runs" / datetime.now().strftime("qwen-vulkan-%Y%m%d-%H%M%S-%f")
    print(folder, flush=True)
    result = asyncio.run(verify(folder, args.device))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (folder / "result.json").write_text(text, encoding="utf-8")
    print(text)
