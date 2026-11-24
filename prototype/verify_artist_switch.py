import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from media_chat import DEFAULT_DB
from verify_artist_request import OBSERVER
from verify_memory import new_chat, send


async def verify(delegated=False):
    folder = ROOT / "runs" / datetime.now().strftime("artist-switch-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    db = folder / "catalog.sqlite3"
    with closing(sqlite3.connect(DEFAULT_DB.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(db)) as copy:
            source.backup(copy)
    server = BrowserServer(folder, 18473)
    original = os.environ.get("MOSS_MEDIA_DB")
    os.environ["MOSS_MEDIA_DB"] = str(db)
    try:
        await server.start()
    finally:
        if original is None:
            os.environ.pop("MOSS_MEDIA_DB")
        else:
            os.environ["MOSS_MEDIA_DB"] = original
    cases = [
        ("我想听刘德华的歌", "刘德华", True),
        ("换一个刀郎的歌", "刀郎", True),
        ("再换一首刀郎的歌", "刀郎", True),
        ("换成刘德华的歌", "刘德华", True),
        ("只看看刀郎的歌，不要播放", "刀郎", False),
    ]
    if delegated:
        cases = [(prompt, "刀郎", play)
                 for wording in ("你帮我选一首吧", "你来决定")
                 for prompt, play in (("只看看刀郎的歌", False), (wording, True), ("换一个", True))]
    report = {"passed":False, "cases":[], "page_errors":[]}
    previous_id = previous_title = previous_artist = None
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            await confirm_muted_edge(browser)
            page = await browser.new_page(viewport={"width":1440, "height":900}, reduced_motion="reduce")
            page.set_default_timeout(60000)
            await page.add_init_script(OBSERVER)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
            await page.locator(".preferences summary").click()
            await page.wait_for_function("document.querySelector('#agent-model').value === 'local'")
            await page.get_by_role("checkbox", name="MOSS 语音播报").check()
            await page.locator(".preferences summary").click()
            panel = page.get_by_test_id("media-projection")
            for pos, (prompt, artist, play) in enumerate(cases, 1):
                if delegated and pos == 4:
                    await new_chat(page, server)
                    previous_id = previous_title = previous_artist = None
                case = {"prompt":prompt, "artist":artist}
                report["cases"].append(case)
                answer = await send(page, server, prompt)
                turn = (await server.state())["turns"][-1]
                case.update(reply=answer, route=turn.get("source"), model=turn.get("model_id"))
                assert turn["status"] == "replied" and turn["source"] == "media" and turn["model_id"] == "local", case
                assert not any(word in answer for word in ("？", "?", "你想听哪", "你更喜欢"))
                media = turn["media"]
                selected = media["selected"]
                assert media["online_calls"] == 0 and media["autoplay"] == play
                assert len(media["items"]) >= 2
                assert all((item.get("artist") == artist or artist in Path(item["path"]).parts)
                           and Path(item["path"]).drive.upper() == "G:" for item in media["items"])
                title = selected["title"]
                case.update(selected=title, candidates=len(media["items"]), turn=turn["id"])
                await panel.get_by_test_id("track-choice").first.wait_for()
                assert await panel.locator(".media-choice button > span").all_inner_texts() == [i["title"] for i in media["items"]]
                active = "Array.from(document.querySelectorAll('audio.media-audio')).filter(a=>!a.paused&&!a.ended)"
                if play:
                    await page.wait_for_function("id=>{" + "const a=" + active + "; return a.length===1 && a[0].dataset.audioTurn===id && a[0].currentTime>.2}", arg=turn["id"])
                    async with asyncio.timeout(8):
                        while True:
                            receipt = (await server.state())["turns"][-1].get("browser_playback") or {}
                            if receipt.get("event") == "started" and receipt.get("media_id") == selected["media_id"]:
                                break
                            await asyncio.sleep(.05)
                    if artist == previous_artist:
                        assert selected["media_id"] != previous_id and title != previous_title
                    previous_id, previous_title, previous_artist = selected["media_id"], title, artist
                    case["receipt"] = receipt["event"]
                elif previous_id:
                    get_previous = "id=>{const a=" + active + "; return a.length===1 && a[0].dataset.mediaId===id ? a[0].currentTime : null}"
                    position = await page.evaluate(get_previous, previous_id)
                    assert position is not None
                    await asyncio.sleep(.5)
                    assert (await page.evaluate(get_previous, previous_id)) > position + .2
                    assert not (await server.state())["turns"][-1].get("browser_playback")
                    case["still_playing"] = previous_title
                else:
                    assert not await page.evaluate("()=>" + active + ".length")
                    assert not (await server.state())["turns"][-1].get("browser_playback")
                timing = await page.evaluate("()=>{const {old,start,...rest}=attempts.at(-1);return rest}")
                assert timing["visible_ms"] is not None and timing["trusted"]
                assert len(timing["music"]) == int(play)
                case.update(visible_ms=timing["visible_ms"], reply_ms=timing["reply_ms"],
                            speech_ms=[s["ms"] for s in timing["speech"]],
                            started_ms=[m["ms"] for m in timing["music"]])
                await page.screenshot(path=str(folder / f"step-{pos}.png"))
                print(json.dumps(case, ensure_ascii=False), flush=True)
            assert not report["page_errors"]
            report["passed"] = True
            await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--delegated", action="store_true")
    asyncio.run(verify(parser.parse_args().delegated))
