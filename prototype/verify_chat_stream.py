import asyncio
import argparse
from datetime import datetime
import json
import sqlite3

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser


async def verify_cancel(page, server):
    await page.get_by_test_id("composer").fill("请用三百字描述一只小猫在花园里的一天。")
    await page.get_by_test_id("send").click()
    row = page.get_by_test_id("turn").last
    await row.get_by_test_id("partial-reply").wait_for()
    turn_id = await row.get_attribute("data-turn-id")
    status, _ = await server.post("/api/cancel", {"turn_id": turn_id})
    assert status == 200
    cancelled = page.locator(f'article[data-turn-id="{turn_id}"][data-status="cancelled"]')
    await cancelled.wait_for()
    assert not await cancelled.get_by_test_id("partial-reply").is_visible()
    await page.reload(wait_until="domcontentloaded")
    await cancelled.wait_for()
    turn = (await server.state())["turns"][-1]
    assert turn["id"] == turn_id and not turn.get("partial_reply") and not turn["reply"]
    await page.get_by_test_id("composer").fill("第一条消息里的白猫在哪儿晒太阳？用十个字以内回答。")
    await page.get_by_test_id("send").click()
    await page.get_by_test_id("turn").nth(2).get_by_test_id("final").wait_for()
    turns = (await server.state())["turns"]
    assert [item["status"] for item in turns] == ["replied", "cancelled", "replied"]
    assert not turns[1].get("partial_reply") and not turns[1].get("reply")
    assert "长椅" in turns[2]["reply"]
    return turns[1]["status"], turns[2]["reply"]


async def run(baseline=False, model_url="http://127.0.0.1:18222/v1",
              port=None, check_cancel=False):
    folder = ROOT / "runs" / datetime.now().strftime("local-text-stream-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, port or 18491, model_url=model_url)
    report = {"passed": False, "model_url": model_url}
    await server.start()
    try:
        status, state = await server.post("/api/select-model", {"id": "local"})
        assert status == 200 and state["model_id"] == "local"
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(viewport={"width": 1360, "height": 840}, reduced_motion="reduce")
                page.set_default_timeout(320000)
                await page.add_init_script("""localStorage.setItem('moss.voice.enabled','off');
                    window.trace = {submitCount:0, audio:[], partials:[]};
                    for (const type of ['moss-speech-start', 'moss-media-start'])
                        window.addEventListener(type, () => trace.audio.push(type));
                    document.addEventListener('submit', e => {
                        if (e.target.id === 'composer') {trace.start = performance.now(); trace.submitCount++;}
                    }, true);
                    new MutationObserver(() => {
                        if (!trace.start || trace.finalMs !== undefined) return;
                        const part = document.querySelector('[data-testid=partial-reply]');
                        const final = document.querySelector('[data-testid=final]');
                        const ms = Math.round(performance.now() - trace.start);
                        if (part && part.checkVisibility() && part.textContent.trim() && trace.partials.at(-1)?.text !== part.textContent)
                            trace.partials.push({ms, text:part.textContent});
                        if (final && final.checkVisibility() && final.textContent.trim()) trace.finalMs = ms;
                    }).observe(document,{childList:true,subtree:true,characterData:true,attributes:true});""")
                await page.goto(server.url + "?chat=1", wait_until="domcontentloaded")
                await page.get_by_test_id("composer").fill("请原样复述这句话：今天下午我在公园散步，看见一只白猫趴在长椅上晒太阳。")
                await page.get_by_test_id("send").click()
                row = page.get_by_test_id("turn").last
                await row.locator('[data-testid="final"], .turn-error').first.wait_for()
                turn = (await server.state())["turns"][-1]
                trace = await page.evaluate("() => trace")
                report.update(status=turn["status"], error=turn.get("error"), native=turn.get("native_result"),
                              trace=trace, reply=turn.get("reply"), turns=len((await server.state())["turns"]))
                assert turn["status"] == "replied" and not turn.get("native_receipts")
                assert turn["model_id"] == "local"
                assert trace["submitCount"] == 1 and trace["audio"] == [] and report["turns"] == 1
                final = (await row.get_by_test_id("final").inner_text()).strip()
                assert "白猫" in final and "晒太阳" in final
                first = (trace["partials"] or [{"ms":trace["finalMs"], "text":final}])[0]
                if not baseline:
                    assert trace["partials"], "页面没有显示中间文字"
                    assert first["ms"] < trace["finalMs"] and len(first["text"].strip()) < len(final)
                assert final == turn["reply"].strip() and all(final.startswith(p["text"].strip()) for p in trace["partials"])
                assert await page.get_by_test_id("partial-reply").count() == 0 or not await page.get_by_test_id("partial-reply").first.is_visible()
                assert not await row.locator("audio").count()
                report.update(first_visible_ms=first["ms"], completed_ms=trace["finalMs"],
                              lead_ms=trace["finalMs"] - first["ms"], updates=len(trace["partials"]))
                if check_cancel:
                    report["cancelled_while_streaming"], report["after_cancel"] = await verify_cancel(page, server)
            finally:
                await browser.close()
        await server.stop()
        with sqlite3.connect(server.data / "session.sqlite3") as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_turns'").fetchone()
            count, status = db.execute("SELECT count(*), min(status) FROM native_turns").fetchone() if exists else (0,None)
            assert count == 0 and status is None
        report.update(native_dispatches=count, passed=True)
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--model-url", default="http://127.0.0.1:18222/v1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--check-cancel", action="store_true")
    options = parser.parse_args()
    asyncio.run(run(options.baseline, options.model_url, options.port, options.check_cancel))
