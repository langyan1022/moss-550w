import asyncio
from datetime import datetime
import hashlib
import json
import os

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_speech_continuity import MEASURE


FIRST = "不客气。"
SECOND = "好，先到这里。"
READ = "朗读下面这段话：[pause 200ms]" + FIRST + "[pause 1.5s][pause 500ms]" + SECOND + "[pause 200ms]"


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("narration-timing-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "model_connections": 0, "wav": []}
    os.environ["MOSS_WINDOWS_REMINDERS"] = "0"

    async def reject(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    sink = await asyncio.start_server(reject, "127.0.0.1", 0)
    port = sink.sockets[0].getsockname()[1]
    server = BrowserServer(folder, 18969, model_url=f"http://127.0.0.1:{port}/v1")
    try:
        async with sink:
            await server.start()
            for marker in ("[pause 11s]", "[pause 500]", "[pause 0ms]", "[pause 40ms"):
                status, error = await server.post("/api/message", {"text": "朗读以下文字：" + FIRST + marker + SECOND})
                assert status == 409 and "停顿" in error["error"], (status, error)
            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                try:
                    await confirm_muted_edge(browser)
                    page = await browser.new_page(reduced_motion="reduce")
                    await page.add_init_script(MEASURE)
                    responses = []
                    page.on("response", lambda response: responses.append(response) if response.url.endswith("/api/speak") else None)
                    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                    await page.get_by_test_id("composer").fill(READ)
                    await page.get_by_test_id("send").click()
                    await page.wait_for_function("speechTest.events.some(e=>e.type==='end'||e.type==='error')", timeout=20000)
                    measured = await page.evaluate("({clips:speechTest.clips,events:speechTest.events,requests:speechTest.requests})")
                    clips, events = measured["clips"], measured["events"]
                    assert len(clips) == 2 and all(clip["first"] is not None for clip in clips), measured
                    assert [row["text"] for row in measured["requests"]] == [FIRST, SECOND]
                    assert events[-1]["type"] == "end"
                    starts = [event for event in events if event["type"] == "start"]
                    wall_gap = 1000 * (clips[1]["start"] - clips[0]["end"])
                    lead = starts[0]["ms"] - events[0]["ms"]
                    tail = events[-1]["ms"] - (clips[1]["scheduled"] + clips[1]["duration"]*1000)
                    assert 1960 <= wall_gap <= 2400 and lead >= 180 and tail >= 170, (wall_gap, lead, tail)
                    for response, original in zip(responses, ("welcome.wav", "done.wav"), strict=True):
                        assert response.status == 200 and response.headers.get("content-type") == "audio/wav"
                        wave = await response.body()
                        assert wave == (ROOT / "voice/lines" / original).read_bytes()
                        report["wav"].append(hashlib.sha256(wave).hexdigest())
                    turn = (await server.state())["turns"][-1]
                    assert turn["source"] == "narration" and turn["reply"] == FIRST + SECOND
                    assert turn["narration"]["cues"] == [
                        {"pause_ms": 200}, {"text": FIRST}, {"pause_ms": 2000}, {"text": SECOND}, {"pause_ms": 200}]
                    report.update(line_gap_ms=round(wall_gap, 1), leading_ms=round(lead, 1), trailing_ms=round(tail, 1),
                                  prefetch_before_pause=measured["requests"][1]["ms"] < clips[0]["scheduled"] + 1000*clips[0]["duration"])
                    assert report["prefetch_before_pause"]

                    await page.get_by_test_id("composer").fill("再念一遍")
                    await page.get_by_test_id("send").click()
                    await page.wait_for_function("speechTest.clips.length === 3")
                    await page.wait_for_function("document.querySelector('#speaker-state').textContent==='停顿'")
                    await page.locator("#interrupt").click()
                    await page.wait_for_timeout(2500)
                    assert not await page.evaluate("mossSpeech.active()")
                    assert await page.evaluate("speechTest.clips.length") == 3
                    repeat = (await server.state())["turns"][-1]
                    assert repeat["source"] == "readback" and repeat["readback_of"] == turn["id"]
                    report.update(repeated_then_interrupted_silence=True, passed=True)
                finally:
                    await browser.close()
            assert report["model_connections"] == 0
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
