import argparse
import asyncio
from datetime import datetime
import json
import os
import re
from unittest.mock import patch
from urllib.parse import urljoin

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from serve import application
from verify_context_reply import free_port
from verify_duplex_voice_browser import PROBE, make_voice, recorded_fixture
from voice_output import MossSpeech


CREATE = "明天上午十点到十一点安排日程：产品评审"
APPROVAL = "确认创建"
UNSENT = "还没发送的文字"
TRACK = PROBE.replace("'/api/cancel'].includes", "'/api/cancel','/api/approve'].includes").replace(
    "if (url === '/api/cancel') entry.turnId = JSON.parse(options.body).turn_id;",
    "if (['/api/approve','/api/cancel'].includes(url)) entry.payload = JSON.parse(options.body);")


async def exercise(page, moss, report, *, baseline):
    mic = page.locator("#mic")
    await mic.click()
    await expect(mic).to_have_attribute("aria-pressed", "true")
    composer = page.get_by_test_id("composer")

    async def propose(text):
        await composer.fill(text)
        await page.get_by_test_id("send").click()
        row = page.get_by_test_id("turn").last
        await expect(row.get_by_text("新建本机日程")).to_be_visible()
        turn = moss.state()["turns"][-1]
        return row, turn

    async def recognize(clip, word):
        await page.evaluate("name => sayFixture(name)", clip)
        match = await page.wait_for_function(r"""word => duplexQa.calls.find(row => row.url==='/api/transcribe' &&
            row.status===200 && row.text?.replace(/[\s\p{P}]+/gu,'')===word)""", arg=word, timeout=16000)
        value = await match.json_value()
        report.setdefault("asr", []).append(value)
        assert value["live"] == 1, value

    row, proposal = await propose(CREATE)
    key = proposal["id"]
    await page.wait_for_function("id => duplexQa.playback.some(row=>row.id===id && row.name==='end')", arg=key, timeout=18000)
    await page.wait_for_function("id => performance.now()-duplexQa.playback.find(row=>row.id===id && row.name==='end').at>450", arg=key)
    await composer.fill(UNSENT)
    await recognize("approve", APPROVAL)
    if baseline:
        await expect(composer).to_have_value(re.compile(APPROVAL))
        assert moss.state()["turns"][-1]["status"] == "pending"
        assert moss.db.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 0
        report["reproduced"] = "语音转写正确，确认只被留在输入框，日程仍待确认。"
        return
    await expect(row.get_by_test_id("final")).to_contain_text("产品评审")
    await expect(composer).to_have_value(UNSENT)
    saved = moss.state()["turns"][-1]
    assert saved["calendar_receipt"]["operation_id"] == key
    report["confirmed_id"] = key
    await page.wait_for_function("""id => duplexQa.playback.some(row => row.id===id && row.name==='start' &&
        row.text?.includes('已保存') && duplexQa.playback.some(end => end.id===id && end.name==='end' && end.at > row.at))""",
                                 arg=key, timeout=18000)

    row, second = await propose(CREATE.replace("产品评审", "样品复查"))
    active_key = second["id"]
    await page.wait_for_function("id=>duplexQa.playback.some(row=>row.id===id && row.name==='start')", arg=active_key)
    context = {"chatId":second["chat_id"], "turnId":active_key, "calendarTurn":active_key, "heardInPlayback":False}
    rejected = await page.evaluate("""async context => {
        const reject = window.mossSubmitOverlappingDecision;
        return Promise.all([
            {original:'确认创建吧，不。', filtered:'确认创建'},
            {original:'确认创建', filtered:'确认创建', heardInPlayback:true},
            {original:'确认创建', filtered:'确认创建', turnId:'old'},
            {original:'确认创建', filtered:'确认创建', chatId:'old'},
            {original:'确认创建', filtered:'确认创建', calendarTurn:'old'},
        ].map(x => reject({...context,...x})));
    }""", context)
    assert rejected == [False] * 5, rejected
    assert await page.evaluate("""({chatId, key}) => window.mossSubmitSpokenAction('确认创建', chatId, key)""",
                               {"chatId":second["chat_id"], "key":key}) is False
    await composer.fill(UNSENT)
    await recognize("cancel", "取消")
    assert report["asr"][-1]["playing"], report["asr"][-1]
    await expect(row).to_have_attribute("data-status", "cancelled")
    await expect(composer).to_have_value(UNSENT)
    assert await page.evaluate("chat => window.mossSubmitSpokenAction('确认创建', chat)", second["chat_id"]) is False
    trace = await page.evaluate("duplexSnapshot()")
    actions = [call for call in trace["calls"] if call["url"] in {"/api/approve", "/api/cancel"}]
    assert [(call["url"], call["payload"], call["status"]) for call in actions] == [
        ("/api/approve", {"turn_id":key, "pending_calendar_only":True}, 200),
        ("/api/cancel", {"turn_id":active_key, "pending_calendar_only":True}, 200),
    ], actions
    for verb in ("approve", "cancel"):
        stale = await page.request.post(urljoin(page.url, "/api/" + verb), data={"turn_id":active_key, "pending_calendar_only":True})
        assert stale.status == 409
    assert moss.db.execute("SELECT title FROM calendar_events").fetchall() == [("产品评审",)]
    await mic.click()
    await page.wait_for_function("duplexQa.tracks.length===1 && duplexQa.tracks[0].readyState==='ended'")
    report.update(rejected_overlap=len(rejected), held_input_preserved=True, scoped_actions=actions, database_rows=1)


async def run(baseline):
    folder = ROOT / "runs" / datetime.now().strftime("calendar-voice-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed":False, "baseline":baseline, "page_errors":[], "model_connections":0,
              "input":"Windows Mandarin audio fixture via Edge MediaStream and actual SenseVoice", "output":"MOSS playback fixture"}
    for name, wording in (("approve", APPROVAL), ("cancel", "取消")):
        target = folder / name
        target.mkdir()
        make_voice(target, text=wording)
    _, long_playback = recorded_fixture()
    short_playback = (ROOT / "voice/lines/done.wav").read_bytes()
    catalog = folder / "media.sqlite3"
    MediaLibrary(catalog)

    async def model_guard(_reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def output(_speaker, text, disconnected, category="reply", cached_only=False, *, timings=None):
        assert text and not disconnected()
        return long_playback if category == "confirm" else short_playback

    async def fixture(request):
        name = request.match_info["name"]
        if name not in {"approve", "cancel"}:
            raise web.HTTPNotFound()
        return web.FileResponse(folder / name / "command.wav")

    env = {"MOSS_MEDIA_DB":str(catalog), "MOSS_PROVIDER_DIR":str(folder / "providers"),
           "LOCALAPPDATA":str(folder / "profile"), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
           "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0", "CUDA_VISIBLE_DEVICES":""}
    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            port = free_port()
            with patch.dict(os.environ, env), patch.object(MossSpeech, "render", output):
                moss = Moss(folder / "data", model_url, "isolated-calendar-voice")
                app = application(moss, lambda: "standby", folder / "data", port, "calendar-voice", asyncio.Event())
                app.router.add_get("/fixture/{name}", fixture)
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", port).start()
                    async with async_playwright() as playwright:
                        browser = await open_browser(playwright)
                        try:
                            await confirm_muted_edge(browser)
                            page = await browser.new_page(permissions=["microphone"], reduced_motion="reduce")
                            page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))
                            await page.add_init_script(TRACK)
                            await page.goto(f"http://127.0.0.1:{port}/?chat=1&scene=off", wait_until="domcontentloaded")
                            await page.locator(".connection.online").wait_for()
                            await page.bring_to_front()
                            await exercise(page, moss, report, baseline=baseline)
                        finally:
                            if "page" in locals():
                                report["browser"] = await page.evaluate("window.duplexSnapshot?.()")
                            await browser.close()
                finally:
                    await runner.cleanup()
                    await moss.close()
        assert not report["page_errors"] and not report["model_connections"], report
        report["passed"] = True
    finally:
        path = folder / "result.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(path, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    asyncio.run(run(parser.parse_args().baseline))
