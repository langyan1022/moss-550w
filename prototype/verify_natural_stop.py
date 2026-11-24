import argparse
import asyncio
from contextlib import AsyncExitStack, closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import perf_counter
import wave

from aiohttp import web
import psutil
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge
from local_media.library import MediaLibrary
from verify_configured_mcp import isolated, process_ids
from verify_voice_receipts import listen


SLOW_CLOCK = "用 MCP 取消演练调用 稍后读时间"
GATED_CLOCK = "用 MCP 通道演练调用 稍后读时间"
PENDING_SOURCE = ROOT / "runs/deepseek-draft-recovery-20261120-193021-040897/prepare/session.sqlite3"
STOP_TIMING = """
window.stopTimings = {};
const nativeFetch = window.fetch;
window.fetch = function(input, options) {
  if (input !== '/api/cancel') return nativeFetch.apply(this, arguments);
  const id = JSON.parse(options.body).turn_id;
  const row = stopTimings[id] = {request:performance.now()};
  return nativeFetch.apply(this, arguments).then(response => {
    row.response = performance.now();
    return response;
  });
};
const NativeEventSource = window.EventSource;
window.EventSource = class extends NativeEventSource {
  constructor(...args) {
    super(...args);
    this.addEventListener('state', event => {
      const state = JSON.parse(event.data);
      for (const {id, status} of state.turns) {
        if (stopTimings[id] && ['cancelled','saved'].includes(status)) stopTimings[id].sse ??= performance.now();
      }
    });
  }
};
new MutationObserver(() => {
  for (const [id, row] of Object.entries(stopTimings)) {
    const status = document.querySelector(`[data-turn-id="${id}"] .turn-status`);
    if (['本轮已取消。','草稿已保存。'].includes(status?.textContent)) row.visible ??= performance.now();
  }
}).observe(document, {childList:true, subtree:true, characterData:true});
"""


def setup_catalog(folder):
    music = folder / "music"
    music.mkdir()
    with wave.open(str(music / "隔离停止测试.wav"), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "NONE"))
        audio.writeframes(b"\0\0" * 8000 * 50)
    library = MediaLibrary(folder / "media.sqlite3")
    library.configure([{"id":"isolated", "kind":"local", "path":str(music)}])
    library.scan()
    return str(library.db_path)


async def open_page(browser, server):
    page = await browser.new_page(reduced_motion="reduce")
    await page.add_init_script("localStorage.setItem('moss.voice.enabled', 'off')")
    await page.add_init_script(STOP_TIMING)
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.url, perf_counter())) if request.method == "POST" else None)
    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.locator(".connection.online").wait_for(timeout=10000)
    return page, errors, requests


async def command(page, text):
    await page.get_by_test_id("composer").fill(text)
    await page.get_by_test_id("send").click(timeout=5000)


async def recognized(page, text):
    await page.evaluate("""async text => {
        const terminal = document.querySelector('script[src^="/static/agent-terminal.js"]').src;
        await (await import(terminal)).submitRecognizedText(text);
    }""", text)


async def wait_state(server, condition, *, turn_id=None):
    async with asyncio.timeout(10):
        while True:
            turns = (await server.state())["turns"]
            turn = next((row for row in turns if row["id"] == turn_id), None) if turn_id else (turns[-1] if turns else None)
            if turn and condition(turn):
                return turn
            await asyncio.sleep(.03)


async def wait_process(server, previous_starts, *, argument=None):
    log = server.folder / "web-1.log"
    async with asyncio.timeout(8):
        while log.read_bytes().count(b"SLOW_CLOCK_START") <= previous_starts:
            await asyncio.sleep(.04)
    if argument is None:
        running = process_ids(server)
    else:
        running = set()
        for child in psutil.Process(server.pid).children(recursive=True):
            try:
                if argument in child.cmdline():
                    running.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    assert running
    return running


async def observe_exit(requests, before, url, running):
    async with asyncio.timeout(10):
        while not (started := next((stamp for address, stamp in requests[before:] if address == url), None)):
            await asyncio.sleep(.01)
        while any(psutil.pid_exists(pid) for pid in running):
            await asyncio.sleep(.02)
        return round((perf_counter() - started) * 1000, 1)


async def timings(page, turn_id):
    await page.wait_for_function("id => stopTimings[id]?.sse && stopTimings[id]?.visible && stopTimings[id]?.response", arg=turn_id)
    row = await page.evaluate("id => stopTimings[id]", turn_id)
    return {field + "_ms":round(row[field] - row["request"], 1) for field in ("sse", "visible", "response")}


async def separate_audio_lanes(page, server, playing, release, requests):
    log = server.folder / "web-1.log"
    before_count = log.read_bytes().count(b"SLOW_CLOCK_START")
    await command(page, GATED_CLOCK)
    clock = await wait_state(server, lambda row: row["status"] == "thinking" and row.get("mcp_request"))
    active = page.locator(f'[data-turn-id="{clock["id"]}"][data-status="thinking"]')
    await active.wait_for(state="attached")
    running = await wait_process(server, before_count, argument="--clock-gate")
    composer = page.get_by_test_id("composer")
    draft = "还没发出的内容"
    pause_events = []

    async def verify_clock_is_pending():
        saved = next(row for row in (await server.state())["turns"] if row["id"] == clock["id"])
        assert saved["status"] == "thinking" and not saved.get("reply"), saved
        assert all(psutil.pid_exists(pid) for pid in running), running

    for from_voice in (False, True):
        before = len(requests)
        position = await playing.evaluate("audio => audio.currentTime")
        if from_voice:
            await composer.fill(draft)
            await recognized(page, "停止播报。")
            assert await composer.input_value() == draft
        else:
            await command(page, "停止播报。")
            await page.wait_for_function("document.getElementById('message').value === ''")
        assert not requests[before:], requests[before:]
        await verify_clock_is_pending()
        await page.wait_for_function("""([id,position]) => {
            const audio = document.querySelector(`[data-media-id="${id}"].media-audio`);
            return audio && !audio.paused && audio.currentTime > position + .05;
        }""", arg=[await playing.get_attribute("data-media-id"), position])

        before = len(requests)
        pause = "帮我暂停一下音乐。" if from_voice else "暂停音乐。"
        if from_voice:
            await recognized(page, pause)
            assert await composer.input_value() == draft
        else:
            await command(page, pause)
        paused = await wait_state(server, lambda row: row["user"] == pause and row["status"] == "replied")
        assert paused["source"] == "transport" and paused["transport"]["event"] == "paused", paused
        assert paused["cloud_requests"]["total"] == 0
        assert [url for url, _ in requests[before:]] == [server.url + "/api/message", server.url + "/api/transport-result"]
        assert await playing.evaluate("audio => audio.paused && !audio.ended")
        await verify_clock_is_pending()
        pause_events.append({"from_asr_text":from_voice, "turn_id":paused["id"], "reply":paused["reply"]})
        await command(page, "继续播放。")
        await wait_state(server, lambda row: row["user"] == "继续播放。" and (row.get("transport") or {}).get("event") == "playing")
        assert await playing.evaluate("audio => !audio.paused")

    release.touch(exist_ok=False)
    complete = await wait_state(server, lambda row: row["status"] == "replied", turn_id=clock["id"])
    assert complete["source"] == "context" and "时间" in complete["reply"], complete
    assert complete["cloud_requests"]["total"] == 0 and not complete.get("error"), complete
    await page.locator(f'[data-turn-id="{clock["id"]}"] [data-testid="final"]').wait_for()
    return {"stop_speech_kept_media_and_clock":True, "pause_events":pause_events,
            "clock_turn_id":clock["id"], "clock_reply":complete["reply"], "unsent_text_preserved":True}


async def slow_clock_cases(browser, root, model_url, report, audio_lanes_only=False):
    server = isolated(root / "clock")
    server.model_url = model_url
    server.data.mkdir(parents=True)
    service = {"command":"project-python", "args":[str(ROOT / "verify_configured_mcp.py"), "--local-clock-after-wait"],
               "server_name":"local_clock_after_wait", "tools":{"read_later":"稍后读时间"}, "timeout_seconds":30.0}
    release = server.data / "allow-clock-reply"
    gated = {**service, "args":[str(Path(__file__).resolve()), "--clock-gate", str(release)]}
    (server.data / "mcp-servers.json").write_text(json.dumps({"servers":{"取消演练":service, "通道演练":gated}},
                                                          ensure_ascii=False), encoding="utf-8")
    await server.start()
    page, errors, requests = await open_page(browser, server)
    try:
        await command(page, "播放《隔离停止测试》")
        first = await wait_state(server, lambda turn: (turn.get("browser_playback") or {}).get("event") == "started")
        playing = page.locator(f"audio[data-media-id='{first['browser_playback']['media_id']}']").last
        assert await playing.evaluate("audio => !audio.paused")
        report["audio_lanes"] = await separate_audio_lanes(page, server, playing, release, requests)
        assert not errors, errors
        if audio_lanes_only:
            return

        for text, by_voice in (("MOSS，先停一下。", False), ("别说了！", True), ("先等等。", True)):
            started = (server.folder / "web-1.log").read_bytes().count(b"SLOW_CLOCK_START")
            await command(page, SLOW_CLOCK)
            turn = await wait_state(server, lambda row: row["status"] == "thinking" and row.get("mcp_request"))
            running = await wait_process(server, started)
            input_box = page.get_by_test_id("composer")
            await input_box.fill("不要停止回复。")
            assert await page.get_by_test_id("send").is_disabled()
            original = ""
            if by_voice:
                await input_box.fill("这是还没有发送的草稿")
                await recognized(page, "听写的普通内容")
                original = await input_box.input_value()
                assert original == "这是还没有发送的草稿\n听写的普通内容"
                assert (await server.state())["turns"][-1]["status"] == "thinking"
            mark, before = perf_counter(), len(requests)
            cancel_url = server.url + "/api/cancel"
            async with asyncio.TaskGroup() as pending:
                exit_seen = pending.create_task(observe_exit(requests, before, cancel_url, running))
                if by_voice:
                    await recognized(page, text)
                else:
                    await command(page, text)
            stopped = await wait_state(server, lambda row: row["id"] == turn["id"] and row["status"] == "cancelled")
            assert [address for address, _ in requests[before:]] == [cancel_url] and not stopped.get("receipt")
            await page.wait_for_function("value => document.getElementById('message').value === value", arg=original)
            assert all(not psutil.pid_exists(pid) for pid in running)
            assert await playing.evaluate("audio => !audio.paused")
            report["cases"].append({"text":text, "from_asr_text":by_voice, "cancel_ms":round((perf_counter() - mark) * 1000, 1),
                                    "mcp_exited":True, "mcp_exit_ms":exit_seen.result(), "draft_preserved":bool(original),
                                    "other_media_kept_playing":True, **await timings(page, turn["id"])})

        await command(page, "停止播放")
        stopped = await wait_state(server, lambda row: row.get("source") == "transport" and row["status"] == "replied")
        assert stopped["transport"]["event"] == "stopped", stopped["reply"]
        assert await playing.evaluate("audio => audio.paused")
        await command(page, "在吗")
        next_turn = await wait_state(server, lambda row: row["user"] == "在吗" and row["status"] == "replied")
        assert next_turn["source"] == "smalltalk" and not errors
        report.update(media_stop=True, next_turn_succeeded=True)
    finally:
        await page.close()
        await server.stop()


async def thinking_model_case(browser, root, model_url, model_connected, model_closed, report):
    server = isolated(root / "thinking")
    server.model_url = model_url
    await server.start()
    try:
        assert (await server.post("/api/select-model", {"id":"local"}))[0] == 200
        page, errors, requests = await open_page(browser, server)
        try:
            await command(page, "请写一段简短的春日随笔，开头描写校门口的梧桐。")
            await asyncio.wait_for(model_connected.wait(), timeout=10)
            turn = await wait_state(server, lambda row: row["status"] == "thinking" and row["source"] == "holo")
            input_box = page.get_by_test_id("composer")
            before = len(requests)
            for non_command in ("不要等一下。", "等一下会议就开始。", "帮我解释『等一下』这个词。"):
                await input_box.fill(non_command)
                assert await page.get_by_test_id("send").is_disabled(), non_command
            draft = "要继续编辑的草稿"
            await input_box.fill(draft)
            await recognized(page, "等一下。")
            await wait_state(server, lambda row: row["id"] == turn["id"] and row["status"] == "cancelled")
            await asyncio.wait_for(model_closed.wait(), timeout=10)
            assert await input_box.input_value() == draft
            assert [url for url, _ in requests[before:]] == [server.url + "/api/cancel"]
            await input_box.fill("")
            await recognized(page, "在吗")
            followup = await wait_state(server, lambda row: row["id"] != turn["id"] and row["status"] == "replied")
            assert followup["source"] == "smalltalk" and not errors
            assert report["loopback_model_connections"] == 1
            report["thinking_model"] = {"text":"等一下。", "model_connection_closed":True, "draft_preserved":True,
                                        "additional_model_requests":0, "next_voice_text_submitted":True,
                                        **await timings(page, turn["id"])}
        finally:
            await page.close()
    finally:
        await server.stop()


async def draft_cases(browser, root, model_url, report):
    server = isolated(root / "draft")
    server.model_url, server.draft_model_reply = model_url, True
    server.data.mkdir(parents=True)
    with (closing(sqlite3.connect(PENDING_SOURCE.as_uri() + "?mode=ro", uri=True)) as source,
          closing(sqlite3.connect(server.data / "session.sqlite3")) as target):
        source.backup(target)
    await server.start()
    try:
        status, state = await server.post("/api/select-model", {"id":"local"})
        assert status == 200 and state["model_id"] == "local"
        page, errors, requests = await open_page(browser, server)
        try:
            initial = state["turns"][-1]
            assert initial["status"] == "pending" and initial["preview"] == "周一核对书目"
            before = len(requests)
            await page.get_by_test_id("composer").fill("等一下。")
            assert await page.get_by_test_id("send").is_disabled()
            assert await page.evaluate("""chat => window.mossSubmitSpokenAction('先等等。', chat) === null""",
                                       state["chat_id"])
            await command(page, "别说了。")
            await page.wait_for_function("document.getElementById('message').value === ''")
            kept = (await server.state())["turns"][-1]
            assert kept["id"] == initial["id"] and kept["status"] == "pending" and requests[before:] == []
            await command(page, "确认保存")
            saved = await wait_state(server, lambda row: row["id"] == initial["id"] and row["status"] == "saving" and row["receipt"])
            receipt = saved["receipt"]
            mark, before = perf_counter(), len(requests)
            await command(page, "停止回复。")
            done = await wait_state(server, lambda row: row["id"] == initial["id"] and row["status"] == "saved")
            path = server.data / "drafts" / receipt["filename"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]
            assert done["receipt"] == receipt and [address for address, _ in requests[before:]] == [server.url + "/api/cancel"] and not errors
            report["saved_receipt"] = {"pending_kept":True, "same_receipt":True,
                                       "cancel_ms":round((perf_counter() - mark) * 1000, 1), "sha256":receipt["sha256"],
                                       **await timings(page, initial["id"])}
        finally:
            await page.close()
    finally:
        await server.stop()


async def media_with_pending_draft(browser, root, report):
    draft = "明天核对书目和借阅记录。"
    prompt = f"帮我保存一份本地草稿，内容逐字写：{draft}"
    call_id = "pending-draft-while-playing"
    server = isolated(root / "media-with-pending-draft")
    requests = []

    async def complete(request):
        payload = await request.json()
        requests.append(payload["messages"][-1])
        assert requests == [{"role": "user", "content": prompt}] and not payload.get("stream"), payload
        assert any(tool["function"]["name"] == "save_draft" for tool in payload["tools"]), payload
        call = {"id": call_id, "type": "function", "function": {
            "name": "save_draft", "arguments": json.dumps({"text": draft}, ensure_ascii=False)}}
        return web.json_response({"id": call_id, "object": "chat.completion", "created": 0,
                                  "model": payload["model"], "choices": [{"index": 0, "finish_reason": "tool_calls",
                                  "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})

    model = web.Application()
    model.router.add_post("/v1/chat/completions", complete)
    async with AsyncExitStack() as stack:
        server.model_url = await listen(stack, model) + "/v1"
        await server.start()
        stack.push_async_callback(server.stop)
        assert (await server.post("/api/select-model", {"id": "local"}))[0] == 200
        page, errors, _ = await open_page(browser, server)
        stack.push_async_callback(page.close)
        await command(page, "播放《隔离停止测试》")
        started = await wait_state(server, lambda row: (row.get("browser_playback") or {}).get("event") == "started")
        playing = page.locator(f"audio[data-media-id='{started['browser_playback']['media_id']}']").last
        track = await playing.element_handle()
        await command(page, prompt)
        pending = await wait_state(server, lambda row: row["user"] == prompt and row["status"] == "pending")
        card = page.locator(f"[data-turn-id='{pending['id']}']")
        await card.get_by_test_id("approve").wait_for()
        assert pending["preview"] == draft and pending["call_id"] == call_id
        assert await playing.evaluate("audio => !audio.paused && audio.currentTime > 0")
        drafts = server.data / "drafts"

        async def assert_pending():
            row = next(row for row in (await server.state())["turns"] if row["id"] == pending["id"])
            assert (row["status"], row["call_id"], row["preview"]) == ("pending", call_id, draft), row
            assert not row["receipt"] and not list(drafts.iterdir()), row
            assert await card.get_by_test_id("approve").is_visible()

        await assert_pending()
        composer = page.get_by_test_id("composer")
        for ambiguous in ("保存草稿：暂停音乐", "不要暂停一下音乐", "暂停一下音乐然后锁屏",
                          "‘暂停一下音乐’是什么意思？", "刚才暂停音乐了吗？", "可以暂停一下音乐吗？"):
            await composer.fill(ambiguous)
            assert await page.get_by_test_id("send").is_disabled(), ambiguous
        count = len((await server.state())["turns"])
        for ambiguous in ("停止", "等一下", "保存草稿：暂停音乐"):
            status, _ = await server.post("/api/message", {
                "chat_id": pending["chat_id"], "text": ambiguous,
                "player": {"state": "playing", "turn_id": started["id"],
                           "media_id": started["browser_playback"]["media_id"]},
                "transport_request_id": "not-an-explicit-control",
            })
            assert status == 409, ambiguous
        assert len((await server.state())["turns"]) == count
        await composer.fill("莫斯，暂停一下这首歌")
        assert await page.get_by_test_id("send").is_enabled(), "待确认草稿阻塞了明确的暂停命令"
        began = perf_counter()
        await page.get_by_test_id("send").click(timeout=5000)
        await page.wait_for_function("audio => audio.isConnected && audio.paused", arg=track, timeout=5000)
        pause_ms = round((perf_counter() - began) * 1000, 1)
        await page.wait_for_function("document.getElementById('message').value === ''", timeout=5000)
        await assert_pending()

        unsent = "待会儿还要继续编辑的文字"
        await composer.fill(unsent)
        await recognized(page, "继续播放这首。")
        await page.wait_for_function("audio => audio.isConnected && !audio.paused", arg=track, timeout=5000)
        await assert_pending()
        assert await composer.input_value() == unsent
        await recognized(page, "停止播放。")
        await page.wait_for_function("audio => audio.isConnected && audio.paused && audio.currentTime < .1",
                                     arg=track, timeout=5000)
        await assert_pending()
        assert await composer.input_value() == unsent
        controls = [row["transport"] for row in (await server.state())["turns"] if row.get("source") == "transport"]
        assert [row["event"] for row in controls] == ["paused", "playing", "stopped"], controls
        assert all(row["target_turn"] == started["id"] for row in controls), controls

        before_restart = server.pid
        await server.stop()
        await page.locator(".connection:not(.online)").wait_for(timeout=10000)
        await server.start()
        await page.locator(".connection.online").wait_for(timeout=10000)
        assert server.pid != before_restart and await composer.input_value() == unsent
        await assert_pending()

        async def cached_voice(route):
            data = route.request.post_data_json
            if data["category"] == "reply" and data["text"] == "已保存。":
                await route.continue_()
            else:
                errors.append(f"意外的播报请求 {data}")
                await route.abort()

        await page.route("**/api/speak", cached_voice)
        await page.evaluate("""() => {
            window.savedVoiceTurn = null;
            window.addEventListener('moss-speech-start', ({detail}) => {
                window.savedVoiceTurn = detail.id;
            }, {once:true});
        }""")
        await page.locator(".preferences summary").click()
        await page.get_by_role("checkbox", name="MOSS 语音播报").check()
        await page.locator(".preferences summary").click()
        async with page.expect_response(lambda response: response.url.endswith("/api/speak"), timeout=10000) as spoken:
            await recognized(page, "确认保存。")
        audio_response = await spoken.value
        assert audio_response.status == 200 and await audio_response.body() == (ROOT / "voice/lines/saved-draft.wav").read_bytes()
        await page.wait_for_function("id => window.savedVoiceTurn === id", arg=pending["id"], timeout=8000)
        await card.get_by_test_id("receipt").wait_for(timeout=10000)
        assert await composer.input_value() == unsent
        saved = next(row for row in (await server.state())["turns"] if row["id"] == pending["id"])
        receipt = saved["receipt"]
        path = drafts / receipt["filename"]
        contents = path.read_bytes()
        assert saved["call_id"] == call_id and contents == draft.encode("utf-8")
        assert hashlib.sha256(contents).hexdigest() == receipt["sha256"]
        status, after_retry = await server.post("/api/approve", {"turn_id": pending["id"]})
        repeated = next(row for row in after_retry["turns"] if row["id"] == pending["id"])
        assert status == 200 and repeated["receipt"] == receipt
        assert list(drafts.iterdir()) == [path] and len(requests) == 1 and not errors, errors
        report["media_with_pending_draft"] = {"typed_pause": True, "recognized_resume_and_stop": True,
                                               "recognized_confirmation": True, "unsent_text_preserved": True,
                                               "receipt_audio_played": True, "typed_pause_ms": pause_ms,
                                               "pending_preserved_on_restart": True,
                                               "playback_events": [row["event"] for row in controls],
                                               "draft_id": pending["id"], "call_id": call_id,
                                               "files_before_confirmation": 0, "files_after_retry": 1,
                                               "receipt_sha256": receipt["sha256"], "model_requests": len(requests)}


def gated_clock(release):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    clock = FastMCP("local_clock_after_wait")

    @clock.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
    async def read_later() -> dict[str, str]:
        print("SLOW_CLOCK_START", file=sys.stderr, flush=True)
        while not release.is_file():
            await asyncio.sleep(.03)
        return {"时间":datetime.now().astimezone().isoformat(timespec="seconds")}

    clock.run(transport="stdio")


async def run(audio_lanes_only=False):
    folder = ROOT / "runs" / datetime.now().strftime("natural-stop-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_KEV_ENABLED="0", MOSS_MEDIA_DB=setup_catalog(folder),
                      MOSS_MEM0_ENABLED="0", MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"))
    report = {"passed":False, "cases":[], "loopback_model_connections":0, "physical_devices":False}
    model_connected, model_closed = asyncio.Event(), asyncio.Event()

    async def hold_model_reply(reader, writer):
        report["loopback_model_connections"] += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            model_connected.set()
            await reader.read()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            model_closed.set()

    gate = await asyncio.start_server(hold_model_reply, "127.0.0.1", 0)
    model_url = f"http://127.0.0.1:{gate.sockets[0].getsockname()[1]}/v1"
    try:
        edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=str(edge), headless=True,
                                                args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                                                      "--disable-renderer-backgrounding"])
            try:
                await confirm_muted_edge(browser)
                await slow_clock_cases(browser, folder, model_url, report, audio_lanes_only)
                assert report["loopback_model_connections"] == 0
                if not audio_lanes_only:
                    await thinking_model_case(browser, folder, model_url, model_connected, model_closed, report)
                    await draft_cases(browser, folder, model_url, report)
                    await media_with_pending_draft(browser, folder, report)
            finally:
                await browser.close()
        report["passed"] = True
    finally:
        gate.close()
        await gate.wait_closed()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audio-lanes-only", action="store_true")
    mode.add_argument("--clock-gate", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.clock_gate:
        gated_clock(args.clock_gate)
    else:
        asyncio.run(run(args.audio_lanes_only))
