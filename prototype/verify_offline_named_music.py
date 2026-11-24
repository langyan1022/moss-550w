import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from unittest.mock import patch

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import DEFAULT_DB
from verify_memory import send


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("offline-named-music-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    index = folder / "index.sqlite3"
    with (closing(sqlite3.connect(DEFAULT_DB.as_uri() + "?mode=ro", uri=True)) as src,
          closing(sqlite3.connect(index)) as dest):
        src.backup(dest)
        # 从磁盘文件及原始标签/歌手目录检查预期结果，独立于点歌排序。
        songs = dest.execute("SELECT media_id,artist,title,path FROM media WHERE kind='audio'").fetchall()

    def expected_ids(artist, title):
        result = set()
        for key, credit, track, name in songs:
            path = Path(name)
            if (track in {title, title + "(Live)"} and (credit == artist or artist in path.parts)
                    and path.drive.upper() == "G:" and path.is_file()):
                result.add(key)
        return result

    cases = [
        ("刀郎的西海情歌", "刀郎", "西海情歌", "play"),
        ("刘德华的一起走过的日子", "刘德华", "一起走过的日子", "play"),
        ("刘德华的冰雨", "刘德华", "冰雨", "play"),
        ("刀郎的冰雨", "刀郎", "冰雨", "missing"),
        ("搜索刀郎的西海情歌", "刀郎", "西海情歌", "list"),
        ("只列出刀郎的西海情歌的版本，不要播放", "刀郎", "西海情歌", "list"),
        ("只看刘德华的冰雨的版本", "刘德华", "冰雨", "list"),
    ]
    report = {"passed": False, "requests_to_model": [], "turns": []}

    async def no_inference(request):
        report["requests_to_model"].append(request.path)
        return web.json_response({"error": "model offline"}, status=503)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", no_inference)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18990).start()
    server = BrowserServer(folder, 18989, model_url="http://127.0.0.1:18990/v1")
    try:
        with patch.dict(os.environ, {"MOSS_MEDIA_DB": str(index), "MOSS_KEV_ENABLED": "0"}):
            await server.start()
        assert (await server.state())["model_id"] == "local"
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(viewport={"width":1280, "height":800}, reduced_motion="reduce")
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                for prompt, artist, title, action in cases:
                    expected = expected_ids(artist, title)
                    started = perf_counter()
                    assert bool(expected) == (action != "missing"), (artist, title, expected)
                    reply = await send(page, server, prompt)
                    turn = (await server.state())["turns"][-1]
                    media = turn.get("media") or {}
                    items = media.get("items", [])
                    selected = (media.get("selected") or {}).get("media_id")
                    row = {"prompt": prompt, "reply": reply, "expected_ids": sorted(expected),
                           "returned_ids": [i["media_id"] for i in items], "selected_id": selected,
                           "autoplay": media.get("autoplay"), "reply_ms": round((perf_counter()-started)*1000, 1)}
                    report["turns"].append(row)
                    assert turn["status"] == "replied" and media.get("online_calls", 0) == 0, row
                    assert not report["requests_to_model"], report["requests_to_model"]
                    assert set(row["returned_ids"]) == expected and bool(media.get("autoplay")) == (action == "play"), row
                    if action == "play":
                        assert selected in expected and (media["selected"].get("artist") == artist), row
                        await page.wait_for_function("""id => [...document.querySelectorAll('.media-audio')].some(
                            a=>a.dataset.audioTurn===id && !a.paused && a.currentTime>.25 && a.duration>5)""",
                            arg=turn["id"], timeout=45000)
                        async with asyncio.timeout(15):
                            while True:
                                receipt = (await server.state())["turns"][-1].get("browser_playback") or {}
                                if receipt.get("event") == "started" and receipt.get("media_id") == selected:
                                    break
                                await asyncio.sleep(.08)
                        timing = await page.locator(".media-audio").evaluate_all("""(all,id)=>{
                            const a=all.find(x=>x.dataset.audioTurn===id && !x.paused);
                            return {src:a.currentSrc, seconds:a.currentTime, duration:a.duration};
                        }""", turn["id"])
                        assert timing["src"].endswith("/media/"+selected)
                        row.update(audio=timing, browser_receipt=receipt)
                        if prompt == cases[0][0]:
                            controls = report["transport_controls"] = []
                            for control, paused in (("暂停音乐",True),("继续播放",False)):
                                answer = await send(page, server, control)
                                await page.wait_for_function("""([id,p])=>[...document.querySelectorAll('.media-audio')]
                                    .some(a=>a.currentSrc.endsWith('/media/'+id) && a.paused===p)""",arg=[selected,paused])
                                command = (await server.state())["turns"][-1]
                                event = (command.get("transport") or {}).get("event")
                                assert event and command["status"] == "replied" and not command.get("partial_reply")
                                controls.append({"text":control,"reply":answer,"event":event,"media_id":selected})
                    else:
                        await page.wait_for_timeout(350)
                        checked = (await server.state())["turns"][-1]
                        assert not checked.get("browser_playback") and not await page.locator(".media-audio").evaluate_all(
                            "(all,id)=>all.some(a=>a.dataset.audioTurn===id && !a.paused)", turn["id"]), row
                        if action == "missing":
                            assert not selected and "没有" in reply, row
            finally:
                await browser.close()
        report["passed"] = True
    finally:
        await server.stop()
        await runner.cleanup()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
