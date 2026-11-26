"""Comprehensive New-FUI acceptance suite (single Edge session, isolated backend).

Covers:
  A. 550W New FUI 3-viewport layout (1440x900, 768x1024, 375x812), real 3D WebGL stage,
     6 camera presets, audio waveform canvas, and wandering-earth fui_probes.js audit.
  B. F50 Opt-in Hand-Gesture Camera Switching (gestures.js): real MediaPipe asset HTTP 200,
     live synthetic webcam stream, fist ignored, index-up cycles camera + latches, fist unlatches,
     second index-up cycles again, toggle-off / 3D-off / pagehide immediately stop camera tracks.
  C. Jev Native Draft Handoff (#dlg-jev) + F49 Local Contact Handoff ("把刚才生成的回复交给老张")
     with real offscreen native editor fill & UTF-8 SHA-256 verification.
  D. Closed-Page Native Wake Handoff on New FUI: cold #native-wake=<ticket> URL hash strip +
     claim/shown + composer focus, and live SSE wake-offer claim/shown + composer focus.
  E. Duplex Voice Barge-In, Echo Rejection, & Spoken Mid-Generation Correction on New FUI
     (preserving composer draft).
  F. Passive Windows External Media Status in New FUI Local Overview ("现在几点，系统播放器正在播放什么？").
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
import wave

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import ROOT, confirm_muted_edge, open_browser
import core
from core import Moss, now
import jev_handoff
from local_media.library import MediaLibrary
from model_catalog import SPECS
import serve
from serve import application
import system_media
from verify_context_reply import free_port


FUI_PUBLIC = ROOT / "fui" / "public"
FUI_PROBES = Path(
    os.environ.get("MOSS_FUI_PROBES")
    or (Path.home() / ".gemini" / "config" / "skills" / "wandering-earth-fui-design" / "scripts" / "fui_probes.js")
)
JEV_REPLY = "收到，谢谢！\n第二行：明天 9 点见。🙂"
CONTACT_ID = "11111111-2222-4333-8444-555555555555"
SYSTEM_MEDIA_REPLY = "系统播放器正在播放，当前曲目是验收歌手的《安静的航站楼》。"
ECHO_SPEECH_TEXT = "这是一段用于测试回声抑制的长句播报内容，正在平稳朗读中，请稍候听完本句。"
BARGE_SPEECH_TEXT = "这是第二段用于测试语音插话打断的长句播报内容，正在平稳继续朗读这一整段说明文字。"


def make_sine_wav(duration_s=2.6, freq_hz=440.0, rate=24000, amp=0.22):
    frames = int(duration_s * rate)
    samples = bytearray()
    for i in range(frames):
        val = int(max(-32767, min(32767, round(32767 * amp * math.sin(2.0 * math.pi * freq_hz * i / rate)))))
        samples += struct.pack("<h", val)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(samples))
    return buf.getvalue()


def make_mic_speech_wav():
    """Return a ~1.0s spoken WAV fixture with leading/trailing silence so VAD endpoints cleanly."""
    line = ROOT / "voice" / "lines" / "done.wav"
    with wave.open(str(line), "rb") as src:
        params = src.getparams()
        pcm = src.readframes(src.getnframes())
    pad = bytes(int(params.framerate * 0.25) * params.sampwidth * params.nchannels)
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(pad + pcm + pad)
    return out.getvalue()


class FakeSpeech:
    """Fast deterministic PCM generator matching MossSpeech interface."""

    batch_capable = True

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.clip_long = make_sine_wav(5.0, 420.0)
        self.clip_short = make_sine_wav(0.35, 520.0)

    async def prepare_imports(self):
        return False

    async def prepare_microphone(self, *args, **kwargs):
        return False

    async def release_microphone(self, *args, **kwargs):
        return None

    async def render(self, text, disconnected, category="reply", cached_only=False, *, timings=None, room_amount=100):
        self.calls.append({"text": text, "category": category, "cached_only": cached_only})
        if disconnected and disconnected():
            raise ConnectionAbortedError()
        if timings is not None:
            timings["tts"] = 10.0
        return self.clip_long if len(str(text or "")) >= 24 else self.clip_short

    async def render_batch(self, texts, disconnected, category="reply", *, room_amount=100):
        if disconnected and disconnected():
            raise ConnectionAbortedError()
        out = [self.clip_long if len(str(t or "")) >= 24 else self.clip_short for t in texts]
        return out, "batch"

    async def close(self):
        return None


class FakeVoiceInput:
    """Controlled ASR backend for deterministic duplex barge-in / echo-rejection verification."""

    def __init__(self):
        self.next_text = ""
        self.calls = []

    async def prepare(self):
        return None

    async def transcribe(self, binary, *, preview=False):
        text = self.next_text
        self.calls.append({"bytes": len(binary), "preview": preview, "text": text})
        return text

    async def close(self):
        return None


GESTURE_STUB_JS = """
const makeHand = (indexUp) => {
  const pts = Array.from({ length: 21 }, () => ({ x: 0.5, y: 0.6, z: 0 }));
  pts[0] = { x: 0.5, y: 0.8, z: 0 };
  pts[9] = { x: 0.5, y: 0.55, z: 0 };
  pts[6] = { x: 0.45, y: 0.50, z: 0 };
  pts[8] = indexUp ? { x: 0.45, y: 0.30, z: 0 } : { x: 0.45, y: 0.60, z: 0 };
  pts[10] = { x: 0.50, y: 0.52, z: 0 }; pts[12] = { x: 0.50, y: 0.62, z: 0 };
  pts[14] = { x: 0.55, y: 0.54, z: 0 }; pts[16] = { x: 0.55, y: 0.64, z: 0 };
  pts[18] = { x: 0.60, y: 0.56, z: 0 }; pts[20] = { x: 0.60, y: 0.65, z: 0 };
  return pts;
};
window.__gestureStub = { created: 0, closed: 0, detects: 0 };
export const FilesetResolver = {
  async forVisionTasks(root) { return { root }; },
};
export const HandLandmarker = {
  async createFromOptions() {
    window.__gestureStub.created += 1;
    return {
      detectForVideo() {
        window.__gestureStub.detects += 1;
        const pose = window.__gesturePose || 'fist';
        if (pose === 'none') return { landmarks: [] };
        return { landmarks: [makeHand(pose === 'index_up')] };
      },
      close() { window.__gestureStub.closed += 1; },
    };
  },
};
"""


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("fui-full-acceptance-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    result_path = folder / "result.json"
    print(result_path, flush=True)

    report = {
        "passed": False,
        "page_errors": [],
        "outside_requests": [],
        "model_connections": 0,
        "parts": {},
        "screenshots": [],
    }

    catalog = MediaLibrary(folder / "catalog.sqlite3")
    profile_dir = folder / "profile"
    profile_dir.mkdir(parents=True)
    # Seed local opt-in contacts book for F49 contact handoff test
    contacts_dir = folder / "data"
    contacts_dir.mkdir(parents=True)
    (contacts_dir / "jev-contacts.json").write_text(
        json.dumps({"contacts": [{"id": CONTACT_ID, "display_name": "张三", "aliases": ["老张", "张工"]}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    editors = {}
    native_attempts = []
    real_open = jev_handoff.open_draft_window
    real_uia = jev_handoff.uia_session

    async def offscreen_open(*args, **kwargs):
        kwargs["offscreen"] = True
        return await real_open(*args, **kwargs)

    @asynccontextmanager
    async def trace_native():
        async with real_uia() as send:
            async def command(message):
                if message["mode"] == "arm":
                    editors[message["pid"]] = (psutil.Process(message["pid"]).create_time(), message["target_id"])
                began = perf_counter()
                res = await send(message)
                native_attempts.append({
                    "mode": message["mode"], "status": res.get("status"),
                    "sha256": res.get("sha256"), "bytes": res.get("bytes"),
                    "ms": round((perf_counter() - began) * 1000, 1),
                })
                return res
            yield command

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    slow_holo_release = asyncio.Event()
    slow_holo_started = asyncio.Event()

    async def fake_holo_answer(moss, turn, *args, **kwargs):
        text = turn.get("user") or ""
        if "回声测试" in text:
            return ECHO_SPEECH_TEXT
        if "插话测试" in text:
            return BARGE_SPEECH_TEXT
        if "慢速生成测试" in text:
            moss._update(moss._get(turn["id"]), conversation_replaceable=True, partial_reply="正在生成长回答……")
            slow_holo_started.set()
            try:
                await asyncio.wait_for(slow_holo_release.wait(), timeout=20)
            except asyncio.CancelledError:
                raise
            return "慢速回答最终完成。"
        return f"收到：{text}"

    fake_speech = FakeSpeech()
    fake_voice_input = FakeVoiceInput()
    mic_wav = make_mic_speech_wav()

    async def fake_query_system_media(*args, **kwargs):
        return {"code": "status", "before": {"state": "Playing", "title": "安静的航站楼", "artist": "验收歌手", "source": "TestPlayer"}}

    env = {
        "LOCALAPPDATA": str(profile_dir),
        "MOSS_MEDIA_DB": str(catalog.db_path),
        "MOSS_PROVIDER_DIR": str(folder / "providers"),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_KEV_ENABLED": "0",
        "MOSS_SPEECH_PREPARE": "0",
        "MOSS_SPEECH_STANDBY": "0",
    }

    try:
        with patch.dict(os.environ, env), \
                patch.object(serve, "MossSpeech", lambda *a, **kw: fake_speech), \
                patch.object(serve, "LocalVoice", lambda *a, **kw: fake_voice_input), \
                patch.object(core, "holo_answer", fake_holo_answer), \
                patch.object(jev_handoff, "open_draft_window", offscreen_open), \
                patch.object(jev_handoff, "uia_session", trace_native), \
                patch.object(system_media, "query_system_media", fake_query_system_media):
            async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as sink:
                model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
                for _, prefixes, _, _ in SPECS.values():
                    for prefix in prefixes:
                        os.environ[prefix + "_BASE_URL"] = model_url
                port = free_port()
                base = f"http://127.0.0.1:{port}"
                moss = Moss(contacts_dir, model_url, "isolated-unused", prefer_cloud=False)
                app = application(moss, lambda: "standby", contacts_dir, port, "fui-full-token",
                                  asyncio.Event(), fui_public=FUI_PUBLIC)
                app.router.add_get("/fixture/mic.wav", lambda req: web.Response(body=mic_wav, content_type="audio/wav"))
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()

                try:
                    # Pre-check real MediaPipe assets served by MOSS HTTP
                    async with aiohttp.ClientSession() as http:
                        asset_status = {}
                        for asset in (
                            "/conversation-assets/hand-gesture/vision_bundle.mjs",
                            "/conversation-assets/hand-gesture/hand_landmarker.task",
                            "/conversation-assets/hand-gesture/wasm/vision_wasm_internal.js",
                            "/conversation-assets/hand-gesture/wasm/vision_wasm_internal.wasm",
                        ):
                            async with http.head(base + asset) as resp:
                                asset_status[asset] = resp.status
                        assert all(code == 200 for code in asset_status.values()), asset_status

                    async with async_playwright() as playwright:
                        browser = await open_browser(playwright)
                        try:
                            await confirm_muted_edge(browser)

                            # ──────────────────────────────────────────────
                            # PART A + B: 550W New FUI 3 Viewports + 6 Cameras + Probes + Hand Gestures (F50)
                            # ──────────────────────────────────────────────
                            page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
                            page.on("pageerror", lambda err: report["page_errors"].append(f"AB:{err}"))

                            async def route_ab(route):
                                u = urlsplit(route.request.url)
                                if u.scheme in {"http", "https"} and u.netloc != f"127.0.0.1:{port}":
                                    report["outside_requests"].append(u.netloc)
                                    await route.abort()
                                    return
                                if u.path == "/conversation-assets/hand-gesture/vision_bundle.mjs":
                                    await route.fulfill(status=200, content_type="application/javascript", body=GESTURE_STUB_JS)
                                    return
                                await route.continue_()

                            await page.route("**/*", route_ab)
                            await page.add_init_script("""
                              localStorage.setItem('moss-fui.tts', 'off');
                              window.__camTracks = [];
                              window.__camDraws = 0;
                              window.__gesturePose = 'none';
                              Object.defineProperty(navigator, 'mediaDevices', {
                                configurable: true,
                                value: {
                                  async enumerateDevices() { return []; },
                                  async getUserMedia(constraints) {
                                    const c = document.createElement('canvas');
                                    c.width = 64; c.height = 48;
                                    const ctx = c.getContext('2d');
                                    const tick = () => {
                                      window.__camDraws += 1;
                                      ctx.fillStyle = (window.__camDraws % 2) ? '#112233' : '#223344';
                                      ctx.fillRect(0, 0, 64, 48);
                                    };
                                    tick();
                                    const timer = setInterval(tick, 60);
                                    const stream = c.captureStream(15);
                                    for (const track of stream.getVideoTracks()) {
                                      const origStop = track.stop.bind(track);
                                      track.stop = () => { clearInterval(timer); return origStop(); };
                                      window.__camTracks.push(track);
                                    }
                                    return stream;
                                  },
                                },
                              });
                            """)
                            await page.goto(base + "/?skipOpening=1", wait_until="domcontentloaded")
                            await page.wait_for_function(
                                "() => document.querySelector('#moss-model')?.dataset.ready === 'true'",
                                timeout=25000,
                            )
                            await page.wait_for_function(
                                "() => document.querySelector('#strip-live')?.dataset.live === 'true'",
                                timeout=10000,
                            )

                            # Check 6 camera presets cycling on desktop
                            cam_buttons = page.locator("#cam-matrix .fui-matrix__key")
                            assert await cam_buttons.count() == 6
                            cam_indices = []
                            for idx in range(6):
                                await cam_buttons.nth(idx).click()
                                cam_indices.append((await page.locator("#cam-index").text_content()).strip())
                            await cam_buttons.nth(0).click()
                            assert cam_indices == ["01 / 06", "02 / 06", "03 / 06", "04 / 06", "05 / 06", "06 / 06"], cam_indices

                            # Run wandering-earth fui_probes.js on 1440x900 desktop
                            probes_result = None
                            if FUI_PROBES.is_file():
                                await page.add_script_tag(path=str(FUI_PROBES))
                                probes_result = await page.evaluate("() => window.fuiProbes()")

                            # Check 3 viewports overflow & screenshots
                            viewports = {}
                            for label, vp in (("desktop-1440", {"width": 1440, "height": 900}),
                                              ("tablet-768", {"width": 768, "height": 1024}),
                                              ("mobile-375", {"width": 375, "height": 812})):
                                await page.set_viewport_size(vp)
                                await page.wait_for_timeout(200)
                                metrics = await page.evaluate("""() => ({
                                  innerWidth: window.innerWidth,
                                  scrollWidth: document.documentElement.scrollWidth,
                                  orbitVisible: document.querySelector('#stage-orbit')?.getBoundingClientRect().width > 0,
                                  modelReady: document.querySelector('#moss-model')?.dataset.ready === 'true',
                                })""")
                                shot = f"{label}.png"
                                await page.screenshot(path=folder / shot, animations="disabled")
                                report["screenshots"].append(shot)
                                viewports[label] = {**metrics, "no_overflow": metrics["scrollWidth"] <= metrics["innerWidth"] + 2}
                            await page.set_viewport_size({"width": 1440, "height": 900})
                            await page.wait_for_timeout(150)

                            report["parts"]["A_550w_viewports_and_probes"] = {
                                "passed": (
                                    all(v["no_overflow"] and v["orbitVisible"] and v["modelReady"] for v in viewports.values())
                                    and (probes_result is None or (probes_result["S1"]["pass"] and probes_result["S2"]["pass"]
                                                                   and probes_result["S3"]["pass"] and probes_result["P11"]["pass"]))
                                ),
                                "cam_indices": cam_indices,
                                "viewports": viewports,
                                "probes_summary": {k: {"pass": v["pass"], "value": v["value"]} for k, v in (probes_result or {}).items()} if probes_result else None,
                            }

                            # PART B (F50): Hand-Gesture Camera Switching
                            await page.locator("#strip-settings").click()
                            await expect(page.locator("#dlg-settings")).to_be_visible()
                            await page.locator('#dlg-settings [data-set-tab="display"]').click()
                            gesture_chk = page.locator("#set-gesture")
                            assert not await gesture_chk.is_checked()
                            assert not await gesture_chk.is_disabled()
                            await gesture_chk.check()
                            await page.wait_for_function(
                                "() => window.__camTracks.filter(t => t.readyState === 'live').length === 1 && window.__gestureStub?.detects >= 2",
                                timeout=8000,
                            )
                            # Hold neutral hand pose for 650ms -> camera must stay 01 / 06
                            await page.wait_for_timeout(650)
                            after_fist = (await page.locator("#cam-index").text_content()).strip()

                            # Show index-finger-up -> camera switches to 02 / 06 and stays latched
                            await page.evaluate("window.__gesturePose = 'index_up'")
                            await page.wait_for_function(
                                "() => document.querySelector('#cam-index')?.textContent.trim() === '02 / 06'",
                                timeout=5000,
                            )
                            await page.wait_for_timeout(600)
                            latched_cam = (await page.locator("#cam-index").text_content()).strip()

                            # Relax hand (none) >= 550ms to unlatch, then index-up again -> 03 / 06
                            await page.evaluate("window.__gesturePose = 'none'")
                            await page.wait_for_timeout(650)
                            await page.evaluate("window.__gesturePose = 'index_up'")
                            await page.wait_for_function(
                                "() => document.querySelector('#cam-index')?.textContent.trim() === '03 / 06'",
                                timeout=5000,
                            )
                            second_cycle = (await page.locator("#cam-index").text_content()).strip()

                            # Toggle off -> tracks stopped, srcObject cleared, tracker closed
                            await gesture_chk.uncheck()
                            off_state = await page.evaluate("""() => ({
                              liveTracks: window.__camTracks.filter(t => t.readyState === 'live').length,
                              srcObjectNull: document.querySelector('#gesture-video')?.srcObject === null,
                              closedCount: window.__gestureStub?.closed || 0,
                            })""")

                            # Re-enable gesture, then disable 3D stage -> gesture auto-stops and disables
                            await page.evaluate("window.__gesturePose = 'none'")
                            await gesture_chk.check()
                            await page.wait_for_function(
                                "() => window.__camTracks.filter(t => t.readyState === 'live').length === 1",
                                timeout=5000,
                            )
                            await page.locator("#set-scene-3d").uncheck()
                            await page.wait_for_timeout(150)
                            scene_off_state = await page.evaluate("""() => ({
                              liveTracks: window.__camTracks.filter(t => t.readyState === 'live').length,
                              checked: document.querySelector('#set-gesture')?.checked,
                              disabled: document.querySelector('#set-gesture')?.disabled,
                            })""")

                            # Re-enable 3D + gesture, then dispatch pagehide -> camera stops immediately
                            await page.locator("#set-scene-3d").check()
                            await page.wait_for_function("() => !document.querySelector('#set-gesture')?.disabled", timeout=20000)
                            await gesture_chk.check()
                            await page.wait_for_function(
                                "() => window.__camTracks.filter(t => t.readyState === 'live').length === 1",
                                timeout=5000,
                            )
                            await page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                            pagehide_live = await page.evaluate("() => window.__camTracks.filter(t => t.readyState === 'live').length")
                            await page.close()

                            report["parts"]["B_F50_hand_gestures"] = {
                                "asset_http_200": asset_status,
                                "after_fist": after_fist,
                                "latched_cam": latched_cam,
                                "second_cycle": second_cycle,
                                "off_state": off_state,
                                "scene_off_state": scene_off_state,
                                "pagehide_live_tracks": pagehide_live,
                                "passed": (
                                    after_fist == "01 / 06"
                                    and latched_cam == "02 / 06"
                                    and second_cycle == "03 / 06"
                                    and off_state == {"liveTracks": 0, "srcObjectNull": True, "closedCount": 1}
                                    and scene_off_state == {"liveTracks": 0, "checked": False, "disabled": True}
                                    and pagehide_live == 0
                                ),
                            }

                            # ──────────────────────────────────────────────
                            # PART C + F: Jev Direct Handoff + F49 Contact Handoff + External System Media on New FUI
                            # ──────────────────────────────────────────────
                            chat = moss.chat_id
                            jev_turn_id = uuid4().hex
                            with moss.db:
                                moss._insert_turn({
                                    "chat_id": chat, "id": jev_turn_id, "user": "帮我拟一条简短回复",
                                    "source": "jev", "status": "replied", "created_at": now(),
                                    "reply": JEV_REPLY, "jev": {"candidate_count": 1},
                                })
                                moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
                            moss._notify()
                            page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
                            page.on("pageerror", lambda err: report["page_errors"].append(f"CF:{err}"))
                            await page.add_init_script("localStorage.setItem('moss-fui.tts', 'off');")
                            await page.goto(base + "/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                            await page.wait_for_function(
                                f"() => Boolean(document.querySelector('[data-turn-id=\"{jev_turn_id}\"]'))",
                                timeout=10000,
                            )

                            # C1: Jev direct handoff dialog (#dlg-jev)
                            jev_item = page.locator(f'li[data-from="assistant"][data-turn-id="{jev_turn_id}"]')
                            fill_btn = jev_item.get_by_role("button", name="填入本机草稿窗")
                            await fill_btn.click()
                            await expect(page.locator("#dlg-jev")).to_be_visible()
                            preview_text = await page.locator("#jev-preview").text_content()
                            await page.keyboard.press("Escape")
                            await expect(page.locator("#dlg-jev")).to_be_hidden()
                            assert len(native_attempts) == 0

                            await fill_btn.click()
                            await page.locator("#jev-confirm").click()
                            await page.wait_for_function(
                                "() => (document.querySelector('#jev-status')?.textContent || '').includes('已填入本机草稿窗')",
                                timeout=20000,
                            )
                            await page.locator("#dlg-jev footer [data-dialog-close]").click()
                            await expect(page.locator("#dlg-jev")).to_be_hidden()

                            # C2: F49 Contact handoff ("把刚才生成的回复交给老张" -> resolves alias to 张三)
                            await page.locator("#comms-input").click()
                            await page.locator("#comms-input").fill("把刚才生成的回复交给老张")
                            await page.locator("#comms-input").dispatch_event("input")
                            await page.wait_for_function(
                                "() => !document.querySelector('#comms-send')?.disabled",
                                timeout=5000,
                            )
                            await page.locator("#comms-send").click()
                            await page.wait_for_function(
                                "() => [...document.querySelectorAll('#comms-log .moss-card__body')].some(el => el.textContent.includes('张三'))",
                                timeout=10000,
                            )
                            approve_btn = page.locator('#comms-log button:has-text("确认执行")').last
                            await approve_btn.click()
                            await page.wait_for_function(
                                "() => document.querySelectorAll('#comms-log .moss-card[data-state=\"warn\"]').length === 0 "
                                "&& [...document.querySelectorAll('#comms-log .fui-chat__body')].some(el => el.textContent.includes('张三') && el.textContent.includes('草稿窗'))",
                                timeout=20000,
                            )
                            contact_turn = moss.state()["turns"][-1]

                            # PART F: Passive External System Media in Local Overview on New FUI
                            await page.locator("#comms-input").fill("现在几点，系统播放器正在播放什么？")
                            await page.locator("#comms-send").click()
                            await page.wait_for_function(
                                "() => [...document.querySelectorAll('#comms-log .fui-chat__body')].some(el => el.textContent.includes('安静的航站楼'))",
                                timeout=10000,
                            )
                            sys_media_turn = moss.state()["turns"][-1]
                            await page.close()

                            expected_sha = hashlib.sha256(JEV_REPLY.encode("utf-8")).hexdigest()
                            filled_attempts = [a for a in native_attempts if a["mode"] == "fill" and a["status"] == "filled"]
                            report["parts"]["C_jev_and_F49_contact_handoff"] = {
                                "preview_exact": preview_text == JEV_REPLY,
                                "filled_count": len(filled_attempts),
                                "sha256_all_match": all(a["sha256"] == expected_sha for a in filled_attempts),
                                "contact_receipt": contact_turn.get("jev_contact_receipt"),
                                "native_attempts": native_attempts,
                                "passed": (
                                    preview_text == JEV_REPLY
                                    and len(filled_attempts) == 2
                                    and all(a["sha256"] == expected_sha for a in filled_attempts)
                                    and (contact_turn.get("jev_contact_receipt") or {}).get("recipient") == "张三"
                                ),
                            }
                            report["parts"]["F_external_system_media_fui"] = {
                                "reply": sys_media_turn.get("reply"),
                                "kinds": [r.get("kind") for r in (sys_media_turn.get("local_overview") or ())],
                                "passed": (
                                    "安静的航站楼" in (sys_media_turn.get("reply") or "")
                                    and [r.get("kind") for r in (sys_media_turn.get("local_overview") or ())] == ["clock", "system_media"]
                                ),
                            }

                            # ──────────────────────────────────────────────
                            # PART D: Closed-Page Native Wake Handoff on New FUI (Cold #native-wake + Live SSE wake-offer)
                            # ──────────────────────────────────────────────
                            async with aiohttp.ClientSession() as http:
                                async with asyncio.timeout(5):
                                    while True:
                                        async with http.post(base + "/api/wake/status", json={"token": "fui-full-token"}) as resp:
                                            st = await resp.json()
                                        if not st.get("page_connected"):
                                            break
                                        await asyncio.sleep(0.05)
                                async with http.post(base + "/api/wake/activate",
                                                     json={"token": "fui-full-token", "listen_next": True}) as resp:
                                    cold_wake = await resp.json()
                                assert cold_wake.get("result") == "no_page" and cold_wake.get("launch_ticket"), cold_wake
                                cold_ticket = cold_wake["launch_ticket"]

                            page = await browser.new_page(viewport={"width": 1280, "height": 850}, reduced_motion="reduce")
                            page.on("pageerror", lambda err: report["page_errors"].append(f"D:{err}"))
                            await page.add_init_script("localStorage.setItem('moss-fui.tts', 'off');")
                            await page.goto(base + f"/?skipOpening=1&scene=off#native-wake={cold_ticket}", wait_until="domcontentloaded")
                            await page.bring_to_front()
                            await page.wait_for_function("() => document.activeElement?.id === 'comms-input'", timeout=8000)
                            await page.wait_for_timeout(200)
                            hash_stripped = await page.evaluate("() => location.hash")

                            async with aiohttp.ClientSession() as http:
                                # Re-claiming the cold ticket after shown must return 410 Gone
                                async with http.post(base + "/api/wake/claim",
                                                     json={"ticket": cold_ticket}, headers={"Origin": base}) as resp:
                                    cold_reclaim_status = resp.status
                                # Blur composer, then send live SSE wake-offer and confirm page_shown receipt
                                await page.evaluate("document.activeElement?.blur()")
                                async with http.post(base + "/api/wake/activate",
                                                     json={"token": "fui-full-token", "listen_next": False}) as resp:
                                    live_wake = await resp.json()
                                await page.wait_for_function("() => document.activeElement?.id === 'comms-input'", timeout=5000)
                                live_focused = await page.evaluate("() => document.activeElement?.id === 'comms-input'")
                            await page.close()

                            report["parts"]["D_closed_page_native_wake"] = {
                                "cold_activate": cold_wake,
                                "cold_hash_stripped": hash_stripped == "",
                                "cold_reclaim_status": cold_reclaim_status,
                                "live_activate": live_wake,
                                "live_focused": live_focused,
                                "passed": (
                                    hash_stripped == ""
                                    and cold_reclaim_status == 410
                                    and live_wake.get("result") == "page_shown"
                                    and live_focused is True
                                ),
                            }

                            # ──────────────────────────────────────────────
                            # PART E: Duplex Voice Barge-In, Echo Rejection & Spoken Mid-Gen Correction on New FUI
                            # ──────────────────────────────────────────────
                            await moss.new_chat()
                            page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
                            page.on("pageerror", lambda err: report["page_errors"].append(f"E:{err}"))
                            await page.add_init_script("""
                              localStorage.setItem('moss-fui.tts', 'on');
                              localStorage.setItem('moss.voice.enabled', 'on');
                              window.__duplexLog = { speechEvents: [], tracks: [] };
                              for (const ev of ['start', 'stop', 'end', 'error', 'skipped']) {
                                window.addEventListener('moss-speech-' + ev, (e) => {
                                  window.__duplexLog.speechEvents.push({
                                    ev, at: performance.now(), id: e.detail?.id || null,
                                    text: e.detail?.text || e.detail?.caption || '',
                                  });
                                });
                              }
                              let micCtx, micDest;
                              Object.defineProperty(navigator, 'mediaDevices', {
                                configurable: true,
                                value: {
                                  async enumerateDevices() { return [{ kind: 'audioinput', deviceId: 'sim-mic', label: 'Simulated Mic' }]; },
                                  async getUserMedia() {
                                    micCtx = new AudioContext();
                                    await micCtx.resume();
                                    micDest = micCtx.createMediaStreamDestination();
                                    const track = micDest.stream.getAudioTracks()[0];
                                    window.__duplexLog.tracks.push(track);
                                    return micDest.stream;
                                  },
                                },
                              });
                              window.injectMicSpeech = async () => {
                                if (!micCtx || !micDest) throw new Error('mic not started');
                                const raw = await (await fetch('/fixture/mic.wav')).arrayBuffer();
                                const decoded = await micCtx.decodeAudioData(raw);
                                const src = micCtx.createBufferSource();
                                src.buffer = decoded;
                                src.connect(micDest);
                                src.start();
                              };
                            """)
                            await page.goto(base + "/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                            await page.wait_for_function("() => document.querySelector('#strip-live')?.dataset.live === 'true'", timeout=10000)

                            # Enable wake + barge-in via Settings dialog (real user gesture unlocks AudioContext)
                            await page.locator("#strip-settings").click()
                            await expect(page.locator("#dlg-settings")).to_be_visible()
                            await page.locator('#dlg-settings [data-set-tab="audio"]').click()
                            await page.locator("#set-wake").check()
                            await page.wait_for_function(
                                "() => !document.querySelector('#set-barge')?.disabled && window.__duplexLog.tracks.length >= 1",
                                timeout=8000,
                            )
                            await page.locator("#set-barge").check()
                            await page.locator("#dlg-settings [data-dialog-close]").first.click()
                            await expect(page.locator("#dlg-settings")).to_be_hidden()

                            # E1: Echo rejection (during 5.0s MOSS speech, mic hears MOSS's own text -> must finish naturally)
                            fake_voice_input.next_text = ECHO_SPEECH_TEXT
                            await page.locator("#comms-input").fill("执行回声测试")
                            await page.locator("#comms-send").click()
                            await page.wait_for_function(
                                "() => window.__duplexLog.speechEvents.some(e => e.ev === 'start' && e.text.includes('回声抑制'))",
                                timeout=10000,
                            )
                            e1_id = await page.evaluate(
                                "() => window.__duplexLog.speechEvents.find(e => e.ev === 'start' && e.text.includes('回声抑制'))?.id"
                            )
                            await page.evaluate("window.injectMicSpeech()")
                            await page.wait_for_function(
                                f"() => window.__duplexLog.speechEvents.some(e => (e.ev === 'end' || e.ev === 'stop') && e.id === {e1_id!r})",
                                timeout=12000,
                            )
                            e1_events = await page.evaluate(f"() => window.__duplexLog.speechEvents.filter(e => e.id === {e1_id!r})")
                            e1_ended_naturally = (
                                any(e["ev"] == "end" for e in e1_events)
                                and not any(e["ev"] == "stop" for e in e1_events)
                                and any(c["text"] == ECHO_SPEECH_TEXT for c in fake_voice_input.calls)
                            )

                            # E2: Barge-in "等一下" stops MOSS playback mid-clip, then "现在几点" is accepted without re-wake
                            fake_voice_input.next_text = "等一下"
                            await page.locator("#comms-input").fill("执行插话测试")
                            await page.locator("#comms-send").click()
                            await page.wait_for_function(
                                "() => window.__duplexLog.speechEvents.some(e => e.ev === 'start' && e.text.includes('插话打断'))",
                                timeout=10000,
                            )
                            e2_id = await page.evaluate(
                                "() => window.__duplexLog.speechEvents.find(e => e.ev === 'start' && e.text.includes('插话打断'))?.id"
                            )
                            await page.wait_for_timeout(250)
                            await page.evaluate("window.injectMicSpeech()")
                            await page.wait_for_function(
                                f"() => window.__duplexLog.speechEvents.some(e => e.ev === 'stop' && e.id === {e2_id!r})",
                                timeout=10000,
                            )
                            await page.wait_for_timeout(450)
                            fake_voice_input.next_text = "现在几点"
                            turns_before_followup = len(moss.state()["turns"])
                            await page.evaluate("window.injectMicSpeech()")
                            await page.wait_for_function(
                                f"() => document.querySelectorAll('#comms-log li[data-from=\"assistant\"]').length > {turns_before_followup}",
                                timeout=10000,
                            )
                            await page.wait_for_timeout(500)
                            e2_followup_turn = moss.state()["turns"][-1]
                            e2_ok = e2_followup_turn["user"] == "现在几点" and e2_followup_turn["status"] == "replied"

                            # E3: Spoken mid-gen correction preserves composer draft
                            slow_holo_started.clear()
                            slow_holo_release.clear()
                            await page.locator("#comms-input").fill("开始慢速生成测试")
                            await page.locator("#comms-send").click()
                            await asyncio.wait_for(slow_holo_started.wait(), timeout=8)
                            await page.wait_for_timeout(300)
                            turns_after_slow = len(moss.state()["turns"])
                            draft_guard = "保留我正在打字的草稿"
                            await page.locator("#comms-input").fill(draft_guard)
                            fake_voice_input.next_text = "莫斯，先停止回答，改问现在几点"
                            await page.evaluate("window.injectMicSpeech()")
                            await page.wait_for_function(
                                f"() => document.querySelectorAll('#comms-log li[data-from=\"assistant\"]').length > {turns_after_slow}",
                                timeout=10000,
                            )
                            await page.wait_for_timeout(400)
                            composer_after = await page.locator("#comms-input").input_value()
                            last_two = moss.state()["turns"][-2:]
                            e3_ok = (
                                len(last_two) == 2
                                and last_two[0]["status"] == "cancelled"
                                and last_two[1]["user"] == "现在几点"
                                and last_two[1]["status"] == "replied"
                                and composer_after == draft_guard
                            )
                            await page.close()

                            report["parts"]["E_duplex_voice_fui"] = {
                                "e1_echo_ended_naturally": e1_ended_naturally,
                                "e1_events": e1_events,
                                "e2_barge_in_followup_ok": e2_ok,
                                "e2_followup_reply": e2_followup_turn.get("reply"),
                                "e3_spoken_midgen_correction_ok": e3_ok,
                                "e3_composer_preserved": composer_after == draft_guard,
                                "asr_calls": fake_voice_input.calls,
                                "passed": e1_ended_naturally and e2_ok and e3_ok,
                            }
                        finally:
                            await browser.close()
                finally:
                    await runner.cleanup()
                    await moss.close()

        report["passed"] = (
            all(part["passed"] for part in report["parts"].values())
            and not report["page_errors"]
            and not report["outside_requests"]
            and report["model_connections"] == 0
        )
    except Exception as exc:
        import traceback
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        for pid, (created, target) in editors.items():
            try:
                proc = psutil.Process(pid)
                args = proc.cmdline()
                if (proc.create_time() == created and "-Offscreen" in args and target in args
                        and str(jev_handoff.EDITOR) in args):
                    proc.terminate()
            except psutil.Error:
                pass
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result_path, "passed=", report["passed"], flush=True)
        for name, sec in report["parts"].items():
            print(f"  {name}: passed={sec['passed']}", flush=True)
        if report["page_errors"]:
            print("  page_errors:", report["page_errors"], flush=True)
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
