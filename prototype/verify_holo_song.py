import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from media_chat import DEFAULT_DB, MediaChat
from memory_store import install_schema
from verify_artist_request import OBSERVER
from verify_memory import send


async def verify_editions(folder):
    root = folder / "test-music"
    artist = root / "测试歌手"
    artist.mkdir(parents=True)
    titles = ["白帆", "白帆(Live)", "白帆之外"]
    for title in titles:
        with wave.open(str(artist / f"{title}.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "NONE"))
            wav.writeframes(b"\0\0" * 16000)
    library = MediaLibrary(folder / "editions.sqlite3")
    library.configure([{"id": "test", "label": "测试音乐", "kind": "local", "path": str(root)}])
    library.scan()
    media = MediaChat(folder)
    media.db = library.db_path
    turn = {"chat_id": uuid4().hex, "id": uuid4().hex}
    original = {"action": "named_song", "title": "白帆", "artist": "测试歌手"}
    _, view = await media.execute(original, None, turn)
    assert view["autoplay"] and [i["title"] for i in view["items"]] == titles[:2]
    assert view["selected"]["title"] == "白帆"
    (artist / "白帆.wav").rename(artist / "test-unavailable.wav")
    _, view = await media.execute(original, None, {**turn, "id": uuid4().hex})
    assert view["selected"]["title"] == "白帆(Live)" and view["autoplay"]
    (artist / "白帆(Live).wav").rename(artist / "test-live-unavailable.wav")
    reply, view = await media.execute(original, None, {**turn, "id": uuid4().hex})
    assert not view["selected"] and not view["autoplay"] and "无法打开" in reply


async def verify(model, tool_only, archive=None, speech=False):
    folder = ROOT / "runs" / datetime.now().strftime("holo-song-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    db = folder / "catalog.sqlite3"
    with closing(sqlite3.connect(DEFAULT_DB.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(db)) as target:
            source.backup(target)
    os.environ["MOSS_MEDIA_DB"] = str(db)
    media = MediaChat(folder)
    server = BrowserServer(folder, 18532)
    report = {"passed": False, "model": model, "speech_enabled": speech, "tool": {}, "turns": []}
    try:
        await verify_editions(folder)
        report["edition_order_and_unavailable"] = True
        chat = uuid4().hex
        for phrase, expected in (("冰雨", True), ("刘德华的冰雨", True), ("刀郎的冰雨", False)):
            intent = await media.named_request(phrase)
            assert intent and intent["action"] == "named_song", phrase
            reply, view = await media.execute(intent, None, {"chat_id": chat, "id": uuid4().hex})
            selected = view["selected"]
            assert bool(selected) == expected and view["online_calls"] == 0, (phrase, view)
            if selected:
                assert selected["title"] == "冰雨(Live)" and selected["artist"] == "刘德华"
                assert selected["artist_source"] == "folder" and Path(selected["path"]).parent.name == "刘德华"
                assert view["autoplay"] == (phrase == "刘德华的冰雨")
            report["tool"][phrase] = {"reply": reply, "count": len(view["items"]), "id": selected and selected["media_id"]}
        for phrase in ("谢谢", "不要播放刘德华的冰雨", "介绍一下刘德华的冰雨", "刘德华的冰雨是谁唱的？"):
            assert await media.named_request(phrase) is None, phrase
        if tool_only:
            report["passed"] = True
            return
        if archive:
            origin = json.loads(archive.read_text(encoding="utf-8-sig"))
            assert origin["user"] == "刘德华的冰雨" and not origin.get("media") and not origin.get("holo_tools")
            server.data.mkdir()
            with sqlite3.connect(server.data / "session.sqlite3") as history:
                install_schema(history)
                history.execute("INSERT INTO chats VALUES (?,?,?)", (origin["chat_id"], "归档点歌", origin["created_at"]))
                history.execute("UPDATE state SET active_chat=? WHERE id=1", (origin["chat_id"],))
                history.execute("INSERT INTO turns(id,doc,chat_id) VALUES(?,?,?)",
                                (origin["id"], json.dumps(origin, ensure_ascii=False), origin["chat_id"]))
        await server.start()
        status, _ = await server.post("/api/select-model", {"id": model})
        assert status == 200
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            await confirm_muted_edge(browser)
            try:
                page = await browser.new_page(viewport={"width": 1360, "height": 820}, reduced_motion="reduce")
                voice = "on" if speech else "off"
                await page.add_init_script(f"localStorage.setItem('moss.voice.enabled','{voice}')")
                await page.add_init_script(OBSERVER)
                page.set_default_timeout(90000)
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                prompts = ("直接放吧，别废话了",) if archive else ("冰雨", "刘德华的冰雨", "直接放吧，别废话了", "刀郎的冰雨")
                for phrase in prompts:
                    reply = await send(page, server, phrase)
                    state = await server.state()
                    turn = state["turns"][-1]
                    view = turn.get("media") or {}
                    selected = view.get("selected") or {}
                    event = {"question": phrase, "reply": reply, "source": turn.get("source"),
                             "tool": turn.get("holo_tools"), "engine": state["agent_mode"],
                             "count": len(view.get("items") or []), "id": selected.get("media_id")}
                    report["turns"].append(event)
                    assert "哪个版本" not in reply and "哪一个版本" not in reply, event
                    assert view.get("online_calls") == 0, event
                    if phrase == "刀郎的冰雨":
                        assert not view.get("autoplay") and not view.get("selected") and "没有" in reply, event
                    elif phrase == "直接放吧，别废话了" and not archive:
                        assert not view.get("autoplay") and "本页" in reply, event
                        before = await page.evaluate("() => [...document.querySelectorAll('.media-audio')].find(x=>!x.paused).currentTime")
                        await page.wait_for_function("t=>[...document.querySelectorAll('.media-audio')].some(x=>!x.paused && x.currentTime>t+.25)", arg=before)
                        assert not (await page.evaluate("()=>attempts.at(-1).music")), event
                    else:
                        assert selected["media_id"] == report["tool"]["冰雨"]["id"], event
                        if phrase == "刘德华的冰雨" or archive:
                            assert view.get("autoplay") and selected["artist"] == "刘德华", event
                        if not archive:
                            assert event["engine"] == "holojarvis" and event["tool"] == ["play_song"], event
                        if view.get("autoplay"):
                            await page.wait_for_function("id=>[...document.querySelectorAll('.media-audio')].some(x=>x.dataset.audioTurn===id && !x.paused && x.currentTime>.2)", arg=turn["id"])
                            async with asyncio.timeout(10):
                                while True:
                                    row = (await server.state())["turns"][-1]
                                    receipt = row.get("browser_playback") or {}
                                    if receipt.get("event") == "started" and receipt.get("media_id") == selected["media_id"]:
                                        break
                                    await asyncio.sleep(.05)
                            event["started"] = receipt["event"]
                            event["started_ms"] = (await page.evaluate("()=>attempts.at(-1).music"))[0]["ms"]
                            event["speech_started_ms"] = await page.evaluate("()=>attempts.at(-1).speech.map(s=>s.ms)")
                            if speech:
                                assert len(event["speech_started_ms"]) == 1, event
                    print(event, flush=True)
                assert any(event.get("started") == "started" for event in report["turns"])
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="local")
    parser.add_argument("--tool-only", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--speech", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify(args.model, args.tool_only, args.archive, args.speech))
