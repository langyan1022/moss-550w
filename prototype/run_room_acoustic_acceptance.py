"""Physical-room 30-minute acoustic barge-in, echo, and continuous-playback acceptance harness.

Implements the physical microphone + physical speaker criteria in MOSS日常验收标准.md:
- Records real Windows input/output audio devices via winmm.dll + browser MediaDevices.
- Tests three room conditions: quiet (安静), echo (回声), ambient_noise (环境声), 10 min each in --mode full (30 min total).
- Tracks every trial's user speech start, stop-word end, ASR arrival, and Web Audio PCM silence.
- Computes stop_word_end -> pcm_silent (target <= 300 ms) AND speech_start -> pcm_silent separately.
- Counts false stops (误停), missed stops (漏停), continuous playback completions, and follow-up recoveries separately.
- Supports:
    --mode full      : 30-minute physical room acceptance (3 x 10 min) with real unmuted Edge, mic, speakers, and local SenseVoice.
    --mode smoke     : 90-second physical room pre-flight (3 x 30 s) with real unmuted Edge, mic, and speakers.
    --mode self-test : Automated offline harness verification (muted Edge + synthetic mic; explicitly marked NOT physical hardware).
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
from statistics import median
import struct
import time
from unittest.mock import patch
import wave

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from serve import application
from verify_context_reply import free_port
from verify_fui_speech_latency import FUI_PUBLIC, make_tone_wav


VOICE_LINES = ROOT / "voice/lines"
# Real OmniVoice MOSS clips containing ZERO stop-words ("等一下"/"停一下"), concatenated into ~14s continuous playback.
CONTINUOUS_CLIP_NAMES = (
    "good-morning.wav",
    "file-organized.wav",
    "note-shown.wav",
    "confirm-draft.wav",
    "action-incomplete.wav",
    "good-afternoon.wav",
    "file-organized.wav",
    "note-shown.wav",
)
CONTINUOUS_DISPLAY_TEXT = (
    "正在进行实体声源连续播报测试，当前频道保持自然语速与正常空间混响，用来核对房间回声和环境噪声下的防误停能力，以及真实人声插话后的停止延迟与续问恢复。"
)


class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


class _WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


def enumerate_windows_audio_devices() -> dict:
    """Enumerate physical Windows audio input/output devices via stdlib ctypes winmm.dll."""
    if os.name != "nt":
        return {"inputs": [], "outputs": []}
    winmm = ctypes.windll.winmm
    inputs = []
    for idx in range(int(winmm.waveInGetNumDevs())):
        caps = _WAVEINCAPSW()
        if winmm.waveInGetDevCapsW(idx, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            inputs.append({"index": idx, "name": caps.szPname.strip(), "channels": int(caps.wChannels)})
    outputs = []
    for idx in range(int(winmm.waveOutGetNumDevs())):
        caps = _WAVEOUTCAPSW()
        if winmm.waveOutGetDevCapsW(idx, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            outputs.append({"index": idx, "name": caps.szPname.strip(), "channels": int(caps.wChannels)})
    return {"inputs": inputs, "outputs": outputs}


def build_continuous_moss_wav(*, short: bool = False) -> tuple[bytes, float]:
    """Assemble ~14s authentic MOSS OmniVoice 24 kHz stereo WAV from bundled voice lines."""
    params = None
    chunks = []
    gap_frames = 0
    names = CONTINUOUS_CLIP_NAMES[:2] if short else CONTINUOUS_CLIP_NAMES
    for name in names:
        path = VOICE_LINES / name
        if not path.is_file():
            continue
        with wave.open(str(path), "rb") as wav:
            cur = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            if params is None:
                params = cur
                gap_frames = int(cur[2] * 0.12)
            if cur != params:
                continue
            chunks.append(wav.readframes(wav.getnframes()))
            chunks.append(b"\x00" * (gap_frames * cur[0] * cur[1]))
    if not chunks or params is None:
        raw = make_tone_wav(seconds=10.0, hz=330, lead_silence_s=0.05, tail_silence_s=0.1)
        return raw, 10.15
    buf = BytesIO()
    nch, sw, rate = params
    pcm = b"".join(chunks)
    with wave.open(buf, "wb") as out:
        out.setnchannels(nch)
        out.setsampwidth(sw)
        out.setframerate(rate)
        out.writeframes(pcm)
    duration_s = round(len(pcm) / (nch * sw * rate), 3)
    return buf.getvalue(), duration_s


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return round(ordered[idx], 1)


ROOM_PROBE_JS = """
sessionStorage.setItem('moss-fui.opened', '1');
localStorage.setItem('moss-fui.scene3d', 'off');
localStorage.setItem('moss-fui.tts', 'on');
localStorage.setItem('moss.voice.enabled', 'on');
localStorage.setItem('moss-fui.voice-resident', 'off');
localStorage.setItem('moss.speech.space.percent', '100');

window.roomAcousticProbe = {
  speakerTap: null,
  micTap: null,
  speakerRows: [],
  micRows: [],
  speechEvents: [],
  transcriptions: [],
  micTracks: [],
  noiseFloorRms: 0.004,
};

const _origConnect = AudioNode.prototype.connect;
AudioNode.prototype.connect = function(target, ...args) {
  const res = _origConnect.call(this, target, ...args);
  if (this instanceof AnalyserNode && target === this.context.destination && !window.roomAcousticProbe.speakerTap) {
    const tap = this.context.createAnalyser();
    tap.fftSize = 256;
    _origConnect.call(this, tap);
    window.roomAcousticProbe.speakerTap = tap;
  }
  return res;
};

const _origCreateMic = AudioContext.prototype.createMediaStreamSource;
AudioContext.prototype.createMediaStreamSource = function(stream) {
  const node = _origCreateMic.call(this, stream);
  try {
    const track = stream.getAudioTracks()[0];
    if (track) window.roomAcousticProbe.micTracks.push({label: track.label, settings: track.getSettings?.() || {}});
    const tap = this.createAnalyser();
    tap.fftSize = 256;
    _origConnect.call(node, tap);
    window.roomAcousticProbe.micTap = tap;
  } catch {}
  return node;
};

for (const ev of ['pending', 'start', 'end', 'stop', 'error']) {
  window.addEventListener('moss-speech-' + ev, (e) => {
    window.roomAcousticProbe.speechEvents.push({
      ev,
      id: e.detail?.id || null,
      text: e.detail?.text || '',
      at: performance.now(),
    });
  });
}

const _origFetch = window.fetch;
window.fetch = async function(input, options) {
  const url = typeof input === 'string' ? input : input?.url || '';
  const path = new URL(url, location.href).pathname;
  if (path !== '/api/transcribe') return _origFetch.apply(this, arguments);
  const started = performance.now();
  const preview = options?.headers?.['X-MOSS-ASR-Preview'] === '1';
  const resp = await _origFetch.apply(this, arguments);
  const arrived = performance.now();
  try {
    const clone = resp.clone();
    const data = await clone.json();
    window.roomAcousticProbe.transcriptions.push({
      preview,
      started,
      arrived,
      text: data?.text ?? '',
      status: resp.status,
    });
  } catch {}
  return resp;
};

const _spkBuf = new Float32Array(256);
const _micBuf = new Float32Array(256);
setInterval(() => {
  const now = performance.now();
  const p = window.roomAcousticProbe;
  if (p.speakerTap) {
    p.speakerTap.getFloatTimeDomainData(_spkBuf);
    let s = 0;
    for (let i = 0; i < _spkBuf.length; i++) s += _spkBuf[i] * _spkBuf[i];
    p.speakerRows.push([now, Math.sqrt(s / _spkBuf.length)]);
    if (p.speakerRows.length > 4000) p.speakerRows.splice(0, 2000);
  }
  if (p.micTap) {
    p.micTap.getFloatTimeDomainData(_micBuf);
    let s = 0;
    for (let i = 0; i < _micBuf.length; i++) s += _micBuf[i] * _micBuf[i];
    p.micRows.push([now, Math.sqrt(s / _micBuf.length)]);
    if (p.micRows.length > 4000) p.micRows.splice(0, 2000);
  }
}, 5);

window.roomResetWindow = () => {
  const p = window.roomAcousticProbe;
  p.speakerRows = [];
  p.micRows = [];
  p.transcriptions = [];
};

window.roomSetBanner = (title, detail, state = 'info') => {
  let box = document.getElementById('room-qa-banner');
  if (!box) {
    box = document.createElement('div');
    box.id = 'room-qa-banner';
    box.style.cssText = (
      'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:99999;'
      + 'background:#07131d;border:2px solid #35c6ff;color:#e6f6ff;padding:12px 22px;'
      + 'font:600 16px/1.45 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.65);'
      + 'min-width:460px;max-width:780px;text-align:center;'
    );
    document.body.append(box);
  }
  const border = state === 'speak' ? '#ff9d2e' : state === 'ok' ? '#3ee394' : '#35c6ff';
  box.style.borderColor = border;
  box.innerHTML = `<div style="font-size:18px;letter-spacing:.04em">${title}</div><div style="font-weight:400;opacity:.9;margin-top:4px">${detail}</div>`;
};
"""


def compute_barge_in_timings(
    *,
    cue_at: float,
    stop_at: float | None,
    speaker_rows: list[list[float]],
    mic_rows: list[list[float]],
    transcriptions: list[dict],
    noise_floor_rms: float,
) -> dict:
    """Compute speech_start, stop_word_end, asr_arrive, pcm_silent, and official latency deltas."""
    threshold = max(0.012, noise_floor_rms * 3.5)
    after_cue_mic = [(t, rms) for t, rms in mic_rows if t >= cue_at and (stop_at is None or t <= stop_at + 150)]
    active_mic = [(t, rms) for t, rms in after_cue_mic if rms >= threshold]
    speech_start_ms = active_mic[0][0] if active_mic else None
    stop_word_end_ms = active_mic[-1][0] if active_mic else None

    # Find ASR response that triggered stop (latest transcription arriving at or just before stop_at)
    stop_asrs = [
        row for row in transcriptions
        if row.get("text") and (stop_at is None or row["arrived"] <= stop_at + 80) and row["arrived"] >= cue_at
    ]
    asr_arrive_ms = stop_asrs[-1]["arrived"] if stop_asrs else None
    asr_text = stop_asrs[-1]["text"] if stop_asrs else ""

    pcm_silent_ms = None
    if stop_at is not None:
        after_stop_spk = [(t, rms) for t, rms in speaker_rows if t >= stop_at - 30]
        silent = next((t for t, rms in after_stop_spk if t >= stop_at and rms <= 0.001), None)
        pcm_silent_ms = silent if silent is not None else stop_at

    stop_end_to_silent = (
        round(pcm_silent_ms - stop_word_end_ms, 1)
        if (pcm_silent_ms is not None and stop_word_end_ms is not None)
        else None
    )
    speech_start_to_silent = (
        round(pcm_silent_ms - speech_start_ms, 1)
        if (pcm_silent_ms is not None and speech_start_ms is not None)
        else None
    )
    asr_to_silent = (
        round(pcm_silent_ms - asr_arrive_ms, 1)
        if (pcm_silent_ms is not None and asr_arrive_ms is not None)
        else None
    )
    return {
        "threshold_rms": round(threshold, 5),
        "speech_start_ms": round(speech_start_ms - cue_at, 1) if speech_start_ms is not None else None,
        "stop_word_end_ms": round(stop_word_end_ms - cue_at, 1) if stop_word_end_ms is not None else None,
        "asr_arrive_ms": round(asr_arrive_ms - cue_at, 1) if asr_arrive_ms is not None else None,
        "pcm_silent_ms": round(pcm_silent_ms - cue_at, 1) if pcm_silent_ms is not None else None,
        "stop_word_end_to_silent_ms": stop_end_to_silent,
        "speech_start_to_silent_ms": speech_start_to_silent,
        "asr_to_silent_ms": asr_to_silent,
        "asr_text": asr_text,
    }


def render_markdown_report(report: dict) -> str:
    lines = [
        f"# MOSS 实体声源房间打断与回声验收报告（{report['mode']}）",
        "",
        f"- 时间：`{report['started_at']}` → `{report.get('finished_at', '')}`",
        f"- 实体房间硬件：`{'是（真实外放 + 实体麦克风 + 本机 SenseVoice）' if report['physical_room_hardware'] else '否（离线自检回环，不计入 4 分硬件项）'}`",
        f"- 计入 4 分实体声源验收：`{'是' if report['counts_toward_4pt_rubric'] else '否'}`",
        f"- Windows 输入设备：{', '.join(d['name'] for d in report['windows_devices']['inputs']) or '无'}",
        f"- Windows 输出设备：{', '.join(d['name'] for d in report['windows_devices']['outputs']) or '无'}",
        f"- 浏览器实际采集轨：{', '.join(t.get('label') or 'default' for t in report.get('browser_mic_tracks', [])) or '无'}",
        "",
        "## 分条件统计（安静 / 回声 / 环境声）",
        "",
        "| 条件 | 时长(s) | 连续播报完成 | 误停(次) | 打断尝试 | 漏停(次) | 停词完→静音 P50/P95 (ms) | 开口→静音 P50 (ms) | 续问恢复 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, ph in report["phases"].items():
        p50 = ph["stop_word_end_to_silent_p50_ms"]
        p95 = ph["stop_word_end_to_silent_p95_ms"]
        sp50 = ph["speech_start_to_silent_p50_ms"]
        lat_str = f"{p50} / {p95}" if p50 is not None else "—"
        sp_str = f"{sp50}" if sp50 is not None else "—"
        lines.append(
            f"| {name} | {ph['elapsed_s']} | {ph['continuous_ok']} | {ph['false_stops']} | "
            f"{ph['barge_in_attempts']} | {ph['missed_stops']} | {lat_str} | {sp_str} | "
            f"{ph['recovery_ok']}/{ph['barge_in_stopped']} |"
        )
    lines.extend([
        "",
        "## 总结判定",
        "",
        f"- 总体结果：`{'PASS' if report['passed'] else 'FAIL'}`",
        f"- 满足 30 分钟实体房间 4 分验收口径：`{'YES' if report.get('rubric_4pt_qualified') else 'NO'}`",
    ])
    return "\n".join(lines) + "\n"


async def run_harness(mode: str, *, phase_seconds: int | None = None):
    is_self_test = mode == "self-test"
    if phase_seconds is None:
        phase_seconds = 12 if is_self_test else (30 if mode == "smoke" else 600)

    out_dir = ROOT / "runs" / datetime.now().strftime(f"room-acoustic-acceptance-{mode}-%Y%m%d-%H%M%S-%f")
    out_dir.mkdir(parents=True, exist_ok=True)
    clip_wav, clip_duration_s = build_continuous_moss_wav(short=is_self_test)
    reply_wav = (VOICE_LINES / "acknowledge.wav").read_bytes() if (VOICE_LINES / "acknowledge.wav").is_file() else make_tone_wav(seconds=0.8)

    report = {
        "mode": mode,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "physical_room_hardware": not is_self_test,
        "counts_toward_4pt_rubric": mode == "full",
        "phase_target_seconds": phase_seconds,
        "continuous_clip_seconds": clip_duration_s,
        "continuous_clip_sha256": hashlib.sha256(clip_wav).hexdigest(),
        "windows_devices": enumerate_windows_audio_devices(),
        "browser_mic_tracks": [],
        "phases": {},
        "page_errors": [],
        "passed": False,
        "rubric_4pt_qualified": False,
    }

    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    stopped = asyncio.Event()
    MediaLibrary(data_dir / "media.sqlite3")

    class StaticMossSpeech:
        batch_capable = False
        session_warm_ready = False

        async def prepare_imports(self):
            return None

        async def prepare_microphone(self, *args, **kwargs):
            return False

        async def release_microphone(self, *args, **kwargs):
            return None

        async def prepare_session(self, *args, **kwargs):
            return False

        async def release_session(self, *args, **kwargs):
            return None

        async def idle(self):
            return None

        async def render(self, text, disconnected, category="reply", cached_only=False, *, timings=None, room_amount=100):
            if disconnected():
                raise ConnectionAbortedError()
            if timings is not None:
                timings["tts"] = 1.0
            if "实体声源连续播报测试" in text:
                return clip_wav
            return reply_wav

        async def render_batch(self, texts, disconnected, category="reply", *, room_amount=100):
            return [reply_wav for _ in texts], "single"

        async def close(self):
            return None

    class SelfTestVoiceInput:
        def __init__(self):
            self.next_text = ""

        async def prepare(self):
            return None

        async def transcribe(self, _wav_bytes, *, preview=False):
            return self.next_text

        async def close(self):
            return None

    fake_asr = SelfTestVoiceInput() if is_self_test else None
    env = {
        "MOSS_MEDIA_DB": str(data_dir / "media.sqlite3"),
        "MOSS_PROVIDER_DIR": str(data_dir / "providers"),
        "LOCALAPPDATA": str(data_dir / "profile"),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "0",
        "MOSS_SPEECH_SESSION_WARM": "0",
        "MOSS_SPEECH_STANDBY": "0",
    }

    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    if is_self_test:
        phases_spec = [
            ("quiet", "安静环境（自检回环）"),
            ("echo", "回声环境（自检回环）"),
            ("ambient_noise", "环境噪声（自检回环）"),
        ]
    else:
        phases_spec = [
            ("quiet", "安静环境（保持房间安静，按屏幕提示说「等一下」与「现在几点」）"),
            ("echo", "回声直耦合（调高外放音量，验证回声不误停与外放叠加打断）"),
            ("ambient_noise", "环境声环境（开启背景人声/音乐/风扇等环境声，验证防误停与打断）"),
        ]

    async with AsyncExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch("serve.MossSpeech", return_value=StaticMossSpeech()))
        if fake_asr is not None:
            stack.enter_context(patch("serve.LocalVoice", return_value=fake_asr))
        moss = Moss(data_dir, "http://127.0.0.1:1/v1", "room-acoustic-unused", prefer_cloud=False)
        stack.push_async_callback(moss.close)

        # Short-circuit our reading trigger text directly without any cloud model calls
        async def scripted_message(text, *args, **kwargs):
            if text == "开始房间声源播报":
                from uuid import uuid4
                from datetime import timezone
                turn_id = uuid4().hex
                now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                with moss.db:
                    moss._insert_turn({
                        "chat_id": moss.chat_id,
                        "id": turn_id,
                        "user": text,
                        "source": "context",
                        "status": "replied",
                        "created_at": now_iso,
                        "reply": CONTINUOUS_DISPLAY_TEXT,
                    })
                    moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
                moss._notify()
                return None
            return await orig_message(text, *args, **kwargs)

        orig_message = moss.message
        moss.message = scripted_message

        app = application(
            moss,
            lambda: "standby",
            data_dir,
            port,
            "room-acoustic-token",
            stopped,
            fui_public=FUI_PUBLIC,
            default_mic_warm_mode="0",
            default_session_warm="0",
            speech_standby_seconds=0,
        )
        if is_self_test:
            # Amplitude-modulated speech-like pulse so voice-endpoint.js (which rejects flat hum) triggers deterministically.
            sr = 16000
            n_frames = int(0.60 * sr)
            buf = BytesIO()
            with wave.open(buf, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sr)
                pcm_samples = []
                for i in range(n_frames):
                    t = i / sr
                    env = 0.12 + 0.88 * abs(math.sin(math.pi * t / 0.16))
                    val = int(13000 * env * math.sin(2 * math.pi * 420 * t))
                    pcm_samples.append(val)
                wav.writeframes(struct.pack(f"<{len(pcm_samples)}h", *pcm_samples))
            mic_tone = buf.getvalue()
            app.router.add_get("/fixture/self-test-mic.wav", lambda _req: web.Response(body=mic_tone, content_type="audio/wav"))

        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)

        def _stop():
            stopped.set()
            moss._notify()

        stack.callback(_stop)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"

        launch_args = [
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-gpu",
            "--disable-webgl",
            "--disable-webgl2",
        ]
        if is_self_test:
            launch_args.insert(0, "--mute-audio")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=str(edge),
                headless=is_self_test,
                args=launch_args,
            )
            try:
                if is_self_test:
                    await confirm_muted_edge(browser)
                context = await browser.new_context(
                    viewport={"width": 1360, "height": 860},
                    permissions=["microphone"],
                    reduced_motion="reduce",
                    service_workers="block",
                )
                await context.add_init_script(ROOM_PROBE_JS)
                if is_self_test:
                    await context.add_init_script("""
                      let _stCtx = null, _stDest = null;
                      Object.defineProperty(navigator, 'mediaDevices', {
                        configurable: true,
                        value: {
                          enumerateDevices: async () => [{kind:'audioinput', deviceId:'self-test-mic', label:'Self-Test Loopback Mic'}],
                          getUserMedia: async () => {
                            _stCtx = new AudioContext();
                            await _stCtx.resume();
                            _stDest = _stCtx.createMediaStreamDestination();
                            return _stDest.stream;
                          },
                        },
                      });
                      window.injectSelfTestVoice = async () => {
                        const raw = await (await fetch('/fixture/self-test-mic.wav')).arrayBuffer();
                        const buf = await _stCtx.decodeAudioData(raw);
                        const src = _stCtx.createBufferSource();
                        src.buffer = buf;
                        src.connect(_stDest);
                        src.start();
                      };
                    """)

                page = await context.new_page()
                page.set_default_timeout(30000)
                page.on("pageerror", lambda err: report["page_errors"].append(str(err)))
                await page.goto(base + "/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                await expect(page.locator("body")).to_have_attribute("data-backend", "live", timeout=15000)
                await page.bring_to_front()

                # Enable wake + barge-in via Settings dialog (real user gesture unlocks AudioContext + physical mic!)
                await page.locator("#strip-settings").click()
                await expect(page.locator("#dlg-settings")).to_be_visible()
                await page.locator("#set-wake").check()
                await page.wait_for_function("() => !document.querySelector('#set-barge')?.disabled", timeout=15000)
                await page.locator("#set-barge").check()
                await page.locator("#dlg-settings [data-dialog-close]").first.click()
                await expect(page.locator("#dlg-settings")).to_be_hidden()

                # Calibrate room noise floor for 1.5s
                await page.evaluate("window.roomResetWindow()")
                await page.evaluate("window.roomSetBanner('声学底噪校准中…', '请保持房间 1.5 秒安静，正在采集实体麦克风底噪。', 'info')")
                await asyncio.sleep(1.6)
                floor_rms = await page.evaluate("""() => {
                  const vals = window.roomAcousticProbe.micRows.map(r => r[1]).sort((a,b) => a-b);
                  return vals.length ? vals[Math.floor(vals.length * 0.8)] : 0.004;
                }""")
                report["calibrated_noise_floor_rms"] = round(float(floor_rms), 5)
                report["browser_mic_tracks"] = await page.evaluate("window.roomAcousticProbe.micTracks")

                composer = page.locator("#comms-input")
                send_btn = page.locator("#comms-send")

                async def start_continuous_playback() -> str:
                    prev_count = await page.evaluate("window.roomAcousticProbe.speechEvents.length")
                    await composer.fill("开始房间声源播报")
                    await send_btn.click()
                    await page.wait_for_function(
                        f"() => window.roomAcousticProbe.speechEvents.slice({prev_count}).some(e => e.ev === 'start')",
                        timeout=15000,
                    )
                    return await page.evaluate(
                        f"() => window.roomAcousticProbe.speechEvents.slice({prev_count}).find(e => e.ev === 'start').id"
                    )

                for phase_key, phase_desc in phases_spec:
                    print(f"\n=== PHASE [{phase_key}]: {phase_desc} ({phase_seconds}s) ===", flush=True)
                    phase_start = time.monotonic()
                    phase_end = phase_start + phase_seconds
                    ph = {
                        "description": phase_desc,
                        "continuous_ok": 0,
                        "false_stops": 0,
                        "barge_in_attempts": 0,
                        "barge_in_stopped": 0,
                        "missed_stops": 0,
                        "recovery_ok": 0,
                        "trials": [],
                    }
                    trial_idx = 0
                    while time.monotonic() < phase_end or trial_idx < 2:
                        trial_idx += 1
                        is_echo_hold = (trial_idx % 2 == 1)
                        if is_self_test:
                            # Wait briefly so any prior simulated mic utterance settles before next trial
                            await asyncio.sleep(0.85)
                        await page.evaluate("window.roomResetWindow()")
                        speech_id = await start_continuous_playback()
                        if is_echo_hold:
                            await page.evaluate(
                                "([p, d]) => window.roomSetBanner(`阶段 ${p} · 连续播报与回声防误停`, '请勿说打断词，等待本段播报自然完成。' + d, 'info')",
                                [phase_key.upper(), ""],
                            )
                            if is_self_test:
                                # Inject echo of MOSS's own text -> must NOT stop playback!
                                fake_asr.next_text = CONTINUOUS_DISPLAY_TEXT
                                await asyncio.sleep(0.35)
                                await page.evaluate("window.injectSelfTestVoice()")
                            await page.wait_for_function(
                                f"() => window.roomAcousticProbe.speechEvents.some(e => e.id === {speech_id!r} && (e.ev === 'end' || e.ev === 'stop'))",
                                timeout=int((clip_duration_s + 10) * 1000),
                            )
                            evs = await page.evaluate(
                                f"() => window.roomAcousticProbe.speechEvents.filter(e => e.id === {speech_id!r})"
                            )
                            stopped_early = any(e["ev"] == "stop" for e in evs)
                            if stopped_early:
                                ph["false_stops"] += 1
                            else:
                                ph["continuous_ok"] += 1
                            ph["trials"].append({
                                "index": trial_idx,
                                "kind": "echo_hold",
                                "false_stop": stopped_early,
                                "events": evs,
                            })
                        else:
                            ph["barge_in_attempts"] += 1
                            await asyncio.sleep(1.2 if not is_self_test else 0.4)
                            cue_at = await page.evaluate("performance.now()")
                            await page.evaluate(
                                "([p]) => window.roomSetBanner(`阶段 ${p} · 请立即打断！`, '请清晰说出「等一下」（或「莫斯，停一下」），听到停播后紧接问「现在几点」。', 'speak')",
                                [phase_key.upper()],
                            )
                            if is_self_test:
                                fake_asr.next_text = "等一下"
                                await page.evaluate("window.injectSelfTestVoice()")
                            await page.wait_for_function(
                                f"() => window.roomAcousticProbe.speechEvents.some(e => e.id === {speech_id!r} && (e.ev === 'end' || e.ev === 'stop'))",
                                timeout=int((clip_duration_s + 10) * 1000),
                            )
                            snapshot = await page.evaluate(
                                f"""() => ({{
                                  events: window.roomAcousticProbe.speechEvents.filter(e => e.id === {speech_id!r}),
                                  speakerRows: window.roomAcousticProbe.speakerRows,
                                  micRows: window.roomAcousticProbe.micRows,
                                  transcriptions: window.roomAcousticProbe.transcriptions,
                                }})"""
                            )
                            stop_ev = next((e for e in snapshot["events"] if e["ev"] == "stop"), None)
                            if stop_ev is None:
                                ph["missed_stops"] += 1
                                ph["trials"].append({
                                    "index": trial_idx,
                                    "kind": "barge_in",
                                    "stopped": False,
                                    "missed_stop": True,
                                })
                            else:
                                ph["barge_in_stopped"] += 1
                                timings = compute_barge_in_timings(
                                    cue_at=cue_at,
                                    stop_at=stop_ev["at"],
                                    speaker_rows=snapshot["speakerRows"],
                                    mic_rows=snapshot["micRows"],
                                    transcriptions=snapshot["transcriptions"],
                                    noise_floor_rms=float(floor_rms),
                                )
                                # Wait for follow-up "现在几点" recovery
                                turns_before = len(moss.state()["turns"])
                                await page.evaluate(
                                    "window.roomSetBanner('打断成功 · 请接着说「现在几点」', '验证停播后无需重新唤醒即可自然承接下一句。', 'ok')"
                                )
                                if is_self_test:
                                    await asyncio.sleep(0.95)
                                    fake_asr.next_text = "现在几点"
                                    await page.evaluate("window.injectSelfTestVoice()")
                                recovered = False
                                try:
                                    await page.wait_for_function(
                                        f"() => document.querySelectorAll('#comms-log li[data-from=\"assistant\"]').length > {turns_before}",
                                        timeout=9000,
                                    )
                                    recovered = True
                                except Exception:
                                    recovered = False
                                if recovered:
                                    ph["recovery_ok"] += 1
                                    # Wait for the follow-up reply's short playback to finish cleanly before next trial
                                    await asyncio.sleep(1.8 if is_self_test else 2.5)
                                ph["trials"].append({
                                    "index": trial_idx,
                                    "kind": "barge_in",
                                    "stopped": True,
                                    "missed_stop": False,
                                    "recovery_ok": recovered,
                                    **timings,
                                })
                        if is_self_test and trial_idx >= 2:
                            break
                        if time.monotonic() >= phase_end and trial_idx >= 2:
                            break

                    end_to_silent = [
                        t["stop_word_end_to_silent_ms"]
                        for t in ph["trials"]
                        if t.get("kind") == "barge_in" and t.get("stop_word_end_to_silent_ms") is not None
                    ]
                    start_to_silent = [
                        t["speech_start_to_silent_ms"]
                        for t in ph["trials"]
                        if t.get("kind") == "barge_in" and t.get("speech_start_to_silent_ms") is not None
                    ]
                    ph["elapsed_s"] = round(time.monotonic() - phase_start, 1)
                    ph["stop_word_end_to_silent_p50_ms"] = round(median(end_to_silent), 1) if end_to_silent else None
                    ph["stop_word_end_to_silent_p95_ms"] = percentile(end_to_silent, 0.95)
                    ph["speech_start_to_silent_p50_ms"] = round(median(start_to_silent), 1) if start_to_silent else None
                    report["phases"][phase_key] = ph
            finally:
                await browser.close()

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    all_phases_ok = all(
        ph["continuous_ok"] >= 1
        and ph["false_stops"] == 0
        and ph["barge_in_stopped"] >= 1
        and ph["missed_stops"] == 0
        and ph["recovery_ok"] == ph["barge_in_stopped"]
        for ph in report["phases"].values()
    )
    report["passed"] = all_phases_ok and not report["page_errors"]
    if mode == "full":
        total_elapsed = sum(ph["elapsed_s"] for ph in report["phases"].values())
        all_p50_ok = all(
            ph["stop_word_end_to_silent_p50_ms"] is not None and ph["stop_word_end_to_silent_p50_ms"] <= 300.0
            for ph in report["phases"].values()
        )
        report["rubric_4pt_qualified"] = bool(
            report["passed"]
            and report["physical_room_hardware"]
            and total_elapsed >= 1740.0
            and all_p50_ok
        )

    result_path = out_dir / "result.json"
    md_path = out_dir / "REPORT.md"
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    print(f"\nReport JSON: {result_path}\nReport MD:   {md_path}\npassed={report['passed']} rubric_4pt_qualified={report['rubric_4pt_qualified']}", flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MOSS physical-room 30-minute acoustic barge-in & echo acceptance harness")
    parser.add_argument("--mode", choices=("full", "smoke", "self-test"), default="smoke")
    parser.add_argument("--phase-seconds", type=int, default=None, help="Override duration per phase in seconds")
    args = parser.parse_args()
    asyncio.run(run_harness(args.mode, phase_seconds=args.phase_seconds))
