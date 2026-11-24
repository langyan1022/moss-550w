import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
import psutil
from pydantic import SecretStr

from browser_lab import ROOT
from core import Moss
from local_media.library import MediaLibrary
from mcp_configured import CLOCK_REQUESTS, MCP_DATA, receipt_key
from model_catalog import Profile, SPECS
from serve import application
from verify_configured_mcp import isolated
from verify_context_reply import free_port
from verify_named_natural_mcp import protocol_log


FOLLOW_UP = "按格林尼治的标准报一下此刻的钟面"
NAMED_QUERY = "用 MCP 时钟，" + FOLLOW_UP
THIRD_PARTY_QUERY = "给对账的同事报一下此刻的世界标准时间"
SYNTHETIC_MODEL = "qa-mcp-choice"
FIXED_QUERY = "用 MCP 时钟查询当前 UTC 时间"
CLOCK_SETTINGS = {"command":"project-python", "args":["-m", "mcp_local_clock"], "server_name":"moss_local_clock",
                  "tools":{"read_clock":"当前时间", "read_date":"当前日期"}, "timeout_seconds":15.0}


async def listen(stack, app, port):
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    stack.push_async_callback(runner.cleanup)
    await web.TCPSite(runner, "127.0.0.1", port).start()


def check_clock(reply, *, utc=True):
    stamp = datetime.now(timezone.utc) if utc else datetime.now().astimezone()
    label = "协调世界时" if utc else "本机时间"
    match = re.search(rf"{label}现在是 (\d{{1,2}}) 点 (\d{{2}}) 分。", reply or "")
    assert match and any((int(match[1]), int(match[2])) == (now.hour, now.minute)
                         for now in (stamp, stamp - timedelta(minutes=1))), reply


def check_date(reply):
    today = datetime.now().date()
    assert f"今天是 {today.year} 年 {today.month} 月 {today.day} 日。" in (reply or ""), reply


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("mcp-model-match-%Y%m%d-%H%M%S-%f")
    data = folder / "data"
    data.mkdir(parents=True)
    print(folder, flush=True)
    endpoint = isolated(folder)
    model_port = free_port()
    model_url = f"http://127.0.0.1:{model_port}/v1"
    protocol = folder / "mcp-stderr.log"
    report = {"passed":False, "cases":[], "model_requests":0, "stdio_starts":0, "blocked_network":[]}
    clock = CLOCK_SETTINGS
    configured_rule = {"tool":"read_clock", "phrases":["对账世界时", "核对对账用的协调世界时"], "args":{"zone":"UTC"}}
    third_party = {**clock, "args":[str(ROOT / "mcp_local_clock.py")], "tools":{"read_clock":"对账时刻"},
                   "natural":[configured_rule]}
    config = data / "mcp-servers.json"
    catalog = folder / "empty-catalog.sqlite3"
    environment = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "MOSS_MEDIA_DB":str(catalog), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0"}
    profiles = {key:Profile(key, label, model_url, SYNTHETIC_MODEL, SecretStr("fixture" if key == "deepseek" else ""))
                for key, (label, _, _, _) in SPECS.items()}
    clock_choices = [{"id":index, "tool":clock["tools"][rule["tool"]], "phrases":rule["phrases"]}
                     for index, rule in enumerate(CLOCK_REQUESTS, 1)]
    assert [rule["tool"] for rule in CLOCK_REQUESTS] == ["read_clock", "read_clock", "read_date"]
    third_choices = [{"id":1, "tool":"对账时刻", "phrases":configured_rule["phrases"]}]

    def configure(settings=clock):
        config.write_text(json.dumps({"servers":{"时钟":settings, "业务时钟":third_party,
                                              "审计时钟":{**third_party, "model_match":True}}}, ensure_ascii=False),
                          encoding="utf-8")

    calls = []
    pending = []

    def answer(content, *, hold=False):
        arrived, finished = asyncio.Event(), asyncio.Event()
        gate = asyncio.Event() if hold else None
        pending.append((content, arrived, finished, gate))
        return arrived, finished, gate

    async def model_probe(request):
        calls.append(await request.json())
        report["model_requests"] = len(calls)
        if not pending:
            return web.json_response({"error":{"message":"出现了额外模型请求。"}}, status=500)
        content, arrived, finished, gate = pending.pop(0)
        arrived.set()
        try:
            if gate:
                await gate.wait()
            return web.json_response({"id":"mcp-choice", "object":"chat.completion", "created":0,
                                      "model":SYNTHETIC_MODEL, "choices":[{"index":0, "finish_reason":"stop",
                                      "message":{"role":"assistant", "content":json.dumps(content, ensure_ascii=False)}}]})
        finally:
            finished.set()

    audit_active = False

    def audit(event, args):
        if not audit_active:
            return
        if event == "subprocess.Popen" and "mcp_local_clock" in str(args[1]):
            report["stdio_starts"] += 1
        elif event in {"socket.connect", "socket.getaddrinfo"}:
            host, port, *_ = args[1] if event == "socket.connect" else args
            if host not in ("127.0.0.1", b"127.0.0.1") or port not in (endpoint.port, model_port):
                report["blocked_network"].append({"host":str(host), "port":port})
                raise PermissionError("测试只允许两个回环 HTTP 端口。")

    def checkpoint():
        return len(calls), report["stdio_starts"], protocol.stat().st_size, perf_counter()

    async def begin(text):
        status, state = await endpoint.post("/api/message", {"text":text, "chat_id":moss.chat_id})
        assert status == 200, (status, state)
        turn = state["turns"][-1]
        assert turn["source"] == "context" and turn["mcp_request"] is True, turn
        assert turn["model_id"] == "deepseek" and turn["model"] == SYNTHETIC_MODEL, turn
        return turn

    async def observe(turn, baseline, *, expected_input=None, tools=0, key=None, status=None):
        model_count, stdio_count, offset, started = baseline
        turn_id = turn["id"]
        async with asyncio.timeout(20):
            while turn["status"] == "thinking":
                await asyncio.sleep(.03)
                turn = (await endpoint.state())["turns"][-1]
                assert turn["id"] == turn_id, turn
        outbound = calls[model_count:]
        messages = protocol.read_bytes()[offset:]
        case = {"input":turn["user"], "status":turn["status"], "reply":turn.get("reply"), "error":turn.get("error"),
                "mcp_receipt":turn.get("mcp_receipt"), "stdio_starts":report["stdio_starts"] - stdio_count,
                "tool_calls":messages.count(b"CallToolRequest"), "outbound":len(outbound),
                "cloud_requests":turn["cloud_requests"], "ms":round((perf_counter() - started) * 1000, 1)}
        report["cases"].append(case)
        requests = int(expected_input is not None)
        assert len(outbound) == requests, case
        if requests:
            body, = outbound
            assert body["model"] == SYNTHETIC_MODEL and not body.get("stream") and not body.get("tools"), body
            assert [message["role"] for message in body["messages"]] == ["system", "user"], body
            actual_input = json.loads(body["messages"][-1]["content"])
            case["model_input"] = actual_input
            assert actual_input == expected_input, case
        assert turn["cloud_requests"] == {"total":requests, "complete":True,
                                          "by_provider":{"deepseek":1} if requests else {}}, case
        assert case["stdio_starts"] == tools and case["tool_calls"] == tools, case
        assert not psutil.Process().children(recursive=True), case
        assert turn.get("mcp_receipt") == key, case
        if status:
            assert turn["status"] == status, case
        elif key:
            assert turn["status"] == "replied", case
        else:
            assert turn["status"] in {"replied", "error"}, case
        return turn

    async def send(text, **expectations):
        baseline = checkpoint()
        return await observe(await begin(text), baseline, **expectations)

    expected_clock = {"request":FOLLOW_UP, "choices":clock_choices}
    utc_key = receipt_key("时钟", "read_clock", {"zone":"UTC"})
    try:
        configure()
        MediaLibrary(catalog)
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(protocol_log(protocol))
            stack.callback(MCP_DATA.reset, MCP_DATA.set(data))
            model_app = web.Application()
            model_app.router.add_post("/v1/chat/completions", model_probe)
            await listen(stack, model_app, model_port)
            with patch("core.profiles", return_value=profiles):
                moss = Moss(data, "http://127.0.0.1:9/v1", "qa-unused-local")
            stack.push_async_callback(moss.close)
            app = application(moss, lambda: "standby", data, endpoint.port, "isolated", asyncio.Event())
            await listen(stack, app, endpoint.port)
            sys.addaudithook(audit)
            audit_active = True
            code, state = await endpoint.post("/api/select-model", {"id":"deepseek"})
            assert code == 200 and state["model_id"] == "deepseek", (code, state)

            fixed = await send(FIXED_QUERY, tools=1, key=utc_key)
            check_clock(fixed["reply"])
            await send("用 MCP 时钟" + FOLLOW_UP, status="replied")
            await send("用 MCP 业务时钟，" + THIRD_PARTY_QUERY, status="replied")

            answer({"choice":1})
            utc = await send(NAMED_QUERY, expected_input=expected_clock, tools=1, key=utc_key)
            check_clock(utc["reply"])
            local_question = "我想听一下电脑钟面上此刻的时和分"
            answer({"choice":2})
            local = await send("用 MCP 时钟 " + local_question,
                               expected_input={"request":local_question, "choices":clock_choices},
                               tools=1, key=receipt_key("时钟", "read_clock", {}))
            check_clock(local["reply"], utc=False)
            date_question = "把今天日历的年月日按数字读给我"
            answer({"choice":3})
            date = await send("通过 MCP 时钟：" + date_question,
                              expected_input={"request":date_question, "choices":clock_choices},
                              tools=1, key=receipt_key("时钟", "read_date", {}))
            check_date(date["reply"])
            answer({"choice":1})
            third = await send("用 MCP 审计时钟，" + THIRD_PARTY_QUERY,
                               expected_input={"request":THIRD_PARTY_QUERY, "choices":third_choices},
                               tools=1, key=receipt_key("审计时钟", "read_clock", {"zone":"UTC"}))
            check_clock(third["reply"])

            for content in ({"choice":None}, {"choice":True}, {"choice":999}, {"choice":1, "args":{"zone":"local"}}):
                answer(content)
                await send(NAMED_QUERY, expected_input=expected_clock)
                report["cases"][-1]["selected_value"] = content

            arrived, finished, gate = answer({"choice":1}, hold=True)
            baseline = checkpoint()
            changed = await begin(NAMED_QUERY)
            await asyncio.wait_for(arrived.wait(), timeout=10)
            configure({**clock, "tools":{"read_date":"当前日期"}})
            gate.set()
            await asyncio.wait_for(finished.wait(), timeout=10)
            await observe(changed, baseline, expected_input=expected_clock)
            report["cases"][-1]["configuration_replaced_before_model_reply"] = True
            configure()

            arrived, finished, gate = answer({"choice":1}, hold=True)
            baseline = checkpoint()
            running = await begin(NAMED_QUERY)
            await asyncio.wait_for(arrived.wait(), timeout=10)
            assert not psutil.Process().children(recursive=True)
            code, state = await endpoint.post("/api/cancel", {"turn_id":running["id"]})
            assert code == 200 and state["turns"][-1]["id"] == running["id"], (code, state)
            gate.set()
            await asyncio.wait_for(finished.wait(), timeout=10)
            await observe((await endpoint.state())["turns"][-1], baseline,
                          expected_input=expected_clock, status="cancelled")
            assert not pending and not report["blocked_network"]
            report["passed"] = True
    finally:
        audit_active = False
        outcome = folder / "result.json"
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


async def run_browser_live():
    from playwright.async_api import async_playwright

    from browser_lab import confirm_muted_edge
    from model_catalog import allowed, profiles
    from verify_chat_providers import OBSERVE
    from verify_memory import send

    phrase = "按格林尼治的钟报一下此刻的时分"
    unsupported_place = "告诉我此刻檀香山当地的时间"
    selected = profiles()["deepseek"]
    if not selected.public()["available"] or not allowed("deepseek", selected.url):
        raise RuntimeError("需要现有可用的正式 DeepSeek 配置。")
    selected_profiles = {key:selected if key == "deepseek" else Profile(key, label, url, model, SecretStr(""))
                         for key, (label, _, url, model) in SPECS.items()}
    provider_url = selected.url + "/chat/completions"
    folder = ROOT / "runs" / datetime.now().strftime("mcp-model-match-live-%Y%m%d-%H%M%S-%f")
    endpoint = isolated(folder)
    endpoint.data.mkdir(parents=True)
    print(folder, flush=True)
    (endpoint.data / "mcp-servers.json").write_text(
        json.dumps({"servers":{"时钟":CLOCK_SETTINGS}}, ensure_ascii=False), encoding="utf-8")
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    environment = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "MOSS_MEDIA_DB":str(catalog), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0"}
    report = {"passed":False, "cases":[], "paid_cap":2, "outbound":[], "blocked_browser_hosts":[],
              "provider":"deepseek", "model":selected.model, "screenshot":None}
    protocol = folder / "mcp-stderr.log"
    original_client = Profile.client

    async def outbound(request):
        if request.method != "POST" or str(request.url) != provider_url:
            raise PermissionError("验收只允许正式 DeepSeek 对话端点。")
        body = json.loads(request.content)
        second_message = body["messages"][-1]
        prompt = json.loads(second_message["content"])["request"]
        if (body["model"] != selected.model or [entry["role"] for entry in body["messages"]] != ["system", "user"]
                or prompt not in {phrase, unsupported_place} or prompt in report["outbound"]
                or len(report["outbound"]) >= report["paid_cap"]):
            raise PermissionError("验收只允许每道固定时钟题各出站一次。")
        report["outbound"].append(prompt)

    def guarded_client(profile):
        if profile is not selected:
            raise PermissionError("验收只允许当前选中的 DeepSeek 配置。")
        client = original_client(profile)
        client._client.event_hooks["request"].append(outbound)
        return client

    async def browser_local_only(route):
        url = urlsplit(route.request.url)
        if (url.scheme, url.netloc) == ("http", f"127.0.0.1:{endpoint.port}"):
            await route.continue_()
        else:
            report["blocked_browser_hosts"].append(url.netloc)
            await route.abort()

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch("core.profiles", return_value=selected_profiles))
            stack.enter_context(patch.object(Profile, "client", guarded_client))
            stack.enter_context(protocol_log(protocol))
            stack.callback(MCP_DATA.reset, MCP_DATA.set(endpoint.data))
            moss = Moss(endpoint.data, endpoint.model_url, "qa-unused-local")
            stack.push_async_callback(moss.close)
            await listen(stack, application(moss, lambda: "standby", endpoint.data, endpoint.port,
                                           "isolated-live", asyncio.Event()), endpoint.port)
            code, state = await endpoint.post("/api/select-model", {"id":"deepseek"})
            assert code == 200 and state["model_id"] == "deepseek" and not state["turns"] and not state["memories"]
            playwright = await stack.enter_async_context(async_playwright())
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            browser = await playwright.chromium.launch(
                executable_path=str(edge), headless=True, args=["--mute-audio", "--disable-gpu",
                "--disable-background-networking", "--disable-component-update",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)
            context = await browser.new_context(viewport={"width":1400, "height":960}, locale="zh-CN",
                                                reduced_motion="reduce", service_workers="block")
            await context.route("**/*", browser_local_only)
            page = await context.new_page()
            page.set_default_timeout(25000)
            await page.add_init_script(OBSERVE)
            await page.goto(endpoint.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            states = (("用 MCP 时钟，" + phrase, 1, True), (FIXED_QUERY, 0, True),
                      ("用 MCP 时钟，" + unsupported_place, 1, False))
            for prompt, expected_requests, should_call in states:
                before = len(report["outbound"])
                offset = protocol.stat().st_size
                visible = await send(page, endpoint, prompt)
                await page.wait_for_function("window.bench?.final != null")
                timing = await page.evaluate("bench")
                turn = (await endpoint.state())["turns"][-1]
                assert timing["turnId"] == turn["id"], timing
                tool_calls = protocol.read_bytes()[offset:].count(b"CallToolRequest")
                report["cases"].append({"input":prompt, "turn_id":turn["id"], "visible_reply":visible, "first_visible_ms":timing["first"],
                                        "complete_ms":timing["final"], "cloud_requests":turn["cloud_requests"],
                                        "outbound":len(report["outbound"]) - before, "tool_calls":tool_calls,
                                        "mcp_receipt":turn.get("mcp_receipt")})
                assert turn["user"] == prompt and turn["reply"].strip() == visible.strip(), turn
                assert turn["source"] == "context" and turn["status"] == "replied" and turn["model_id"] == "deepseek"
                assert len(report["outbound"]) - before == expected_requests
                assert turn["cloud_requests"] == {"complete":True, "total":expected_requests,
                                                  "by_provider":{"deepseek":1} if expected_requests else {}}
                assert tool_calls == int(should_call)
                if should_call:
                    assert turn.get("mcp_receipt") == receipt_key("时钟", "read_clock", {"zone":"UTC"})
                    check_clock(visible)
                else:
                    assert not turn.get("mcp_receipt") and "没有能处理" in visible
            assert report["outbound"] == [phrase, unsupported_place]
            await page.screenshot(path=folder / "mcp-model-match.png", animations="disabled")
            report.update(passed=True, screenshot="mcp-model-match.png")
    finally:
        outcome = folder / "result.json"
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-live", action="store_true", help="用静音 Edge 和当前 DeepSeek 做三轮时钟验收，最多两笔模型请求。")
    asyncio.run(run_browser_live() if parser.parse_args().browser_live else run())
