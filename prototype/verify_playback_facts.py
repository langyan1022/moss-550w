import asyncio
from datetime import datetime
import json
import sqlite3
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import new_chat, send
from verify_playlist_recovery import restore_list


async def read_legacy_receipt(folder):
    archive = ROOT/"runs/playlist-recovery-20261120-140007/data/session.sqlite3"
    server = BrowserServer(folder/"legacy",18460,model_url="http://127.0.0.1:18278/v1")
    server.data.mkdir(parents=True)
    with sqlite3.connect(archive.as_uri()+"?mode=ro",uri=True) as src, sqlite3.connect(server.data/"session.sqlite3") as db:
        src.backup(db)
        old = json.loads(db.execute("""SELECT doc FROM turns WHERE
            json_extract(doc,'$.browser_playback.event')='started' AND chat_id=(SELECT active_chat FROM state WHERE id=1)
            ORDER BY json_extract(doc,'$.browser_playback.at') DESC LIMIT 1""").fetchone()[0])
    receipt, item = old["browser_playback"], old["media"]["selected"]
    assert "play_id" not in receipt and item["media_id"]==receipt["media_id"]
    await server.start()
    try:
        status,_ = await server.post("/api/message",{"text":"刚才听的是哪首？"})
        assert status==200
        async with asyncio.timeout(10):
            while not (last := (await server.state())["turns"][-1]).get("reply"):
                await asyncio.sleep(.05)
        assert item["title"] in last["reply"]
        return {"archive":archive.parent.parent.name,"media_id":item["media_id"],"answer":last["reply"]}
    finally:
        await server.stop()


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("playback-facts-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18459, model_url="http://127.0.0.1:18278/v1")
    archive, _ = restore_list(server.data)
    first, second = (archive["media"]["items"][i] for i in (1, 4))
    report = {"passed": False, "answers": [], "page_errors": []}
    await server.start()
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            await confirm_muted_edge(browser)
            report["device_output_muted"] = True
            context = await browser.new_context(viewport={"width":1440,"height":900},reduced_motion="reduce")
            page = await context.new_page()
            page.set_default_timeout(50000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            await page.goto(server.url+"?chat=1", wait_until="domcontentloaded")

            async def question(prompt, *expected):
                before = perf_counter()
                answer = await send(page, server, prompt)
                report["answers"].append({"prompt":prompt,"answer":answer,"ms":round(1000*(perf_counter()-before))})
                assert all(word in answer for word in expected), (prompt,answer,expected)
                return (await server.state())["turns"][-1]

            async def wait_play(turn_id):
                await page.wait_for_function("""id => {
                    const a=document.querySelector('[data-audio-turn="'+id+'"]');
                    return a && !a.paused && !a.ended && a.currentTime>.1 && a.readyState>=3;
                }""", arg=turn_id)

            playing = await question("第二首", first["title"])
            tid = playing["id"]
            await wait_play(tid)
            audio = page.locator(f'[data-audio-turn="{tid}"]')
            other = await page.context.new_page()
            await other.goto(server.url+"?chat=1", wait_until="domcontentloaded")
            other_answer = await send(other,server,"当前在播什么歌？")
            assert "当前页面没有正在播放" in other_answer and not await audio.evaluate("a=>a.paused")
            report["other_tab"] = other_answer
            await other.close()
            position = await audio.evaluate("a=>a.currentTime")
            lookup = await question("搜索 西海情歌 · 刀郎", second["title"])
            assert lookup["media"]["selected"]["media_id"]==second["media_id"] and not lookup["media"]["autoplay"]
            assert await audio.evaluate("a=>!a.paused && a.currentTime") > position
            assert first["title"] in await page.locator("#music-now").inner_text()
            await question("现在在播什么？","正在播放",first["title"])
            await question("我刚才听了哪首？",first["title"])
            await question("刚才选的是哪首？",second["title"])
            await page.locator("#music-now").click()
            toggle = page.locator("#tool-player .player-toggle")
            await toggle.click()
            assert await audio.evaluate("a=>a.paused")
            await question("现在播放的是哪首歌？","暂停",first["title"])

            await page.locator(f'[data-turn-id="{lookup["id"]}"] .projection-link').click()
            await toggle.click()
            await wait_play(lookup["id"])
            await question("刚才听的是哪首？",second["title"])
            await page.locator(f'[data-turn-id="{tid}"] .projection-link').click()
            await toggle.click()
            await wait_play(tid)
            assert await page.locator(f'[data-audio-turn="{lookup["id"]}"]').evaluate("a=>a.paused")
            await question("刚才听的是哪首？",first["title"])
            await page.locator("#tool-player .player-seek").press("End")
            await page.wait_for_function("id=>document.querySelector('[data-audio-turn=\"'+id+'\"]').ended",arg=tid)
            await question("现在播放的是哪首歌？","结束",first["title"])
            await page.reload(wait_until="domcontentloaded")
            await question("当前在放什么歌？","当前页面","没有正在播放")
            await question("我刚才听的是哪首？",first["title"])
            await server.stop()
            await server.start()
            await page.reload(wait_until="domcontentloaded")
            await question("刚才听的是哪首？",first["title"])
            state = await server.state()
            status,_ = await server.post("/api/message",{"chat_id":state["chat_id"],"text":"现在在播什么歌？"})
            assert status==200
            await page.get_by_test_id("turn").nth(len(state["turns"])).get_by_test_id("final").wait_for()
            latest = (await server.state())["turns"][-1]["reply"]
            assert "没有实时状态" in latest and first["title"] in latest, latest
            report["without_live_snapshot"] = latest
            await new_chat(page, server)
            await question("刚才听了哪首？","没有","回执")
            await question("现在播放的是哪首歌？","没有正在播放")
            chat = (await server.state())["chat_id"]
            status,_ = await server.post("/api/message", {"text":"现在播什么？","chat_id":chat,
                "player":{"state":"playing","turn_id":tid,"media_id":first["media_id"]}})
            assert status==409
            report["cross_chat_http"] = status
            assert not report["page_errors"], report["page_errors"]
            report["legacy"] = await read_legacy_receipt(folder)
            report["passed"] = True
            await browser.close()
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
