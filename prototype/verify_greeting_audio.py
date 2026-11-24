import argparse
import asyncio
import audioop
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
from statistics import median
from unittest.mock import patch
import wave

from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_context_reply import free_port
from verify_speech_delivery import inspect_audio
from verify_speech_long_reading import PROBE
from verify_speech_prepare_ab import free_gpu, other_speaker
from voice_output import RECORDED_LINES


CASES = (
    ("你好", "你好。", "hello.wav"),
    ("莫斯，早上好。", "早上好。", "good-morning.wav"),
    ("晚上好", "晚上好。", "good-evening.wav"),
    ("晚安", "晚安。", "good-night.wav"),
)
NEW_CASES = (
    ("早安！", "早安。", "morning-peace.wav"),
    ("您好！", "您好。", "hello-formal.wav"),
    ("莫斯，早。", "早。", "morning-short.wav"),
    ("中午好", "中午好。", "good-noon.wav"),
    ("MOSS，下午好", "下午好。", "good-afternoon.wav"),
)


def stereo_metrics(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 2 and wav.getsampwidth() == 2
        pcm = wav.readframes(wav.getnframes())
    peak = audioop.max(pcm, 2)
    channel_difference = audioop.rms(audioop.tomono(pcm, 2, 1, -1), 2)
    assert peak < 32767 and channel_difference > 0
    return {"peak_pcm16": peak, "channel_difference_rms": channel_difference}


def prepare_lines(baseline, verified_subset=False):
    report = json.loads((baseline / "result.json").read_text(encoding="utf-8"))
    assert report["mode"] == "synthesis"
    cases = report["cases"]
    if verified_subset:
        assert report["suite"] == "new-greetings"
        assert [(case["input"], case["wording"], case["asset"]) for case in cases] == list(NEW_CASES)
        assert all(isinstance(case.get("asr_matches"), bool) and "asr" in case for case in cases)
        cases = [case for case in cases if case["asr_matches"]]
        assert cases, "新增问候没有通过 ASR 的候选"
    else:
        assert report["passed"]
    folder = ROOT / "runs" / datetime.now().strftime("greeting-trim-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    prepared = []
    for case in cases:
        name = case["asset"]
        data = (baseline / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == case["sha256"] and case["asr_matches"]
        with wave.open(io.BytesIO(data), "rb") as original:
            params = original.getparams()
            frame_size = params.nchannels * params.sampwidth
            rate = params.framerate
            pcm = original.readframes(params.nframes)
        assert params.nchannels == 2 and params.sampwidth == 2
        block = rate // 100
        quiet_until = 0
        for frame in range(0, params.nframes, block):
            segment = pcm[frame * frame_size:(frame + block) * frame_size]
            if audioop.max(segment, 2) >= 82 or audioop.rms(segment, 2) >= 16:
                break
            quiet_until = frame + block
        # 保留检测到声音前 75 毫秒，并原样保存此后的双声道采样。
        start = max(0, quiet_until - round(.075 * rate))
        assert start < params.nframes
        expected = pcm[start * frame_size:]
        with wave.open(str(folder / name), "wb") as target:
            target.setparams(params)
            target.writeframes(expected)
        changed = (folder / name).read_bytes()
        with wave.open(io.BytesIO(changed), "rb") as encoded:
            assert encoded.readframes(encoded.getnframes()) == expected
        prepared.append({"asset": name, "removed_ms": round(1000 * start / rate, 1),
                         "before_sha256": case["sha256"], "after_sha256": hashlib.sha256(changed).hexdigest(),
                         **stereo_metrics(changed)})
    (folder / "trim.json").write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8")
    print(folder, flush=True)


def verified_recordings(evidence):
    installed = ROOT / "voice" / "lines"
    new_cases = tuple(case for case in NEW_CASES if RECORDED_LINES.get(("reply", case[1])) == case[2])
    if evidence is not None:
        rows = {row["asset"]: row for row in json.loads((evidence / "trim.json").read_text(encoding="utf-8"))}
        assert set(rows) == {name for _, _, name in new_cases}, rows
        for name, row in rows.items():
            data = (evidence / name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == row["after_sha256"], name
            assert (installed / name).read_bytes() == data, name
    for _, text, filename in CASES + new_cases:
        assert RECORDED_LINES.get(("reply", text)) == filename, text
    return installed, new_cases + CASES


async def probe_unrecorded(page, server, report, received, greeting_count, prompt, wording=None):
    requests = []

    async def intercept(route):
        requests.append(route.request.post_data_json)
        await route.fulfill(status=503, json={"error": "隔离验收拦截动态语音"})

    url = server.url + "/api/speak"
    await page.route(url, intercept)
    try:
        await page.evaluate("resetGreetingMeasure()")
        await page.get_by_test_id("composer").fill(prompt)
        await page.get_by_test_id("send").click()
        await page.wait_for_function("readingFinished()", timeout=12000)
        trace = await page.evaluate("collectReadingMetrics()")
    finally:
        await page.unroute(url, intercept)
    request, = requests
    response, = received
    received.clear()
    state = await server.state()
    turn = state["turns"][-1]
    arm = {
        "input": prompt, "wording": turn["reply"], "source": turn["source"],
        "request_text": request["text"], "category": request["category"],
        "intercepted_synthesis_request": response.status == 503,
        "active_pcm_clips": len(trace["clips"]), "cloud_requests": turn["cloud_requests"]["total"],
    }
    report.setdefault("unrecorded_arm", []).append(arm)
    assert len(state["turns"]) == greeting_count + 1 and turn["user"] == prompt, arm
    if wording:
        assert turn["source"] == "smalltalk" and turn["reply"] == request["text"] == wording, arm
    else:
        assert turn["source"] != "smalltalk" and re.search(r"\d{2}:\d{2}:\d{2}", turn["reply"]), arm
    assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, arm
    assert request["text"] and (request["category"], request["text"]) not in RECORDED_LINES, arm
    assert arm["intercepted_synthesis_request"] and not trace["clips"] and trace["events"][-1]["type"] == "error", arm


async def measure(page, server, folder, report, baseline, cases):
    responses, errors = [], []
    page.on("response", lambda response: responses.append(response) if response.url == server.url + "/api/speak" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.add_init_script(PROBE + """
window.resetGreetingMeasure = () => { trace.events.length = trace.clips.length = 0; buffers.clear(); };
localStorage.setItem('moss.voice.enabled','on');
""")
    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.locator(".connection.online").wait_for()
    for index, (prompt, text, filename) in enumerate(cases):
        await page.evaluate("resetGreetingMeasure()")
        await page.get_by_test_id("composer").fill(prompt)
        await page.get_by_test_id("send").click()
        await page.wait_for_function("readingFinished()", timeout=160000)
        trace = await page.evaluate("({...collectReadingMetrics(), time_origin: performance.timeOrigin})")
        start = next(item for item in trace["events"] if item["type"] == "start")
        pending = next(item for item in trace["events"] if item["type"] == "pending")
        clip, = trace["clips"]
        response, = responses
        responses.clear()
        assert response.ok and response.request.post_data_json["text"] == text
        assert response.headers["x-moss-delivery"] == start["category"] == "reply"
        assert trace["events"][-1]["type"] == "end" and start["text"] == text and clip["first"] is not None, trace
        state = await server.state()
        turn = state["turns"][-1]
        assert len(state["turns"]) == index + 1 and turn["source"] == "smalltalk" and turn["reply"] == text
        assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, turn
        await expect(page.locator(f'[data-turn-id="{turn["id"]}"]').get_by_test_id("final")).to_have_text(text)
        wav = await response.body()
        output = clip["output"]
        output_clock = output["performanceTime"] > 0 and output["contextTime"] <= clip["start"]
        first_ms = ((clip["first"] - output["contextTime"]) * 1000 + output["performanceTime"] if output_clock
                    else (clip["first"] - clip["start"]) * 1000 + clip["scheduled"])
        seconds, _ = inspect_audio(wav)
        sha = hashlib.sha256(wav).hexdigest()
        server_timing = dict((name, float(duration)) for name, duration in
                             re.findall(r"([\w_]+);dur=([\d.]+)", response.headers.get("server-timing", "")))
        if baseline:
            assert wav == (baseline / filename).read_bytes(), text
            assert "worker" not in server_timing, (text, server_timing)
            assert not (server.data / "voice-prompts").is_dir(), "固定短句启动了合成进程"
        (folder / filename).write_bytes(wav)
        first_ms = round(first_ms - trace["submitted"], 1)
        timing = response.request.timing
        request_ms = timing["startTime"] - trace["time_origin"] - trace["submitted"]
        report["cases"].append({"input": prompt, "wording": text, "asset": filename, "sha256": sha,
                                "audio_seconds": seconds, "first_active_ms": first_ms, "native_output_clock": output_clock,
                                "pending_ms": round(pending["at"] - trace["submitted"], 1),
                                "request_ms": round(request_ms, 1), "http_body_ms": round(timing["responseEnd"], 1),
                                "scheduled_ms": round(clip["scheduled"] - trace["submitted"], 1),
                                "pcm_lead_ms": round((clip["first"] - clip["start"]) * 1000, 1),
                                "cloud_requests": turn["cloud_requests"]["total"], "server_timing_ms": server_timing,
                                "recorded_wav_verified": bool(baseline), **stereo_metrics(wav)})
        print(text, first_ms, flush=True)
    if report["suite"] == "recorded-all":
        # A synthetic 503 proves the normal dynamic request without invoking a worker.
        count = len(cases)
        for prompt, text, filename in NEW_CASES:
            if (prompt, text, filename) not in cases:
                await probe_unrecorded(page, server, report, responses, count, prompt, text)
                count += 1
        await probe_unrecorded(page, server, report, responses, count, "现在几点？")
    assert not errors, errors


async def watch(server, task, report):
    parent = psutil.Process(server.pid)
    while not task.done():
        available = psutil.virtual_memory().available // 2**20
        report["min_available_mib"] = min(report["min_available_mib"], available)
        for process in parent.children(recursive=True):
            try:
                if any(part.endswith("managed_worker.py") for part in process.cmdline()):
                    report["worker_seen"] = True
                    report["worker_peak_rss_mib"] = max(report["worker_peak_rss_mib"], process.memory_info().peak_wset // 2**20)
            except psutil.NoSuchProcess:
                continue
        if available < 4608:
            report["resource_stop"] = "可用内存低于 4.5 GiB"
            task.cancel()
            return
        await asyncio.sleep(.35)


def recognize(folder, cases):
    from voice import asr_engine

    def words(value):
        return "".join(character for character in value if character.isalnum())

    try:
        for case in cases:
            _, pcm = inspect_audio((folder / case["asset"]).read_bytes())
            text = case["asr"] = asr_engine.transcribe(pcm)
            case["asr_matches"] = words(case["wording"]) == words(text)
    finally:
        asr_engine.close()
    assert all(case["asr_matches"] for case in cases), cases


async def main(baseline, cases=CASES, suite="original-greetings", evidence=None):
    available = psutil.virtual_memory().available // 2**20
    gpu = None if baseline else free_gpu()
    if not baseline and (available < 8192 or gpu < 3800 or other_speaker()):
        raise RuntimeError(f"语音独占测试资源不足。可用内存 {available} MiB，显存 {gpu} MiB。")
    folder = ROOT / "runs" / datetime.now().strftime("greeting-audio-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "mode": "recorded" if baseline else "synthesis", "suite": suite, "cases": [],
              "start_free_mib": available, "min_available_mib": available, "start_gpu_free_mib": gpu,
              "worker_seen": False, "worker_peak_rss_mib": 0, "physical_speaker": False, "model_connections": 0}
    if evidence is not None:
        report["verified_asset_evidence"] = str(evidence.resolve())
    print(folder, flush=True)

    async def unexpected_model(_reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with AsyncExitStack() as stack:
            sink = await stack.enter_async_context(await asyncio.start_server(unexpected_model, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            catalog = folder / "media.sqlite3"
            MediaLibrary(catalog)
            environment = {
                "LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0",
                "MOSS_SPEECH_MIC_WARM": "0",
            }
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    environment[prefix + "_BASE_URL"] = model_url
                    environment[prefix + "_API_KEY"] = "isolated-qa"
            server = BrowserServer(folder, free_port(), model_url=model_url)
            stack.push_async_callback(server.stop)
            with patch.dict(os.environ, environment):
                await server.start()
            playwright = await stack.enter_async_context(async_playwright())
            browser = await open_browser(playwright)
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)
            page = await browser.new_page(reduced_motion="reduce")
            async with asyncio.TaskGroup() as group:
                work = group.create_task(measure(page, server, folder, report, baseline, cases))
                group.create_task(watch(server, work, report))
        assert report["worker_seen"] != bool(baseline)
        assert report["model_connections"] == 0, report
        if suite == "new-greetings" and baseline is None:
            cold, *warm = (case["server_timing_ms"] for case in report["cases"])
            assert cold.get("checkpoint", 0) > 0 and "spawn" in cold, cold
            assert all("worker" in timing and "checkpoint" not in timing and "spawn" not in timing for timing in warm), warm
        if evidence is not None:
            verified_cases = [case for case in report["cases"] if (evidence / case["asset"]).is_file()]
            recognize(folder, verified_cases)
        elif baseline is None or (baseline / "trim.json").is_file():
            recognize(folder, report["cases"])
        report["median_first_active_ms"] = median(case["first_active_ms"] for case in report["cases"])
        report["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--baseline", type=Path, help="比对播放音频与验收源文件，固定全量模式使用新增验收目录")
    mode.add_argument("--prepare", type=Path, help="裁剪合成音频片头；与 --new-greetings 共用时只处理通过听写的候选")
    suite = parser.add_mutually_exclusive_group()
    suite.add_argument("--new-greetings", action="store_true", help="测五句新增问候，早安先走冷启动")
    suite.add_argument("--recorded-all", action="store_true", help="验收已发布的固定问候及未缓存的正常语音路径")
    options = parser.parse_args()
    if options.prepare:
        if options.recorded_all:
            parser.error("--prepare 与 --recorded-all 不能一起使用")
        prepare_lines(options.prepare, verified_subset=options.new_greetings)
    elif options.recorded_all:
        installed, cases = verified_recordings(options.baseline)
        asyncio.run(main(installed, cases, "recorded-all", evidence=options.baseline))
    elif options.new_greetings:
        asyncio.run(main(options.baseline, NEW_CASES, "new-greetings"))
    else:
        asyncio.run(main(options.baseline))
