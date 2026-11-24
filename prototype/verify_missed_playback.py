import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

from browser_lab import ROOT, BrowserServer, confirm_muted_edge, open_browser
from media_chat import recover_artist_request
from memory_store import install_schema
from verify_memory import send


async def verify(archive):
    folder = ROOT / "runs" / datetime.now().strftime("missed-playback-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18474)
    server.data.mkdir(parents=True)
    if archive:
        with closing(sqlite3.connect(archive.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = [json.loads(v) for v, in db.execute("SELECT doc FROM turns WHERE json_extract(doc,'$.user')=?",
                                                       ("换一个刀郎的歌",))]
    else:
        async with aiohttp.ClientSession() as client:
            async with client.get("http://127.0.0.1:18221/api/state") as response:
                response.raise_for_status()
                rows = (await response.json())["turns"]
    origin = next(t for t in reversed(rows) if t["user"] == "换一个刀郎的歌" and
                  t.get("media_actions") == ["search"] and not t["media"].get("autoplay") and not t.get("browser_playback"))
    chat = origin["chat_id"]
    now = datetime.now(timezone.utc)
    origin["created_at"] = (now - timedelta(minutes=4)).isoformat()
    with sqlite3.connect(server.data / "session.sqlite3") as db:
        install_schema(db)
        db.execute("INSERT INTO chats VALUES (?,?,?)", (chat, origin["user"], origin["created_at"]))
        db.execute("UPDATE state SET active_chat=? WHERE id=1", (chat,))
        db.execute("INSERT INTO turns(id,doc,chat_id) VALUES (?,?,?)",
                   (origin["id"],json.dumps(origin, ensure_ascii=False),chat))
    for prompt in ("怎么没放", "那就放啊", "再试一次"):
        assert recover_artist_request(prompt, [origin], chat)["artist"] == "刀郎"
        assert recover_artist_request(prompt, [origin], "other") is None
        assert recover_artist_request(prompt, [{**origin,"user":"只看看刀郎的歌，不要播放"}], chat) is None
        assert recover_artist_request(prompt, [{**origin,"browser_playback":{"event":"started"}}], chat) is None
        assert recover_artist_request(prompt, [origin], chat, {"at":now.isoformat()}) is None
        assert recover_artist_request(prompt, [{**origin,"created_at":(now-timedelta(hours=2)).isoformat()}], chat) is None
        assert recover_artist_request(prompt, [origin, {**origin,"user":"今天几号"}], chat) is None
    await server.start()
    report = []
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            await confirm_muted_edge(browser)
            page = await browser.new_page(reduced_motion="reduce")
            await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
            def playing():
                return page.evaluate("()=>[...document.querySelectorAll('.media-audio')].filter(a=>!a.paused).map(a=>a.dataset.audioTurn)")
            for prompt in ("啥玩意", "哎", "那就放啊", "再试一次"):
                reply = await send(page, server, prompt)
                turn = (await server.state())["turns"][-1]
                assert turn["source"] == "media" and turn["model_id"] == "local"
                if prompt in {"啥玩意", "哎"}:
                    assert "只收到了歌曲检索结果" in reply and not turn["media"]["autoplay"]
                    assert not await playing() and not turn.get("browser_playback")
                elif prompt == "那就放啊":
                    assert turn["retried_artist_turn"] == origin["id"] and turn["media"]["autoplay"]
                    assert "刀郎" in reply
                    async with asyncio.timeout(10):
                        while True:
                            latest = (await server.state())["turns"][-1]
                            if (latest.get("browser_playback") or {}).get("event") == "started":
                                break
                            await asyncio.sleep(.05)
                    playback_turn = turn["id"]
                    assert await playing() == [playback_turn]
                else:
                    assert "已收到开始播放的回执" in reply and not turn["media"]["autoplay"]
                    assert not turn.get("browser_playback") and await playing() == [playback_turn]
                report.append({"text":prompt,"reply":reply,"autoplay":turn["media"]["autoplay"]})
            assert sum((t.get("browser_playback") or {}).get("event") == "started" for t in (await server.state())["turns"]) == 1
            await browser.close()
        print(json.dumps(report, ensure_ascii=False), flush=True)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    asyncio.run(verify(parser.parse_args().archive))
