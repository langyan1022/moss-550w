"""Compare automatic reply playback using an existing OmniVoice WAV in muted Edge."""

import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from statistics import median
from struct import pack_into
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_speech_chat_prepare import first_audible
from verify_speech_long_reading import PROBE
from verify_spoken_speech_prepare_ab import ACTIVITY_QUESTION, battery, volume
from voice_output import MossSpeech


FIXTURE = ROOT / "runs/spoken-speech-activity-ab-20261122-095243-926154/2-activity-cuda"
DEPLOYED = ROOT / "static/agent-speech.js"
FIXTURE_RESPONSE_DELAY_MS = 120
PCM_PROBE = """
const silentHeadBuffers = [];
const startForSilentHead = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...args) {
  const started = startForSilentHead.apply(this, args);
  silentHeadBuffers.push(this.buffer);
  return started;
};
window.silentHeadSnapshot = async () => {
  const trace = collectReadingMetrics();
  const clips = await Promise.all(silentHeadBuffers.map(async (audio, index) => {
    const channels = Array.from({length:audio.numberOfChannels}, (_, i) => audio.getChannelData(i));
    let zeroFrames = 0;
    while (zeroFrames < audio.length && channels.every(channel => channel[zeroFrames] === 0)) zeroFrames++;
    const decoded = new Uint8Array(audio.numberOfChannels * audio.length * 4);
    channels.forEach((channel, i) => decoded.set(new Uint8Array(
      channel.buffer, channel.byteOffset, channel.byteLength), i * audio.length * 4));
    const digest = await crypto.subtle.digest("SHA-256", decoded);
    const playing = trace.clips[index];
    return {...playing, channels:audio.numberOfChannels, sample_rate:audio.sampleRate,
      zero_frames:zeroFrames, offset_frames:Math.round(playing.offset * audio.sampleRate),
      decoded_sha256:Array.from(new Uint8Array(digest), byte=>byte.toString(16).padStart(2,"0")).join("")};
  }));
  return {...trace, clips};
};
"""


def read_fixture():
    record = json.loads((FIXTURE / "result.json").read_text(encoding="utf-8"))
    assert record["enabled"] and record["speech_request"]["category"] == "reply", record
    original = (FIXTURE / "reply.wav").read_bytes()
    assert sha256(original).hexdigest() == record["sha256"], "录制文件与原报告不一致"
    with wave.open(BytesIO(original), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth()) == (2, 2)
        params = source.getparams()
        pcm = bytearray(source.readframes(source.getnframes()))
    # A right-channel 1 LSB sample tests the strict all-zeros rule without changing the original.
    marker_frame = params.framerate // 100
    marker_offset = marker_frame * params.nchannels * params.sampwidth + params.sampwidth
    assert not any(pcm[:marker_offset + params.sampwidth])
    pack_into("<h", pcm, marker_offset, 1)
    guard = BytesIO()
    with wave.open(guard, "wb") as target:
        target.setparams(params)
        target.writeframes(pcm)
    return record, original, guard.getvalue()


async def trial(browser, server, variant, asset, scenario, audio, expected, report):
    source = "narration" if scenario == "narration" else "context"
    text = expected["speech_request"]["text"]
    prompt = f"朗读以下文字：[pause 220ms]{text}" if scenario == "narration" else ACTIVITY_QUESTION
    context = await browser.new_context(reduced_motion="reduce", service_workers="block")
    page = await context.new_page()
    base = urlsplit(server.url)
    row = {"variant":variant, "scenario":scenario, "speech_requests":[], "blocked":[], "page_errors":[],
           "agent_speech_sha256":sha256(asset).hexdigest()}
    report["trials"].append(row)
    responses = []
    page.on("pageerror", lambda error: row["page_errors"].append(str(error)))
    page.on("response", lambda response: responses.append(response)
            if response.url == server.url + "/api/speak" else None)

    async def isolate(route):
        request = route.request
        target = urlsplit(request.url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            row["blocked"].append(request.url)
            return await route.abort()
        if target.path == "/static/agent-speech.js":
            return await route.fulfill(content_type="text/javascript", body=asset)
        if target.path == "/api/speak":
            received = request.post_data_json
            row["speech_requests"].append(received)
            if received != expected["speech_request"]:
                return await route.fulfill(status=400, json={"error":"播报内容与固定录音不符"})
            # Let the output device establish its clock in every arm.
            await asyncio.sleep(FIXTURE_RESPONSE_DELAY_MS / 1000)
            return await route.fulfill(content_type="audio/wav", body=audio, headers={"X-MOSS-Batch-Capable":"0"})
        if target.path in {"/api/prepare-speech", "/api/speak-batch", "/api/transcribe", "/api/prepare-voice"}:
            row["blocked"].append(target.path)
            return await route.abort()
        return await route.continue_()

    try:
        await context.route("**/*", isolate)
        await page.add_init_script("localStorage.setItem('moss.voice.enabled','on');" + PROBE + PCM_PROBE)
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(timeout=12000)
        async with page.expect_response(server.url + "/api/new-chat", timeout=5000) as created:
            await page.get_by_test_id("new-chat").click()
        assert (await created.value).ok
        await page.get_by_test_id("composer").fill(prompt)
        await page.get_by_test_id("send").click()
        await page.wait_for_function("readingFinished()", timeout=15000)
        trace = row["trace"] = await page.evaluate("silentHeadSnapshot()")
        turn, = (await server.state())["turns"]
        assert turn["user"] == prompt and turn["source"] == source and turn["status"] == "replied", turn
        assert turn["reply"] == (text if scenario == "narration" else expected["reply"]), turn
        assert turn["cloud_requests"]["total"] == 0, turn
        assert row["speech_requests"] == [expected["speech_request"]], row
        assert not row["blocked"] and not row["page_errors"], row
        response, = responses
        row["response_sha256"] = sha256(await response.body()).hexdigest()
        assert response.ok and row["response_sha256"] == sha256(audio).hexdigest(), row
        clip, = trace["clips"]
        started, = [event for event in trace["events"] if event["type"] == "start"]
        # The display puts separate readings on separate lines; synthesis joins the same words.
        assert started["text"].replace("\n", "") == text.replace("\n", ""), trace
        assert trace["events"][-1]["type"] == "end", trace
        assert clip["channels"] == 2, clip
        first, output_clock = first_audible(clip, trace["submitted"])
        assert first > 0 and output_clock == "web_audio_output_timestamp", (output_clock, clip)
        row.update(first_pcm_ms=first, pcm_clock=output_clock,
                   first_pcm_from_schedule_ms=round((clip["first"]-clip["start"]) * 1000, 1),
                   applied_offset_ms=round(clip["offset"] * 1000, 3),
                   exact_zero_head_ms=round(clip["zero_frames"] * 1000 / clip["sample_rate"], 3),
                   cloud_requests=0)
        if scenario == "ordinary" and variant == "candidate":
            assert 20 <= row["applied_offset_ms"] <= 120, row
            minimum = round(.012 * clip["sample_rate"])
            assert clip["zero_frames"] - clip["offset_frames"] >= minimum, clip
        else:
            assert clip["offset_frames"] == 0, row
        if scenario == "narration":
            pending = next(item for item in trace["events"] if item["type"] == "pending")
            assert started["at"] - pending["at"] >= 200, trace["events"]
        if scenario == "early_nonzero":
            assert row["exact_zero_head_ms"] < 20, row
        print(variant, scenario, row["applied_offset_ms"], row["first_pcm_ms"], flush=True)
    finally:
        await context.close()


def comparison(report):
    ordinary = [row for row in report["trials"] if row["scenario"] == "ordinary"]
    assert len({row["response_sha256"] for row in ordinary}) == 1
    assert len({row["trace"]["clips"][0]["decoded_sha256"] for row in ordinary}) == 1
    assert all(row["speech_requests"] == ordinary[0]["speech_requests"] for row in ordinary)
    by_variant = {variant:[row for row in ordinary if row["variant"] == variant] for variant in ("baseline", "candidate")}
    result = report["ordinary"] = {}
    for metric in ("first_pcm_ms", "first_pcm_from_schedule_ms"):
        arm = {variant:round(median(row[metric] for row in rows), 1) for variant, rows in by_variant.items()}
        result[metric] = {**arm, "saved":round(arm["baseline"] - arm["candidate"], 1)}
    offset = median(row["applied_offset_ms"] for row in by_variant["candidate"])
    # The probe scans in 10 ms RMS windows whose start moves along with the decoded offset.
    assert abs(result["first_pcm_from_schedule_ms"]["saved"] - offset) <= 12, result
    result["paired_submit_to_first_pcm_savings_ms"] = [
        round(before["first_pcm_ms"] - after["first_pcm_ms"], 1)
        for before, after in zip(by_variant["baseline"], by_variant["candidate"], strict=True)]


async def run(baseline, candidate, pair):
    folder = ROOT / "runs" / datetime.now().strftime("speech-silent-head-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0",
                      MOSS_SPEECH_MIC_WARM="0", CUDA_VISIBLE_DEVICES="")
    report = {"passed":False, "fixture":str(FIXTURE / "reply.wav"), "trials":[],
              "fixture_response_delay_ms":FIXTURE_RESPONSE_DELAY_MS,
              "model_connections":0, "unexpected_speech_calls":0}
    metadata, original, marker = read_fixture()
    assets = {"baseline":baseline.read_bytes(), "candidate":candidate.read_bytes()}
    assert assets["baseline"] != assets["candidate"], "基线与候选脚本完全相同"
    order = ("baseline", "candidate") if pair else ("baseline", "candidate", "candidate", "baseline")

    async def reject_cloud(_reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def reject_speech(*_args, **_kwargs):
        report["unexpected_speech_calls"] += 1
        raise AssertionError("播放对照仅使用浏览器提供的既有录音")

    try:
        async with await asyncio.start_server(reject_cloud, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            server = BrowserServer(folder, free_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "isolated-unused", prefer_cloud=False)
            app = application(moss, lambda:"standby", server.data, server.port, "isolated", asyncio.Event())
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", server.port).start()
                with (patch("local_overview.device_status", battery), patch("local_overview.run_desktop", volume),
                      patch.object(MossSpeech, "render", reject_speech)):
                    async with async_playwright() as playwright:
                        edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                        browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                            args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                                  "--disable-renderer-backgrounding"])
                        try:
                            await confirm_muted_edge(browser)
                            for variant in order:
                                await trial(browser, server, variant, assets[variant], "ordinary", original, metadata, report)
                            for scenario, audio in (("narration", original), ("early_nonzero", marker)):
                                await trial(browser, server, "candidate", assets["candidate"], scenario, audio, metadata, report)
                        finally:
                            await browser.close()
            finally:
                await runner.cleanup()
                await moss.close()
        assert report["model_connections"] == report["unexpected_speech_calls"] == 0, report
        comparison(report)
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        print(folder, report["passed"], flush=True)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="以同一份立体声音频对照短答首声与静音跳过边界。")
    parser.add_argument("--baseline", type=Path, default=DEPLOYED)
    parser.add_argument("--candidate", type=Path, required=True, help="候选 agent-speech.js 的路径")
    parser.add_argument("--pair", action="store_true", help="先跑一组前后对照，默认运行四轮 ABBA")
    args = parser.parse_args()
    asyncio.run(run(args.baseline.resolve(), args.candidate.resolve(), args.pair))
