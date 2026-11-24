import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import DEFAULT_DB, song_title_key
from verify_memory import new_chat, send


async def verify(server, page, report):
    events = report["event_http"] = []
    page.on("response", lambda response: events.append({"status": response.status, **response.request.post_data_json})
            if response.url.endswith("/api/media-event") else None)
    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
    await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
    assert (await server.state())["model_id"] == "local"

    async def play(prompt, artist=None):
        reply = await send(page, server, prompt)
        turn = (await server.state())["turns"][-1]
        media = turn["media"]
        item = media["selected"]
        assert turn["source"] == "media" or (turn["source"] == "holo" and "play_song" in turn["holo_tools"])
        assert media["online_calls"] == 0 and media["autoplay"]
        assert Path(item["path"]).drive.casefold() == "g:" and item["source_kind"] == "local"
        assert item["title"] in reply and "？" not in reply
        if artist:
            assert item["artist"] == artist
        node = page.locator(f'[data-audio-turn="{turn["id"]}"]')
        await page.wait_for_function("""id=>{const a=document.querySelector('[data-audio-turn="'+id+'"]');
            return a&&!a.paused&&a.readyState>=3&&a.duration>20&&a.currentTime>.15}""", arg=turn["id"])
        t0 = await node.evaluate("a=>a.currentTime")
        await page.wait_for_function("""([id,t])=>document.querySelector('[data-audio-turn="'+id+'"]').currentTime>t+.25""",
                                     arg=[turn["id"], t0])
        t1 = await node.evaluate("a=>a.currentTime")
        assert (await node.evaluate("a=>a.currentSrc")).endswith("/media/" + item["media_id"])
        async with asyncio.timeout(20):
            while True:
                started = (await server.state())["turns"][-1].get("browser_playback") or {}
                if started.get("event") == "started" and started.get("media_id") == item["media_id"]:
                    break
                await asyncio.sleep(.08)
        report["cases"].append(dict(prompt=prompt, title=item["title"], artist=item["artist"],
                                    candidates=len(media["items"]), playback_seconds=[t0, t1], receipt=started["event"]))
        return item

    for artist, prompts in (("刀郎", ("播放刀郎的歌吧", "刀郎是歌手，随便一手", "随便一首歌")),
                            ("刘德华", ("帮我播放一下刘德华的歌吧", "再来一首", "换一首"))):
        songs = [await play(prompt, artist) for prompt in prompts]
        assert len({song["media_id"] for song in songs}) == len(songs)
        assert len({song_title_key(song["title"]) for song in songs}) == len(songs)
    await play("来一首周杰伦的歌", "周杰伦")
    named = await play("播放周杰伦的七里香", "周杰伦")
    replay = await play("播放周杰伦的七里香", "周杰伦")
    assert replay["media_id"] == named["media_id"]
    await play("放张学友的歌呗", "张学友")
    only = await play("播放一棵小葱的歌吧", "一棵小葱")
    assert (await play("再来一首", "一棵小葱"))["media_id"] == only["media_id"]
    await new_chat(page, server)
    await play("随便一首歌")
    for prompt, expected in (("播放刀郎的歌，先别播", "冲动的惩罚"),
                             ("不要播放刀郎的歌", "没有发起新的播放"),
                             ("播放不存在歌手ZZ59的歌吧", "没有找到"),
                             ("播放王菲的歌吧", "DSF")):
        await new_chat(page, server)
        reply = await send(page, server, prompt)
        turn = (await server.state())["turns"][-1]
        assert expected in reply and turn["source"] == "media"
        assert not turn["media"]["autoplay"] and not turn.get("browser_playback")
        assert not await page.locator(".media-audio").evaluate_all("nodes=>nodes.some(a=>!a.paused)")
        report["cases"].append(dict(prompt=prompt, autoplay=False, reply=reply))


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("artist-phrasing-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    copy = folder / "library.sqlite3"
    with closing(sqlite3.connect(DEFAULT_DB.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(copy)) as target:
            original.backup(target)
    server = BrowserServer(folder, 18787, model_url="http://127.0.0.1:18789/v1")
    report = {"passed": False, "cases": []}
    original = os.environ.get("MOSS_MEDIA_DB")
    try:
        os.environ["MOSS_MEDIA_DB"] = str(copy)
        await server.start()
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                await verify(server, await browser.new_page(reduced_motion="reduce"), report)
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        if original is None:
            os.environ.pop("MOSS_MEDIA_DB", None)
        else:
            os.environ["MOSS_MEDIA_DB"] = original
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
