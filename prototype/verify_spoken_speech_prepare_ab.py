import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import median
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

import aiohttp
from aiohttp import web
import psutil
from playwright.async_api import async_playwright
from pydantic import SecretStr

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from device_status import memory_sample
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port
from verify_context_voice_e2e import make_voice
from verify_local_overview import QUESTION
from verify_local_overview_browser import UI_PROBE
from verify_speech_chat_prepare import first_audible
from verify_speech_cold_stages import IDLE_SECONDS, IMPORT_SETTLE_SECONDS, STANDBY_SECONDS, standby_resources, wait_for_workers
from verify_speech_long_reading import PROBE as READING_PROBE
from verify_speech_prepare_ab import free_gpu, other_speaker
from verify_speech_prepare import workers
from verify_spoken_speech_prepare_browser import PROBE as INPUT_PROBE
from voice_output import MossSpeech


MIB = 2**20
FIXED_CLOCK = "2026年11月21日，星期六，21:42:13。"
FIXED_BATTERY = "电量 63%（使用电池）。"
FIXED_VOLUME = "系统音量为 37%，当前未静音。"
SHORT_QUESTION = "现在几点？"
ACTIVITY_QUESTION = "电脑还有多少电，音量多大？"
PRIME_TEXT = "校验准备，窗外的云层已经散开。"
DEVICE_READINGS = (("battery", FIXED_BATTERY), ("volume", FIXED_VOLUME))
DEVICE_SPEECH = ("电量63%，使用电池。", "系统音量37%，未静音。")
FACT_SPEECH = {
    SHORT_QUESTION: (FIXED_CLOCK, "现在晚上九点四十二分。"),
    ACTIVITY_QUESTION: (FIXED_BATTERY + FIXED_VOLUME, "".join(DEVICE_SPEECH)),
    QUESTION: (
        "2026年11月21日，星期六，21点42分13秒。电量 63%（使用电池）。系统音量为 37%，当前未静音。",
        "现在晚上九点四十二分。电量63%，使用电池。系统音量37%，未静音。",
    ),
}
SPLIT_FACT_SPEECH = {
    QUESTION: ("现在晚上九点四十二分。", "".join(DEVICE_SPEECH)),
    ACTIVITY_QUESTION: DEVICE_SPEECH,
}
ACTIVITY_PROBE = """
window.micInput = {voice:[]};
const captureWorklet = window.AudioWorkletNode;
window.AudioWorkletNode = class extends captureWorklet {
  constructor(context, name, options) {
    super(context, name, options);
    if (name !== 'moss-voice-capture') return;
    this.port.addEventListener('message', ({data}) => {
      if (!(data instanceof Float32Array) || !data.length) return;
      micInput.frameMs = data.length / context.sampleRate * 1000;
      micInput.captureAt ??= performance.now() - micInput.frameMs;
      let square = 0;
      for (const sample of data) square += sample * sample;
      if (square / data.length >= .002 ** 2) micInput.voice.push(performance.now());
    });
  }
};
"""


async def battery(intent):
    assert intent["metric"] == "battery", intent
    return FIXED_BATTERY


async def volume(intent):
    assert intent["action"] == "get_volume", intent
    return FIXED_VOLUME


def delay_recording(fixture, output, seconds):
    if not seconds:
        return fixture
    with wave.open(str(fixture), "rb") as source, wave.open(str(output), "wb") as target:
        target.setparams(source.getparams())
        target.writeframes(bytes(source.getframerate() * source.getsampwidth() * source.getnchannels() * seconds))
        target.writeframes(source.readframes(source.getnframes()))
    return output


def server_timings(headers):
    return {name: float(value) for name, value in
            re.findall(r"([a-z_]+);dur=([\d.]+)", headers.get("server-timing", ""))}


async def warm_worker(base):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
        async with client.post(base + "/api/speak", json={"text": PRIME_TEXT, "category": "reply"}) as response:
            audio = await response.read()
            assert response.ok and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE", response.status
            assert response.headers.get("x-moss-batch-capable") == "1", response.headers
            timings = server_timings(response.headers)
            assert "spawn" in timings and timings["load"] > 0, timings
            return timings


async def seed_activity_standby(server, row):
    row["prime_timing_ms"] = await warm_worker(server.url)
    initial = set(workers(server))
    assert initial, "首次合成未检测到语音进程"
    row["prime_pids"] = sorted(initial)
    starting = await wait_for_workers(server, lambda pids: pids and not initial.intersection(pids), IDLE_SECONDS + 8)
    assert not any(psutil.pid_exists(pid) for pid in initial), initial
    await asyncio.sleep(IMPORT_SETTLE_SECONDS)
    sample = await standby_resources(server)
    row["before_listening"] = sample
    assert set(starting) <= set(sample["pids"]) and not initial.intersection(sample["pids"]), sample
    assert not sample["cuda_pids"] and sample["rss_mib"] < 1280 and sample["private_mib"] < 3584, sample
    assert min(sample["physical_available_mib"], sample["commit_available_mib"]) >= 8192, sample


def measure_input(row, data, transcribe, message):
    input_audio = data["input"]
    voice = [at for at in input_audio["voice"] if at < transcribe["at"]]
    assert voice, input_audio
    first, last = voice[0], voice[-1]
    assert data["mic"] <= input_audio["captureAt"] < first <= last < transcribe["at"] < message["at"], (
        input_audio, transcribe, message)
    row.update(first_input_at=round(first, 1), last_input_at=round(last, 1), input_frame_ms=round(input_audio["frameMs"], 1),
               input_tail_to_asr_ms=round(transcribe["at"] - last, 1),
               asr_to_message_ms=round(message["at"] - transcribe["at"], 1),
               input_tail_to_message_ms=round(message["at"] - last, 1))
    return first, last


def check_activity_request(row, data, transcribe, speaking, preflight, message):
    assert preflight and all(item["status"] == 204 for item in preflight), preflight
    preparation, = [item for item in preflight if not item["body"].get("release")]
    body = preparation["body"]
    if row["activity"]:
        assert set(body) == {"mic"} and re.fullmatch(r"[0-9a-f]{32}", body["mic"]), preparation
    else:
        assert body == {}, preparation
    first, last = measure_input(row, data, transcribe, message)
    assert first + 350 < preparation["at"] < last and message["at"] <= speaking["at"], (preparation, row)
    assert preparation["focus"] and not preparation["hidden"] and preparation["mic"] == "true", preparation
    row.update(prepare_from_first_input_ms=round(preparation["at"] - first, 1),
               prepare_before_asr_ms=round(transcribe["at"] - preparation["at"], 1))


async def collect(playwright, base, recording, folder, row, moss, previous_speech, fact_baseline, fact_split, *,
                  prime=True):
    steps_comparison = "steps" in row
    fact_comparison = bool(fact_baseline or fact_split or steps_comparison)
    split = bool(fact_split and row["enabled"])
    activity_comparison = "activity" in row
    if row.get("thermal") == "warm" and prime:
        row["prime_timing_ms"] = await warm_worker(base)
    browser = await playwright.chromium.launch(
        executable_path=str(Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"),
        headless=True, args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                             "--disable-renderer-backgrounding", "--use-fake-device-for-media-stream",
                             "--use-fake-ui-for-media-stream", f"--use-file-for-fake-audio-capture={recording}%noloop"])
    try:
        await confirm_muted_edge(browser)
        context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
        page = await context.new_page()
        row["outside"] = outside = []
        row["errors"] = errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        responses = []
        page.on("response", lambda response: responses.append(response) if response.url == base + "/api/speak" else None)

        async def controlled_request(route):
            url = route.request.url
            if urlsplit(url).scheme in {"http", "https"} and not url.startswith(base + "/"):
                outside.append(url)
                await route.abort()
            elif fact_baseline and not row["enabled"] and urlsplit(url).path in {
                "/static/speech-delivery.js", "/static/agent-speech.js"
            }:
                await route.fulfill(path=fact_baseline / urlsplit(url).path.rsplit("/", 1)[1],
                                    content_type="text/javascript")
            elif split and urlsplit(url).path == "/static/speech-delivery.js":
                await route.fulfill(path=fact_split, content_type="text/javascript")
            elif previous_speech and not row["enabled"] and urlsplit(url).path == "/static/agent-speech.js":
                await route.fulfill(path=previous_speech, content_type="text/javascript")
            elif (not activity_comparison and not fact_comparison and "mic_warm" not in row and not previous_speech
                  and url == base + "/api/prepare-speech" and not row["enabled"]):
                # Suppress the server lease only. The current UI, audio clock, ASR, and /api/speak remain real.
                await route.fulfill(status=204)
            else:
                await route.continue_()

        await context.route("**/*", controlled_request)
        if fact_comparison or activity_comparison:
            await page.add_init_script(UI_PROBE)
            await page.add_init_script(ACTIVITY_PROBE)
        await page.add_init_script(INPUT_PROBE + READING_PROBE + """
            localStorage.setItem('moss.voice.enabled','on');
            document.addEventListener('click', event => {
              if (event.target.closest('#mic')) window.micSubmitAt = performance.now();
            }, true);
        """)
        await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(timeout=15000)
        await page.bring_to_front()
        await page.locator("#mic").click()
        if previous_speech:
            await page.locator("#mic[aria-pressed=true]").wait_for(timeout=12000)
            enabled = page.get_by_role("checkbox", name="MOSS 语音播报", include_hidden=True)
            await enabled.evaluate("input=>{ input.checked = false; }")
            await page.get_by_test_id("composer").fill(QUESTION)
            await page.get_by_test_id("send").click()
            modal = page.locator('#local-status-panel[open][data-auto-result][data-phase="complete"]')
            await modal.wait_for(timeout=5000)
            await enabled.evaluate("input=>{ input.checked = true; window.infoPanelReady = performance.now(); }")
        await page.wait_for_function("readingFinished()", timeout=130000)
        data = await page.evaluate("""() => ({mic:micSubmitAt, audio:collectReadingMetrics(), input:window.micInput,
            calls:voiceInputQa.filter(entry=>entry.at >= (window.infoPanelReady || 0)),
            preparation:performance.getEntriesByName(location.origin + '/api/prepare-speech')
                .map(entry=>({sent:entry.startTime,received:entry.responseEnd}))})""")
        row["calls"] = data["calls"]
        row["audio"] = data["audio"]
        assert row["audio"]["events"][-1]["type"] == "end", row["audio"]["events"]
        assert not outside and not errors, row
        turns = moss.state()["turns"]
        assert len(turns) == (2 if previous_speech else 1), turns
        turn = turns[-1]
        assert turn["status"] == "replied" and not turn["cloud_requests"]["total"], turn
        if activity_comparison or row.get("question") == ACTIVITY_QUESTION:
            assert turn["source"] == "context" and turn["reply"] == "\n".join(reply for _, reply in DEVICE_READINGS), turn
            assert [(reading["kind"], reading["reply"]) for reading in turn["local_overview"]] == list(DEVICE_READINGS), turn
            panel = page.locator('#local-status-panel:modal[data-phase="complete"]')
            assert await panel.is_visible() and await panel.locator("[data-reading-kind]").count() == len(DEVICE_READINGS)
            row["reply"] = turn["reply"]
        elif row.get("question") == SHORT_QUESTION:
            assert turn["source"] == "holo" and turn["reply"] == FIXED_CLOCK, turn
        else:
            assert turn["source"] == "context" and turn["reply"] == "\n".join((FIXED_CLOCK, FIXED_BATTERY, FIXED_VOLUME)), turn
        calls = {item["url"]: item for item in row["calls"]}
        for url in ("/api/transcribe", "/api/message"):
            assert sum(item["url"] == url for item in row["calls"]) == 1, row["calls"]
        voice_requests = [item for item in row["calls"] if item["url"] == "/api/speak"]
        assert len(voice_requests) == (2 if split else 1), row["calls"]
        preflight = [item for item in row["calls"] if item["url"] == "/api/prepare-speech"]
        transcribe, speaking = calls["/api/transcribe"], voice_requests[0]
        assert transcribe["at"] < speaking["at"] < speaking["complete"], row["calls"]
        if activity_comparison:
            check_activity_request(row, data, transcribe, speaking, preflight, calls["/api/message"])
        elif fact_comparison:
            assert not preflight, preflight
            measure_input(row, data, transcribe, calls["/api/message"])
            visible = await page.evaluate("window.firstCompleteReply")
            assert visible and visible["text"] == turn["reply"], (visible, turn)
            row["visible_from_message_ms"] = round(visible["at"] - calls["/api/message"]["at"], 1)
            row["visible_reply"] = visible["text"]
            if row["question"] == QUESTION:
                readings = (("clock", FIXED_CLOCK), *DEVICE_READINGS)
                assert [(item["kind"], item["reply"]) for item in turn["local_overview"]] == list(readings), turn
                panel = page.locator('#local-status-panel[open][data-auto-result][data-phase="complete"]')
                assert await panel.locator("[data-reading-kind]").count() == len(readings)
        elif "mic_warm" in row:
            assert all(item["status"] == 204 for item in preflight), preflight
            mic = [item for item in preflight if item["body"].get("mic") and not item["body"].get("release")]
            assert len(mic) == int(row["mic_warm"]), preflight
            if mic:
                preparation, = mic
                assert (re.fullmatch(r"[0-9a-f]{32}", preparation["body"]["mic"])
                        and data["mic"] <= preparation["at"] < transcribe["at"]
                        and preparation["focus"] and not preparation["hidden"]), preparation
                row["mic_click_to_prepare_ms"] = round(preparation["at"] - data["mic"], 1)
                row["prepare_before_asr_ms"] = round(transcribe["at"] - preparation["at"], 1)
                row["prepare_before_speak_ms"] = round(speaking["at"] - preparation["at"], 1)
                matching = min(data["preparation"], key=lambda entry: abs(entry["sent"] - preparation["at"]))
                assert abs(matching["sent"] - preparation["at"]) < 50 and matching["received"] > matching["sent"], matching
                row["prepare_completed_before_speak_ms"] = round(speaking["at"] - matching["received"], 1)
            else:
                preparation, = preflight
                assert preparation["body"] == {} and preparation["at"] < transcribe["at"], preflight
                row["prepare_before_asr_ms"] = round(transcribe["at"] - preparation["at"], 1)
        else:
            assert len(preflight) == int(row["enabled"] or not previous_speech), row["calls"]
        if preflight and "mic_warm" not in row and not fact_comparison and not activity_comparison:
            preparation, = preflight
            assert preparation["body"] == {} and preparation["status"] == 204 and preparation["at"] < transcribe["at"]
            row["prepare_before_asr_ms"] = round(transcribe["at"] - preparation["at"], 1)
        assert len(responses) == len(voice_requests), row["calls"]
        wavs, payloads, timings_by_part = [], [], []
        for response in responses:
            audio = await response.body()
            assert response.ok and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE", response.status
            assert response.headers.get("x-moss-batch-capable") == "1", response.headers
            wavs.append(audio)
            payloads.append(response.request.post_data_json)
            timings_by_part.append(server_timings(response.headers))
        row["speech_request"] = payloads[0]
        row["timing_ms"] = timings_by_part[0]
        assert "imports" in row["timing_ms"], row["timing_ms"]
        if activity_comparison:
            assert payloads == [{"text": "".join(DEVICE_SPEECH),
                                 "category": "reply", "prefer_cached": False}], payloads
            load_ms = row["timing_ms"]["load"]
            assert ("spawn" not in row["timing_ms"]
                    and (load_ms == 0 if row["activity"] else load_ms > 0)), row["timing_ms"]
        elif fact_comparison:
            compact = FACT_SPEECH[row["question"]][1]
            variant = int(bool(fact_split) or steps_comparison or row["enabled"])
            expected = SPLIT_FACT_SPEECH[row["question"]] if split else (FACT_SPEECH[row["question"]][variant],)
            assert payloads == [{"text": part, "category": "reply", "prefer_cached": False} for part in expected], row
            if split:
                assert "".join(payload["text"] for payload in payloads) == compact, payloads
            starts = [event for event in row["audio"]["events"] if event["type"] == "start"]
            assert len(starts) == len(expected), starts
            for index, start in enumerate(starts):
                assert start["id"] == turn["id"] and not start["shortCue"], starts
                assert start["count"] == len(expected) and start["sentence"] == index + 1, starts
                assert start["text"].replace("\n", "") == expected[index], starts
            for index, timings in enumerate(timings_by_part):
                cold = index == 0 and row["thermal"] == "cold"
                assert ("spawn" in timings) == cold and (timings["load"] > 0 if cold else timings["load"] == 0), timings
                if not prime:
                    assert "cache" not in timings and timings["generate"] > 0, timings
            row["speech_requests"] = payloads
            row["timing_by_part_ms"] = timings_by_part
        elif "mic_warm" in row:
            load_ms = row["timing_ms"]["load"]
            assert ("spawn" not in row["timing_ms"]
                    and (load_ms == 0 if row["mic_warm"] else load_ms > 0)), row
        else:
            assert ("spawn" in row["timing_ms"]) != row["enabled"], row["timing_ms"]
        row["body_from_mic_ms"] = round(voice_requests[-1]["complete"] - data["mic"], 1)
        clips = row["audio"]["clips"]
        assert len(clips) == len(voice_requests) and all(clip["first"] is not None and clip["last"] is not None
                                                         for clip in clips), clips
        clip = clips[0]
        row["first_pcm_from_mic_ms"], row["first_pcm_clock"] = first_audible(clip, data["mic"])
        row["first_pcm_from_asr_ms"], _ = first_audible(clip, transcribe["at"])
        if activity_comparison or fact_comparison:
            row["first_pcm_from_input_tail_ms"], _ = first_audible(clip, row["last_input_at"])
            row["first_pcm_from_message_ms"], _ = first_audible(clip, calls["/api/message"]["at"])
        if fact_comparison:
            submitted = calls["/api/message"]["at"]
            last = clips[-1]
            row["last_pcm_from_message_ms"], row["last_pcm_clock"] = first_audible({**last, "first": last["last"]}, submitted)
            row["last_pcm_from_input_tail_ms"], _ = first_audible({**last, "first": last["last"]}, row["last_input_at"])
            row["playback_by_part"] = [
                {"first_pcm_from_input_tail_ms": first_audible(part, row["last_input_at"])[0],
                 "first_pcm_from_message_ms": first_audible(part, submitted)[0],
                 "last_pcm_from_message_ms": first_audible({**part, "first":part["last"]}, submitted)[0],
                 "sha256": hashlib.sha256(audio).hexdigest()}
                for part, audio in zip(clips, wavs)
            ]
            if row["question"] == ACTIVITY_QUESTION:
                assert row["first_pcm_clock"] == row["last_pcm_clock"] == "web_audio_output_timestamp", clips
            row["active_play_ms"] = round(sum(1000 * (part["last"] - part["first"]) for part in clips), 1)
            row["voice_gap_ms"] = round(sum(max(0, 1000 * (b["first"] - a["last"]))
                                              for a, b in zip(clips, clips[1:])), 1)
        row["asr_text"] = transcribe["text"]
        row["sha256"] = hashlib.sha256(b"".join(wavs)).hexdigest()
        for index, audio in enumerate(wavs):
            (folder / ("reply.wav" if index == 0 else f"reply-{index + 1}.wav")).write_bytes(audio)
        if steps_comparison:
            path = folder / "reply.wav"
            with wave.open(str(path), "rb") as output:
                channels, width, rate, frames, _, _ = output.getparams()
                assert channels == 2 and width == 2 and rate > 0 and frames > 0, output.getparams()
                row["wav"] = {"channels": channels, "sample_width": width, "sample_rate": rate,
                              "frames": frames, "duration_ms": round(1000 * frames / rate, 1)}
                row["pcm_sha256"] = hashlib.sha256(output.readframes(frames)).hexdigest()
            row["file"] = path.relative_to(folder.parent).as_posix()
    finally:
        await browser.close()


async def resources():
    sample = memory_sample()
    return {"ram_mib": sample.available // MIB, "commit_mib": sample.available_pagefile // MIB,
            "gpu_mib": await asyncio.to_thread(free_gpu)}


async def start_resources():
    sample = await resources()
    available, commit, gpu = sample["ram_mib"], sample["commit_mib"], sample["gpu_mib"]
    if min(available, commit) < 8192 or gpu < 3800 or other_speaker():
        raise RuntimeError(f"合成前的空闲内存 {available} MiB，提交余量 {commit} MiB，显存 {gpu} MiB，或已有语音进程。")
    return resource_row(sample)


def resource_row(sample):
    return {f"{label}_{key}": value for label in ("start", "min") for key, value in sample.items()}


async def watch_resources(task, aggregate, current_round=None):
    while not task.done():
        sample = await resources()
        row = current_round() if current_round else None
        for key, value in sample.items():
            minimum = "min_" + key
            aggregate[minimum] = min(aggregate[minimum], value)
            if row is not None:
                row[minimum] = min(row[minimum], value)
        available, commit, gpu = sample["ram_mib"], sample["commit_mib"], sample["gpu_mib"]
        if min(available, commit) < 4500 or gpu < 512:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise RuntimeError(f"停止测试。空闲内存 {available} MiB，提交余量 {commit} MiB，显存 {gpu} MiB。")
        await asyncio.wait({task}, timeout=.8)
    await task


async def measure(playwright, folder, model_url, recording, enabled, previous_speech, *,
                  lease=None, fact_baseline=None, fact_split=None, fact=None, activity_ab=False):
    folder.mkdir()
    row = {"enabled": enabled, **await start_resources()}
    if fact is not None:
        row.update(fact)
    if activity_ab:
        row["activity"] = enabled
        os.environ["MOSS_SPEECH_MIC_WARM"] = "activity" if enabled else "0"
    elif lease is not None:
        row.update(lease)
        os.environ["MOSS_SPEECH_MIC_WARM"] = "1" if row["mic_warm"] else "0"
    if lease is not None or activity_ab:
        isolated_profiles = {
            key: Profile(key, label, model_url, model, SecretStr("isolated-only" if key == "deepseek" else ""))
            for key, (label, _, __, model) in SPECS.items()
        }
        with patch("core.profiles", return_value=isolated_profiles):
            moss = Moss(folder / "data", model_url, "isolated-unused", prefer_cloud=True)
        assert moss.model_id == "deepseek"
    else:
        moss = Moss(folder / "data", model_url, "isolated-unused")
    port = free_port()
    server = BrowserServer(folder, port, model_url=model_url)
    server.pid = os.getpid()
    speech_settings = {"speech_idle_seconds": IDLE_SECONDS, "speech_standby_seconds": STANDBY_SECONDS} if activity_ab else {}
    app = application(moss, lambda: "standby", folder / "data", port, "isolated", asyncio.Event(), **speech_settings)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    task = None

    async def play_case():
        if activity_ab:
            await seed_activity_standby(server, row)
        await collect(playwright, server.url, recording, folder, row, moss, previous_speech, fact_baseline, fact_split)
        if activity_ab:
            current = workers(server)
            row["reply_worker_pids"] = current
            assert set(row["before_listening"]["pids"]) <= set(current), current

    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        task = asyncio.create_task(play_case())
        await watch_resources(task, row)
        return row
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()
        await moss.close()
        if activity_ab:
            row["after_shutdown_pids"] = workers(server)
        (folder / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        if activity_ab:
            assert not row["after_shutdown_pids"], row["after_shutdown_pids"]


async def measure_reused_worker(playwright, folder, model_url, recording, report, *, question, fact_split,
                                steps_comparison=False):
    budget = report["reused_worker_resources"] = await start_resources()
    data = folder / "shared-data"
    moss = Moss(data, model_url, "isolated-unused")
    speech = MossSpeech(data)
    server = BrowserServer(folder, free_port(), model_url=model_url)
    server.pid = os.getpid()
    with patch("serve.MossSpeech", return_value=speech):
        app = application(moss, lambda: "standby", data, server.port, "isolated", asyncio.Event())
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    task = row = None

    if steps_comparison:
        original_worker_reply = speech._worker_reply

        async def compare_steps(request, cancel, disconnected, timeout):
            assert request["steps"] == 16 and "text" in request and request["room_amount"] == 100, request
            if row is not None:
                request = {**request, "steps": row["steps"]}
            reply = await original_worker_reply(request, cancel, disconnected, timeout)
            info = reply["info"]
            assert reply["code"] == 0 and info["steps"] == request["steps"], reply
            assert info["device"] == info["decoder"] == "cuda", info
            record = {key: request[key] for key in ("text", "steps", "speed", "room_amount", "warm_voice")}
            record["worker_reported_steps"] = info["steps"]
            if row is None:
                report["prime_worker_request"] = record
            else:
                row.setdefault("worker_requests", []).append(record)
            return reply

        # Override only this isolated service's instance. The production class and browser requests remain intact.
        speech._worker_reply = compare_steps

    async def play_cases():
        nonlocal row
        report["prime_timing_ms"] = await warm_worker(server.url)
        worker = speech.worker
        assert worker is not None and worker.returncode is None and worker.pid in workers(server)
        report["reused_worker_pid"] = worker.pid
        for index, enabled in enumerate((False, True, True, False), 1):
            steps = 12 if enabled else 16
            mode = f"warm-{steps}-steps" if steps_comparison else ("warm-split" if enabled else "warm-compact")
            case = folder / f"{index}-{mode}"
            case.mkdir()
            row = {"enabled": enabled, "thermal": "warm", "question": question, **resource_row(await resources())}
            if steps_comparison:
                row["steps"] = steps
            try:
                async with speech.lock:
                    assert speech.worker is worker and worker.returncode is None
                    speech.lines.clear()
                row["chat_id"] = await moss.new_chat()
                assert not moss.state()["turns"]
                await collect(playwright, server.url, recording, case, row, moss, None, None, fact_split, prime=False)
                assert speech.worker is worker and worker.returncode is None and worker.pid in workers(server)
                assert len(speech.lines) == (2 if fact_split and enabled else 1), "每段均须由本轮重新合成"
                if steps_comparison:
                    request, = row["worker_requests"]
                    assert request["worker_reported_steps"] == steps, request
                row["speech_worker_pid"] = worker.pid
                report["rounds"].append(row)
                print(index, mode, worker.pid, row["first_pcm_from_input_tail_ms"], flush=True)
            finally:
                (case / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                row = None

    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", server.port).start()
        task = asyncio.create_task(play_cases())
        await watch_resources(task, budget, lambda: row)
        assert len({item["chat_id"] for item in report["rounds"]}) == 4
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()
        await moss.close()
        report["after_shutdown_pids"] = workers(server)
        assert not report["after_shutdown_pids"], report["after_shutdown_pids"]


async def run(abba, previous_speech=None, *, lease_leads=(), question=QUESTION, fact_baseline=None,
              fact_split=None, thermal_states=(), activity_ab=False, reuse_warm_worker=False, steps_comparison=False):
    if steps_comparison:
        question = ACTIVITY_QUESTION
        thermal_states = ("warm",)
        reuse_warm_worker = True
    if reuse_warm_worker and (not (fact_split or steps_comparison) or thermal_states != ("warm",)):
        raise ValueError("复用热机只支持热态拆段或步数对照")
    fact_comparison = bool(fact_baseline or fact_split or steps_comparison)
    prefix = "spoken-first-fact-ab" if fact_comparison else "spoken-speech-prepare-ab"
    if activity_ab:
        prefix = "spoken-speech-activity-ab"
        question = ACTIVITY_QUESTION
    if steps_comparison:
        prefix = "spoken-steps-ab"
    folder = ROOT / "runs" / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "model_connections": 0, "rounds": [], "uses_fixed_readings": True,
              "input": "Windows Huihui → Edge file microphone → real SenseVoice", "output": "CUDA OmniVoice → muted Edge PCM",
              "automatic_panel_open":bool(previous_speech), "question":question, "lease_leads_s":lease_leads}
    if reuse_warm_worker:
        report["reuse_warm_worker"] = True
    if activity_ab:
        report.update(idle_seconds=IDLE_SECONDS, standby_seconds=STANDBY_SECONDS, activity_ab=True,
                      voice_input_clock="capture_worklet_active_frame")
    if fact_comparison:
        if not steps_comparison:
            report["fact_baseline" if fact_baseline else "fact_split"] = str(fact_baseline or fact_split)
        report["thermal_states"] = thermal_states
        report["voice_input_clock"] = "capture_worklet_active_frame"
        if fact_split:
            report["speech_delivery_sha256"] = {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in (("compact", ROOT / "static/speech-delivery.js"), ("split", fact_split))
            }
    if steps_comparison:
        report["inference_steps_order"] = [16, 12, 12, 16]
        report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("static/agent-voice.js", "static/agent-speech.js", "static/speech-delivery.js",
                         "voice_output.py", "voice/clone_omnivoice.py")}
    fixture = make_voice(folder, text=question)
    if previous_speech:
        fixture = delay_recording(fixture, folder / "file-microphone-after-panel.wav", 4)
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0" if fact_comparison else "1",
                      MOSS_SPEECH_STANDBY="0", MOSS_SPEECH_MIC_WARM="0")

    async def reject_cloud(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with await asyncio.start_server(reject_cloud, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            with (patch("assistant_brain.local_clock_reply", return_value=FIXED_CLOCK),
                  patch.multiple("local_overview", local_clock_reply=lambda: FIXED_CLOCK, device_status=battery,
                                 run_desktop=volume)):
                async with async_playwright() as playwright:
                    if reuse_warm_worker:
                        await measure_reused_worker(playwright, folder, model_url, fixture, report,
                                                    question=question, fact_split=fact_split,
                                                    steps_comparison=steps_comparison)
                    cycles = () if reuse_warm_worker else thermal_states or lease_leads or (None,)
                    for cycle, lead in enumerate(cycles):
                        thermal = lead if fact_comparison else None
                        if thermal:
                            lead = None
                        recording = fixture if lead is None else delay_recording(
                            fixture, folder / f"file-microphone-{lead}s.wav", lead)
                        order = (False, True, True, False) if abba or activity_ab else (False, True)
                        if cycle % 2:
                            order = order[::-1]
                        for comparison in order:
                            index = len(report["rounds"]) + 1
                            fact = None
                            if thermal:
                                name = ("split" if comparison else "compact") if fact_split else (
                                    "concise" if comparison else "baseline")
                                mode, lease = f"{thermal}-{name}", None
                                fact = {"thermal": thermal, "question": question}
                            elif activity_ab:
                                mode, lease = ("activity-cuda" if comparison else "activity-cpu"), None
                            elif lead is None:
                                mode, lease = ("ahead" if comparison else "baseline"), None
                            else:
                                mode = "mic-warm" if comparison else "imports"
                                lease = {"mic_warm":comparison, "extra_input_silence_s":lead, "question":question}
                            row = await measure(playwright, folder / f"{index}-{mode}", model_url, recording,
                                                comparison, previous_speech, lease=lease, fact_baseline=fact_baseline,
                                                fact_split=fact_split, fact=fact, activity_ab=activity_ab)
                            report["rounds"].append(row)
                            print(index, mode, lead, row["first_pcm_from_asr_ms"], flush=True)
        rounds = report["rounds"]
        assert not report["model_connections"], report
        if fact_comparison:
            names = ("compact", "split") if fact_split else ("baseline", "concise")
            if steps_comparison:
                names = ("steps16", "steps12")
                assert [row["steps"] for row in rounds] == report["inference_steps_order"]
                assert len({json.dumps({key:value for key,value in row["worker_requests"][0].items()
                                       if key not in {"steps", "worker_reported_steps"}}, sort_keys=True)
                            for row in rounds}) == 1, rounds
                for name, digest in report["source_sha256"].items():
                    assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
            report["unique_wave_hashes_by_variant"] = {
                name: len({row["sha256"] for row in rounds if row["enabled"] is enabled})
                for name, enabled in zip(names, (False, True))
            }
            for thermal in thermal_states:
                group = [row for row in rounds if row["thermal"] == thermal]
                summary = report.setdefault("by_thermal", {}).setdefault(thermal, {})
                for metric in ("first_pcm_from_asr_ms", "visible_from_message_ms", "first_pcm_from_message_ms",
                               "first_pcm_from_input_tail_ms", "input_tail_to_message_ms", "last_pcm_from_message_ms",
                               "last_pcm_from_input_tail_ms", "active_play_ms", "voice_gap_ms"):
                    baseline = median(row[metric] for row in group if not row["enabled"])
                    concise = median(row[metric] for row in group if row["enabled"])
                    summary[metric] = {names[0]: baseline, names[1]: concise, "saved": round(baseline - concise, 1)}
                if steps_comparison:
                    for metric in ("generate", "mix", "tts"):
                        baseline = median(row["timing_ms"][metric] for row in group if not row["enabled"])
                        candidate = median(row["timing_ms"][metric] for row in group if row["enabled"])
                        summary[metric + "_ms"] = {names[0]: baseline, names[1]: candidate,
                                                   "saved": round(baseline - candidate, 1)}
        else:
            assert len({row["sha256"] for row in rounds}) == 1, report
        if activity_ab or question == ACTIVITY_QUESTION:
            transcripts = sorted({row["asr_text"] for row in rounds})
            question_words = re.sub(r"[\s，,。.!！?？、；;]+", "", question)
            assert all(re.sub(r"[\s，,。.!！?？、；;]+", "", text) == question_words for text in transcripts), transcripts
            assert len({row["reply"] for row in rounds}) == 1
            report["asr_transcripts"] = transcripts
            if activity_ab:
                for metric in ("first_pcm_from_input_tail_ms", "first_pcm_from_message_ms"):
                    baseline = median(row[metric] for row in rounds if not row["activity"])
                    ahead = median(row[metric] for row in rounds if row["activity"])
                    report[metric + "_saved"] = round(baseline - ahead, 1)
        for lead in (() if fact_comparison else lease_leads or (None,)):
            group = [row for row in rounds if row.get("extra_input_silence_s") == lead]
            summary = report if lead is None else report.setdefault("by_extra_input_silence_s", {}).setdefault(lead, {})
            for metric in ("first_pcm_from_mic_ms", "first_pcm_from_asr_ms", "body_from_mic_ms"):
                baseline = median(row[metric] for row in group if not row["enabled"])
                ahead = median(row[metric] for row in group if row["enabled"])
                summary[metric + "_saved"] = round(baseline - ahead, 1)
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--abba", action="store_true", help="每组运行四轮交错对照。")
    parser.add_argument("--previous-speech", type=Path, help="打开自动本机状态弹窗后对比旧版播报脚本。")
    parser.add_argument("--lease-ab", action="store_true", help="比较开麦后 CUDA 权重准备与普通的模块预导入。")
    parser.add_argument("--activity-ab", action="store_true", help="从 CPU 待机开始，交错对比确认开口后 CUDA 准备与普通预导入。")
    parser.add_argument("--lead-seconds", nargs="+", type=int, help="文件麦克风额外垫的静音秒数，默认 0 5 10。")
    questions = {"composite": QUESTION, "short": SHORT_QUESTION, "device": ACTIVITY_QUESTION}
    parser.add_argument("--question-mode", choices=questions, default="composite")
    parser.add_argument("--fact-baseline", type=Path, help="含旧版 speech-delivery.js 与 agent-speech.js 的目录。")
    parser.add_argument("--fact-split", type=Path, help="把当前复合读数拆成两个片段的替代 speech-delivery.js。")
    parser.add_argument("--thermal", choices=("cold", "warm", "both"), help="事实播报对照的工作进程状态。")
    parser.add_argument("--reuse-warm-worker", action="store_true", help="热态拆段对照共用一个语音进程，强制四轮交错。")
    parser.add_argument("--steps-ab", action="store_true", help="用当前整句播报和同一 CUDA 热机交错比较 16 步和 12 步。")
    args = parser.parse_args()
    if sum(map(bool, (args.lease_ab, args.previous_speech, args.fact_baseline, args.fact_split, args.activity_ab,
                     args.steps_ab))) > 1:
        parser.error("各种对照模式须独立运行。")
    if (args.lead_seconds and not args.lease_ab) or (args.thermal and not (args.fact_baseline or args.fact_split)):
        parser.error("额外静音仅用于开麦准备对照。冷热选项仅用于事实播报对照。")
    if args.question_mode == "short" and not (args.lease_ab or args.fact_baseline):
        parser.error("单句仅用于开麦准备或事实播报对照。")
    if args.question_mode == "device" and not (args.fact_split or args.steps_ab):
        parser.error("仅问电量和音量的用例用于拆段或步数对照。")
    if args.fact_baseline and not all((args.fact_baseline / name).is_file()
                                     for name in ("speech-delivery.js", "agent-speech.js")):
        parser.error("基线目录须包含旧版 speech-delivery.js 和 agent-speech.js。")
    if args.fact_split and not args.fact_split.is_file():
        parser.error("拆段对照须提供替代脚本文件，并使用复合读数。")
    if args.reuse_warm_worker and not (args.fact_split and args.thermal == "warm"):
        parser.error("--reuse-warm-worker 需要 --fact-split 与 --thermal warm。")
    lead_times = args.lead_seconds or (0, 5, 10)
    if any(seconds < 0 for seconds in lead_times) or len(set(lead_times)) != len(lead_times):
        parser.error("额外静音秒数须互不相同，且均为非负整数。")
    thermal_states = ()
    if args.fact_baseline or args.fact_split:
        thermal = args.thermal or "both"
        thermal_states = ("cold", "warm") if thermal == "both" else (thermal,)
    asyncio.run(run(args.abba, args.previous_speech, lease_leads=lead_times if args.lease_ab else (),
                    question=questions[args.question_mode],
                    fact_baseline=args.fact_baseline, fact_split=args.fact_split, thermal_states=thermal_states,
                    activity_ab=args.activity_ab, reuse_warm_worker=args.reuse_warm_worker,
                    steps_comparison=args.steps_ab))
