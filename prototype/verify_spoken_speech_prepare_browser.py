import argparse
import asyncio
from array import array
from datetime import datetime
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import async_playwright
from pydantic import SecretStr

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port
from verify_context_voice_e2e import make_voice
from verify_local_overview import QUESTION
from voice_output import GIB, MossSpeech


PROBE = """
window.voiceInputQa = [];
const watch = new Set(['/api/prepare-voice', '/api/prepare-speech', '/api/transcribe', '/api/message', '/api/speak']);
const fetchForQa = window.fetch;
window.fetch = async function(url, options) {
  if (!watch.has(url)) return fetchForQa.apply(this, arguments);
  const entry = {url, at:performance.now(), focus:document.hasFocus(), hidden:document.hidden,
    mic:document.getElementById('mic').getAttribute('aria-pressed')};
  voiceInputQa.push(entry);
  if (url === '/api/prepare-speech') entry.body = JSON.parse(options.body);
  if (url === '/api/transcribe') entry.audioBytes = options.body.size;
  const response = await fetchForQa.apply(this, arguments);
  entry.status = response.status;
  if (url === '/api/transcribe') entry.text = (await response.clone().json()).text;
  if (url === '/api/speak') entry.complete = await response.clone().arrayBuffer().then(() => performance.now());
  return response;
};
for (const state of ['start', 'end', 'error'])
  addEventListener('moss-speech-' + state, () => voiceInputQa.push({url:state, at:performance.now()}));
"""


def short_capture(source, output):
    with wave.open(str(source), "rb") as full:
        assert full.getparams()[:3] == (1, 2, 16000)
        pcm = array("h", full.readframes(full.getnframes()))
    onset = next(index for index, sample in enumerate(pcm) if abs(sample) > 190)
    burst = pcm[onset:onset + 16000 // 6].tobytes()
    with wave.open(str(output), "wb") as recording:
        recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        recording.writeframes(bytes(16000) + burst + bytes(16000 * 12))


async def check_activity(page, case, entry):
    mic = page.locator("#mic")
    if case == "media":
        await page.evaluate("""() => {
          const audio = window.guardMedia = new Audio('/static/music-ack.wav');
          audio.className = 'media-audio';
          audio.loop = true;
          document.body.append(audio);
          document.querySelector('#mic').addEventListener('pointerdown', () => {
            audio.play().catch(error => { window.guardMediaError = String(error); });
          }, {once:true});
        }""")
    await mic.click()
    await page.locator("#mic[aria-pressed=true]").wait_for(timeout=12000)
    if case == "media":
        await page.wait_for_function("!guardMedia.paused && guardMedia.currentTime > 0", timeout=3500)
        assert not await page.evaluate("window.guardMediaError")
    if case == "manual":
        await page.wait_for_function("""voiceInputQa.some(e => e.url === '/api/prepare-speech' &&
            e.body?.mic && !e.body.release && e.status === 204)""", timeout=12000)
    elif case == "silence":
        await page.wait_for_timeout(1600)
        assert await mic.get_attribute("aria-pressed") == "true"
    else:
        await page.wait_for_function("""voiceInputQa.some(e => e.url === '/api/transcribe' &&
            e.status === 200 && e.audioBytes > 44)""", timeout=18000)
    await mic.click()
    await page.locator("#mic[aria-pressed=false]").wait_for(timeout=5000)
    attempted = await page.evaluate("voiceInputQa.some(e=>e.url==='/api/prepare-speech' && e.body?.mic)")
    if attempted and case in {"manual", "memory"}:
        await page.wait_for_function("""voiceInputQa.some(e => e.url === '/api/prepare-speech' &&
            e.body?.release && e.status === 204)""", timeout=5000)
    traffic = await page.evaluate("voiceInputQa")
    keyed = [row for row in traffic if row["url"] == "/api/prepare-speech" and row["body"]]
    if case in {"manual", "memory"}:
        start, release = keyed
        key = start["body"]["mic"]
        assert re.fullmatch("[0-9a-f]{32}", key), start
        assert start["body"] == {"mic": key} and release["body"] == {"mic": key, "release": True}, keyed
        assert start["mic"] == "true" and start["focus"] and not start["hidden"], start
        assert start["at"] < release["at"] and start["status"] == release["status"] == 204, keyed
        if case == "memory":
            assert any(row["url"] == "/api/prepare-speech" and row["body"] == {} and row["status"] == 204
                       for row in traffic), traffic
    else:
        assert not keyed, traffic
    assert not any(row["url"] in {"/api/message", "/api/speak"} for row in traffic), traffic
    entry["keyed_preflight_requests"] = len(keyed)
    return keyed


async def check_case(playwright, moss, case, folder, base, recording, report, prepared, *, activity_starts=None):
    activity = activity_starts is not None
    starts_before = len(activity_starts) if activity else 0
    entry = report["cases"][case] = {
        "requests": [], "outside": [], "page_errors": [],
    }
    prep_before = len(prepared)
    turns_before = len(moss.state()["turns"])
    exe = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    browser = await playwright.chromium.launch(executable_path=str(exe), headless=True, args=[
        "--mute-audio", "--disable-gpu", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        f"--use-file-for-fake-audio-capture={recording}%noloop",
    ])
    try:
        await confirm_muted_edge(browser)
        context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
        page = await context.new_page()
        page.on("pageerror", lambda exc: entry["page_errors"].append(str(exc)))

        async def local_only(route):
            url = route.request.url
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and not url.startswith(base + "/"):
                entry["outside"].append(url)
                await route.abort()
            elif activity and parsed.path == "/api/prepare-voice":
                await route.fulfill(status=204)
            elif activity and parsed.path == "/api/transcribe":
                await route.fulfill(status=200, content_type="application/json", body='{"text":""}')
            elif activity and parsed.path in {"/api/message", "/api/speak", "/api/speak-batch"}:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", local_only)
        voice = "off" if case == "disabled" else "on"
        await page.add_init_script(PROBE + f"\nlocalStorage.setItem('moss.voice.enabled','{voice}');")
        await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(timeout=15000)
        await page.bring_to_front()
        await page.wait_for_function("document.hasFocus() && !document.hidden")
        if activity:
            state = await (await page.request.get(base + "/api/state")).json()
            assert state["speech_mic_warm"] is True and state["speech_mic_warm_on_activity"] is True
            assert state["model_id"] == ("local" if case == "local" else "deepseek"), state["model_id"]
            keyed = await check_activity(page, case, entry)
            entry["eligible_worker_starts"] = activity_starts[starts_before:]
            if case == "manual":
                assert entry["eligible_worker_starts"] == [keyed[0]["body"]["mic"]], entry
            else:
                assert not entry["eligible_worker_starts"], entry
            assert len(moss.state()["turns"]) == turns_before
            assert not entry["outside"] and not entry["page_errors"], entry
            return
        if case == "passive":
            await page.locator(".preferences summary").click()
            await page.locator("#wake-listen").check()
            await page.wait_for_function(
                "voiceInputQa.some(e=>e.url==='/api/transcribe' && e.status===200)", timeout=60000
            )
        else:
            await page.locator("#mic").click()
            await page.locator("#mic[aria-pressed=true]").wait_for(timeout=12000)
            if case == "short":
                await page.wait_for_function("""() => voiceInputQa.some(e=>e.url==='/api/transcribe' &&
                    e.status===200 && e.audioBytes>44)""", timeout=16000)
                await page.wait_for_timeout(300)
                assert not await page.evaluate("voiceInputQa.some(e=>e.url==='/api/message')")
            else:
                await page.locator('#local-status-panel[data-phase="complete"]').wait_for(timeout=85000)
                turn = moss.state()["turns"][-1]
                assert turn["source"] == "context" and turn["status"] == "replied", turn
                assert [item["kind"] for item in turn.get("local_overview", [])] == ["clock", "battery", "volume"], turn
                assert turn["cloud_requests"]["total"] == 0
                entry["recognized"] = turn["user"]
                if case == "manual":
                    await page.wait_for_function("voiceInputQa.some(e=>e.url==='end')", timeout=35000)
                else:
                    assert not await page.evaluate("voiceInputQa.some(e=>e.url==='/api/speak')")

        if case in {"short", "passive"}:
            assert len(moss.state()["turns"]) == turns_before
        entry["requests"] = await page.evaluate("voiceInputQa")
        preflight = [row for row in entry["requests"] if row["url"] == "/api/prepare-speech"]
        if case == "manual":
            preflight, = preflight
            assert preflight["body"] == {} and preflight["status"] == 204
            assert preflight["focus"] and not preflight["hidden"] and preflight["mic"] == "true"
            asr = next(row for row in entry["requests"] if row["url"] == "/api/transcribe")
            message = next(row for row in entry["requests"] if row["url"] == "/api/message")
            speak = next(row for row in entry["requests"] if row["url"] == "/api/speak")
            assert asr["status"] == speak["status"] == 200 and asr["text"]
            assert preflight["at"] < asr["at"] < message["at"] <= speak["at"]
            entry["prepare_before_asr_ms"] = round(asr["at"] - preflight["at"], 1)
            entry["prepare_before_speak_ms"] = round(speak["at"] - preflight["at"], 1)
        else:
            assert not preflight, entry
        assert len(prepared) - prep_before == (case == "manual")
        assert not entry["outside"] and not entry["page_errors"], entry
    finally:
        if "page" in locals():
            entry["requests"] = await page.evaluate("window.voiceInputQa ?? []")
            entry["status"] = await page.locator("#status").text_content()
        await browser.close()
        (folder / f"{case}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(question=QUESTION, cases=("manual", "disabled", "short", "passive"), *, activity=False):
    prefix = "spoken-speech-activity-guards" if activity else "spoken-speech-prepare-browser"
    folder = ROOT / "runs" / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "model_connections": 0, "question": question, "voice_worker": "schema checked, cached WAV fixture",
              "input": "Edge file microphone + actual CPU ASR including the clipped phoneme", "cases": {}}
    if activity:
        report.update(voice_worker="real backend eligibility and intercepted weight load; no GPU worker",
                      input="Edge file microphone; intercepted ASR")
    recordings = folder / "microphone"
    recordings.mkdir()
    spoken = make_voice(recordings, text=question)
    short = recordings / "brief-noise.wav"
    short_capture(spoken, short)
    silent = recordings / "silence.wav"
    if activity:
        with wave.open(str(silent), "wb") as recording:
            recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            recording.writeframes(bytes(16000 * 2 * 8))
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="1", MOSS_SPEECH_STANDBY="0", CUDA_VISIBLE_DEVICES="",
                      MOSS_SPEECH_MIC_WARM="activity" if activity else "0")
    prepared = []
    activity_starts = [] if activity else None
    limits = SimpleNamespace(available=12 * GIB, available_pagefile=12 * GIB)

    async def prepare_without_weights(_speaker):
        prepared.append(True)

    async def intercept_weight_start(_speaker, key, eligible, prime):
        assert eligible() and prime is False
        activity_starts.append(key)
        return True

    async def play_fixture(_speaker, text, disconnected, category="reply", cached_only=False, *, timings=None):
        assert text and not disconnected() and category == "reply"
        return (ROOT / "voice/lines/done.wav").read_bytes()

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
            if activity:
                profiles = {key: Profile(key, label, model_url, model, SecretStr("fixture" if key == "deepseek" else ""))
                            for key, (label, _, __, model) in SPECS.items()}
                with patch("core.profiles", return_value=profiles):
                    moss = Moss(folder / "data", model_url, "isolated-unused", prefer_cloud=True)
            else:
                moss = Moss(folder / "data", model_url, "isolated-unused")
            port = free_port()
            app = application(moss, lambda: "standby", folder / "data", port, "isolated", asyncio.Event())
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                base = f"http://127.0.0.1:{port}"
                with (patch.object(MossSpeech, "prepare_imports", prepare_without_weights),
                      patch.object(MossSpeech, "render", play_fixture),
                      patch.object(MossSpeech, "_load_for_microphone", intercept_weight_start),
                      patch("voice_output.memory_sample", return_value=limits)):
                    async with async_playwright() as playwright:
                        for case in cases:
                            limits.available = (11 if case == "memory" else 12) * GIB
                            if case == "local":
                                await moss.select_model("local")
                            recording = {"short": short, "silence": silent}.get(case, spoken)
                            await check_case(playwright, moss, case, folder, base, recording, report, prepared,
                                             activity_starts=activity_starts)
                assert not report["model_connections"], report
                if not activity:
                    assert len(prepared) == int("manual" in cases), report
                report["passed"] = True
            finally:
                await runner.cleanup()
                await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default=QUESTION, help="时间、电量和音量的语音测试原句")
    parser.add_argument("--case", action="append", choices=("manual", "disabled", "short", "passive"))
    parser.add_argument("--activity-guards", action="store_true", help="口语活动租约回归，不启动识别或语音推理进程")
    args = parser.parse_args()
    if args.activity_guards:
        asyncio.run(run(args.question, ("silence", "short", "disabled", "media", "manual", "memory", "local"), activity=True))
    else:
        asyncio.run(run(args.question, tuple(dict.fromkeys(args.case))) if args.case else run(args.question))
