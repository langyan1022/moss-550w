import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from statistics import median
from time import perf_counter

import aiohttp
import psutil

from browser_lab import BrowserServer, ROOT
from verify_context_reply import free_port
from verify_speech_prepare_ab import free_gpu, other_speaker


MODE_ENV = "MOSS_OMNIVOICE_MINIMAL_IMPORTS"
AUDIO_ONLY_ENV = "MOSS_OMNIVOICE_AUDIO_ONLY_IMPORTS"
READOUT = "2026年11月21日，星期六，17点36分48秒。电量 100%（已接电）。系统音量为 80%，当前已静音。"
VERSE = "醉里挑灯看剑，梦回吹角连营。"
CPU_READOUT, CPU_VERSE = "电量 63%。", "醉里挑灯看剑。"
CACHE = ROOT / "runs/verse-reference-20261120-221137-572766/data/voice-prompts"
MIB = 2**20


async def render(server, text, category, path):
    started = perf_counter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=115)) as client:
        async with client.post(server.url + "/api/speak", json={"text": text, "category": category}) as response:
            data = await response.read()
            assert response.ok and data[:4] == b"RIFF" and data[8:12] == b"WAVE", (response.status, data[:240])
            stages = {name: float(value) for name, value in
                      re.findall(r"([a-z_]+);dur=([\d.]+)", response.headers.get("server-timing", ""))}
    elapsed = round((perf_counter() - started) * 1000, 1)
    path.write_bytes(data)
    return {"body_ms": elapsed, "sha256": hashlib.sha256(data).hexdigest(), "server_timing_ms": stages}


async def sample_resources(server, task, resources):
    while not task.done():
        mem = psutil.virtual_memory().available // MIB
        gpu = await asyncio.to_thread(free_gpu) if resources["start_gpu_free_mib"] is not None else None
        resources["min_free_mib"] = min(resources["min_free_mib"], mem)
        if gpu is not None:
            resources["min_gpu_free_mib"] = min(resources["min_gpu_free_mib"], gpu)
        for proc in psutil.Process(server.pid).children(recursive=True):
            try:
                if any(Path(arg).name == "managed_worker.py" for arg in proc.cmdline()):
                    rss = proc.memory_info().peak_wset // MIB
                    resources["peak_worker_rss_mib"] = max(resources["peak_worker_rss_mib"], rss)
            except psutil.NoSuchProcess:
                continue
        if mem < 4300 or (gpu is not None and gpu < 512):
            resources["stopped"] = f"RAM {mem} MiB，显存 {gpu} MiB"
            task.cancel()
            return
        await asyncio.sleep(.8)


async def watch(server, request, resources):
    async with asyncio.TaskGroup() as group:
        task = group.create_task(request)
        group.create_task(sample_resources(server, task, resources))
    return task.result()


async def measure(folder, mode, cpu, readout, verse):
    gpu_free = None if cpu else await asyncio.to_thread(free_gpu)
    free = psutil.virtual_memory().available // MIB
    if free < 8192 or (gpu_free is not None and gpu_free < 3800) or other_speaker():
        raise RuntimeError(f"未启动合成，RAM {free} MiB，显存 {gpu_free} MiB，或语音进程正被使用")
    resources = {"start_free_mib": free, "min_free_mib": free, "start_gpu_free_mib": gpu_free,
                 "min_gpu_free_mib": gpu_free, "peak_worker_rss_mib": 0}
    record = {"mode": mode, "resources": resources}
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    shutil.copytree(CACHE, server.data / "voice-prompts")
    os.environ[MODE_ENV] = "0" if mode == "default" else "1"
    os.environ[AUDIO_ONLY_ENV] = "1" if mode == "without-vision" else "0"
    try:
        await server.start()
        record["cold"] = await watch(server, render(server, readout, "reply", folder / "本机读数.wav"), resources)
        record["warm"] = await watch(server, render(server, verse, "verse", folder / "诗词.wav"), resources)
        assert "spawn" in record["cold"]["server_timing_ms"] and "spawn" not in record["warm"]["server_timing_ms"]
        assert not (await server.state())["turns"]
        return record
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(cpu, compare_vision=False):
    folder = ROOT / "runs" / datetime.now().strftime("speech-optional-imports-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    readout, verse = (CPU_READOUT, CPU_VERSE) if cpu else (READOUT, VERSE)
    report = {"passed": False, "device": "cpu" if cpu else "auto", "readout": readout, "verse": verse, "arms": []}
    print(folder, flush=True)
    try:
        os.environ["MOSS_SPEECH_STANDBY"] = "0"
        os.environ["MOSS_SPEECH_PREPARE"] = "0"
        if cpu:
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        pair = ("with-vision", "without-vision") if compare_vision else ("default", "optional-imports-off")
        modes = pair if cpu and not compare_vision else pair + pair[::-1]
        for index, mode in enumerate(modes, 1):
            arm = await measure(folder / f"{index}-{mode}", mode, cpu, readout, verse)
            report["arms"].append(arm)
            print(index, mode, arm["cold"]["body_ms"], arm["cold"]["server_timing_ms"], flush=True)
        for metric in ("body_ms", "imports"):
            values = [arm["cold"]["body_ms"] if metric == "body_ms" else arm["cold"]["server_timing_ms"]["imports"]
                      for arm in report["arms"]]
            pairs = [values[0] - values[1]]
            if len(values) == 4:
                pairs.append(values[3] - values[2])
            report[metric + "_saved_ms"] = round(median(pairs), 1)
        report["same_wave_bytes"] = {
            kind: len({row[kind]["sha256"] for row in report["arms"]}) == 1 for kind in ("cold", "warm")
        }
        assert all(report["same_wave_bytes"].values()), report["same_wave_bytes"]
        report["passed"] = True
    except (Exception, asyncio.CancelledError) as error:
        report["error"] = str(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-smoke", action="store_true", help="Use short CPU speech samples with CUDA disabled.")
    parser.add_argument("--compare-vision", action="store_true", help="Compare torchvision imports over four cold starts.")
    args = parser.parse_args()
    asyncio.run(main(args.cpu_smoke, args.compare_vision))
