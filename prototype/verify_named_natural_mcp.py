import asyncio
from contextlib import AsyncExitStack, ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
import secrets
import sys
from time import perf_counter

import psutil
import win32api
import win32con
import win32event
import win32process
from aiohttp import web

from browser_lab import ROOT
from context_tools import parse_context
from core import Moss
from local_media.library import MediaLibrary
from mcp_configured import MCP_DATA
from model_catalog import SPECS
from serve import application
from verify_configured_mcp import isolated


DATE_STATUS = "帮我核对工作日期了吗？"
CLOCK_STATUS = "用时钟查询当前 UTC 时间了吗？"
NAMED_CLOCK_STATUS = ("用 MCP 时钟查询当前 UTC 时间了？", "用 MCP 时钟查询当前 UTC 时间了吗？")


@contextmanager
def protocol_log(path):
    with path.open("wb", buffering=0) as log:
        descriptor = sys.stderr.fileno()
        original = os.dup(descriptor)
        try:
            sys.stderr.flush()
            os.dup2(log.fileno(), descriptor)
            yield
        finally:
            sys.stderr.flush()
            os.dup2(original, descriptor)
            os.close(original)


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("named-natural-mcp-%Y%m%d-%H%M%S-%f")
    data = folder / "data"
    data.mkdir(parents=True)
    print(folder, flush=True)
    clock = {"command":"project-python", "args":["-m", "mcp_local_clock"], "server_name":"moss_local_clock",
             "timeout_seconds":30, "tools":{"read_date":"当前日期", "read_clock":"当前时间"}}
    calendar = {**clock, "tools":{"read_date":"当前日期"},
                "natural":[{"tool":"read_date", "phrases":["核对工作日期", "确认工作日期", "工作日期", "本月日期"]}]}
    delayed = {"command":"project-python", "args":[str(ROOT / "verify_configured_mcp.py"), "--local-clock-after-wait"],
               "server_name":"local_clock_after_wait", "timeout_seconds":30.0, "tools":{"read_later":"延时日期"},
               "natural":[{"tool":"read_later", "phrases":["核对延时日期"]}]}
    calendar_book = {key:value for key, value in delayed.items() if key != "natural"}

    def configure(clock_settings):
        config = {"servers":{"时钟":clock_settings, "日历":calendar, "日历本":calendar_book, "延时日历":delayed}}
        (data / "mcp-servers.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    configure(clock)
    catalog = folder / "empty-catalog.sqlite3"
    os.environ.update(LOCALAPPDATA=str(folder / "local-profile"), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0")
    for _, prefixes, _, _ in SPECS.values():
        for prefix in prefixes:
            os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"
    MediaLibrary(catalog)
    endpoint = isolated(folder)
    outcome = folder / "result.json"
    protocol = folder / "mcp-stderr.log"
    report = {"passed":False, "cases":[], "negative_parses":{}, "stdio_starts":0,
              "http_routes":{}, "child_commands":[], "service_starts":0, "windows_reminders":False,
              "model_connections":0, "blocked_network":[]}

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    def children():
        return [proc.pid for proc in psutil.Process().children(recursive=True) if proc.is_running()]

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(protocol_log(protocol))
            guard = await stack.enter_async_context(await asyncio.start_server(model_guard, "127.0.0.1", 0))
            model_port = guard.sockets[0].getsockname()[1]

            def audit(event, args):
                if event == "subprocess.Popen":
                    report["child_commands"].append(str(args[1]))
                    if any(name in str(args[1]) for name in ("mcp_local_clock", "--local-clock-after-wait")):
                        report["stdio_starts"] += 1
                    return
                if event == "socket.connect":
                    host, port, *_ = args[1]
                elif event == "socket.getaddrinfo":
                    host, port = args[:2]
                else:
                    return
                if host == "127.0.0.1" and port in (endpoint.port, model_port):
                    return
                report["blocked_network"].append({"host":host, "port":port})
                raise PermissionError("隔离验收只允许测试 HTTP 及模型计数端口。")

            sys.addaudithook(audit)
            stack.callback(MCP_DATA.reset, MCP_DATA.set(data))
            service = await stack.enter_async_context(AsyncExitStack())

            @web.middleware
            async def observe_request(request, handler):
                routes = report["http_routes"]
                routes[request.path] = routes.get(request.path, 0) + 1
                return await handler(request)

            async def start_service():
                instance = Moss(data, f"http://127.0.0.1:{model_port}/v1", "qa-local")
                service.push_async_callback(instance.close)
                assert instance.timers.windows is None and instance.index is None
                app = application(instance, lambda:"external", data, endpoint.port,
                                  secrets.token_urlsafe(24), asyncio.Event())
                app.middlewares.append(observe_request)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                service.push_async_callback(runner.cleanup)
                await web.TCPSite(runner, "127.0.0.1", endpoint.port).start()
                report["service_starts"] += 1
                return instance

            moss = await start_service()
            original = (await endpoint.state())["chat_id"]
            assert all(not profile.public()["available"] for profile in moss.cloud.values())

            async def send(text, *, stdio, success=False):
                start = perf_counter()
                count = report["stdio_starts"]
                offset = protocol.stat().st_size
                code, state = await endpoint.post("/api/message", {"chat_id":moss.chat_id, "text":text})
                assert code == 200, (code, state)
                turn = state["turns"][-1]
                key = turn["id"]
                async with asyncio.timeout(40):
                    while turn["status"] == "thinking":
                        await asyncio.sleep(.03)
                        turn = (await endpoint.state())["turns"][-1]
                        assert turn["id"] == key
                started = report["stdio_starts"] - count
                remaining = children()
                messages = protocol.read_bytes()[offset:]
                case = {"input":text, "chat_id":turn["chat_id"], "id":key, "reply":turn.get("reply"),
                        "source":turn.get("source"), "error":turn.get("error"), "status":turn["status"],
                        "receipt":turn.get("mcp_receipt"), "lookup":turn.get("mcp_status"), "stdio_starts":started,
                        "remaining_children":remaining, "protocol_calls":messages.count(b"CallToolRequest"),
                        "cloud_requests":turn["cloud_requests"],
                        "ms":round((perf_counter() - start) * 1000)}
                report["cases"].append(case)
                assert turn["status"] == "replied" and turn["source"] == "context" and turn["mcp_request"] is True, case
                assert started == stdio and case["protocol_calls"] == stdio and not remaining, case
                assert turn["cloud_requests"] == {"complete":True, "by_provider":{}, "total":0}, case
                if success:
                    assert re.fullmatch(r"[a-f0-9]{64}", case["receipt"] or ""), case
                else:
                    assert not case["receipt"], case
                return turn

            async def status(text, match=None):
                turn = await send(text, stdio=0)
                assert "matched_turn_id" in turn["mcp_status"], turn
                assert turn["mcp_status"]["matched_turn_id"] == (match["id"] if match else None), turn
                if not match:
                    assert re.search(r"没|未|找不到|查不到|无", turn["reply"]), turn["reply"]
                else:
                    assert match["reply"] in turn["reply"], turn["reply"]
                return turn

            async def clock_time(text):
                turn = await send(text, stdio=1, success=True)
                match = re.fullmatch(r"协调世界时现在是 (\d{1,2}) 点 (\d{2}) 分。", turn["reply"])
                now = datetime.now(timezone.utc)
                assert match and any((int(match[1]), int(match[2])) == (stamp.hour, stamp.minute)
                                     for stamp in (now, now - timedelta(minutes=1))), turn["reply"]
                return turn

            await status(DATE_STATUS)
            await status(CLOCK_STATUS)
            for text in NAMED_CLOCK_STATUS:
                await status(text)
            await send("用 MCP 时钟调用 当前日期", stdio=1, success=True)
            await status(DATE_STATUS)
            local = await send("用 MCP 时钟调用 当前时间 " + json.dumps({"zone":"local"}), stdio=1, success=True)
            assert "本机时间" in local["reply"]
            await status(CLOCK_STATUS)
            date = await send("能否确认工作日期呢？", stdio=1, success=True)
            stamp = datetime.now().date()
            assert date["reply"] == f"今天是 {stamp.year} 年 {stamp.month} 月 {stamp.day} 日。"
            await status(DATE_STATUS, date)
            short_date = await send("通过 MCP 日历看工作日期", stdio=1, success=True)
            assert short_date["reply"] == date["reply"] and short_date["mcp_receipt"] == date["mcp_receipt"]
            month_date = await send("用 MCP 日历 本月日期", stdio=1, success=True)
            assert month_date["mcp_receipt"] == date["mcp_receipt"]
            longer_name = "用 MCP 日历本 月日期"
            parsed = parse_context(longer_name)
            assert parsed and parsed.get("server") == "日历本" and parsed.get("mode") != "call", parsed
            book = await send(longer_name, stdio=0)
            assert "「日历本」" in book["reply"] and not book.get("mcp_status"), book
            report["prefix_collision"] = {"input":longer_name, "resolved_server":parsed["server"], "stdio_starts":0}
            receipts = []
            for text in ("用 MCP 时钟查一下 UTC 时间", "通过 MCP 时钟查询当前 UTC 时间",
                         "用 MCP 时钟查 UTC 时间", "UTC 现在几点了？"):
                receipts.append((await clock_time(text))["mcp_receipt"])
            utc = await send("用 MCP 时钟调用 当前时间 " + json.dumps({"zone":"UTC"}), stdio=1, success=True)
            assert "协调世界时" in utc["reply"] and set(receipts) == {utc["mcp_receipt"]}
            await status(CLOCK_STATUS, utc)
            for text in NAMED_CLOCK_STATUS:
                await status(text, utc)
            unknown = await send("用 MCP 未配置钟查一下 UTC 时间", stdio=0)
            assert "协调世界时现在是" not in unknown["reply"]
            blocked_named = ("用 MCP 时钟 不要查询当前 UTC 时间", "用 MCP 时钟 查询“当前 UTC 时间”",
                             "用 MCP 时钟 查询当前 UTC 时间，顺便查看今天日期")
            for text in ("不要核对工作日期", "请不要用 MCP 时钟查询当前 UTC 时间", "比较今天日期和工作日期",
                         "解释「UTC几点」这个句子", "保存草稿：核对工作日期", *blocked_named):
                parsed = parse_context(text)
                assert not parsed or parsed.get("mode") not in {"call", "call_status", "model_match"}, (text, parsed)
                report["negative_parses"][text] = parsed
            for text in blocked_named:
                turn = await send(text, stdio=0)
                assert not turn.get("mcp_receipt") and not turn.get("mcp_status"), turn
            code, fresh = await endpoint.post("/api/new-chat", {})
            assert code == 200 and fresh["chat_id"] != original
            await status(CLOCK_STATUS)
            await status(DATE_STATUS)
            assert (await endpoint.post("/api/switch-chat", {"id":original}))[0] == 200
            count = report["stdio_starts"]
            offset = protocol.stat().st_size
            code, state = await endpoint.post("/api/message", {"chat_id":original, "text":"核对延时日期"})
            pending = state["turns"][-1]
            assert code == 200 and pending["status"] == "thinking" and pending["mcp_request"]
            async with asyncio.timeout(30):
                while b"SLOW_CLOCK_START" not in protocol.read_bytes()[offset:]:
                    await asyncio.sleep(.03)
            running = children()
            assert running and report["stdio_starts"] == count + 1
            with ExitStack() as tracked:
                handles = {}
                names = {}
                for pid in running:
                    names[pid] = psutil.Process(pid).name()
                    handle = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION, False, pid)
                    tracked.callback(handle.Close)
                    handles[pid] = handle
                started = perf_counter()
                code, state = await endpoint.post("/api/cancel", {"turn_id":pending["id"]})
                acknowledged = perf_counter()
                cancelled = state["turns"][-1]
                samples = []
                report["cancel"] = {"turn_id":cancelled["id"], "started_marker":"SLOW_CLOCK_START", "processes":names,
                                    "receipt":cancelled.get("mcp_receipt"), "ms":round((acknowledged - started) * 1000),
                                    "cloud_requests":cancelled["cloud_requests"], "exit_samples":samples}
                assert code == 200 and cancelled["id"] == pending["id"] and cancelled["status"] == "cancelled"
                assert not cancelled.get("mcp_receipt") and cancelled["cloud_requests"] == {
                    "total":0, "complete":True, "by_provider":{}}
                for delay in (0, .05, .10, .50, 1.5):
                    await asyncio.sleep(max(0, delay - (perf_counter() - acknowledged)))
                    alive = [pid for pid, handle in handles.items() if win32event.WaitForSingleObject(handle, 0)
                             != win32event.WAIT_OBJECT_0]
                    samples.append({"after_ack_ms":round((perf_counter() - acknowledged) * 1000), "alive":alive})
                    if not alive:
                        break
                report["cancel"]["exit_codes"] = {pid:win32process.GetExitCodeProcess(h) for pid, h in handles.items()}
                assert not alive, report["cancel"]
            report["cancel"]["exited"] = True
            assert not children() and protocol.read_bytes()[offset:].count(b"CallToolRequest") == 1
            await status("帮我核对延时日期了吗？")
            configure({**clock, "cwd":str(folder / "缺失的工作目录")})
            failed = await send("用 MCP 时钟查一下 UTC 时间", stdio=0)
            assert "配置无效" in failed["reply"]
            await status(CLOCK_STATUS, utc)
            await service.aclose()
            moss = await start_service()
            assert (await endpoint.state())["chat_id"] == original
            await status(CLOCK_STATUS, utc)
            assert set(report["http_routes"]) == {
                "/api/state", "/api/message", "/api/new-chat", "/api/switch-chat", "/api/cancel"}
            assert not report["blocked_network"] and report["model_connections"] == 0
            report["passed"] = True
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
