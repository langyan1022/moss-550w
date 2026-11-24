import asyncio
from datetime import datetime
from email import policy
from email.parser import BytesParser
from io import BytesIO
import json
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send
from verify_speech_continuity import MEASURE


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("speech-batch-page-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18637, model_url="http://127.0.0.1:9/v1")
    report = {"passed":False, "page_errors":[]}
    await server.start()
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script(MEASURE)
                page.on("pageerror", lambda err: report["page_errors"].append(str(err)))
                responses = []
                page.on("response", lambda res: responses.append(res) if "/api/speak" in res.url else None)
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                passages = ["清晨沿着河岸走到桥头，树影刚好落在青石板上。",
                            "过了桥，往左边走，可以看见一座白色的亭子。",
                            "等夕阳落进水面，我们沿原路回家。"]
                await send(page, server, "记住：" + "\n\n".join(passages))
                await page.wait_for_function("speechTest.events.some(e=>e.type==='end'||e.type==='error')", timeout=180000)
                clips = await page.evaluate("speechTest.clips")
                events = await page.evaluate("speechTest.events")
                assert events[-1]["type"] == "end" and len(clips) == 3, events
                assert [r.url.removeprefix(server.url) for r in responses] == ["/api/speak","/api/speak-batch"]
                assert all(r.status == 200 for r in responses)
                assert responses[0].headers.get("x-moss-batch-capable") == "1"
                pair = responses[-1]
                assert pair.request.post_data_json["texts"] == passages[1:]
                content_type = pair.headers["content-type"].encode("ascii")
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: "+content_type+b"\r\nMIME-Version: 1.0\r\n\r\n"+await pair.body())
                durations = []
                for i, part in enumerate(message.iter_parts()):
                    assert part.get_param("name", header="content-disposition") == str(i)
                    with wave.open(BytesIO(part.get_payload(decode=True))) as wav:
                        durations.append(wav.getnframes() / wav.getframerate())
                assert len(durations) == 2
                for actual, expected in zip(clips[1:],durations):
                    assert abs(actual["duration"]-expected) < .01 and actual["first"] is not None
                starts = [e["text"].strip() for e in events if e["type"] == "start"]
                assert [s.removeprefix("已记住：") for s in starts] == passages, starts
                mode = pair.headers.get("x-moss-synthesis")
                assert mode in ("batch", "serial")
                report.update(mode=mode, first_batch_capable=True, starts=starts, durations=durations,
                              gaps_ms=[round(1000*(b["start"]-a["end"]),1) for a,b in zip(clips,clips[1:])],
                              audible_gaps_ms=[round(1000*(b["first"]-a["last"]),1) for a,b in zip(clips,clips[1:])])
                def is_pair(request):
                    return request.url.endswith("/api/speak-batch")

                async with page.expect_event("requestfailed", predicate=is_pair, timeout=90000) as cancelled:
                    async with page.expect_request(is_pair, timeout=90000):
                        await send(page, server, "记住：" + "\n\n".join([passages[0],
                            "昨晚落过雨，巷口的桂花开了。", "趁天还早，我们先到街上散步吧。"]))
                    await page.locator("#interrupt").click()
                failure = (await cancelled.value).failure
                assert failure and "ABORT" in failure.upper(), failure
                count = await page.evaluate("speechTest.clips.length")
                assert count == 4, count
                # 等到当前音频原应播完以后，再检查下一段有没有偷偷出声。
                last_duration = await page.evaluate("speechTest.clips.at(-1).duration")
                await page.wait_for_timeout(1000*(last_duration + 1))
                assert not await page.evaluate("mossSpeech.active()")
                assert await page.evaluate("speechTest.clips.length") == count
                assert not report["page_errors"]
                report.update(passed=True, interrupted=True, cancelled_request=failure)
            finally:
                await browser.close()
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report.get("mode"), report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
