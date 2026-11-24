"""Verify the spoken correction handoff in Edge against an isolated Holo stream."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import ROOT, confirm_muted_edge
from verify_configured_mcp import isolated
from verify_natural_stop import setup_catalog, wait_state
from verify_voice_receipts import listen


ASSETS = {
    "/": ("conversation.html", "text/html"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/agent-terminal.js": ("agent-terminal.js", "text/javascript"),
    "/static/agent-voice.js": ("agent-voice.js", "text/javascript"),
}
PROSE = "请写一段春日随笔，从校门口的梧桐写起。"
TOOL_PROSE = "请描写傍晚街角的风景。"
PREVIEW = "校门口的梧桐树抽出了新叶。"
DRAFT = "这段没发出去的文字还要继续编辑。"
SAVE_PROMPT = "帮我保存一份本地草稿，内容逐字写：明天核对书目。"
CORRECTION = "先停止回答，改问现在几点？"
NEGATIVES = (
    "“先停止回答，改问现在几点”是什么意思？",
    "先不要停止回答，改问现在几点？",
    "如果要停止回答，改问现在几点合适吗？",
)


def chunk(model, delta, finish=None):
    event = {"id":"correction-fixture", "object":"chat.completion.chunk", "created":0, "model":model,
             "choices":[{"index":0, "delta":delta, "finish_reason":finish}]}
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")


def model_fixture(report):
    model = web.Application()
    closed = asyncio.Queue()

    async def reply(request):
        payload = await request.json()
        last = payload["messages"][-1]
        prompt, model_name = last.get("content"), payload["model"]
        purpose = "unexpected"
        if prompt == SAVE_PROMPT and not payload.get("stream"):
            purpose = "draft"
        elif prompt == PROSE and payload.get("stream"):
            purpose = "prose"
        elif prompt == TOOL_PROSE and payload.get("stream"):
            purpose = "tool_request"
        elif last["role"] == "tool" and payload.get("stream") and last.get("tool_call_id") == "fixture-get-time":
            purpose = "tool_followup"
        report["model_requests"].append(purpose)
        if purpose == "draft":
            assert any(tool["function"]["name"] == "save_draft" for tool in payload["tools"])
            call = {"id":"fixture-draft", "type":"function", "function":{
                "name":"save_draft", "arguments":json.dumps({"text":"明天核对书目。"}, ensure_ascii=False)}}
            return web.json_response({"id":"fixture-draft", "object":"chat.completion", "created":0, "model":model_name,
                                      "choices":[{"index":0, "finish_reason":"tool_calls", "message":{
                                          "role":"assistant", "content":None, "tool_calls":[call]}}]})
        if purpose == "tool_request":
            assert any(tool["function"]["name"] == "get_time" for tool in payload["tools"])
            call = {"index":0, "id":"fixture-get-time", "type":"function", "function":{
                "name":"get_time", "arguments":"{}"}}
            events = chunk(model_name, {"tool_calls":[call]}) + chunk(model_name, {}, "tool_calls")
            return web.Response(body=events + b"data: [DONE]\n\n", content_type="text/event-stream")
        if purpose == "unexpected":
            return web.json_response({"error":"unexpected model request"}, status=400)
        stream = web.StreamResponse(headers={"Content-Type":"text/event-stream"})
        await stream.prepare(request)
        await stream.write(chunk(model_name, {"role":"assistant", "content":PREVIEW}))
        transport = request.transport
        try:
            async with asyncio.timeout(25):
                while transport is not None and not transport.is_closing():
                    await asyncio.sleep(.015)
        finally:
            closed.put_nowait(purpose)
        return stream

    model.router.add_post("/v1/chat/completions", reply)
    return model, closed


async def recognized(page, text, turn_id):
    await page.evaluate("""async ({text, turnId}) => {
        const url = document.querySelector('script[src^="/static/agent-terminal.js"]').src;
        await (await import(url)).submitRecognizedText(text, null, turnId);
    }""", {"text":text, "turnId":turn_id})


async def submit(page, text):
    await page.get_by_test_id("composer").fill(text)
    await page.get_by_test_id("send").click()


async def new_chat(page, server):
    status, state = await server.post("/api/new-chat", {})
    assert status == 200 and not state["turns"], state
    await page.wait_for_function("id => correctionState?.chat_id === id", arg=state["chat_id"])
    assert not (await server.state())["turns"]


def scoped(turn):
    return {"turn_id":turn["id"], "chat_id":turn["chat_id"], "conversation_only":True}


async def wait_client_turn(page, turn):
    await page.wait_for_function("""turn => correctionState?.turns.some(row =>
        row.id === turn.id && row.status === turn.status &&
        Boolean(row.conversation_replaceable) === Boolean(turn.conversation_replaceable))""", arg=turn)


async def positive(page, server, report, requests, model_closed, spoken, *, challenge):
    await submit(page, PROSE)
    live = await wait_state(server, lambda row: row["user"] == PROSE and row["status"] == "thinking"
                            and row.get("partial_reply") == PREVIEW and row.get("conversation_replaceable") is True)
    await wait_client_turn(page, live)
    composer = page.get_by_test_id("composer")
    if challenge:
        marker = len(requests)
        for text in NEGATIVES:
            await composer.fill(text)
            await expect(page.get_by_test_id("send")).to_be_disabled()
            await recognized(page, text, live["id"])
        for stale_id in (None, uuid4().hex):
            await recognized(page, spoken, stale_id)
        assert requests[marker:] == []
        assert (await server.state())["turns"][-1]["id"] == live["id"]
        status, _ = await server.post("/api/cancel", {**scoped(live), "chat_id":uuid4().hex})
        assert status == 409

    await composer.fill(DRAFT if challenge else "")
    calls, mark = len(requests), perf_counter()
    async with page.expect_response(lambda response: response.url == server.url + "/api/cancel") as response:
        await recognized(page, spoken, live["id"])
    cancellation = await response.value
    assert cancellation.status == 200
    receipt = await cancellation.json()
    cancelled, = [row for row in receipt["turns"] if row["id"] == live["id"]]
    assert cancelled["status"] == "cancelled" and not cancelled.get("receipt"), cancelled
    assert await asyncio.wait_for(model_closed.get(), 5) == "prose"
    follow = await wait_state(server, lambda row: row["id"] != live["id"] and row["status"] == "replied")
    assert follow["user"].rstrip("？?。") == "现在几点" and follow.get("holo_tools") == ["get_time"], follow
    assert follow["chat_id"] == live["chat_id"] and len((await server.state())["turns"]) == 2
    assert await composer.input_value() == (DRAFT if challenge else "")
    sent = requests[calls:]
    assert [request["path"] for request in sent] == ["/api/cancel", "/api/message"], sent
    assert sent[0]["body"] == scoped(live)
    assert sent[1]["body"]["text"] == follow["user"] and sent[1]["body"]["chat_id"] == live["chat_id"]
    elapsed = round((perf_counter() - mark) * 1000, 1)
    await wait_client_turn(page, follow)
    await composer.fill("")
    before = len(requests)
    await recognized(page, spoken, live["id"])
    assert len(requests) == before and len((await server.state())["turns"]) == 2, requests[before:]
    report["cases"].append({"spoken":spoken, "saw_partial_reply":True, "scoped_payload":True,
                            "model_stream_closed":True, "local_clock_replied":True,
                            "draft_preserved":challenge, "duplicates_suppressed":True,
                            "negative_and_stale_guards":challenge, "handoff_complete_ms":elapsed})


async def protected_case(page, server, report, requests, prompt, status, predicate, model_closed=None):
    await submit(page, prompt)
    protected = await wait_state(server, lambda row: row["user"] == prompt and row["status"] == status and predicate(row))
    await wait_client_turn(page, protected)
    assert not protected.get("conversation_replaceable"), protected
    before = len(requests)
    await recognized(page, CORRECTION, protected["id"])
    assert len(requests) == before
    rejected, _ = await server.post("/api/cancel", scoped(protected))
    assert rejected == 409
    last = (await server.state())["turns"][-1]
    assert last["status"] == status and last["id"] == protected["id"] and not last.get("receipt"), last
    if model_closed is not None:
        assert (await server.post("/api/cancel", {"turn_id":protected["id"]}))[0] == 200
        assert await asyncio.wait_for(model_closed.get(), 5) == "tool_followup"
    report["cases"].append({"protected":(protected.get("desktop_confirmation") or {}).get("action")
                            or protected.get("holo_tools") or "draft", "state_kept":status,
                            "rejected_scoped_api_status":rejected, "browser_posts":0})


async def exercise(browser, server, report, model_closed, assets):
    context = await browser.new_context(reduced_motion="reduce", service_workers="block")
    seen = set()
    requests = []

    async def route_request(route):
        request = route.request
        path = urlsplit(request.url).path
        if not request.url.startswith(server.url + "/"):
            report["outside"].append(urlsplit(request.url).hostname)
            await route.abort()
        elif path in assets:
            seen.add(path)
            body, mime = assets[path]
            await route.fulfill(body=body, content_type=mime + "; charset=utf-8")
        elif request.method == "POST" and path not in {"/api/message", "/api/cancel"}:
            report["other_writes"].append(path)
            await route.abort()
        else:
            await route.continue_()

    try:
        await context.route("**/*", route_request)
        await context.add_init_script("""
            localStorage.setItem('moss.voice.enabled', 'off');
            window.correctionState = null;
            addEventListener('moss-agent-state', e => window.correctionState = e.detail);
        """)
        page = await context.new_page()
        page.set_default_timeout(8000)
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        page.on("request", lambda request: requests.append({"path":urlsplit(request.url).path,
                                                            "body":request.post_data_json}) if request.method == "POST" else None)
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        await positive(page, server, report, requests, model_closed, CORRECTION, challenge=True)
        await new_chat(page, server)
        await positive(page, server, report, requests, model_closed, "先停止回答改问现在几点", challenge=False)
        await new_chat(page, server)
        await protected_case(page, server, report, requests, TOOL_PROSE, "thinking",
                             lambda row: row.get("holo_tools") == ["get_time"], model_closed)
        await new_chat(page, server)
        await protected_case(page, server, report, requests, "锁屏", "pending",
                             lambda row: bool(row.get("desktop_confirmation")))
        pending = (await server.state())["turns"][-1]
        assert (await server.post("/api/cancel", {"turn_id":pending["id"]}))[0] == 200
        await new_chat(page, server)
        await protected_case(page, server, report, requests, SAVE_PROMPT, "pending", lambda row: row.get("call_id"))
        assert seen == set(assets), seen
        assert not any(report[key] for key in ("outside","other_writes","errors")), report
        assert report["model_requests"] == ["prose","prose","tool_request","tool_followup","draft"], report
        assert not list((server.data / "drafts").iterdir())
    finally:
        await context.close()


async def verify(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("voice-correction-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    assets = {path:((candidate / filename).read_bytes(), mime) for path, (filename, mime) in ASSETS.items()}
    report = {"passed":False, "cases":[], "model_requests":[], "outside":[], "other_writes":[], "errors":[],
              "entry":"actual submitRecognizedText browser entry with real HTTP and a loopback model stream",
              "physical_audio":False, "asr_engine_run":False,
              "asset_sha256":{path:sha256(body).hexdigest() for path, (body, _) in assets.items()}}
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB":setup_catalog(folder), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0",
                   "MOSS_SPEECH_MIC_WARM":"0"}
    model, model_closed = model_fixture(report)
    try:
        with patch.dict(os.environ, environment):
            async with AsyncExitStack() as stack:
                server = isolated(folder / "service")
                server.model_url = await listen(stack, model) + "/v1"
                await server.start()
                stack.push_async_callback(server.stop)
                assert (await server.post("/api/select-model", {"id":"local"}))[0] == 200
                edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                        args=["--mute-audio", "--disable-gpu", "--disable-renderer-backgrounding"])
                    try:
                        await confirm_muted_edge(browser)
                        report["browser"] = browser.version
                        await exercise(browser, server, report, model_closed, assets)
                    finally:
                        await browser.close()
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    asyncio.run(verify(parser.parse_args().candidate.resolve()))
