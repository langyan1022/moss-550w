"""Check independent voice submission while a typed draft is open in muted Edge."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_duplex_voice_browser import PROBE
from verify_natural_stop import wait_state
from verify_voice_correction_browser import PROSE, SAVE_PROMPT, model_fixture


DRAFT = "这条消息晚点发给同事。\n第二句还没写完  "
QUESTION = "现在几点"
RESPONSE_LOSS_QUESTION = "现在几点？"
WINDOW_REQUEST = "识别当前窗口的文字"
WINDOW_COMMAND = "用这段生成回复"
WINDOW_TEXT = "同事：周五下午方便开会吗？"
WINDOW_REPLY = "周五下午可以。"
CALENDAR_REQUEST = "明天上午10点到11点安排日程：隔离环境核对"
SCREEN = """
Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:async()=>{
    const canvas=document.createElement('canvas');canvas.width=640;canvas.height=180;
    const ctx=canvas.getContext('2d');ctx.fillRect(0,0,640,180);
    const stream=canvas.captureStream(15);setTimeout(()=>ctx.fillRect(0,0,1,1),60);
    return stream;
}});
window.draftQaState = null;
addEventListener('moss-agent-state', e => window.draftQaState = e.detail);
"""


async def exercise(server, browser, report, assets, model_closed, stopped, moss):
    context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
    try:
        calls = []
        report["requests"] = calls
        seen = set()
        transcript = QUESTION

        async def route_request(route):
            request = route.request
            path = urlsplit(request.url).path
            if not request.url.startswith(server.url + "/"):
                report["outside"].append(urlsplit(request.url).hostname)
                await route.abort()
            elif path in assets:
                seen.add(path)
                await route.fulfill(body=assets[path], content_type="text/javascript; charset=utf-8")
            elif path == "/fixture/utterance":
                await route.fulfill(path=ROOT / "voice/lines/hello.wav", content_type="audio/wav")
            elif path == "/api/transcribe":
                await route.fulfill(json={"text":transcript})
            elif path == "/api/context/ocr":
                assert request.post_data_buffer.startswith(b"\x89PNG\r\n\x1a\n")
                await route.fulfill(json={"text":WINDOW_TEXT})
            elif path in {"/api/speak", "/api/prepare-speech", "/api/prepare-voice"}:
                await route.fulfill(status=204)
            else:
                if request.method == "POST":
                    body = request.post_data_json
                    calls.append({"path":path, "body":body})
                    if path == "/api/message" and body["text"] == RESPONSE_LOSS_QUESTION:
                        accepted = await route.fetch()
                        assert accepted.ok, await accepted.text()
                        accepted_turn = (await accepted.json())["turns"][-1]
                        assert accepted_turn["user"] == RESPONSE_LOSS_QUESTION
                        report["dropped_response_turn"] = accepted_turn["id"]
                        await route.abort("connectionreset")
                        return
                await route.continue_()

        await context.route("**/*", route_request)
        page = await context.new_page()
        await page.add_init_script(PROBE.replace("'moss.voice.enabled','on'", "'moss.voice.enabled','off'")
                                   .replace("source.start();", "source.start(context.currentTime + .25);") + SCREEN)
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        await page.goto(server.url + "/?chat=1&scene=off")
        await page.locator(".connection.online").wait_for()
        await page.wait_for_function("Boolean(window.draftQaState?.chat_id)")
        await page.bring_to_front()
        composer, mic = page.get_by_test_id("composer"), page.locator("#mic")

        async def client_turn(turn):
            await page.wait_for_function("""row => draftQaState?.turns.some(turn =>
                turn.id === row.id && turn.status === row.status)""", arg=turn)

        async def post_text(text, state):
            status, _ = await server.post("/api/message", {"text":text})
            assert status == 200
            turn = await wait_state(server, lambda row: row["user"] == text and row["status"] == state)
            await client_turn(turn)
            return turn

        async def recognize(text, calendar_id=None):
            await page.evaluate("""async ([words, calendarId]) => {
                const url = document.querySelector('script[src^="/static/agent-terminal.js"]').src;
                await (await import(url)).submitRecognizedText(words, calendarId);
            }""", [text, calendar_id])

        async def verify_blocked(expected):
            before = len(calls)
            await recognize(QUESTION)
            assert len(calls) == before
            assert await composer.input_value() == DRAFT
            await expect(page.locator("#status")).to_have_text(expected)

        await mic.click()
        await page.locator("#mic[aria-pressed=true]").wait_for()
        for starting_draft in (DRAFT, ""):
            await composer.fill(starting_draft)
            previous = len(calls)
            await page.evaluate("sayFixture('utterance')")
            await page.get_by_test_id("turn").nth(previous).get_by_test_id("final").wait_for()
            state = await server.state()
            turn = state["turns"][-1]
            assert turn["user"] == QUESTION and turn.get("holo_tools") == ["get_time"], turn
            assert turn["cloud_requests"]["total"] == 0 and await composer.input_value() == starting_draft
            assert [item["path"] for item in calls[previous:]] == ["/api/message"]
            assert calls[-1]["body"]["text"] == QUESTION and calls[-1]["body"]["chat_id"] == state["chat_id"]
            report["cases"].append({"case":"continuous WAV input", "typed_draft": bool(starting_draft),
                                    "received":turn["reply"], "draft_preserved":True})
        trace = await page.evaluate("duplexSnapshot()")
        assert len(trace["clicks"]) == len(trace["tracks"]) == 1 and trace["tracks"][0]["state"] == "live"
        await mic.click()
        await page.wait_for_function("duplexQa.tracks[0].readyState==='ended'")

        await composer.fill(DRAFT)
        await page.locator(".preferences summary").click()
        wake = page.locator("#wake-listen")
        await wake.check()
        await expect(page.locator("#wake-status")).to_have_text("等待唤醒")
        before, asr_before = len(calls), await page.evaluate("duplexQa.calls.filter(row=>row.url==='/api/transcribe').length")
        await page.evaluate("sayFixture('utterance')")
        await page.wait_for_function("""n => duplexQa.calls.filter(row=>row.url==='/api/transcribe' && row.text).length > n""",
                                     arg=asr_before)
        await page.wait_for_timeout(250)
        assert len(calls) == before and await composer.input_value() == DRAFT
        transcript = "你好，莫斯，现在几点"
        await page.evaluate("sayFixture('utterance')")
        await page.get_by_test_id("turn").nth(before).get_by_test_id("final").wait_for()
        assert len(calls) == before + 1 and calls[-1]["path"] == "/api/message"
        assert calls[-1]["body"]["text"] == QUESTION and await composer.input_value() == DRAFT
        await wake.uncheck()
        await page.wait_for_function("duplexQa.tracks.length===2 && duplexQa.tracks.every(track=>track.readyState==='ended')")
        await page.locator(".preferences summary").click()
        report["cases"].append({"case":"authorized inline wake", "ambient_posted":False, "draft_preserved":True})

        before = len(calls)
        status_before = await page.locator("#status").inner_text()
        await recognize(RESPONSE_LOSS_QUESTION)
        await expect(page.locator("#error")).to_have_text("连接中断，请等待重新连接后查看本轮状态。")
        await expect(page.locator("#status")).to_have_text(status_before)
        completed = await wait_state(server, lambda row: row["id"] == report["dropped_response_turn"] and row["status"] == "replied")
        await client_turn(completed)
        assert len(calls) == before + 1 and await composer.input_value() == DRAFT
        assert sum(row["user"] == RESPONSE_LOSS_QUESTION for row in (await server.state())["turns"]) == 1
        report["cases"].append({"case":"accepted voice request with lost HTTP response", "turn_id":completed["id"],
                                "no_automatic_retry":True, "draft_preserved":True})

        running = await post_text(PROSE, "thinking")
        streaming = await wait_state(server, lambda row: row["id"] == running["id"] and bool(row.get("partial_reply")))
        await client_turn(streaming)
        await verify_blocked("请等本轮结束后再说一次。")
        assert (await server.post("/api/cancel", {"turn_id":running["id"]}))[0] == 200
        assert await asyncio.wait_for(model_closed.get(), 5) == "prose"
        report["cases"].append({"case":"busy generation", "no_submit":True, "draft_preserved":True})

        pending = await post_text(SAVE_PROMPT, "pending")
        await verify_blocked("请等本轮结束后再说一次。")
        await recognize("取消保存")
        cancelled = await wait_state(server, lambda row: row["id"] == pending["id"] and row["status"] == "cancelled")
        await client_turn(cancelled)
        assert not list((server.data / "drafts").iterdir()) and await composer.input_value() == DRAFT
        assert calls[-1]["path"] == "/api/cancel" and calls[-1]["body"]["turn_id"] == pending["id"]
        report["cases"].append({"case":"pending draft cancellation", "draft_preserved":True})

        calendar = await post_text(CALENDAR_REQUEST, "pending")
        before = len(calls)
        await recognize("确认创建", "expired-qa")
        assert len(calls) == before and await composer.input_value() == DRAFT
        await recognize("确认创建", calendar["id"])
        saved = await wait_state(server, lambda row: row["id"] == calendar["id"] and bool(row.get("calendar_receipt")))
        await client_turn(saved)
        assert saved["calendar_receipt"]["operation_id"] == calendar["id"]
        assert calls[-1]["body"].get("pending_calendar_only") is True and await composer.input_value() == DRAFT
        report["cases"].append({"case":"scoped calendar approval", "stale_ignored":True, "draft_preserved":True})

        origin = await post_text(WINDOW_REQUEST, "replied")
        row = page.locator(f'[data-turn-id="{origin["id"]}"]')
        await row.get_by_test_id("context-action").click()
        await expect(row.get_by_test_id("context-output")).to_have_text(WINDOW_TEXT)
        await row.get_by_role("button", name="用这段文字拟回复").click()
        await recognize(WINDOW_COMMAND)
        drafted = await wait_state(server, lambda turn: turn.get("reply") == WINDOW_REPLY)
        await client_turn(drafted)
        packet = calls[-1]["body"]
        assert packet["reply_context_turn"] == origin["id"] and packet["text"] == f"帮我回复：“{WINDOW_TEXT}”"
        assert DRAFT not in packet["text"] and await composer.input_value() == DRAFT
        before = len(calls)
        await recognize(WINDOW_COMMAND)
        assert len(calls) == before and await composer.input_value() == DRAFT
        await expect(page.locator("#status")).to_have_text("请先打开当前来信的拟回复编辑框。")
        report["cases"].append({"case":"scoped window draft", "stale_ignored":True, "draft_preserved":True})

        stopped.set()
        moss._notify()
        await page.locator(".connection.online").wait_for(state="hidden")
        await verify_blocked("连接中断，语音未提交。")
        stopped.clear()
        await page.locator(".connection.online").wait_for()
        before = len(calls)
        await recognize(QUESTION)
        assert len(calls) == before + 1 and calls[-1]["body"]["text"] == QUESTION
        assert await composer.input_value() == DRAFT
        report["cases"].append({"case":"event-stream disconnect and reconnect", "no_submit_until_reconnected":True,
                                "draft_preserved":True})
        denied = await context.new_page()
        await denied.add_init_script("""
            window.deniedOpens = 0;
            navigator.mediaDevices.getUserMedia = async () => {
                window.deniedOpens++;
                throw new DOMException('fixture', 'NotAllowedError');
            };
        """)
        await denied.goto(server.url + "/?chat=1&scene=off")
        await denied.locator(".connection.online").wait_for()
        await denied.get_by_test_id("composer").fill(DRAFT)
        await denied.locator(".preferences summary").click()
        denied_wake = denied.locator("#wake-listen")
        before = len(calls)
        await denied_wake.click()
        await expect(denied.locator("#wake-status")).to_contain_text("未授予麦克风权限")
        await expect(denied_wake).not_to_be_checked()
        assert len(calls) == before and await denied.get_by_test_id("composer").input_value() == DRAFT
        assert await denied.evaluate("window.deniedOpens") == 1
        report["cases"].append({"case":"wake denied permission", "no_submit":True, "draft_preserved":True})
        assert seen == set(assets) and not report["outside"] and not report["errors"], report
    except Exception:
        if "page" in locals():
            report["last_browser_state"] = await page.evaluate("""() => ({status:document.getElementById('status')?.textContent,
                speech:window.duplexSnapshot?.(), ready:window.draftQaState?.chat_id,
                draft:document.getElementById('message')?.value})""")
        raise
    finally:
        await context.close()


async def run(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("duplex-draft-input-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result, flush=True)
    assets = {f"/static/{name}": (candidate / name).read_bytes() for name in ("app.js", "agent-terminal.js")} if candidate else {}
    report = {"passed":False, "assets":{path:sha256(data).hexdigest() for path, data in assets.items()},
              "cases":[], "model_requests":[], "outside":[], "errors":[]}
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    env = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_MEDIA_DB":str(catalog),
           "MOSS_PROVIDER_DIR":str(folder / "providers"), "MOSS_MEM0_ENABLED":"0",
           "MOSS_WINDOWS_REMINDERS":"0", "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0"}

    async def local_draft(jev, *_args, **_kwargs):
        assert jev["incoming"] == WINDOW_TEXT
        return WINDOW_REPLY, {"selector":"fixture", "candidate_count":1}

    async def host(app, port, stack):
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", port).start()

    try:
        async with AsyncExitStack() as stack:
            model_port = free_port()
            model_url = f"http://127.0.0.1:{model_port}/v1"
            model, closed = model_fixture(report)
            await host(model, model_port, stack)
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    env.update({prefix + "_BASE_URL":model_url, prefix + "_API_KEY":"isolated-qa"})
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch("assistant_brain.draft_reply", local_draft))
            server = BrowserServer(folder, free_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "draft-input-fixture")
            stack.push_async_callback(moss.close)
            stopped = asyncio.Event()
            app = application(moss, lambda:"standby", server.data, server.port, "isolated", stopped)
            await host(app, server.port, stack)
            try:
                playwright = await stack.enter_async_context(async_playwright())
                edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                    args=["--disable-gpu", "--mute-audio", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"])
                stack.push_async_callback(browser.close)
                await confirm_muted_edge(browser)
                await exercise(server, browser, report, assets, closed, stopped, moss)
                assert report["model_requests"] == ["prose", "draft"], report["model_requests"]
                report["passed"] = True
            finally:
                stopped.set()
                moss._notify()
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, help="Override app.js and agent-terminal.js in the isolated browser")
    asyncio.run(run(parser.parse_args().candidate))
