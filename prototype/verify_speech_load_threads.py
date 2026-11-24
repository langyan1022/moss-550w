import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from statistics import median
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

import psutil
import pynvml
import win32job


ROOT = Path(__file__).resolve().parent
VOICE = ROOT / "voice"
MIB = 2**20
SENTENCE = "醉里 挑灯 看剑，梦回 吹角 连营。"


def competing_voice(excluded):
    for process in psutil.process_iter(["name"]):
        if process.pid not in excluded and (process.info["name"] or "").lower() in {"python.exe", "pythonw.exe"}:
            try:
                if any(Path(a).name in {"managed_worker.py", "clone_omnivoice.py"} for a in process.cmdline()):
                    return True
            except psutil.Error:
                pass
    return False


async def measure(folder, threads, gpu, model, prompt_cache, *, decoder="auto", gpu_reserve_mib=512, ram_reserve_mib=3500,
                  sentence=SENTENCE, speed=.90, device="cuda", extra_worker_args=()):
    folder.mkdir()
    assert not competing_voice(set())
    memory = lambda: pynvml.nvmlDeviceGetMemoryInfo(gpu).free // MIB
    start_free = memory()
    assert start_free >= 3800 and psutil.virtual_memory().available >= 6 * 2**30
    job_name = "MossLoadTest-" + uuid4().hex
    job = win32job.CreateJobObject(None, job_name)
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    path = folder / "first.wav"
    packet = {"text": sentence, "output": str(path), "room": True, "device": device, "speed": speed,
              "steps": 16, "request_id": uuid4().hex, "cancel_file": str(folder / "request.cancel")}
    proc = None
    monitoring = None
    available = psutil.virtual_memory().available // MIB
    metrics = {"load_threads": threads, "gpu_free_before_mib": start_free, "gpu_free_min_mib": start_free,
               "ram_free_before_mib": available, "ram_free_min_mib": available,
               "peak_worker_rss_mib": 0, "peak_worker_private_mib": 0}
    try:
        with (folder / "worker.log").open("wb") as log:
            start = perf_counter()
            proc = await asyncio.create_subprocess_exec(sys.executable, "-B", "-u", str(VOICE / "managed_worker.py"),
                     "--worker", "--model", str(model), "--reference", str(VOICE / "reference.wav"),
                     "--transcript", "感谢您十七年来的辛苦付出，恭喜您，明天可以离港回家了。",
                     "--prompt-cache", str(prompt_cache), "--load-threads", str(threads), "--decoder", decoder,
                     *extra_worker_args,
                     stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log, cwd=VOICE,
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     env={**os.environ, "MOSS_SPEECH_JOB": job_name, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            owner = psutil.Process(proc.pid)
            async def monitor():
                next_check = perf_counter() + 2
                while True:
                    processes = [owner, *owner.children(recursive=True)]
                    for process in processes:
                        info = process.memory_info()
                        # Windows 的进程历史峰值可涵盖两次 NVML 采样之间的短暂 CPU 分配。
                        metrics["peak_worker_rss_mib"] = max(metrics["peak_worker_rss_mib"],
                                                             round(getattr(info, "peak_wset", info.rss) / MIB, 1))
                        metrics["peak_worker_private_mib"] = max(metrics["peak_worker_private_mib"],
                                                                 round(getattr(info, "peak_pagefile", info.vms) / MIB, 1))
                    free = memory()
                    metrics["gpu_free_min_mib"] = min(metrics["gpu_free_min_mib"], free)
                    ram = psutil.virtual_memory().available // MIB
                    metrics["ram_free_min_mib"] = min(metrics["ram_free_min_mib"], ram)
                    if free < gpu_reserve_mib or ram < ram_reserve_mib:
                        raise RuntimeError("可用显存或系统内存低于实验保留量")
                    if perf_counter() >= next_check:
                        next_check = perf_counter() + 1
                        if competing_voice({p.pid for p in processes}):
                            raise RuntimeError("日常语音已启动")
                    await asyncio.sleep(.13)
            monitoring = asyncio.create_task(monitor())
            proc.stdin.write(json.dumps(packet, ensure_ascii=False).encode("utf-8") + b"\n")
            await proc.stdin.drain()
            reading = asyncio.create_task(proc.stdout.readline())
            try:
                done, _ = await asyncio.wait((monitoring, reading), timeout=90, return_when=asyncio.FIRST_COMPLETED)
                if monitoring in done:
                    await monitoring
                if reading not in done:
                    raise TimeoutError("语音进程超过90秒")
                reply = json.loads(reading.result())
                metrics["spawn_to_wav_ms"] = round((perf_counter() - start) * 1000)
            finally:
                reading.cancel()
            assert reply.get("request_id") == packet["request_id"] and reply.get("code") == 0
            metrics.update(reply["info"])
            expected = {"cpu", "cuda"} if device == decoder == "auto" else {"cuda" if decoder == "auto" else decoder}
            assert metrics["device"] == "cuda" and metrics["decoder"] in expected and not metrics["warm"]
            metrics["wav_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=10)
            assert proc.returncode == 0
    finally:
        if monitoring:
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
        job.Close()
        if proc:
            await proc.wait()
        metrics["gpu_free_after_mib"] = memory()
        (folder / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("speech-load-threads-%Y%m%d-%H%M%S-%f")
    prompt_cache = folder / "voice-prompts"
    shutil.copytree(ROOT / "runs/verse-reference-20261120-221137-572766/data/voice-prompts", prompt_cache)
    before = {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in prompt_cache.glob("*.pt")}
    studio = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio"))
    hub = studio / "models/models--k2-fsa--OmniVoice"
    model = hub / "snapshots" / (hub / "refs/main").read_text(encoding="utf-8").strip()
    report = {"passed": False, "text": SENTENCE, "snapshot": model.name, "arms": []}
    pynvml.nvmlInit()
    try:
        gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
        for index, count in enumerate((1, 2, 2, 1), 1):
            metrics = await measure(folder / f"{index}-threads-{count}", count, gpu, model, prompt_cache)
            report["arms"].append(metrics)
            print(index, count, metrics["load_seconds"], metrics["spawn_to_wav_ms"], flush=True)
        assert len({r["wav_sha256"] for r in report["arms"]}) == 1
        assert before == {p.name:(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in prompt_cache.glob("*.pt")}
        for key in ("load_seconds", "spawn_to_wav_ms"):
            a, b, c, d = (row[key] for row in report["arms"])
            report["paired_saved_" + key] = [round(a-b, 3), round(d-c, 3)]
            report["median_saved_" + key] = round(median([a-b, d-c]), 3)
        report["passed"] = True
    finally:
        pynvml.nvmlShutdown()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
