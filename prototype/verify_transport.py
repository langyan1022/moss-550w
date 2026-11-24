import asyncio
from datetime import datetime
import json
import sqlite3
from time import perf_counter
from uuid import uuid4

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from media_transport import parse_transport
from verify_memory import send
from verify_playlist_recovery import ARCHIVE, restore_list


async def check_clip(browser, folder):
    server = BrowserServer(folder / "clip", 18482, model_url="http://127.0.0.1:18278/v1")
    server.data.mkdir(parents=True)
    with sqlite3.connect(ARCHIVE.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(server.data / "session.sqlite3") as db:
        original.backup(db)
        doc, = db.execute("""SELECT doc FROM turns WHERE json_extract(doc,'$.media.playback.clip_start_s')>0
            AND chat_id=(SELECT active_chat FROM state WHERE id=1) LIMIT 1""").fetchone()
    turn = json.loads(doc)
    start = turn["media"]["playback"]["clip_start_s"]
    await server.start()
    try:
        context = await browser.new_context(viewport={"width":1440, "height":900}, reduced_motion="reduce")
        await context.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
        page = await context.new_page()
        await page.goto(server.url + "?chat=1", wait_until="domcontentloaded")
        await page.locator(f'[data-turn-id="{turn["id"]}"] [data-testid="open-media"]').click()
        await page.locator("#tool-player .player-toggle").click()
        audio = page.locator(f'[data-audio-turn="{turn["id"]}"]')
        await page.wait_for_function("""([id,start])=>{const a=document.querySelector('[data-audio-turn="'+id+'"]');
            return a && !a.paused && a.currentTime > start+.15;}""", arg=[turn["id"], start])
        reply = await send(page, server, "停止播放")
        actual = await audio.evaluate("a=>({paused:a.paused, position:a.currentTime, src:a.currentSrc})")
        assert "回到起点" in reply and actual["paused"] and abs(actual["position"] - start) < .1, (reply, start, actual)
        await context.close()
        return {"start":start, **actual, "reply":reply}
    finally:
        await server.stop()


def check_transport_phrases():
    protected = ["保存草稿：暂停音乐", "不要暂停音乐", "别停止播放", "音乐暂停了吗？", "怎么停止音乐",
                 "请问如何继续播放", "我说的是“暂停音乐”", "如果我说暂停音乐，就别再播报", "暂停音乐然后删草稿",
                 "我说的是“别放了”", "先别放这首了然后删草稿", "别放了？", "不要播放"]
    assert all(parse_transport(text, True, stoppable=True) is None for text in protected)
    assert all(parse_transport(text, True, stoppable=True, explicit=True) is None for text in [
        *protected, "停止", "暂停", "继续", "停一下", "我不听了",
    ])
    assert parse_transport("请把音乐暂停一下", explicit=True) == ("pause", "audio")
    assert parse_transport("继续播放。", explicit=True) == ("resume", None)
    assert parse_transport("别放这个视频了", explicit=True) == ("stop", "video")
    assert parse_transport("继续", False) is None and parse_transport("继续", True) == ("resume", None)
    assert parse_transport("请把音乐暂停一下", stoppable=True) == ("pause", "audio")
    assert parse_transport("恢复播放音乐", True) == ("resume", "audio")
    colloquial = {"别放了":None, "别播了":None, "先别放这首了":"audio", "不要播放这首":"audio",
                  "我不听了":"audio", "别播这个视频了":"video"}
    for text, kind in colloquial.items():
        assert parse_transport(text, True, stoppable=True) == ("stop", kind)
        assert parse_transport(text, False, stoppable=True) == ("stop", kind)
        assert parse_transport(text, True) is None and parse_transport(text, False) is None
    return {"guarded_phrases":len(protected), "colloquial_phrases":len(colloquial)}


async def check_colloquial_queue(server, source):
    item = source["media"]["selected"]
    request_id = str(uuid4())
    queued = {"turn_id":source["id"], "media_id":item["media_id"]}
    body = {"text":"先别放这首了", "chat_id":source["chat_id"], "player":{"state":"idle"},
            "queued_player":queued, "transport_request_id":request_id}
    wrong_queue = {**body, "queued_player":{**queued, "media_id":"0"*32}}
    assert (await server.post("/api/message", wrong_queue))[0] == 409
    status, state = await server.post("/api/message", body)
    turn = state["turns"][-1]
    control = turn["transport"]
    assert status == 200 and turn["status"] == "player_requested" and turn["reply"] is None, turn
    assert control["target_turn"] == queued["turn_id"] and control["media_id"] == queued["media_id"]
    assert control["action"] == "stop" and control["control_live"] is False
    receipt = {"turn_id":turn["id"], "chat_id":source["chat_id"], "request_id":request_id,
               "target_turn":source["id"], "media_id":item["media_id"],
               "event":"queue_cancelled", "cancelled_queue":True}
    status, state = await server.post("/api/transport-result", receipt)
    final = state["turns"][-1]
    assert status == 200 and final["transport"]["event"] == "queue_cancelled" and "已取消待播放" in final["reply"], final
    return {"event":final["transport"]["event"], "reply":final["reply"], "foreign_media_id_rejected":True}


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("transport-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18481, model_url="http://127.0.0.1:18278/v1")
    archive, _ = restore_list(server.data)
    report = {"passed": False, "controls": [], "errors": [], **check_transport_phrases()}
    await server.start()
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            await confirm_muted_edge(browser)
            report["muted"] = True
            context = await browser.new_context(viewport={"width":1440, "height":900}, reduced_motion="reduce")
            await context.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            page = await context.new_page()
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            submitted = {}
            def record(request):
                if request.url.endswith("/api/message"):
                    body = request.post_data_json
                    submitted[body["text"]] = body.get("transport_request_id")
            page.on("request", record)
            await page.goto(server.url + "?chat=1", wait_until="domcontentloaded")
            await page.wait_for_function("Boolean(window.mossTransport)")
            assert archive["media"]["items"][1]["title"] in await send(page, server, "第二首")
            source = (await server.state())["turns"][-1]
            audio = page.locator(f'[data-audio-turn="{source["id"]}"]')
            await page.wait_for_function("id=>{const n=document.querySelector('[data-audio-turn=\"'+id+'\"]');return n&&!n.paused&&n.currentTime>.3}", arg=source["id"])
            chat = source["chat_id"]

            async def control(tab, prompt, expected):
                before = perf_counter()
                reply = await send(tab, server, prompt)
                turn = (await server.state())["turns"][-1]
                assert turn.get("source") == "transport" and expected in reply, (prompt, reply, turn)
                report["controls"].append({"text":prompt, "reply":reply, "ms":round((perf_counter()-before)*1000),
                                           "event":(turn.get("transport") or {}).get("event")})
                return turn

            sibling = await context.new_page()
            await sibling.goto(server.url + "?chat=1", wait_until="domcontentloaded")
            await control(sibling, "暂停音乐", "当前页面没有")
            assert await audio.evaluate("n=>!n.paused")
            refusal = await send(sibling, server, "不要播放这首")
            assert "没有发起新的播放" in refusal and await audio.evaluate("n=>!n.paused"), refusal
            report["idle_page_did_not_claim_other_page_playback"] = True
            await control(page, "暂停视频", "没有可以暂停的视频")
            assert await audio.evaluate("n=>!n.paused")
            paused = await control(page, "请把音乐暂停一下", "已暂停")
            assert paused["transport"]["event"] == "paused" and await audio.evaluate("n=>n.paused")
            position = await audio.evaluate("n=>n.currentTime")
            await asyncio.sleep(.18)
            assert abs(await audio.evaluate("n=>n.currentTime") - position) < .05
            resumed = await control(page, "继续播放", "正在播放")
            assert resumed["transport"]["event"] == "playing" and await audio.evaluate("n=>!n.paused")
            stopped = await control(page, "别放了", "已回到起点")
            assert stopped["transport"]["event"] == "stopped" and await audio.evaluate("n=>n.paused&&n.currentTime<.1")
            assert "已停止" in await send(page, server, "现在在播什么？")
            report["colloquial_queue"] = await check_colloquial_queue(server, source)
            before = (await server.state())["turns"][-1]["id"]
            claimed = {"text":"恢复音乐", "chat_id":chat, "transport_request_id":str(uuid4()),
                       "player":{"state":"stopped", "turn_id":source["id"], "media_id":source["media"]["selected"]["media_id"]}}
            status, state = await server.post("/api/message", claimed)
            assert status == 200
            waiting = state["turns"][-1]
            assert waiting["id"] != before and waiting["status"] == "player_requested" and waiting["reply"] is None
            await sibling.reload(wait_until="domcontentloaded")
            await sibling.wait_for_function("Boolean(window.mossTransport)")
            assert await audio.evaluate("n=>n.paused")
            public = (await server.state())["turns"][-1]
            assert public["status"] == "player_requested" and "request_id" not in public["transport"]
            wrong = {"turn_id":waiting["id"], "chat_id":chat, **waiting["transport"], "request_id":str(uuid4()),
                     "event":"playing", "position":0}
            assert (await server.post("/api/transport-result", wrong))[0] == 409
            duplicate = {"turn_id":stopped["id"], "chat_id":chat, **stopped["transport"],
                         "request_id":submitted["别放了"]}
            assert (await server.post("/api/transport-result", duplicate))[0] == 200
            await page.reload(wait_until="domcontentloaded")
            await control(page, "继续播放音乐", "当前页面没有")
            assert await page.locator(".media-audio").evaluate_all("nodes=>nodes.every(n=>n.paused)")
            assert not report["errors"]
            await context.close()
            report["clip"] = await check_clip(browser, folder)
            report["passed"] = True
            await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
