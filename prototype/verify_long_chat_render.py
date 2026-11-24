import asyncio
from datetime import datetime
import json

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_history_backend import summarize


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("live-history-render-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18955, model_url="http://127.0.0.1:18954/v1")
    await server.start()
    try:
        chat = (await server.state())["chat_id"]
        for i in range(79):
            status, state = await server.post("/api/message", {"chat_id": chat, "text": ("在吗", "谢谢")[i % 2]})
            assert status == 200 and state["turns"][-1]["status"] == "replied"
        status, _ = await server.post("/api/message", {"chat_id": chat, "text": "只看看刀郎的歌"})
        assert status == 200
        async with asyncio.timeout(10):
            while True:
                media_turn = (await server.state())["turns"][-1]
                if media_turn["status"] != "thinking":
                    break
                await asyncio.sleep(.02)
        assert media_turn["media"]["items"] and not media_turn["media"].get("autoplay")
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce", locale="zh-CN")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(server.url + "/static/index.html", wait_until="domcontentloaded")
                await page.wait_for_function("connected && latest.turns.length === 80 && views.size === 80")
                await page.evaluate("""() => {
                    window.observed = {updates:[], visible:[], media:document.querySelector('.media-audio')};
                    const acceptState = window.accept, drawTurn = window.renderTurn;
                    let ids;
                    window.renderTurn = (...args) => { if (ids) ids.push(args[1].id); return drawTurn(...args); };
                    window.accept = (...args) => {
                      const before = latest.revision, started = performance.now();
                      ids = [];
                      try { return acceptState(...args); }
                      finally {
                        if (latest.revision !== before) observed.updates.push({ms:performance.now()-started, ids});
                        ids = null;
                      }
                    };
                    document.addEventListener('submit', event => {
                      if (event.target.id === 'compose') observed.sent = performance.now();
                    }, true);
                    new MutationObserver(() => {
                      if (!observed.sent || !document.querySelector('#turns .turn:last-child [data-testid=final]')) return;
                      if (views.size !== 81 + observed.visible.length) return;
                      observed.visible.push(performance.now() - observed.sent);
                      observed.sent = null;
                    }).observe(document.querySelector('#turns'), {childList:true, subtree:true, characterData:true});
                }""")
                for i in range(12):
                    await page.get_by_test_id("composer").fill(("在吗", "今天星期几？")[i % 2])
                    await page.get_by_test_id("send").click()
                    await page.wait_for_function("count => observed.visible.length === count", arg=i + 1)
                measured = await page.evaluate("""() => ({visible:observed.visible, updates:observed.updates.slice(),
                    sameMedia:observed.media && observed.media===document.querySelector('.media-audio') && observed.media.paused})""")
                assert measured["sameMedia"] and all(len(update["ids"]) <= 1 for update in measured["updates"])
                status, state = await server.post("/api/remember", {"text": "演练清单使用蓝色封面"})
                assert status == 200
                await page.wait_for_function("revision => latest.revision >= revision", arg=state["revision"])
                assert await page.evaluate("observed.updates.at(-1).ids.length === 0")
                await page.get_by_test_id("new-chat").click()
                await page.wait_for_function("views.size === 0")
                status, _ = await server.post("/api/switch-chat", {"id": chat})
                assert status == 200
                await page.wait_for_function("views.size === 92")
                assert await page.evaluate("document.querySelector('.media-audio').paused") and not errors, errors
                report = {"history": 80, "submissions": 12, "requests": ["在吗", "今天星期几？"],
                          "page": "/static/index.html", "submit_to_visible_dom": summarize(measured["visible"]),
                          "state_to_dom": summarize([u["ms"] for u in measured["updates"]]),
                          "rows_drawn_per_update": [len(u["ids"]) for u in measured["updates"]],
                          "global_only_rows_drawn": 0, "media_remained_paused": True, "restored_turns": 92,
                          "samples_ms": measured["visible"], "errors": errors}
                (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(folder, report["submit_to_visible_dom"], report["state_to_dom"])
            finally:
                await browser.close()
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
