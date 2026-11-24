import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4
import wave

import aiohttp
from playwright.async_api import async_playwright
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from device_status import memory_sample
from model_catalog import Profile, profiles
from serve import application
from verify_context_reply import free_port
from verify_speech_chat_prepare import first_audible
from verify_speech_cold_stages import MIB, STATUS_PROMPT, check_local_status, monitor, open_speech_page
from verify_speech_prepare import workers
from verify_speech_prepare_ab import free_gpu, other_speaker
from verify_voice_receipts import listen
from voice_output import MIC_ACTIVITY_MIN_AVAILABLE, MossSpeech


async def prepare_weights(server):
    started = perf_counter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as client:
        async with client.post(server.url + "/api/prepare-speech", json={"mic":uuid4().hex},
                               headers={"X-MOSS-Voice-Activity":"1"}) as response:
            assert response.status == 204 and response.headers.get("X-MOSS-Mic-Prepared") == "1", response.headers
    return round((perf_counter() - started) * 1000, 1)


async def browser_trial(page, server, row):
    await open_speech_page(page, server, local_status=True)
    assert not workers(server) and not (await server.state())["turns"]
    row["weights_ready_ms"] = await prepare_weights(server)
    preflight, = row["worker_commands"]
    assert preflight["prepare"] and preflight["prime"] is False and preflight["steps"] == 16
    info = preflight["info"]
    assert preflight["code"] == 0 and info["device"] == "cuda" and info["decoder"] == "cuda", preflight
    assert info["load_seconds"] > 0 and info["priming"] == "skipped" and info["generation_seconds"] < .1, preflight
    assert not any(key in info for key in ("room_s", "room_import_s", "room_wait_s")), preflight
    row["prepared_pids"] = workers(server)
    assert preflight["pid"] in row["prepared_pids"], row
    memory = memory_sample()
    row["prepared_free_mib"] = memory.available // MIB
    row["prepared_commit_mib"] = memory.available_pagefile // MIB
    await page.get_by_test_id("composer").fill(STATUS_PROMPT)
    async with page.expect_response(server.url + "/api/speak", timeout=90000) as seen:
        await page.get_by_test_id("send").click()
    response = await seen.value
    assert response.ok, await response.text()
    await page.wait_for_function("readingFinished()", timeout=90000)
    trace = await page.evaluate("""() => ({...collectReadingMetrics(),
      speech:performance.getEntriesByName(location.origin+'/api/speak').map(entry => ({start:entry.startTime,
      body:entry.responseEnd, server:Object.fromEntries(entry.serverTiming.map(({name,duration})=>[name,duration]))}))})""")
    clip, = trace["clips"]
    assert trace["events"][-1]["type"] == "end" and clip["first"] is not None and clip["last"] > clip["first"]
    first_sample, clock = first_audible(clip, trace["submitted"])
    assert first_sample > 0 and clock == "web_audio_output_timestamp", (first_sample, clock, clip)
    fetched, = trace["speech"]
    stages = fetched["server"]
    assert stages["load"] == 0 and "cache" not in stages and "spawn" not in stages and stages["generate"] > 0, stages
    _, synthesis = row["worker_commands"]
    assert not synthesis["prepare"] and synthesis["room"] and synthesis["steps"] == 16, synthesis
    assert synthesis["pid"] == preflight["pid"] and synthesis["code"] == 0
    info = synthesis["info"]
    assert info["device"] == info["decoder"] == "cuda" and info["warm"] is True, synthesis
    if row["overlap"]:
        assert info["room_import_s"] > 0 and info["room_wait_s"] >= 0, synthesis
    turn, = (await server.state())["turns"]
    await check_local_status(page, turn, response.request.post_data_json, row)
    assert turn["cloud_requests"] == {"total":0, "by_provider":{}, "complete":True}
    audio = await response.body()
    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth()) == (2, 2) and wav.getnframes() > wav.getframerate() // 3
        row["pcm_seconds"] = round(wav.getnframes() / wav.getframerate(), 3)
    (server.folder / "reply.wav").write_bytes(audio)
    ready = round(fetched["body"] - trace["submitted"], 1)
    row.update(first_sample_ms=first_sample, audio_clock=clock, wav_ready_ms=ready,
               wav_to_first_sample_ms=round(first_sample - ready, 1), server_timing_ms=stages,
               room_import_ms=1000 * info["room_import_s"] if "room_import_s" in info else None,
               room_wait_ms=1000 * info["room_wait_s"] if "room_wait_s" in info else None,
               sha256=hashlib.sha256(audio).hexdigest(), cloud_requests=0)


async def measure(browser, folder, overlap, current_profiles):
    folder.mkdir()
    memory = memory_sample()
    free, commit = memory.available // MIB, memory.available_pagefile // MIB
    gpu_free = await asyncio.to_thread(free_gpu)
    if min(memory.available, memory.available_pagefile) < MIC_ACTIVITY_MIN_AVAILABLE or gpu_free < 3800 or other_speaker():
        raise RuntimeError(f"本轮未启动。空闲内存 {free} MiB，提交余量 {commit} MiB，显存 {gpu_free} MiB，或已有语音进程。")
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    server.pid = os.getpid()
    row = {"passed":False, "overlap":overlap, "worker_commands":[], "start_free_mib":free, "min_free_mib":free,
           "start_commit_mib":commit, "start_gpu_free_mib":gpu_free, "min_gpu_free_mib":gpu_free,
           "peak_voice_rss_mib":0}
    env = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"),
           "MOSS_MEDIA_DB":str(folder / "catalog.sqlite3"), "MOSS_MEM0_ENABLED":"0",
           "MOSS_WINDOWS_REMINDERS":"0", "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"1",
           "MOSS_SPEECH_MIC_WARM":"activity"}
    original = MossSpeech._worker_reply

    async def observe(speech, request, *args, **kwargs):
        reply = await original(speech, request, *args, **kwargs)
        row["worker_commands"].append({"pid":speech.worker.pid, "prepare":bool(request.get("prepare")),
                                       "steps":request["steps"], "prime":request.get("prime"),
                                       "room":request.get("room", False), "code":reply["code"],
                                       "info":reply.get("info")})
        return reply

    def unexpected_model(_profile):
        raise AssertionError("两项 Windows 读数应在本机读取。")

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env))
            if overlap:
                os.environ.pop("MOSS_SPEECH_OVERLAP_ROOM", None)
            else:
                os.environ["MOSS_SPEECH_OVERLAP_ROOM"] = "0"
            stack.enter_context(patch("core.profiles", return_value=current_profiles))
            stack.enter_context(patch.object(Profile, "client", unexpected_model))
            stack.enter_context(patch.object(MossSpeech, "_worker_reply", observe))
            moss = Moss(server.data, server.model_url, server.model)
            stack.push_async_callback(moss.close)
            await moss.select_model("deepseek")
            await listen(stack, application(moss, lambda:"standby", server.data, server.port,
                                           "isolated", asyncio.Event()), server.port)
            state = await server.state()
            assert state["speech_mic_warm_on_activity"] is True and state["model_id"] == "deepseek"
            row["profile"] = {"provider":state["model_id"], "model":state["model"]}
            context = await browser.new_context(reduced_motion="reduce", service_workers="block")
            stack.push_async_callback(context.close)
            task = asyncio.create_task(browser_trial(await context.new_page(), server, row))
            async with asyncio.TaskGroup() as group:
                group.create_task(monitor(server, task, row))
                await task
            assert row["peak_voice_rss_mib"] > 0
        row["workers_after_close"] = workers(server)
        assert not row["workers_after_close"]
        row["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return row


async def run(reverse=False):
    current_profiles = profiles()
    if not current_profiles["deepseek"].public()["available"]:
        raise RuntimeError("需要当前 DeepSeek 配置以复用正式 GPU 权重准备门控。测试不会调用模型。")
    folder = ROOT / "runs" / datetime.now().strftime("voice-room-overlap-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    order = (True, False, False, True) if reverse else (False, True, True, False)
    report = {"passed":False, "prompt":STATUS_PROMPT, "arms":[], "order":order, "physical_speaker":False}
    try:
        async with async_playwright() as playwright:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            browser = await playwright.chromium.launch(
                executable_path=str(edge), headless=True,
                args=["--mute-audio", "--disable-gpu", "--disable-background-networking"])
            try:
                await confirm_muted_edge(browser)
                for index, enabled in enumerate(order, 1):
                    arm = await measure(browser, folder / f"{index}-{'overlap' if enabled else 'serial'}",
                                        enabled, current_profiles)
                    report["arms"].append(arm)
                    print(index, enabled, arm["first_sample_ms"], arm["server_timing_ms"], flush=True)
            finally:
                await browser.close()
        arms = report["arms"]
        assert len({row["sha256"] for row in arms}) == 1
        assert all(row["readings"] == arms[0]["readings"] for row in arms)
        report["paired_saved_ms"] = {}
        for measure_name in ("first_sample_ms", "wav_ready_ms", "weights_ready_ms"):
            paired = []
            for pair in (arms[:2], arms[2:]):
                serial, overlap = sorted(pair, key=lambda row: row["overlap"])
                paired.append(round(serial[measure_name] - overlap[measure_name], 1))
            report["paired_saved_ms"][measure_name] = {"values":paired, "median":round(median(paired), 1)}
        report["passed"] = True
    finally:
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reverse", action="store_true", help="先测并行导入，再交替置换顺序")
    asyncio.run(run(parser.parse_args().reverse))
