import argparse
import asyncio
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from memory_store import install_schema


async def verify(snapshot, baseline, browser_check):
    folder = ROOT / "runs" / datetime.now().strftime("music-confirmation-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18452 if browser_check else 18451)
    server.data.mkdir(parents=True)
    source = json.loads(snapshot.read_text(encoding="utf-8"))
    assert [turn["user"] for turn in source[:2]] == ["我想听刀郎的歌", "第二首"]
    first, second = source[0]["media"]["items"][:2]
    with sqlite3.connect(server.data / "session.sqlite3") as db:
        chat = install_schema(db)
        for turn in source[:2]:
            doc = dict(turn, chat_id=chat)
            db.execute("INSERT INTO turns(id,doc,chat_id) VALUES(?,?,?)",
                       (doc["id"], json.dumps(doc, ensure_ascii=False), chat))
    report = {"baseline": baseline, "evidence": str(snapshot), "turns": [], "passed": False}

    async def ask(text):
        start = perf_counter()
        code, state = await server.post("/api/message", {"text":text})
        assert code == 200, state
        turn_id = state["turns"][-1]["id"]
        async with asyncio.timeout(150):
            while True:
                turn = (await server.state())["turns"][-1]
                assert turn["id"] == turn_id
                if turn["status"] != "thinking":
                    break
                await asyncio.sleep(.025)
        assert turn["status"] == "replied", turn.get("error")
        media = turn.get("media") or {}
        report["turns"].append({"text":text,"reply":turn["reply"],"source":turn.get("source","model"),
                                "provider":turn["model_id"], "ms":round((perf_counter()-start)*1000,1),
                                "selected":(media.get("selected") or {}).get("media_id"),
                                "suggested":(media.get("suggested") or {}).get("media_id"),
                                "autoplay":media.get("autoplay",False),"mode":media.get("play_mode")})
        return media

    try:
        await server.start()
        suggestion = await ask("你猜")
        assert suggestion["suggested"]["media_id"] == second["media_id"]
        assert suggestion["selected"]["media_id"] == first["media_id"] and not suggestion["autoplay"]
        await server.stop()
        await server.start()
        if browser_check:
            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url+"/?chat=1", wait_until="domcontentloaded")
                await page.locator("#message").fill("好的")
                await page.locator("#send").click()
                await page.wait_for_function("""() => [...document.querySelectorAll('.media-audio')]
                  .some(a => !a.paused && a.currentTime > .25)""",timeout=60000)
                audio = await page.locator(".media-audio").evaluate_all("""items => {
                  const a=items.find(a=>!a.paused);
                  return {src:a.currentSrc, position:a.currentTime, duration:a.duration};
                }""")
                assert audio["src"].endswith(second["media_id"]) and audio["duration"]>15
                assert await page.locator("#tool-player .player-heading strong").inner_text() == second["title"]
                assert await page.locator("#tools-panel").is_visible()
                selected = (await server.state())["turns"][-1]
                assert selected["model_id"] == "local"
                assert selected["source"] == "media"
                report.update(audio=audio, muted=True, provider=selected["model_id"], passed=True)
                await browser.close()
            return
        answer = await ask("好的")
        if not baseline:
            assert answer["autoplay"] and answer["selected"]["media_id"] == second["media_id"]
            assert answer["play_mode"] == "track" and not answer.get("suggested")
            assert not (await ask("好的")).get("autoplay")
            assert report["turns"][-1]["source"] == "acknowledgement" and report["turns"][-1]["reply"] == "好的。"
            await ask("搜索本地音乐 刀郎")
            suggestion = await ask("你猜")
            assert suggestion.get("suggested") and not suggestion["autoplay"]
            before = suggestion["selected"]["media_id"]
            decline = await ask("不用了")
            assert not decline["autoplay"] and not decline.get("suggested") and decline["selected"]["media_id"] == before
            assert not (await ask("可以")).get("autoplay")
            assert report["turns"][-1]["source"] == "acknowledgement"
            await ask("搜索本地音乐 刀郎")
            await ask("你猜")
            await ask("记住：回归暗号是橘子。")
            assert not (await ask("行")).get("autoplay")
            assert report["turns"][-1]["source"] == "acknowledgement"
            status, _ = await server.post("/api/new-chat", {})
            assert status == 200
            assert not (await ask("好")).get("autoplay")
            assert report["turns"][-1]["source"] == "model"
        report["passed"] = True
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify(args.snapshot, args.baseline, args.browser))
