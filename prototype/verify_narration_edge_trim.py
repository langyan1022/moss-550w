import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

from playwright.async_api import Error, async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_context_reply import free_port
from verify_speech_batch import SAMPLES
from verify_speech_long_reading import PROBE


FIXTURES = ROOT / "runs/speech-batch-http-20261120-213421-720853"
DEPLOYED = ROOT / "static/agent-speech.js"
PROSE = SAMPLES["reply"]
VERSE = SAMPLES["verse"]
PROSE_TEXT = "[pause 200ms]" + "\n\n".join(PROSE) + "[pause 1200ms]" + "\n\n".join(PROSE) + "[pause 200ms]"
BATCH_PROBE = """
window.qaBatch = {aborted:false, stopped:false};
const beforeBatchFaultFetch = window.fetch;
window.fetch = function(url, options) {
  if (url === '/api/speak-batch')
    options.signal.addEventListener('abort', () => { qaBatch.aborted = true; }, {once:true});
  return beforeBatchFaultFetch.apply(this, arguments);
};
window.addEventListener('moss-speech-stop', () => { qaBatch.stopped = true; });
"""
BATCH_PLAYED_DIGESTS = """
qaBatch.played = [];
const wavDigests = new WeakMap();
const decodeFixtureWav = BaseAudioContext.prototype.decodeAudioData;
BaseAudioContext.prototype.decodeAudioData = async function(bytes) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const audio = await decodeFixtureWav.call(this, bytes);
  wavDigests.set(audio, [...digest].map(value => value.toString(16).padStart(2, '0')).join(''));
  return audio;
};
const playFixtureWav = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...args) {
  qaBatch.played.push(wavDigests.get(this.buffer) ?? null);
  return playFixtureWav.apply(this, args);
};
"""


def wav_files():
    log = json.loads((FIXTURES / "result.json").read_text(encoding="utf-8"))
    assert log["passed"]
    fixture_digests = {item["category"]:item["sha256"] for item in log["requests"] if item["case"] == "two-new"}
    waves = {}
    for category in ("reply", "verse"):
        for index, text in enumerate(SAMPLES[category]):
            audio = (FIXTURES / f"{category}-{index}.wav").read_bytes()
            assert sha256(audio).hexdigest() == fixture_digests[category][index]
            waves[category, text] = audio
    return waves


async def trial(browser, server, variant, source, waves, category, *, batch_fault=None):
    context = await browser.new_context(reduced_motion="reduce", service_workers="block")
    page = await context.new_page()
    reports = {"category":category, "request_plan":[], "wav_sha256":[], "page_errors":[], "blocked":[]}
    pairs = []
    pair_requested, release_pair, pair_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    if batch_fault:
        reports.update(batch_fault=batch_fault, pair_requests=pairs)
    arrivals = []
    page.on("pageerror", lambda error: reports["page_errors"].append(str(error)))
    page.on("response", lambda response: arrivals.append(response) if response.url.endswith("/api/speak") else None)
    base = urlsplit(server.url)

    async def route_request(route):
        request = route.request
        target = urlsplit(request.url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            reports["blocked"].append(target.netloc)
            return await route.abort()
        if target.path == "/static/agent-speech.js" and variant == "original":
            return await route.fulfill(status=200, content_type="text/javascript", body=source)
        if target.path == "/api/speak":
            body = request.post_data_json
            speech = (body["category"], body["text"])
            reports["request_plan"].append(list(speech))
            if speech not in waves:
                reports["blocked"].append([target.path, *speech])
                return await route.abort()
            return await route.fulfill(status=200, content_type="audio/wav", body=waves[speech],
                                       headers={"X-MOSS-Batch-Capable":"1" if batch_fault else "0"})
        if target.path == "/api/speak-batch" and batch_fault:
            body = request.post_data_json
            pairs.append(body)
            pair_requested.set()
            if batch_fault in {"complete_pair", "missing_part"}:
                boundary = "moss-batch-test"
                texts = body["texts"] if batch_fault == "complete_pair" else body["texts"][:1]
                multipart = b"".join(
                    (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{index}\"; filename=\"{index}.wav\"\r\n"
                     "Content-Type: audio/wav\r\n\r\n").encode("ascii") + waves[body["category"], text] + b"\r\n"
                    for index, text in enumerate(texts)
                ) + f"--{boundary}--\r\n".encode("ascii")
                return await route.fulfill(status=200, content_type=f"multipart/form-data; boundary={boundary}",
                                           body=multipart, headers={"X-MOSS-Batch-Capable":"1"})
            try:
                if batch_fault == "cancelled":
                    await asyncio.wait_for(release_pair.wait(), 10)
                return await route.fulfill(status=503, json={"error":"隔离验收模拟批量合成故障"})
            except Error:
                if batch_fault != "cancelled" or not release_pair.is_set():
                    raise
            finally:
                pair_finished.set()
        if target.path in ("/api/prepare-speech", "/api/speak-batch", "/api/transcribe"):
            reports["blocked"].append(target.path)
            return await route.abort()
        await route.continue_()

    try:
        await context.route("**/*", route_request)
        await page.add_init_script(PROBE + (BATCH_PROBE if batch_fault else "") +
                                   (BATCH_PLAYED_DIGESTS if batch_fault == "complete_pair" else ""))
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        if batch_fault:
            expected = list(reversed(PROSE)) * 3
            script = "朗读以下文字：" + "\n\n".join(expected)
        else:
            expected = PROSE * 2 if category == "reply" else VERSE
            script = "朗读以下文字：" + PROSE_TEXT if category == "reply" else "朗诵以下文字：" + "\n\n".join(VERSE)
        await page.get_by_test_id("composer").fill(script)
        await page.get_by_test_id("send").click()
        if batch_fault == "cancelled":
            await asyncio.wait_for(pair_requested.wait(), 8)
            await page.wait_for_function("collectReadingMetrics().events.some(e => e.type === 'start')")
            await page.locator("#interrupt").click()
            await page.wait_for_function("qaBatch.stopped && qaBatch.aborted")
            release_pair.set()
            await asyncio.wait_for(pair_finished.wait(), 5)
            await page.wait_for_timeout(300)
            reports["trace"] = await page.evaluate("collectReadingMetrics()")
            assert pairs == [{"category":category,"texts":expected[1:3]}], pairs
            assert reports["request_plan"] == [[category, expected[0]]]
            assert len(reports["trace"]["clips"]) == 1 and not await page.evaluate("mossSpeech.active()"), reports
            assert not reports["page_errors"] and not reports["blocked"], reports
            return reports
        await page.wait_for_function("readingFinished()", timeout=40000)
        trace = reports["trace"] = await page.evaluate("collectReadingMetrics()")
        turn = (await server.state())["turns"][-1]
        assert turn["source"] == "narration" and turn["narration"]["category"] == category, turn
        clips = trace["clips"]
        utterances = [row["text"].strip() for row in trace["events"] if row["type"] == "start"]
        assert utterances == expected and trace["events"][-1]["type"] == "end", (utterances, trace["events"])
        assert not reports["page_errors"] and not reports["blocked"], reports
        singles = [expected[0], expected[-1]] if batch_fault == "complete_pair" else expected
        assert reports["request_plan"] == [[category,text] for text in singles], reports["request_plan"]
        if batch_fault:
            pairs_expected = [expected[1:3], expected[3:5]] if batch_fault == "complete_pair" else [expected[1:3]]
            assert pairs == [{"category":category,"texts":texts} for texts in pairs_expected], pairs
            assert sum(row["type"] == "end" for row in trace["events"]) == 1, trace
        reports["wav_sha256"] = [sha256(await response.body()).hexdigest() for response in arrivals]
        assert reports["wav_sha256"] == [sha256(waves[category,text]).hexdigest() for text in singles]
        if batch_fault == "complete_pair":
            reports["played_wav_sha256"] = await page.evaluate("qaBatch.played")
            assert reports["played_wav_sha256"] == [sha256(waves[category,text]).hexdigest() for text in expected], reports
        assert len(clips) == len(expected) and all(clip["first"] is not None for clip in clips)
        reports.update(
            gaps_ms=[round((b["first"]-a["last"])*1000,1) for a,b in zip(clips,clips[1:])],
            schedule_gaps_ms=[round((b["start"]-a["end"])*1000,1) for a,b in zip(clips,clips[1:])],
            leading_ms=round(trace["events"][1]["at"]-trace["events"][0]["at"],1),
            trailing_ms=round(trace["events"][-1]["at"]-
                (clips[-1]["scheduled"] + (clips[-1]["end"]-clips[-1]["start"])*1000),1),
        )
        if category == "reply" and not batch_fault:
            assert 1150 <= reports["schedule_gaps_ms"][1] <= 1600, reports
            assert reports["leading_ms"] >= 175 and reports["trailing_ms"] >= 165, reports
        return reports
    finally:
        release_pair.set()
        await context.close()


def compare(report):
    baseline, changed = report["arms"]["original"], report["arms"]["current"]
    for category in ("reply", "verse"):
        before, after = baseline[category], changed[category]
        assert before["wav_sha256"] == after["wav_sha256"]
        old, new = before["trace"]["clips"], after["trace"]["clips"]
        if category == "verse":
            assert all(not clip["offset"] and not clip["stops"] for clip in [*old, *new]), (old, new)
        else:
            # First start, last tail, and both edges of the marked pause stay at the original buffer boundaries.
            assert not old[0]["offset"] and not new[0]["offset"] and not old[-1]["stops"] and not new[-1]["stops"]
            assert not old[1]["stops"] and not new[1]["stops"] and not old[2]["offset"] and not new[2]["offset"]
            assert any(new[i]["stops"] or new[i+1]["offset"] > 0 for i in (0, 2)), new
            saved = [round(before["gaps_ms"][i]-after["gaps_ms"][i],1) for i in (0,2)]
            report["natural_gap_reductions_ms"] = saved
            report["median_natural_gap_reduction_ms"] = median(saved)
            assert min(saved) >= 70, saved
            assert abs(before["gaps_ms"][1]-after["gaps_ms"][1]) <= 150, (before["gaps_ms"], after["gaps_ms"])


async def verify(baseline, batch_recovery=False):
    original = baseline.read_bytes() if baseline else None
    if original is not None:
        assert original != DEPLOYED.read_bytes(), "基准脚本与当前脚本完全相同"
    prefix = "narration-batch-recovery" if batch_recovery else "narration-edge-trim"
    folder = ROOT / "runs" / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed":False, "fixture":str(FIXTURES), "arms":{}, "model_connections":0,
              "browser_audio_muted":True, "file_hashes":{"original":sha256(original).hexdigest() if original else None,
                "current":sha256(DEPLOYED.read_bytes()).hexdigest()}}
    print(folder, flush=True)

    async def reject_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    catalog = folder / "media.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_KEV_ENABLED="0",
                      MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0", MOSS_MEDIA_DB=str(catalog))
    sink = await asyncio.start_server(reject_model, "127.0.0.1", 0)
    server = BrowserServer(folder, free_port(), model_url=f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1")
    try:
        async with sink:
            waves = wav_files()
            await server.start()
            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                try:
                    await confirm_muted_edge(browser)
                    if batch_recovery:
                        variant = "original" if original else "current"
                        for fault in ("503", "missing_part", "cancelled", "complete_pair"):
                            report["arms"][fault] = await trial(browser, server, variant, original, waves, "reply",
                                                                batch_fault=fault)
                    else:
                        for variant in ("original","current"):
                            arms = report["arms"][variant] = {}
                            for category in ("reply","verse"):
                                arms[category] = await trial(browser, server, variant, original, waves, category)
                finally:
                    await browser.close()
        if not batch_recovery:
            compare(report)
        assert not report["model_connections"]
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        try:
            await server.stop()
        finally:
            result = folder / "result.json"
            result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(result, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="用同一批既有 WAV 对照浏览器读段收尾与显式停顿。")
    parser.add_argument("--baseline", type=Path, help="改动前保存的 agent-speech.js")
    parser.add_argument("--batch-recovery", action="store_true", help="用既有 WAV 测试批量故障降级和取消")
    args = parser.parse_args()
    if not (args.baseline or args.batch_recovery):
        parser.error("请提供 --baseline 或 --batch-recovery")
    asyncio.run(verify(args.baseline, batch_recovery=args.batch_recovery))
