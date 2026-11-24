import asyncio
from datetime import datetime
import json
import re
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import new_chat, send


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("jev-revision-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18683)
    report = {"passed": False, "cases": []}
    await server.start()
    try:
        assert (await server.post("/api/select-model", {"id": "deepseek"}))[0] == 200
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                ctx = await browser.new_context(locale="zh-CN", reduced_motion="reduce")
                await ctx.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                page = await ctx.new_page()
                page.set_default_timeout(120000)
                await page.goto(server.url + "/?chat=1")

                async def say(prompt):
                    start = perf_counter()
                    reply = await send(page, server, prompt)
                    turn = (await server.state())["turns"][-1]
                    row = page.get_by_test_id("turn").last
                    item = {"user": prompt, "reply": reply, "source": turn.get("source"), "tools": turn.get("holo_tools"),
                            "selector": (turn.get("jev") or {}).get("selector"),
                            "copy": await row.get_by_test_id("copy-reply").is_visible(),
                            "ms": round((perf_counter() - start) * 1000)}
                    report["cases"].append(item)
                    print(item, flush=True)
                    return item

                first = await say("帮我回复同事：“这周什么时候开会？”，我想说会议定在周五下午三点，地点在北楼，语气客气。")
                short = await say("再简短一点")
                await server.stop()
                await server.start()
                await page.reload()
                revised = await say("把周五改成周六，其他不变")
                assert all(row["source"] == "jev" and row["copy"]
                           for row in (first, short, revised))
                assert first["tools"] == short["tools"] == ["draft_chat_reply"]
                for row, expected in ((first, "五"), (short, "五"), (revised, "六")):
                    assert re.search(r"(?:周|星期|礼拜)" + expected, row["reply"])
                    assert re.search(r"三点|3点|15[:：]00", row["reply"]) and "北楼" in row["reply"]
                assert len(short["reply"]) <= len(first["reply"])
                assert "周五" not in revised["reply"]
                if "周五" in short["reply"]:
                    assert revised["reply"] == short["reply"].replace("周五", "周六")
                    assert revised["selector"] == "literal_edit" and not revised["tools"]
                formal = await say("语气再正式一点")
                assert formal["source"] == "jev" and formal["copy"] and "北楼" in formal["reply"]
                assert re.search(r"周六|星期六|礼拜六", formal["reply"]) and "周五" not in formal["reply"]
                await new_chat(page, server)
                other = await say("再简短一点")
                assert other["source"] != "jev" and not other["copy"] and "北楼" not in other["reply"]
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
