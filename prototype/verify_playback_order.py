import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
import shutil
import sqlite3
from time import time_ns

from browser_lab import ROOT
from local_media.library import MediaLibrary
from memory_store import install_schema
from verify_configured_mcp import isolated


def recorded(name):
    path = ROOT / "runs" / name / "data/session.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("""SELECT doc FROM turns WHERE json_extract(doc, '$.browser_playback.event')='started'
                          ORDER BY json_extract(doc, '$.browser_playback.at')""")
        return [json.loads(raw) for raw, in rows]


async def verify(baseline):
    folder = ROOT / "runs" / datetime.now().strftime("playback-order-%Y%m%d-%H%M%S-%f")
    server = isolated(folder)
    server.data.mkdir(parents=True)
    MediaLibrary(folder / "empty.sqlite3")
    os.environ.update(MOSS_MEDIA_DB=str(folder / "empty.sqlite3"), MOSS_MEM0_ENABLED="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_PROVIDER_DIR=str(folder / "providers"))
    archive = "playback-facts-20261120-172003-692979"
    earlier_archive = "playback-facts-20261120-150930-877150"
    first, second = recorded(archive)
    prior = next(t for t in recorded(earlier_archive)
                 if t["browser_playback"]["media_id"] == first["browser_playback"]["media_id"])
    legacy = recorded("playlist-recovery-20261120-140007")[-1]
    assert "play_id" not in legacy["browser_playback"]
    starts = [prior["browser_playback"], second["browser_playback"], first["browser_playback"]]
    assert len({r["play_id"] for r in starts}) == 3
    report = {"passed": False, "baseline": baseline, "archives": [earlier_archive, archive],
              "scope": "真实浏览器存档的曲目和播放标识，隔离 HTTP 重放，控制发生时间和交付顺序。", "answers": []}
    with closing(sqlite3.connect(server.data / "session.sqlite3")) as db:
        chat = install_schema(db)
        with db:
            for turn in (legacy, first, second):
                record = dict(turn, chat_id=chat)
                if turn is not legacy:
                    record.pop("browser_playback")
                db.execute("INSERT INTO turns(id,doc,chat_id) VALUES(?,?,?)",
                           (turn["id"], json.dumps(record, ensure_ascii=False), chat))
    # 三个开播时点依次指向甲、乙、重播甲。重新绑定到同一隔离会话的两条真实媒体记录。
    at = time_ns() // 1000 - 3_000_000
    events = [{"chat_id": chat, "turn_id": t["id"], "media_id": r["media_id"], "event": "started",
               "play_id": r["play_id"], "started_at_us": at + i * 500_000}
              for i, (t, r) in enumerate(zip((first, second, first), starts, strict=True))]

    async def answer(expected):
        status, state = await server.post("/api/message", {"chat_id": chat, "text": "刚才听的是哪首？"})
        assert status == 200, state
        async with asyncio.timeout(8):
            while not (last := state["turns"][-1]).get("reply"):
                await asyncio.sleep(.02)
                state = await server.state()
        assert expected["media"]["selected"]["title"] in last["reply"], last["reply"]
        assert last["cloud_requests"]["total"] == 0
        report["answers"].append(last["reply"])

    async def receipt(payload, expected=200):
        status, body = await server.post("/api/media-event", payload)
        assert status == expected, (status, body)

    try:
        await server.start()
        await answer(legacy)
        await receipt(events[1])
        await receipt(events[0])
        await answer(first if baseline else second)
        if not baseline:
            await server.stop()
            await server.start()
            await answer(second)
            await receipt(events[2])
            await answer(first)
            await server.stop()
            await server.start()
            revision = (await server.state())["revision"]
            await receipt(events[0])
            await receipt(events[2])
            state = await server.state()
            assert state["revision"] == revision
            row = next(t for t in state["turns"] if t["id"] == first["id"])
            assert row["browser_playback"]["play_id"] == events[2]["play_id"]
            await answer(first)
            await receipt(dict(events[0], started_at_us=time_ns() // 1000 + 3_600_000_000), 409)
            await receipt(dict(events[0], started_at_us=True), 409)
            status, state = await server.post("/api/new-chat", {})
            assert status == 200 and not state["turns"]
            await receipt(events[1], 409)
            await receipt(dict(events[1], chat_id=state["chat_id"]), 409)
            assert not (await server.state())["turns"]
            report["same_track_stale_and_duplicate"] = "重启后旧回执和重复回执未改变已存记录"
            report["cross_chat"] = "旧对话及冒用新对话标识的回执均返回 409"
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


async def verify_browser():
    from playwright.async_api import async_playwright
    from browser_lab import confirm_muted_edge, open_browser
    from verify_memory import send

    folder = ROOT / "runs" / datetime.now().strftime("playback-order-edge-%Y%m%d-%H%M%S-%f")
    music = folder / "两段隔离音频"
    music.mkdir(parents=True)
    for name, title in (("welcome", "声场甲"), ("done", "声场乙")):
        shutil.copyfile(ROOT / "voice/lines" / (name + ".wav"), music / (title + ".wav"))
    library = MediaLibrary(folder / "catalog.sqlite3")
    library.configure([{"id":"fixture", "label":"隔离音频", "kind":"local", "path":str(music)}])
    library.scan()
    os.environ.update(MOSS_MEDIA_DB=str(library.db_path), MOSS_WINDOWS_REMINDERS="0",
                      MOSS_MEM0_ENABLED="0", MOSS_PROVIDER_DIR=str(folder / "providers"))
    server = isolated(folder)
    outgoing, delivered, advances = [], [], []
    done = asyncio.Event()
    report = {"passed":False, "outgoing":outgoing, "delivered":delivered, "played_seconds":advances}
    try:
        await server.start()
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script("""localStorage.setItem('moss.voice.enabled','off');
                    window.actualStarts = [];
                    document.addEventListener('playing', e => {
                      if(e.target.matches('.media-audio')) actualStarts.push({id:e.target.dataset.mediaId,
                        trusted:e.isTrusted, micros:Math.round((performance.timeOrigin+e.timeStamp)*1000)});
                    }, true);""")

                async def hold_first(route):
                    payload = route.request.post_data_json
                    if payload["event"] != "started":
                        await route.continue_()
                        return
                    outgoing.append(payload)
                    index = len(outgoing) - 1
                    if index == 0:
                        await asyncio.wait_for(done.wait(), 15)
                    response = await route.fetch()
                    delivered.append({"id":payload["media_id"], "status":response.status})
                    await route.fulfill(response=response)
                    if index == 1:
                        done.set()

                await page.route("**/api/media-event", hold_first)
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                for i, title in enumerate(("声场甲", "声场乙")):
                    await send(page, server, "播放《" + title + "》")
                    await page.wait_for_function("""i => {
                        const id = actualStarts[i]?.id;
                        const node = [...document.querySelectorAll('.media-audio')].find(n=>n.dataset.mediaId===id);
                        return node?.played.length && node.played.end(0)>.08;
                    }""", arg=i, timeout=8000)
                    advances.append(await page.evaluate("""i => [...document.querySelectorAll('.media-audio')]
                        .find(n=>n.dataset.mediaId===actualStarts[i].id).played.end(0)""", i))
                async with asyncio.timeout(5):
                    while len(delivered) < 2:
                        await asyncio.sleep(.02)
                report["actual_starts"] = await page.evaluate("actualStarts")
                assert len(outgoing) == len(report["actual_starts"]) == 2
                assert all(event["trusted"] and event["micros"] == sent["started_at_us"] and
                           event["id"] == sent["media_id"] for event, sent in zip(report["actual_starts"], outgoing))
                assert outgoing[0]["started_at_us"] < outgoing[1]["started_at_us"]
                assert delivered == [{"id":r["media_id"], "status":200} for r in reversed(outgoing)]
                report["answer"] = await send(page, server, "刚才听的是哪首？")
                assert "声场乙" in report["answer"]
                assert all(t["cloud_requests"]["total"] == 0 for t in (await server.state())["turns"])
                report["passed"] = True
            finally:
                done.set()
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--edge", action="store_true")
    options = parser.parse_args()
    asyncio.run(verify_browser() if options.edge else verify(options.baseline))
