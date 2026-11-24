import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
import psutil

from browser_lab import ROOT
from core import Moss
from serve import application
from verify_context_reply import free_port
from verify_speech_prepare import gpu_processes
from verify_speech_prepare_ab import free_gpu, other_speaker
from voice_output import GIB, MossSpeech


MIB = 2**20


def speech_workers():
    children = psutil.Process().children(recursive=True)
    found = {}
    for child in children:
        try:
            if any(Path(value).name == "managed_worker.py" for value in child.cmdline()):
                memory = child.memory_info()
                found[child.pid] = round(memory.rss / MIB, 1)
        except psutil.NoSuchProcess:
            continue
    return found


async def wait_until(check, timeout):
    async with asyncio.timeout(timeout):
        while not (value := check()):
            await asyncio.sleep(.15)
    return value


async def verify_pressure():
    folder = ROOT / "runs" / datetime.now().strftime("speech-standby-pressure-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    report = {"passed": False, "simulated_memory_pressure": True, "cases": []}
    try:
        for resource in ("available", "available_pagefile"):
            for already_waiting in (False, True):
                speech = MossSpeech(folder, standby_seconds=1.0)
                voice = SimpleNamespace(returncode=None)
                standby = SimpleNamespace(returncode=None)
                speech.worker = voice
                memory = SimpleNamespace(available=12 * GIB, available_pagefile=12 * GIB)
                started, stopped = [], []

                async def start():
                    started.append(perf_counter())
                    speech.worker = standby

                async def stop():
                    if speech.worker:
                        stopped.append(speech.worker)
                        speech.worker = None

                with (patch("voice_output.STANDBY_MEMORY_CHECK_SECONDS", .04),
                      patch("voice_output.memory_sample", return_value=memory),
                      patch.object(speech, "_start", start), patch.object(speech, "_stop", stop)):
                    if not already_waiting:
                        setattr(memory, resource, 10 * GIB)
                    speech._release_after(0, standby=True)
                    if already_waiting:
                        await wait_until(lambda: speech.worker is standby, .6)
                        setattr(memory, resource, 7 * GIB)
                        await wait_until(lambda: not speech.worker, .6)
                        assert stopped == [voice, standby] and perf_counter() - started[0] < 1
                    else:
                        await wait_until(lambda: not speech.worker, .6)
                        assert stopped == [voice] and not started
                    assert speech.release is None
                    await speech.close()
                report["cases"].append({"resource": resource, "already_waiting": already_waiting,
                                        "started": bool(started), "closed": True})
        speech = MossSpeech(folder, standby_seconds=1.0)
        worker = speech.worker = SimpleNamespace(returncode=None)
        stopped = []

        async def stop_claimed():
            stopped.append(speech.worker)
            speech.worker = None

        with patch.object(speech, "_stop", stop_claimed):
            async with speech.lock:
                deadline = asyncio.get_running_loop().time() + 1
                speech._watch_standby(deadline)
                watcher = asyncio.create_task(speech._release_if_due(speech.release, False, deadline))
                await asyncio.sleep(0)
                assert not watcher.done()
                speech._cancel_release()
            await watcher
            assert not stopped and speech.worker is worker
            await speech.close()
            assert stopped == [worker]
        report["render_claim_invalidates_queued_watcher"] = True
        report["passed"] = True
    finally:
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("speech-standby-http-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "pid": os.getpid(), "speech_idle_seconds": 4.5, "speech_standby_seconds": 32}
    moss = None
    runner = None
    try:
        if (gpu_mib := free_gpu()) < 3800 or psutil.virtual_memory().available < 8192 * MIB or other_speaker():
            raise RuntimeError(f"语音工位暂不可用，本机空闲显存 {gpu_mib} MiB")
        report["start_gpu_free_mib"] = gpu_mib
        data = folder / "data"
        moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
        port = free_port()
        app = application(moss, lambda: "standby", data, port, "isolated", asyncio.Event(),
                          speech_idle_seconds=report["speech_idle_seconds"],
                          speech_standby_seconds=report["speech_standby_seconds"])
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=115)) as client:
            async def say(text, name):
                mark = perf_counter()
                async with client.post(base + "/api/speak", json={"text": text, "category": "reply"}) as reply:
                    body = await reply.read()
                    assert reply.status == 200 and body[:4] == b"RIFF" and body[8:12] == b"WAVE", reply.status
                    row = {"ms": round((perf_counter() - mark) * 1000, 1), "workers": speech_workers(),
                           "sha256": hashlib.sha256(body).hexdigest(), "batch_capable": reply.headers["X-MOSS-Batch-Capable"]}
                report[name] = row
                print(name, row["ms"], row["workers"], flush=True)
                return body, row["workers"]

            def standby_after(previous):
                ready = speech_workers()
                return ready if any(rss >= 625 for rss in ready.values()) and not set(previous).intersection(ready) else None

            assert not speech_workers()
            _, first = await say("明天先把地图放回抽屉，再去买一把新雨伞。", "cold")
            assert first and report["cold"]["batch_capable"] == "1"
            second = await wait_until(lambda: standby_after(first), 38)
            assert not any(psutil.pid_exists(pid) for pid in first)
            assert not set(second).intersection(await asyncio.to_thread(gpu_processes))
            report["import_only_workers"] = second
            report["gpu_free_after_restart_mib"] = await asyncio.to_thread(free_gpu)
            _, resumed = await say("风停以后，把阳台上的花盆搬到靠窗的位置。", "standby_speech")
            assert resumed == second or set(resumed) == set(second)
            assert report["standby_speech"]["batch_capable"] == "1"
            assert report["cold"]["sha256"] != report["standby_speech"]["sha256"]
            third = await wait_until(lambda: standby_after(resumed), 38)
            assert not set(third).intersection(await asyncio.to_thread(gpu_processes))
            cached, _ = await say("不客气。", "recorded")
            assert cached == (ROOT / "voice/lines/welcome.wav").read_bytes()
            assert report["recorded"]["batch_capable"] == "0"
            await wait_until(lambda: not speech_workers(), 37)
            assert not any(psutil.pid_exists(pid) for pid in third)
            report["second_standby_expired"] = True
            assert not moss.state()["turns"]
        report["passed"] = True
    finally:
        try:
            if runner:
                await runner.cleanup()
            if moss:
                await moss.close()
        finally:
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pressure-only", action="store_true", help="模拟内存压力并验证待机回收，无需加载语音模型")
    asyncio.run(verify_pressure() if parser.parse_args().pressure_only else verify())
