import asyncio
from datetime import datetime
import json
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("everyday-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18485)
    report = {"passed": False, "short_turns": []}
    await server.start()
    try:
        for text in ("哎", "MOSS，在吗？", "谢谢！", "算了。"):
            started = perf_counter()
            status, state = await server.post("/api/message", {"text": text})
            turn = state["turns"][-1]
            report["short_turns"].append({
                "text": text, "reply": turn["reply"], "status": turn["status"],
                "source": turn.get("source"), "ms": round((perf_counter() - started) * 1000, 1),
            })
            if turn["status"] == "thinking":
                await server.post("/api/cancel", {"turn_id": turn["id"]})
            assert status == 200 and turn["status"] == "replied" and turn["source"] == "smalltalk", report
            assert turn["reply"] and not any(mark in turn["reply"] for mark in "?？")
            assert not turn.get("media") and not turn.get("receipt")

        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                await page.evaluate("""() => {
                    window.trackEvents = [];
                    for (const name of ['playing', 'pause', 'ended', 'error', 'emptied']) {
                        document.addEventListener(name, event => {
                            const a = event.target;
                            if (a.matches?.('audio.media-audio'))
                                trackEvents.push({name, seconds:a.currentTime,
                                    duration:Number.isFinite(a.duration) ? a.duration : null, error:a.error?.message});
                        }, true);
                    }
                }""")
                await send(page, server, "记住：请叫我星河")
                assert (await server.state())["turns"][-1]["source"] == "memory"
                await send(page, server, "我想听刘德华的歌")
                await page.wait_for_function("""() => Array.from(document.querySelectorAll('.media-audio'))
                    .some(a => !a.paused && a.currentTime > .2)""")
                current_track = """() => {
                    const a = Array.from(document.querySelectorAll('.media-audio')).find(a=>!a.paused);
                    return a ? {id:a.dataset.mediaId,seconds:a.currentTime} : null;
                }"""
                before = await page.evaluate(current_track)
                await send(page, server, "算了")
                same_track_advancing = """previous => Array.from(document.querySelectorAll('.media-audio'))
                    .some(a=>!a.paused && a.dataset.mediaId === previous.id && a.currentTime > previous.seconds + .3)"""
                await page.wait_for_function(same_track_advancing, arg=before)
                report["same_track_after_dismissal"] = True

                await page.locator(".preferences summary").click()
                await page.get_by_role("checkbox", name="MOSS 语音播报").check()
                await page.locator(".preferences summary").click()
                await page.evaluate("""() => {
                    window.draftSpeech = {};
                    for (const name of ['pending', 'stop']) {
                        window.addEventListener('moss-speech-' + name, e=>{draftSpeech[name] = e.detail.id;});
                    }
                }""")
                await page.get_by_test_id("composer").fill("保存一份本地草稿，内容逐字写：明天再听。")
                await page.get_by_test_id("send").click()
                draft = page.get_by_test_id("turn").last
                await draft.locator("[data-testid=preview]").wait_for(timeout=180000)
                draft_id = await draft.get_attribute("data-turn-id")
                await page.wait_for_function("id => window.draftSpeech.pending === id", arg=draft_id)
                status, _ = await server.post("/api/message", {"text": "谢谢"})
                assert status == 409 and (await server.state())["turns"][-1]["id"] == draft_id
                playing_before_cancel = await page.evaluate(current_track)
                report["track_before_cancel"] = await page.evaluate("trackEvents")
                assert playing_before_cancel is not None and playing_before_cancel["id"] == before["id"]
                await page.get_by_test_id("composer").fill("算了。")
                await page.get_by_test_id("send").click(timeout=5000)
                await page.wait_for_function("document.querySelector('#turns').lastElementChild.dataset.status === 'cancelled'")
                last = (await server.state())["turns"][-1]
                assert last["status"] == "cancelled" and last["receipt"] is None
                assert not (server.data / "drafts" / (last["id"] + ".txt")).exists()
                report["draft_cancelled_without_write"] = True
                await page.wait_for_function("""id => window.draftSpeech.stop === id && !window.mossSpeech.active() &&
                    document.getElementById('speaker-state').textContent === '待机'""", arg=draft_id)
                report["draft_voice_cancelled"] = True
                await page.wait_for_function(same_track_advancing, arg=playing_before_cancel, timeout=4000)
                report["same_track_after_draft_cancel"] = True
            finally:
                await browser.close()
        from everyday import everyday_reply
        for text in ("谢谢，帮我保存一份草稿", "把‘在吗’翻成英语", "算了吧还是放第二首", "哎，我不舒服", "在吗？帮我查查天气"):
            assert everyday_reply(text) is None, text
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        print(folder, report, flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
