"""Measure FUI voice cold/warm first-PCM latency (CUDA) and FUI speech/dialogue parity."""

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import time
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from device_status import GIB, memory_sample
from local_media.library import MediaLibrary
from serve import application
from verify_context_reply import free_port
from verify_speech_chat_prepare import first_audible
from verify_speech_prepare_ab import free_gpu
from voice_output import MossSpeech


FUI_PUBLIC = ROOT / "fui/public"
LONG_SAMPLE = (
    "沿着河走。过桥后向左转。前面不远就是老码头，石阶边停着几条木船。"
    "风起的时候，水面会轻轻晃动，灯影拉得很长。夜里走这段路记得慢一点，脚下有青苔，拐角处有一盏暖黄的路灯。"
)

FUI_SPEECH_PROBE = """
sessionStorage.setItem('moss-fui.opened', '1');
localStorage.setItem('moss-fui.scene3d', 'off');
localStorage.setItem('moss-fui.tts', 'on');
localStorage.setItem('moss.voice.enabled', 'on');
localStorage.setItem('moss.speech.space.percent', '100');

window.fuiSpeechQA = {
  submitted: null,
  clips: [],
  events: [],
  requests: [],
  captions: [],
  volumeHistory: [],
};
const buffers = new Map();

document.addEventListener('submit', event => {
  if (event.target.id === 'comms-composer') {
    window.fuiSpeechQA.submitted = performance.now();
  }
}, true);

for (const type of ['pending', 'start', 'progress', 'end', 'stop', 'error', 'skipped']) {
  window.addEventListener('moss-speech-' + type, ({detail}) => {
    window.fuiSpeechQA.events.push({type, ...(detail || {}), at: performance.now()});
    if (detail?.caption) {
      window.fuiSpeechQA.captions.push({type, caption: detail.caption, at: performance.now()});
    }
  });
}

const nativeFetch = window.fetch;
window.fetch = async function(input, options) {
  const url = typeof input === 'string' ? input : input?.url || '';
  const path = new URL(url, location.href).pathname;
  const track = ['/api/speak', '/api/speak-batch', '/api/prepare-speech', '/api/message', '/api/cancel'];
  if (!track.includes(path)) return nativeFetch.apply(this, arguments);
  let body = null;
  try { body = options?.body ? JSON.parse(options.body) : null; } catch {}
  const row = {path, started: performance.now(), body};
  window.fuiSpeechQA.requests.push(row);
  try {
    const response = await nativeFetch.apply(this, arguments);
    row.status = response.status;
    row.responded = performance.now();
    row.server_timing = response.headers.get('Server-Timing');
    row.synthesis = response.headers.get('X-MOSS-Synthesis');
    row.batch_capable = response.headers.get('X-MOSS-Batch-Capable');
    row.announcement = response.headers.get('X-MOSS-Announcement');
    if (path === '/api/speak' && response.ok && response.headers.get('Content-Type')?.startsWith('audio/wav')) {
      const clone = response.clone();
      const bytes = await clone.arrayBuffer();
      row.wav_bytes = bytes.byteLength;
      const digest = await crypto.subtle.digest('SHA-256', bytes);
      row.wav_sha256 = Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
    }
    return response;
  } catch (error) {
    row.error = error?.name || String(error);
    row.responded = performance.now();
    throw error;
  }
};

const originalStart = AudioBufferSourceNode.prototype.start;
const originalStop = AudioBufferSourceNode.prototype.stop;
AudioBufferSourceNode.prototype.start = function(when=0, offset=0, duration) {
  const context = this.context;
  const start = Math.max(when, context.currentTime);
  const scheduled = performance.now();
  const output = context.getOutputTimestamp();
  originalStart.apply(this, arguments);
  const span = duration ?? this.buffer.duration - offset;
  const clip = {start, scheduled, output, offset, duration: this.buffer.duration, end: start + span, stops: []};
  window.fuiSpeechQA.clips.push(clip);
  buffers.set(this, clip);
};
AudioBufferSourceNode.prototype.stop = function(when=0) {
  buffers.get(this)?.stops.push(Math.max(when, this.context.currentTime));
  return originalStop.apply(this, arguments);
};

window.resetFuiSpeechQA = () => {
  window.fuiSpeechQA.submitted = null;
  window.fuiSpeechQA.clips = [];
  window.fuiSpeechQA.events = [];
  window.fuiSpeechQA.requests = [];
  window.fuiSpeechQA.captions = [];
  buffers.clear();
};

window.collectFuiSpeechMetrics = () => {
  for (const [source, clip] of buffers) {
    const audio = source.buffer, rate = audio.sampleRate, frames = Math.round(rate / 100);
    const channels = Array.from({length: audio.numberOfChannels}, (_, n) => audio.getChannelData(n));
    if (clip.stops.length) clip.end = Math.min(clip.end, clip.stops.at(-1));
    const begin = Math.round(clip.offset * rate);
    const finish = Math.min(audio.length, begin + Math.round((clip.end - clip.start) * rate));
    clip.first = null;
    clip.last = null;
    for (let i = begin; i + frames <= finish; i += frames) {
      if (!channels.some(channel => {
        let squares = 0;
        for (let n = i; n < i + frames; n++) squares += channel[n] ** 2;
        return squares / frames >= 10 ** (-45 / 10);
      })) continue;
      clip.first ??= clip.start + (i - begin) / rate;
      clip.last = clip.start + (i + frames - begin) / rate;
    }
  }
  return window.fuiSpeechQA;
};
"""


def make_tone_wav(*, seconds=0.65, hz=440, lead_silence_s=0.09, tail_silence_s=0.26, rate=24000, pause_mid_s=0.0):
    import math
    lead = int(lead_silence_s * rate)
    tail = int(tail_silence_s * rate)
    body = int(seconds * rate)
    buf = BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = bytearray()
        frames.extend(b"\x00\x00\x00\x00" * lead)
        mid_start = int(body * 0.44)
        mid_end = mid_start + int(pause_mid_s * rate)
        for i in range(body):
            if pause_mid_s > 0 and mid_start <= i < mid_end:
                sample = 0
            else:
                sample = int(9500 * math.sin(2 * math.pi * hz * i / rate))
            frames.extend(struct.pack("<hh", sample, sample))
        frames.extend(b"\x00\x00\x00\x00" * tail)
        wav.writeframes(bytes(frames))
    return buf.getvalue()


def resources_snapshot():
    mem = memory_sample()
    return {
        "ram_avail_mib": round(mem.available / (1024 ** 2), 1),
        "commit_avail_mib": round(mem.available_pagefile / (1024 ** 2), 1),
        "gpu_free_mib": free_gpu(),
    }


def summarize_trace(trace):
    submitted = trace["submitted"]
    clips = trace["clips"]
    starts = [ev for ev in trace["events"] if ev["type"] == "start"]
    ends = [ev for ev in trace["events"] if ev["type"] == "end"]
    first_pcm_ms, clock_kind = first_audible(clips[0], submitted) if clips and clips[0].get("first") is not None else (None, None)
    last_pcm_ms = None
    if clips and clips[-1].get("last") is not None:
        c = clips[-1]
        out = c["output"]
        if out["performanceTime"] > 0 and out["contextTime"] <= c["start"]:
            at = out["performanceTime"] + (c["last"] - out["contextTime"]) * 1000
        else:
            at = c["scheduled"] + (c["last"] - c["start"]) * 1000
        last_pcm_ms = round(at - submitted, 1)

    audible_gaps_ms = []
    schedule_gaps_ms = []
    for prev, nxt in zip(clips, clips[1:]):
        if prev.get("last") is not None and nxt.get("first") is not None:
            audible_gaps_ms.append(round((nxt["first"] - prev["last"]) * 1000, 1))
        schedule_gaps_ms.append(round((nxt["start"] - prev["end"]) * 1000, 1))

    speak_reqs = [r for r in trace["requests"] if r["path"] in {"/api/speak", "/api/speak-batch"}]
    return {
        "clip_count": len(clips),
        "start_events": len(starts),
        "end_events": len(ends),
        "first_pcm_ms": first_pcm_ms,
        "clock_kind": clock_kind,
        "last_pcm_ms": last_pcm_ms,
        "first_offset_ms": round(clips[0]["offset"] * 1000, 1) if clips else None,
        "audible_gaps_ms": audible_gaps_ms,
        "schedule_gaps_ms": schedule_gaps_ms,
        "speak_requests": [
            {
                "path": r["path"],
                "text_len": len(r["body"].get("text", "")) if isinstance(r.get("body"), dict) and "text" in r["body"] else None,
                "texts_count": len(r["body"].get("texts", [])) if isinstance(r.get("body"), dict) and "texts" in r["body"] else None,
                "prefer_cached": r["body"].get("prefer_cached") if isinstance(r.get("body"), dict) else None,
                "category": r["body"].get("category") if isinstance(r.get("body"), dict) else None,
                "status": r.get("status"),
                "req_after_submit_ms": round(r["started"] - submitted, 1) if submitted is not None else None,
                "http_ms": round(r["responded"] - r["started"], 1) if r.get("responded") is not None else None,
                "server_timing": r.get("server_timing"),
                "synthesis": r.get("synthesis"),
                "batch_capable": r.get("batch_capable"),
                "announcement": r.get("announcement"),
                "wav_sha256": r.get("wav_sha256"),
            }
            for r in speak_reqs
        ],
        "prepare_requests": [
            {
                "req_after_submit_ms": round(r["started"] - submitted, 1) if submitted is not None else None,
                "body": r.get("body"),
                "status": r.get("status"),
            }
            for r in trace["requests"] if r["path"] == "/api/prepare-speech"
        ],
        "captions": [c["caption"] for c in trace["captions"]],
    }


@asynccontextmanager
async def isolated_fui_server(data_dir: Path, *, speech=None, model_url=None, model="fui-qa-loopback", media_db: Path | None = None):
    port = free_port()
    stopped = asyncio.Event()
    db_path = media_db or (data_dir / "media.sqlite3")
    MediaLibrary(db_path)
    env = {
        "MOSS_MEDIA_DB": str(db_path),
        "MOSS_PROVIDER_DIR": str(data_dir / "providers"),
        "LOCALAPPDATA": str(data_dir / "profile"),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "1",
        "MOSS_SPEECH_STANDBY": "0",
    }
    async with AsyncExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        if speech is not None:
            stack.enter_context(patch("serve.MossSpeech", return_value=speech))
        moss = Moss(
            data_dir,
            model_url or "http://127.0.0.1:1/v1",
            model,
            prefer_cloud=False,
        )
        stack.push_async_callback(moss.close)
        app = application(
            moss,
            lambda: "external",
            data_dir,
            port,
            "fui-qa-token",
            stopped,
            fui_public=FUI_PUBLIC,
            default_mic_warm_mode="activity",
            speech_standby_seconds=0,
        )
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        def _stop():
            stopped.set()
            moss._notify()
        stack.callback(_stop)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        yield f"http://127.0.0.1:{port}", moss


async def run_cuda_benchmark(out_dir: Path, tag: str):
    report = {
        "mode": "cuda",
        "tag": tag,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "resources_before": resources_snapshot(),
        "cases": {},
        "page_errors": [],
        "passed": False,
    }
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    readings = {
        "battery": "电量 61%（使用电池）。",
        "volume": "系统音量为 43%，当前未静音。",
    }

    async def fake_battery(intent):
        return readings["battery"]

    async def fake_volume(intent):
        return readings["volume"]

    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"

    with patch("local_overview.device_status", side_effect=fake_battery), \
         patch("local_overview.run_desktop", side_effect=fake_volume):
        speech = MossSpeech(data_dir, idle_seconds=150, standby_seconds=0)
        async with isolated_fui_server(data_dir, speech=speech) as (base, moss):
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=str(edge),
                    headless=True,
                    args=["--mute-audio", "--disable-gpu", "--disable-webgl", "--disable-webgl2",
                          "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
                )
                try:
                    await confirm_muted_edge(browser)
                    context = await browser.new_context(viewport={"width": 1440, "height": 900},
                                                        reduced_motion="reduce", service_workers="block")
                    await context.add_init_script(FUI_SPEECH_PROBE)

                    async def route_guard(route):
                        url = route.request.url
                        if url.startswith(("http://", "https://")) and not url.startswith(base + "/"):
                            await route.abort()
                        else:
                            await route.continue_()

                    await context.route("**/*", route_guard)
                    page = await context.new_page()
                    page.set_default_timeout(120000)
                    page.on("pageerror", lambda err: report["page_errors"].append(str(err)))
                    await page.goto(base + "/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                    await expect(page.locator("body")).to_have_attribute("data-backend", "live", timeout=10000)
                    await page.bring_to_front()

                    composer = page.locator("#comms-input")
                    send_btn = page.locator("#comms-send")

                    async def run_prompt(name, text, *, timeout_ms=120000):
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill(text)
                        await send_btn.click()
                        await page.wait_for_function(
                            "() => window.fuiSpeechQA.events.some(e => e.type === 'end' || e.type === 'error')",
                            timeout=timeout_ms,
                        )
                        trace = await page.evaluate("window.collectFuiSpeechMetrics()")
                        summary = summarize_trace(trace)
                        summary["resources_after"] = resources_snapshot()
                        summary["terminal_event"] = trace["events"][-1]["type"] if trace["events"] else None
                        report["cases"][name] = summary
                        return summary

                    # Case 1: Cold dynamic short reply (empty cache, fresh worker)
                    print("[cuda] case 1 cold short status", flush=True)
                    c1 = await run_prompt("cold_short_status", "电脑还有多少电，音量多大？", timeout_ms=150000)
                    assert c1["terminal_event"] == "end", c1

                    # Case 2: Warm dynamic short reply (different numbers -> no disk cache hit!)
                    print("[cuda] case 2 warm short status", flush=True)
                    readings["battery"] = "电量 64%（已接电）。"
                    readings["volume"] = "系统音量为 47%，当前未静音。"
                    c2 = await run_prompt("warm_short_status", "电脑还有多少电，音量多大？", timeout_ms=60000)
                    assert c2["terminal_event"] == "end", c2

                    # Case 3: Warm multi-segment long reading
                    print("[cuda] case 3 warm long reading", flush=True)
                    c3 = await run_prompt("warm_long_reading", "请朗读以下文字：" + LONG_SAMPLE, timeout_ms=120000)
                    assert c3["terminal_event"] == "end", c3

                    report["resources_after"] = resources_snapshot()
                    report["passed"] = not report["page_errors"]
                finally:
                    await browser.close()
    return report


async def run_parity_suite(out_dir: Path, tag: str):
    """Test FUI core speech & dialogue parity (F38-F41) using synthetic audio and loopback model."""
    report = {
        "mode": "parity",
        "tag": tag,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "checks": {},
        "page_errors": [],
        "passed": False,
    }
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create isolated local media file under artist directory so local artist + title matches cleanly
    media_dir = out_dir / "music"
    artist_dir = media_dir / "刀郎"
    artist_dir.mkdir(parents=True, exist_ok=True)
    song_path = artist_dir / "试音测试曲.wav"
    song_path.write_bytes(make_tone_wav(seconds=8.0, hz=260, lead_silence_s=0.0, tail_silence_s=0.0))

    # Loopback streaming LLM server for F38 (mid-generation correction) & streaming pre-synth
    stream_opened = asyncio.Event()
    stream_cancelled = asyncio.Event()
    llm_calls = []

    async def llm_complete(request):
        body = await request.json()
        user_msg = body["messages"][-1]["content"]
        llm_calls.append({"user": user_msg})
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await stream.prepare(request)
        if "秋天" in user_msg:
            chunk = {"id": "f38", "object": "chat.completion.chunk", "created": 0, "model": "fui-qa-loopback",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "秋天日照缩短，树叶中的叶绿素"}, "finish_reason": None}]}
            await stream.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            stream_opened.set()
            try:
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                stream_cancelled.set()
                raise
        elif "两段" in user_msg:
            c1 = {"id": "p1", "object": "chat.completion.chunk", "created": 0, "model": "fui-qa-loopback",
                  "choices": [{"index": 0, "delta": {"role": "assistant", "content": "第一段说明在这里完成。\n\n"}, "finish_reason": None}]}
            await stream.write(f"data: {json.dumps(c1, ensure_ascii=False)}\n\n".encode("utf-8"))
            await asyncio.sleep(0.55)
            c2 = {"id": "p2", "object": "chat.completion.chunk", "created": 0, "model": "fui-qa-loopback",
                  "choices": [{"index": 0, "delta": {"content": "第二段补充说明也完成了。"}, "finish_reason": "stop"}]}
            await stream.write(f"data: {json.dumps(c2, ensure_ascii=False)}\n\n".encode("utf-8"))
            await stream.write(b"data: [DONE]\n\n")
        else:
            c = {"id": "ok", "object": "chat.completion.chunk", "created": 0, "model": "fui-qa-loopback",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "已收到。"}, "finish_reason": "stop"}]}
            await stream.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode("utf-8"))
            await stream.write(b"data: [DONE]\n\n")
        return stream

    llm_app = web.Application()
    llm_app.router.add_post("/v1/chat/completions", llm_complete)
    llm_runner = web.AppRunner(llm_app, access_log=None)
    await llm_runner.setup()
    llm_port = free_port()
    await web.TCPSite(llm_runner, "127.0.0.1", llm_port).start()

    # Fake MossSpeech returning synthetic WAVs with known leading/trailing silence & batch capability
    wav_reply = make_tone_wav(seconds=0.55, hz=440, lead_silence_s=0.09, tail_silence_s=0.28, pause_mid_s=0.14)
    wav_short_cue = make_tone_wav(seconds=0.50, hz=520, lead_silence_s=0.02, tail_silence_s=0.05)
    wav_long = make_tone_wav(seconds=2.7, hz=380, lead_silence_s=0.04, tail_silence_s=0.28)
    speak_log = []
    batch_fail_once = {"armed": False}

    class FakeSpeech:
        batch_capable = True
        def __init__(self):
            self.prepared_imports = 0
        async def prepare_imports(self):
            self.prepared_imports += 1
        async def prepare_microphone(self, *args, **kwargs):
            return False
        async def release_microphone(self, *args, **kwargs):
            return None
        async def render(self, text, disconnected, category="reply", cached_only=False, *, timings=None, room_amount=100):
            speak_log.append({"kind": "single", "text": text, "category": category, "cached_only": cached_only,
                              "room_amount": room_amount, "at": time.monotonic()})
            await asyncio.sleep(0.06)
            if disconnected():
                raise ConnectionAbortedError()
            if timings is not None:
                timings["tts"] = 60.0
            if category == "media" and cached_only:
                return None
            return wav_long if len(text) > 35 else wav_reply
        async def render_batch(self, texts, disconnected, category="reply", *, room_amount=100):
            speak_log.append({"kind": "batch", "texts": list(texts), "category": category, "room_amount": room_amount,
                              "at": time.monotonic()})
            if batch_fail_once["armed"]:
                batch_fail_once["armed"] = False
                raise RuntimeError("simulated batch unavailable")
            await asyncio.sleep(0.08)
            if disconnected():
                raise ConnectionAbortedError()
            return [wav_reply for _ in texts], "batch"
        async def close(self):
            return None

    fake_speech = FakeSpeech()

    async def slow_weather(*args, **kwargs):
        await asyncio.sleep(0.75)
        return (
            "北京明天（2026-11-23）晴，最低 2 °C，最高 11 °C，降水概率 10%。",
            {
                "source": "Open-Meteo", "city": "北京", "location_kind": "city",
                "latitude": 39.90, "longitude": 116.40, "date": "2026-11-23",
                "condition": "晴", "temperature_min": 2, "temperature_max": 11,
                "precipitation_probability": 10, "wind_speed": 8.0,
            },
        )

    CLOCK_STR = "2026年11月22日，星期日，19:45:00。"
    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"

    media_db = data_dir / "media.sqlite3"
    lib = MediaLibrary(media_db)
    lib.configure([{"id": "qa-local", "name": "测试曲库", "kind": "local", "path": str(media_dir)}])
    lib.scan()

    try:
        with patch("assistant_brain.fetch_weather", side_effect=slow_weather), \
             patch("local_overview.local_clock_reply", return_value=CLOCK_STR), \
             patch("assistant_brain.local_clock_reply", return_value=CLOCK_STR):
            async with isolated_fui_server(
                data_dir, speech=fake_speech, model_url=f"http://127.0.0.1:{llm_port}/v1", media_db=media_db
            ) as (base, moss):

                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=str(edge), headless=True,
                        args=["--mute-audio", "--disable-gpu", "--disable-webgl", "--disable-webgl2",
                              "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
                    )
                    try:
                        await confirm_muted_edge(browser)
                        context = await browser.new_context(viewport={"width": 1440, "height": 900},
                                                            reduced_motion="reduce", service_workers="block")
                        await context.add_init_script(FUI_SPEECH_PROBE)
                        async def route_guard(route):
                            url = route.request.url
                            if url.startswith(("http://", "https://")) and not url.startswith(base + "/"):
                                await route.abort()
                            else:
                                await route.continue_()
                        await context.route("**/*", route_guard)
                        page = await context.new_page()
                        page.set_default_timeout(12000)
                        page.on("pageerror", lambda err: report["page_errors"].append(str(err)))
                        print("[parity] goto", base, flush=True)
                        await page.goto(base + "/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                        await expect(page.locator("body")).to_have_attribute("data-backend", "live", timeout=10000)
                        await page.bring_to_front()
                        print("[parity] live connected", flush=True)

                        composer = page.locator("#comms-input")
                        send_btn = page.locator("#comms-send")

                        async def wait_speech_end(timeout_ms=10000):
                            try:
                                await page.wait_for_function(
                                    "() => window.fuiSpeechQA.events.some(e => e.type === 'end' || e.type === 'error')",
                                    timeout=timeout_ms,
                                )
                                return True
                            except Exception:
                                return False

                        # --- Check 1 (F40a): Explicit narration [pause 1.2s] ---
                        print("[parity] check 1 narration pause", flush=True)
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill("朗读以下内容：第一句。[pause 1.2s]第二句。")
                        await send_btn.click()
                        finished1 = await wait_speech_end(10000)
                        t_narration = await page.evaluate("window.collectFuiSpeechMetrics()")
                        s_narration = summarize_trace(t_narration)
                        pause_ok = (finished1 and s_narration["clip_count"] == 2
                                    and len(s_narration["schedule_gaps_ms"]) == 1
                                    and s_narration["schedule_gaps_ms"][0] >= 1000)
                        report["checks"]["f40_narration_pause"] = {
                            "passed": pause_ok,
                            "finished": finished1,
                            "clip_count": s_narration["clip_count"],
                            "schedule_gaps_ms": s_narration["schedule_gaps_ms"],
                            "speak_requests": s_narration["speak_requests"],
                        }

                        # --- Check 2 (F40b): Multi-segment speechSegments + /api/speak-batch + quietEdges ---
                        print("[parity] check 2 segments & batch", flush=True)
                        await page.evaluate("window.resetFuiSpeechQA()")
                        long_text = (
                            "朗读以下内容："
                            "第一段足够长用来触发批量预取，让我们确保这一段播放超过两秒半以上，系统就会成组准备后面的两段内容。\n\n"
                            "第二段是紧随其后的短句，用来核对成组预取。\n\n"
                            "第三段也是紧随其后的短句，用来核对段间衔接。"
                        )
                        await composer.fill(long_text)
                        await send_btn.click()
                        finished2 = await wait_speech_end(14000)
                        t_batch = await page.evaluate("window.collectFuiSpeechMetrics()")
                        s_batch = summarize_trace(t_batch)
                        used_batch = any(r["path"] == "/api/speak-batch" for r in s_batch["speak_requests"])
                        report["checks"]["f40_segments_batch_trim"] = {
                            "passed": bool(finished2 and s_batch["clip_count"] >= 3 and used_batch),
                            "finished": finished2,
                            "clip_count": s_batch["clip_count"],
                            "used_batch": used_batch,
                            "first_offset_ms": s_batch["first_offset_ms"],
                            "audible_gaps_ms": s_batch["audible_gaps_ms"],
                            "schedule_gaps_ms": s_batch["schedule_gaps_ms"],
                            "speak_requests": s_batch["speak_requests"],
                        }

                        # --- Check 3 (F40c): Compound local_overview_speech_lead early pre-synth ---
                        print("[parity] check 3 local weather lead", flush=True)
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill("现在几点，明天北京天气怎么样？")
                        await send_btn.click()
                        finished3 = await wait_speech_end(12000)
                        t_lead = await page.evaluate("window.collectFuiSpeechMetrics()")
                        s_lead = summarize_trace(t_lead)
                        first_req_after = s_lead["speak_requests"][0]["req_after_submit_ms"] if s_lead["speak_requests"] else None
                        # Weather lookup sleeps 750ms; if lead pre-synth works, first /api/speak fires well before 750ms!
                        lead_early = first_req_after is not None and first_req_after < 550
                        report["checks"]["f40_local_weather_lead"] = {
                            "passed": bool(finished3 and lead_early and s_lead["clip_count"] >= 2),
                            "finished": finished3,
                            "first_req_after_submit_ms": first_req_after,
                            "first_offset_ms": s_lead["first_offset_ms"],
                            "clip_count": s_lead["clip_count"],
                            "speak_requests": s_lead["speak_requests"],
                        }

                        # --- Check 4 (F41): Music prefer_cached + ducking + background warm ---
                        print("[parity] check 4 music cached + duck", flush=True)
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await page.evaluate("""
                          window.duckLog = [];
                          const observer = new MutationObserver(() => {
                            for (const el of document.querySelectorAll('audio, video')) {
                              if (el.dataset.duckHooked) continue;
                              el.dataset.duckHooked = '1';
                              el.addEventListener('volumechange', () => {
                                window.duckLog.push({volume: el.volume, at: performance.now(), paused: el.paused});
                              });
                            }
                          });
                          observer.observe(document.body, {subtree: true, childList: true});
                        """)
                        await composer.fill("播放刀郎的试音测试曲")
                        await send_btn.click()
                        finished4 = await wait_speech_end(10000)
                        await asyncio.sleep(0.35)
                        t_music = await page.evaluate("window.collectFuiSpeechMetrics()")
                        s_music = summarize_trace(t_music)
                        duck_log = await page.evaluate("window.duckLog")
                        media_vol = await page.evaluate("(() => { const a = document.querySelector('audio'); return a ? {paused:a.paused, volume:a.volume, t:a.currentTime} : null; })()")
                        used_prefer_cached = any(r.get("prefer_cached") is True for r in s_music["speak_requests"])
                        warmed_full = any(r.get("prefer_cached") is False and r.get("category") == "media" for r in s_music["speak_requests"])
                        ducked = any(entry["volume"] <= 0.2 for entry in duck_log) and (media_vol and media_vol["volume"] >= 0.95)
                        report["checks"]["f41_music_cached_duck"] = {
                            "passed": bool(finished4 and used_prefer_cached and warmed_full and ducked),
                            "finished": finished4,
                            "used_prefer_cached": used_prefer_cached,
                            "warmed_full": warmed_full,
                            "ducked_and_restored": ducked,
                            "duck_log": duck_log,
                            "media_vol": media_vol,
                            "speak_requests": s_music["speak_requests"],
                        }

                        # --- Check 5 (F38): Mid-generation correction (typed & spoken replyTurn) ---
                        print("[parity] check 5 midgen correction", flush=True)
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill("请解释为什么秋天树叶会变黄。")
                        await send_btn.click()
                        await asyncio.wait_for(stream_opened.wait(), timeout=8)
                        await page.wait_for_function(
                            "() => [...document.querySelectorAll('#comms-log li[data-from=assistant]')].some(el => el.textContent.includes('叶绿素'))",
                            timeout=8000,
                        )
                        # Test typed replacement
                        await composer.fill("先停止回答，改问现在几点")
                        await send_btn.click()
                        await asyncio.sleep(0.8)
                        turns_after_f38 = moss.state()["turns"]
                        last_turn_f38 = turns_after_f38[-1]
                        prev_turn_f38 = turns_after_f38[-2] if len(turns_after_f38) >= 2 else None
                        f38_reqs = await page.evaluate("window.fuiSpeechQA.requests")
                        scoped_cancel_sent = any(
                            r["path"] == "/api/cancel" and r.get("body", {}).get("conversation_only") is True
                            for r in f38_reqs
                        )
                        f38_ok = (
                            scoped_cancel_sent
                            and prev_turn_f38 is not None and prev_turn_f38["status"] == "cancelled"
                            and last_turn_f38["user"] == "现在几点" and last_turn_f38["status"] == "replied"
                            and "19:45:00" in (last_turn_f38.get("reply") or "")
                        )
                        report["checks"]["f38_midgen_correction"] = {
                            "passed": f38_ok,
                            "scoped_cancel_sent": scoped_cancel_sent,
                            "prev_status": prev_turn_f38["status"] if prev_turn_f38 else None,
                            "last_user": last_turn_f38.get("user"),
                            "last_status": last_turn_f38.get("status"),
                            "last_reply": last_turn_f38.get("reply"),
                            "requests": [{"path": r["path"], "body": r.get("body"), "status": r.get("status")} for r in f38_reqs],
                        }
                        # Ensure any active turn from failed F38 baseline is cancelled before check 6
                        if moss.state()["turns"][-1]["status"] == "thinking":
                            await moss.cancel(moss.state()["turns"][-1]["id"])
                            await asyncio.sleep(0.2)

                        # --- Check 6 (F39): Typed natural stop "先停一下" during thinking ---
                        print("[parity] check 6 natural stop", flush=True)
                        stream_opened.clear()
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill("再解释一次秋天树叶为什么变黄。")
                        await send_btn.click()
                        await asyncio.wait_for(stream_opened.wait(), timeout=8)
                        await composer.fill("先停一下")
                        await send_btn.click()
                        await asyncio.sleep(0.6)
                        last_turn_f39 = moss.state()["turns"][-1]
                        f39_reqs = await page.evaluate("window.fuiSpeechQA.requests")
                        f39_ok = last_turn_f39["status"] == "cancelled" and any(r["path"] == "/api/cancel" for r in f39_reqs)
                        report["checks"]["f39_natural_stop_thinking"] = {
                            "passed": f39_ok,
                            "last_status": last_turn_f39["status"],
                            "requests": [{"path": r["path"], "body": r.get("body"), "status": r.get("status")} for r in f39_reqs],
                        }
                        if moss.state()["turns"][-1]["status"] == "thinking":
                            await moss.cancel(moss.state()["turns"][-1]["id"])
                            await asyncio.sleep(0.2)

                        # --- Check 7: Incremental DOM rendering check ---
                        print("[parity] check 7 incremental DOM", flush=True)
                        before_node_same = await page.evaluate("""
                          (() => {
                            const firstLi = document.querySelector('#comms-log li[data-from=self]');
                            window.__firstTurnNodeRef = firstLi;
                            return Boolean(firstLi);
                          })()
                        """)
                        await composer.fill("现在几点")
                        await send_btn.click()
                        await asyncio.sleep(0.5)
                        dom_preserved = await page.evaluate(
                            "() => Boolean(window.__firstTurnNodeRef && document.querySelector('#comms-log li[data-from=self]') === window.__firstTurnNodeRef)"
                        )
                        report["checks"]["incremental_dom_render"] = {
                            "passed": bool(before_node_same and dom_preserved),
                            "first_node_preserved": dom_preserved,
                        }

                        # --- Check 8: Streaming first-paragraph pre-synthesis ---
                        print("[parity] check 8 streaming first-paragraph pre-synth", flush=True)
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill("请给我写两段说明。")
                        await send_btn.click()
                        finished8 = await wait_speech_end(12000)
                        t_stream = await page.evaluate("window.collectFuiSpeechMetrics()")
                        s_stream = summarize_trace(t_stream)
                        stream_first_req = s_stream["speak_requests"][0]["req_after_submit_ms"] if s_stream["speak_requests"] else None
                        # Stream sleeps 550ms between paragraph 1 and paragraph 2; pre-synth fires well before 550ms!
                        report["checks"]["f40_stream_paragraph_lead"] = {
                            "passed": bool(finished8 and stream_first_req is not None and stream_first_req < 450 and s_stream["clip_count"] >= 2),
                            "finished": finished8,
                            "first_req_after_submit_ms": stream_first_req,
                            "first_offset_ms": s_stream["first_offset_ms"],
                            "clip_count": s_stream["clip_count"],
                            "speak_requests": s_stream["speak_requests"],
                        }

                        # --- Check 9: Independent "停止播报" does NOT cancel an active thinking turn ---
                        print("[parity] check 9 independent stop-speech", flush=True)
                        stream_opened.clear()
                        await page.evaluate("window.resetFuiSpeechQA()")
                        await composer.fill("最后解释一次秋天树叶为什么变黄。")
                        await send_btn.click()
                        await asyncio.wait_for(stream_opened.wait(), timeout=8)
                        await composer.fill("停止播报")
                        await send_btn.click()
                        await asyncio.sleep(0.35)
                        still_thinking = moss.state()["turns"][-1]["status"] == "thinking"
                        c9_reqs = await page.evaluate("window.fuiSpeechQA.requests")
                        no_cancel_sent = not any(r["path"] == "/api/cancel" for r in c9_reqs)
                        report["checks"]["f39_independent_stop_speech"] = {
                            "passed": bool(still_thinking and no_cancel_sent),
                            "still_thinking": still_thinking,
                            "no_cancel_sent": no_cancel_sent,
                        }
                        if moss.state()["turns"][-1]["status"] == "thinking":
                            await moss.cancel(moss.state()["turns"][-1]["id"])
                            await asyncio.sleep(0.2)

                        report["passed"] = (
                            not report["page_errors"]
                            and all(c["passed"] for c in report["checks"].values())
                        )
                    finally:
                        await browser.close()
    finally:
        await llm_runner.cleanup()
    return report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("cuda", "parity"), required=True)
    parser.add_argument("--tag", default="run")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = ROOT / "runs" / f"fui-speech-{args.mode}-{args.tag}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = await (run_cuda_benchmark(out_dir, args.tag) if args.mode == "cuda" else run_parity_suite(out_dir, args.tag))
    result_path = out_dir / "result.json"
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result_path, "passed=", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
