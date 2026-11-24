import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from statistics import median
from time import perf_counter
from unittest.mock import patch

from playwright.async_api import async_playwright
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_context_reply import free_port
from verify_speech_long_reading import PROBE
from verify_speech_prepare_ab import free_gpu, other_speaker


TEXT = "窗外的风停了，屋里的灯慢慢亮起来。"
PROMPT = "请朗读以下文字：" + TEXT
REVIEW_SECONDS = 7
STATUS_FIRST_CLAUSE = "电脑还有多少电、"
STATUS_PROMPT = STATUS_FIRST_CLAUSE + "音量多大？"
STATUS_REVIEW_SECONDS = 4
IDLE_SECONDS = 10
STANDBY_SECONDS = 90
IMPORT_SETTLE_SECONDS = 8
MIB = 1 << 20


async def check_local_status(page, turn, request, stats):
    from verify_local_overview_browser import expected_speech, spoken_words

    readings = turn["local_overview"]
    assert turn["status"] == "replied" and turn["source"] == "context" and turn["user"] == STATUS_PROMPT
    assert [row["kind"] for row in readings] == ["battery", "volume"], readings
    assert all("%" in row["reply"] for row in readings), readings
    assert turn["reply"] == "\n".join(row["reply"] for row in readings), turn["reply"]
    assert request["category"] == "reply" and spoken_words(request["text"]) == expected_speech(readings), request
    modal = page.locator("#local-status-panel:modal")
    assert await modal.is_visible()
    assert await modal.locator("[data-reading-kind]").evaluate_all(
        "rows => rows.map(row => row.dataset.readingKind)") == ["battery", "volume"]
    phases = await page.evaluate("panelSpeechPhases")
    assert await page.evaluate("statusPreparingVisible")
    for event, label in (("start", "正在播报"), ("end", "读取完成")):
        assert {"event": event, "label": label} in phases, phases
    stats.update(readings=readings, speech_text=request["text"], panel_speech_phases=phases,
                 panel_shown_while_preparing=True)


async def open_speech_page(page, server, *, local_status=False):
    probe = PROBE + "\nlocalStorage.setItem('moss.voice.enabled','on');"
    if local_status:
        from verify_local_overview_browser import UI_PROBE

        probe += UI_PROBE + """
window.statusDraftInputs = [];
window.statusPreparingVisible = false;
document.addEventListener('input', event => {
  if (event.target.id === 'message')
    statusDraftInputs.push({text:event.target.value, trusted:event.isTrusted, composing:event.isComposing});
}, true);
new MutationObserver(() => {
  const label = document.querySelector('#local-status-panel:modal #local-status-progress[data-voice-phase="pending"]');
  if (label && !label.hidden && label.textContent === '正在准备播报') statusPreparingVisible = true;
}).observe(document, {subtree:true, attributes:true, attributeFilter:['open', 'data-voice-phase', 'hidden'],
                      childList:true, characterData:true});
"""
    await page.add_init_script(probe)
    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.locator(".connection.online").wait_for()


async def play(page, server, ahead, stats, *, local_status=False, prior_turns=(), worker_before_review=()):
    prepares, speaks = [], []
    page.on("request", lambda value: prepares.append(value) if value.url == server.url + "/api/prepare-speech" else None)
    page.on("response", lambda value: speaks.append(value) if value.url == server.url + "/api/speak" else None)
    await open_speech_page(page, server, local_status=local_status)
    message = page.get_by_test_id("composer")
    start = perf_counter()
    await message.fill(STATUS_FIRST_CLAUSE if local_status else PROMPT)
    if local_status:
        start = perf_counter()
    review = STATUS_REVIEW_SECONDS if local_status else REVIEW_SECONDS
    await asyncio.sleep(max(0, review - (perf_counter() - start)))
    assert [turn["id"] for turn in (await server.state())["turns"]] == list(prior_turns) and not speaks
    assert len(prepares) == int(ahead) and all(req.post_data_json == {} for req in prepares), len(prepares)
    stats["prepare_before_submit"] = len(prepares)
    if local_status:
        from verify_speech_prepare import gpu_processes, workers

        pids = workers(server)
        gpu_pids = sorted(set(pids) & await asyncio.to_thread(gpu_processes))
        stats.update(pre_submit_prepare_pids=pids, pre_submit_cuda_pids=gpu_pids,
                     actual_review_ms=round((perf_counter() - start) * 1000, 1))
        assert bool(pids) == ahead and not gpu_pids, stats
        assert set(worker_before_review) <= set(pids), pids
        await message.fill(STATUS_PROMPT)
    await page.get_by_test_id("send").click()
    await page.wait_for_function("readingFinished()", timeout=120000)
    response, = speaks
    first_ms, submitted, estimated, speech = await speech_clock(page, response)
    stages = speech["server"]
    assert ("spawn" in stages) != ahead, stages
    turn, = [turn for turn in (await server.state())["turns"] if turn["id"] not in prior_turns]
    if local_status:
        assert (await page.evaluate("statusDraftInputs")) == [
            {"text": prompt, "trusted": True, "composing": False} for prompt in (STATUS_FIRST_CLAUSE, STATUS_PROMPT)]
        await check_local_status(page, turn, response.request.post_data_json, stats)
    else:
        assert response.request.post_data_json["text"] == turn["reply"] == TEXT and turn["source"] == "narration"
    assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}
    audio = await response.body()
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    (server.folder / "reply.wav").write_bytes(audio)
    stats.update(first_sample_ms=round(first_ms - submitted, 1), audio_clock="schedule" if estimated else "output",
                 request_ms=round(speech["start"] - submitted, 1), body_ready_ms=round(speech["body"] - submitted, 1),
                 server_timing_ms=stages, sha256=hashlib.sha256(audio).hexdigest(), cloud_requests=0,
                 prepare_requests=len(prepares), preflight_body=[req.post_data_json for req in prepares])


async def speech_clock(page, response):
    assert response.ok
    trace = await page.evaluate("""() => ({...collectReadingMetrics(),
      speech:performance.getEntriesByName(location.origin+'/api/speak').map(entry => ({start:entry.startTime,
      body:entry.responseEnd, server:Object.fromEntries(entry.serverTiming.map(({name,duration})=>[name,duration]))}))})""")
    assert trace["events"][-1]["type"] == "end" and len(trace["clips"]) == 1
    clip, = trace["clips"]
    first = clip["first"]
    assert first is not None and clip["last"] > first and trace["submitted"] is not None
    speech, = trace["speech"]
    stages = speech["server"]
    assert all(name in stages and stages[name] >= 0 for name in ("tts", "imports", "load", "checkpoint", "generate", "mix")), stages
    clock = clip["output"]
    estimated = not (clock["performanceTime"] > 0 and clock["contextTime"] <= clip["start"])
    first_ms = ((first - clip["start"]) * 1000 + clip["scheduled"] if estimated else
                (first - clock["contextTime"]) * 1000 + clock["performanceTime"])
    assert first_ms > trace["submitted"]
    return first_ms, trace["submitted"], estimated, speech


async def monitor(server, task, stats):
    parent = psutil.Process(server.pid)
    while not task.done():
        free = psutil.virtual_memory().available // MIB
        gpu_free = await asyncio.to_thread(free_gpu)
        stats["min_free_mib"] = min(stats["min_free_mib"], free)
        stats["min_gpu_free_mib"] = min(stats["min_gpu_free_mib"], gpu_free)
        for proc in parent.children(recursive=True):
            try:
                if any(Path(arg).name == "managed_worker.py" for arg in proc.cmdline()):
                    stats["peak_voice_rss_mib"] = max(stats["peak_voice_rss_mib"], proc.memory_info().peak_wset // MIB)
            except psutil.NoSuchProcess:
                continue
        if free < 4300 or gpu_free < 512:
            stats["resource_stop"] = f"内存 {free} MiB，显存 {gpu_free} MiB"
            task.cancel()
            return
        await asyncio.sleep(.7)


async def measure(browser, folder, ahead, *, local_status=False):
    free = psutil.virtual_memory().available // MIB
    gpu_free = await asyncio.to_thread(free_gpu)
    if free < 8192 or gpu_free < 3800 or other_speaker():
        raise RuntimeError(f"本轮未启动。内存 {free} MiB，显存 {gpu_free} MiB，或语音已在使用。")
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    stats = {"passed": False, "preload_while_drafting": ahead, "start_free_mib": free, "min_free_mib": free,
             "start_gpu_free_mib": gpu_free, "min_gpu_free_mib": gpu_free, "peak_voice_rss_mib": 0}
    source = ROOT / "runs/verse-reference-20261120-221137-572766/data/voice-prompts"
    shutil.copytree(source, server.data / "voice-prompts")
    os.environ["MOSS_SPEECH_PREPARE"] = "1" if ahead else "0"
    try:
        await server.start()
        assert (await server.state())["speech_prepare"] is ahead
        context = await browser.new_context(reduced_motion="reduce")
        try:
            task = asyncio.create_task(play(await context.new_page(), server, ahead, stats, local_status=local_status))
            async with asyncio.TaskGroup() as group:
                group.create_task(monitor(server, task, stats))
                await task
        finally:
            await context.close()
        assert stats["peak_voice_rss_mib"] > 0 and stats["min_gpu_free_mib"] < gpu_free - 512
        stats["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


async def wait_for_workers(server, condition, seconds):
    from verify_speech_prepare import workers

    async with asyncio.timeout(seconds):
        while True:
            pids = workers(server)
            if condition(pids):
                return pids
            await asyncio.sleep(.15)


async def standby_resources(server):
    from device_status import memory_sample
    from verify_speech_prepare import gpu_processes, workers

    pids = workers(server)
    gpu_pids = sorted(set(pids) & await asyncio.to_thread(gpu_processes))
    processes = [psutil.Process(pid).memory_info() for pid in pids]
    memory = memory_sample()
    return {
        "pids": pids, "cuda_pids": gpu_pids,
        "rss_mib": round(sum(info.rss for info in processes) / MIB, 1),
        "private_mib": round(sum(info.private for info in processes) / MIB, 1),
        "physical_available_mib": round(memory.available / MIB, 1),
        "commit_available_mib": round(memory.available_pagefile / MIB, 1),
    }


async def play_after_idle(context, server, enabled, stats):
    from verify_speech_delivery import inspect_audio
    from verify_speech_prepare import workers

    page = await context.new_page()
    try:
        await open_speech_page(page, server)
        await page.get_by_test_id("composer").fill(PROMPT)
        async with page.expect_response(server.url + "/api/speak", timeout=120000) as received:
            await page.get_by_test_id("send").click()
        response = await received.value
        primed_at = perf_counter()
        initial = workers(server)
        assert initial, "首次播报期间未检测到语音进程"
        await page.wait_for_function("readingFinished()", timeout=120000)
        await speech_clock(page, response)
        audio = await response.body()
        turn, = (await server.state())["turns"]
        assert response.request.post_data_json["text"] == turn["reply"] == TEXT and turn["source"] == "narration"
        assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}
        stats.update(prime_pids=initial, prime_pcm_seconds=inspect_audio(audio)[0],
                     prime_sha256=hashlib.sha256(audio).hexdigest())
    finally:
        await page.close()

    previous = set(initial)
    standby = await wait_for_workers(server, lambda pids: not previous.intersection(pids)
                                     and bool(pids) == enabled, IDLE_SECONDS + 8)
    assert not any(psutil.pid_exists(pid) for pid in initial), initial
    stats["prime_retired_after_response_seconds"] = round(perf_counter() - primed_at, 2)
    await asyncio.sleep(IMPORT_SETTLE_SECONDS)
    resources = await standby_resources(server)
    stats["before_draft"] = resources
    assert set(standby) <= set(resources["pids"]) and bool(resources["pids"]) == enabled
    assert not resources["cuda_pids"], resources
    if enabled:
        assert resources["rss_mib"] < 1280 and resources["private_mib"] < 3584, resources
        assert min(resources["physical_available_mib"], resources["commit_available_mib"]) >= 8192, resources

    page = await context.new_page()
    try:
        await play(page, server, True, stats, local_status=True, prior_turns=(turn["id"],),
                   worker_before_review=resources["pids"])
        assert stats["speech_text"] != TEXT and stats["sha256"] != stats["prime_sha256"]
        assert not previous.intersection(stats["pre_submit_prepare_pids"])
        target_pcm_seconds, _ = inspect_audio((server.folder / "reply.wav").read_bytes())
        stats["target_pcm_seconds"] = target_pcm_seconds
    finally:
        await page.close()


async def measure_standby(browser, folder, enabled, *, check_expiry=False):
    from core import Moss
    from device_status import memory_sample
    from serve import application
    from verify_voice_receipts import listen
    from voice_output import MossSpeech

    memory = memory_sample()
    free = memory.available // MIB
    commit = memory.available_pagefile // MIB
    gpu_free = await asyncio.to_thread(free_gpu)
    if min(free, commit) < 8192 or gpu_free < 3800 or other_speaker():
        raise RuntimeError(f"本轮未启动。内存 {free} MiB，提交额度 {commit} MiB，显存 {gpu_free} MiB，或语音已在使用。")
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    server.pid = os.getpid()
    source = ROOT / "runs/verse-reference-20261120-221137-572766/data/voice-prompts"
    shutil.copytree(source, server.data / "voice-prompts")
    stats = {"passed": False, "standby_enabled": enabled, "start_free_mib": free, "min_free_mib": free,
             "start_gpu_free_mib": gpu_free, "min_gpu_free_mib": gpu_free, "peak_voice_rss_mib": 0,
             "synthesis": []}
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(folder / "catalog.sqlite3"), "MOSS_MEM0_ENABLED": "0",
                   "MOSS_WINDOWS_REMINDERS": "0", "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "1",
                   "MOSS_SPEECH_MIC_WARM": "0"}
    original = MossSpeech._worker_reply

    async def observe_synthesis(speech, *args, **kwargs):
        result = await original(speech, *args, **kwargs)
        info = result["info"]
        stats["synthesis"].append({"pid": speech.worker.pid, "device": info.get("device"),
                                   "decoder": info.get("decoder"), "code": result["code"]})
        return result

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch.object(MossSpeech, "_worker_reply", observe_synthesis))
            moss = Moss(server.data, server.model_url, server.model)
            stack.push_async_callback(moss.close)
            assert moss.model_id == "local"
            seconds = STANDBY_SECONDS if enabled else 0
            app = application(moss, lambda: "standby", server.data, server.port, "standby-test", asyncio.Event(),
                              speech_idle_seconds=IDLE_SECONDS, speech_standby_seconds=seconds)
            assert await listen(stack, app, server.port) == server.url
            assert (await server.state())["speech_prepare"] is True
            context = await browser.new_context(reduced_motion="reduce")
            stack.push_async_callback(context.close)
            task = asyncio.create_task(play_after_idle(context, server, enabled, stats))
            async with asyncio.TaskGroup() as group:
                group.create_task(monitor(server, task, stats))
                await task
            prime, target = stats["synthesis"]
            assert all(row["code"] == 0 and row["device"] == "cuda" for row in (prime, target)), stats["synthesis"]
            assert prime["pid"] in stats["prime_pids"] and target["pid"] in stats["pre_submit_prepare_pids"]
            if check_expiry:
                target_pids = set(stats["pre_submit_prepare_pids"])
                expiring = await wait_for_workers(server, lambda pids: pids and not target_pids.intersection(pids),
                                                  IDLE_SECONDS + 8)
                started = perf_counter()
                await asyncio.sleep(IMPORT_SETTLE_SECONDS)
                sample = await standby_resources(server)
                stats["expiring_standby"] = sample
                assert set(expiring) <= set(sample["pids"]) and not sample["cuda_pids"], sample
                await wait_for_workers(server, lambda pids: not pids, STANDBY_SECONDS + 5)
                stats["standby_expired_seconds"] = round(perf_counter() - started, 2)
                assert STANDBY_SECONDS - 2 <= stats["standby_expired_seconds"] <= STANDBY_SECONDS + 5, stats
                assert not any(psutil.pid_exists(pid) for pid in sample["pids"])
                assert len(stats["synthesis"]) == 2
        assert not await wait_for_workers(server, lambda pids: not pids, 4)
        stats["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


async def main(*, local_status=False, standby_idle=False):
    if standby_idle:
        prefix = "speech-standby-idle"
    else:
        prefix = "speech-cold-stages-local-status" if local_status else "speech-cold-stages"
    folder = ROOT / "runs" / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    status = local_status or standby_idle
    report = {"passed": False, "text": STATUS_PROMPT if status else TEXT,
              "draft_review_seconds": STATUS_REVIEW_SECONDS if status else REVIEW_SECONDS,
              "arms": [], "physical_speaker": False}
    if standby_idle:
        report.update(idle_seconds=IDLE_SECONDS, standby_seconds=STANDBY_SECONDS, prime_prompt=PROMPT)
    try:
        async with async_playwright() as playwright:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                args=["--mute-audio", "--disable-gpu", "--disable-background-networking"])
            try:
                await confirm_muted_edge(browser)
                for index, ahead in enumerate((False, True, True, False), 1):
                    if standby_idle:
                        row = await measure_standby(browser, folder / f"{index}-{'standby' if ahead else 'draft-only'}",
                                                    ahead, check_expiry=index == 3)
                    else:
                        row = await measure(browser, folder / f"{index}-{'preload' if ahead else 'cold'}",
                                            ahead, local_status=local_status)
                    report["arms"].append(row)
                    print(index, ahead, row["first_sample_ms"], row["server_timing_ms"], flush=True)
            finally:
                await browser.close()
        assert len({row["sha256"] for row in report["arms"]}) == 1
        if standby_idle:
            assert all(row["readings"] == report["arms"][0]["readings"] for row in report["arms"])
        a, b, c, d = (row["first_sample_ms"] for row in report["arms"])
        report.update(passed=True, paired_saved_ms=[round(a - b, 1), round(d - c, 1)],
                      median_saved_ms=round(median([a - b, d - c]), 1))
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--local-status", action="store_true", help="使用实际电量和系统音量替代朗读做冷首声对照")
    modes.add_argument("--standby-idle", action="store_true", help="对比空闲后预导入待机与仅输入时预导入的首声时间")
    args = parser.parse_args()
    asyncio.run(main(local_status=args.local_status, standby_idle=args.standby_idle))
