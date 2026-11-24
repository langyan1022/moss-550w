import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import socket
import sys
from time import perf_counter

import psutil

from browser_lab import BrowserServer, ROOT
from local_media.library import MediaLibrary


def process_ids(server):
    active = set()
    for proc in psutil.Process(server.pid).children(recursive=True):
        try:
            if {"mcp_local_clock", "--local-clock-after-wait", "local_media.mcp_search"}.intersection(proc.cmdline()):
                active.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return active


def isolated(folder):
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    return BrowserServer(folder, port, model_url="http://127.0.0.1:9/v1")


async def verify(catalog_only=False):
    folder = ROOT / "runs" / datetime.now().strftime("configured-mcp-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    os.environ["MOSS_WINDOWS_REMINDERS"] = "0"
    report = {"passed":False, "scope":"官方 MCP stdio 实际本机时钟。HTTP 隔离实例，无浏览器或模型。",
              "cases":[]}
    server = isolated(folder)
    model_gate = None
    if catalog_only:
        catalog = folder / "catalog.sqlite3"
        MediaLibrary(catalog)
        catalog_hash = hashlib.sha256(catalog.read_bytes()).hexdigest()
        os.environ["MOSS_MEDIA_DB"] = str(catalog)
        os.environ["MOSS_MEM0_ENABLED"] = "0"
        report.update(scope="隔离 HTTP 对话、本机真实媒体索引及只读时钟 MCP。", model_connections=0)

        async def count_model_connection(reader, writer):
            report["model_connections"] += 1
            writer.close()
            await writer.wait_closed()

        model_gate = await asyncio.start_server(count_model_connection, "127.0.0.1", 0)
        server.model_url = f"http://127.0.0.1:{model_gate.sockets[0].getsockname()[1]}/v1"
    config = server.data / "mcp-servers.json"
    config.parent.mkdir()
    clock = {"command":"project-python", "args":["-m","mcp_local_clock"], "server_name":"moss_local_clock",
             "tools":{"read_clock":"当前时间"}}
    delayed = {"command":"project-python", "args":[str(ROOT / "verify_configured_mcp.py"), "--local-clock-after-wait"],
               "server_name":"local_clock_after_wait", "tools":{"read_later":"稍后读时间"}, "timeout_seconds":4.0}
    profile = {"servers":{"时钟":clock, "慢时钟":{**clock, "timeout_seconds":.1},
                          "远程":{**clock, "command":"https://example.invalid/tools"},
                          "错身份":{**clock, "server_name":"unknown_server"},
                          "日历":{**clock, "tools":{"read_date":"当前日期"}},
                          "稍后读":delayed, "取消演练":{**delayed, "timeout_seconds":30.0}}}
    config.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    blank = isolated(folder / "without-config")

    async def send(instance, message, *, observe=False, expected_source="context"):
        started = perf_counter()
        observed = set()
        status, state = await instance.post("/api/message", {"text":message})
        assert status == 200
        turn = state["turns"][-1]
        async with asyncio.timeout(12):
            while turn["status"] == "thinking":
                if observe:
                    observed.update(await asyncio.to_thread(process_ids, instance))
                await asyncio.sleep(.04)
                turn = (await instance.state())["turns"][-1]
        assert turn["source"] == expected_source and turn["status"] == "replied", turn
        assert not process_ids(instance), message
        if observe:
            assert observed and all(not psutil.pid_exists(pid) for pid in observed), message
        reply = turn["reply"]
        report["cases"].append({"message":message, "reply":reply, "wall_ms":round((perf_counter()-started)*1000, 1),
                                "started_observed":bool(observed), "process_live_after":False})
        return reply

    async def begin_delayed():
        log = server.folder / "web-1.log"
        before = log.read_bytes().count(b"SLOW_CLOCK_START")
        code, state = await server.post("/api/message", {"text":"用 MCP 取消演练调用 稍后读时间"})
        assert code == 200
        turn = state["turns"][-1]
        assert turn["mcp_request"] and turn["status"] == "thinking"
        async with asyncio.timeout(8):
            while log.read_bytes().count(b"SLOW_CLOCK_START") <= before:
                await asyncio.sleep(.05)
        running = process_ids(server)
        assert running
        return turn, running

    try:
        await server.start()
        if catalog_only:
            full_clock = {**clock, "tools":{"read_clock":"当前时间", "read_date":"当前日期"}}
            config.write_text(json.dumps({"servers":{"时钟":full_clock}}, ensure_ascii=False), encoding="utf-8")
            log = server.folder / "web-1.log"

            async def listed(message, expected_discoveries):
                before = log.read_bytes()
                reply = await send(server, message, observe=True)
                protocol = log.read_bytes()[len(before):]
                discoveries = protocol.count(b"ListToolsRequest")
                calls = protocol.count(b"CallToolRequest")
                assert discoveries == expected_discoveries and calls == 0, (message, protocol)
                report["cases"][-1].update(list_tools=discoveries, call_tool=calls)
                return reply

            media = await listed("查看本机媒体 MCP 工具", 1)
            assert "索引来源" in media and "本机或 NAS 媒体" in media
            named = await listed("查看 MCP 时钟的工具", 1)
            assert "当前时间（read_clock）" in named and "当前日期（read_date）" in named
            for message in ("我现在有哪些 MCP 工具？", "列出 MCP 工具"):
                assert await listed(message, 2) == media + "\n\n" + named
            code, state = await server.post("/api/message", {"text":"列出 MCP 工具"})
            turn = state["turns"][-1]
            assert code == 200 and turn["status"] == "thinking" and turn["mcp_request"]
            async with asyncio.timeout(8):
                while True:
                    children = await asyncio.to_thread(psutil.Process(server.pid).children, recursive=True)
                    running = {}
                    for child in children:
                        try:
                            args = child.cmdline()
                        except psutil.NoSuchProcess:
                            continue
                        for module in ("mcp_local_clock", "local_media.mcp_search"):
                            if module in args:
                                running[child.pid] = module
                    if len(set(running.values())) == 2:
                        break
                    await asyncio.sleep(.02)
            code, state = await server.post("/api/cancel", {"turn_id":turn["id"]})
            assert code == 200 and state["turns"][-1]["status"] == "cancelled"
            assert all(not psutil.pid_exists(pid) for pid in running)
            report["cancelled_discovery"] = {"services":sorted(set(running.values())), "all_exited":True}
            config.write_text(json.dumps({"servers":{"时钟":{**full_clock, "timeout_seconds":.1}}}, ensure_ascii=False), encoding="utf-8")
            partial = await listed("列出 MCP 工具", 1)
            assert partial.startswith(media + "\n\n") and "「时钟」未在 0.1 秒内返回" in partial and "read_clock" not in partial
            config.write_text(json.dumps({"servers":{"时钟":clock}}, ensure_ascii=False), encoding="utf-8")
            recovered = await listed("我现在有哪些 MCP 工具？", 2)
            assert "「时钟」已连接 · 1 个只读工具" in recovered and "read_clock" in recovered and "read_date" not in recovered
            config.unlink()
            assert await listed("列出 MCP 工具", 1) == media
            assert hashlib.sha256(catalog.read_bytes()).hexdigest() == catalog_hash
            assert report["model_connections"] == 0 and not process_ids(server)
            report.update(passed=True, index_unchanged=True, no_workload_at_idle=True)
            return
        await blank.start()
        assert not process_ids(server)
        discovered = await send(server, "列出 MCP 服务")
        assert "本机已配置" in discovered and "时钟" in discovered and "日历" in discovered
        assert "还没有配置额外的 MCP 服务" in await send(blank, "列出 MCP 服务")
        listed = await send(server, "查看 MCP 时钟的工具", observe=True)
        assert "已连接 · 1 个只读工具" in listed and "当前时间（read_clock）" in listed and "read_date" not in listed
        content = await send(server, "用 MCP 时钟调用 当前时间 {\"zone\":\"UTC\"}", observe=True)
        assert "MCP 时钟 · 当前时间" in content
        stamp = datetime.fromisoformat(json.loads(content.split("\n",1)[1])["时间"])
        assert stamp.utcoffset() == timedelta(0) and abs((stamp - datetime.now(timezone.utc)).total_seconds()) < 10
        content = await send(server, "通过 MCP 日历调用 read_date", observe=True)
        assert "MCP 日历 · 当前日期" in content and datetime.now().date().isoformat() in content
        assert "没有唯一可调用" in await send(server, "用 MCP 时钟调用 read_date")
        assert "参数需要使用 JSON 对象" in await send(server, "用 MCP 时钟调用 当前时间 []")
        assert "未在 0.1 秒内返回" in await send(server, "用 MCP 慢时钟调用 当前时间")
        assert "未在 4 秒内返回" in await send(server, "用 MCP 稍后读调用 稍后读时间", observe=True)
        assert "配置无效" in await send(server, "查看 MCP 远程的工具")
        assert "未能完成" in await send(server, "查看 MCP 错身份的工具", observe=True)
        assert "没有配置名为" in await send(server, "用 MCP 未配置调用 当前时间")
        assert "可以说" in await send(server, "用 MCP https://example.invalid 执行 read_clock")
        assert "我在" in await send(server, "在吗", expected_source="smalltalk")
        assert not process_ids(server) and not process_ids(blank)
        turn, running = await begin_delayed()
        stamp = perf_counter()
        code, state = await server.post("/api/cancel", {"turn_id":turn["id"]})
        cancel_ms = round((perf_counter() - stamp) * 1000, 1)
        assert code == 200 and state["turns"][-1]["status"] == "cancelled", state
        assert all(not psutil.pid_exists(pid) for pid in running)
        assert "我在" in await send(server, "在吗", expected_source="smalltalk")
        report["cancel"] = {"tool_started":True, "cancel_ms":cancel_ms, "next_turn_succeeded":True, "exited":True}
        _, running = await begin_delayed()
        stamp = perf_counter()
        await server.stop()
        assert all(not psutil.pid_exists(pid) for pid in running)
        report["shutdown"] = {"tool_started":True, "wall_ms":round((perf_counter()-stamp)*1000,1), "exited":True}
        report.update(passed=True, no_workload_at_idle=True, config_isolated=True)
    finally:
        await blank.stop()
        await server.stop()
        if model_gate:
            model_gate.close()
            await model_gate.wait_closed()
        (folder/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


def slow_service():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    clock = FastMCP("local_clock_after_wait")

    @clock.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
    async def read_later() -> dict[str, str]:
        print("SLOW_CLOCK_START", file=sys.stderr, flush=True)
        await asyncio.sleep(20)
        return {"时间":datetime.now().astimezone().isoformat(timespec="seconds")}

    clock.run(transport="stdio")


if __name__ == "__main__":
    if "--local-clock-after-wait" in sys.argv:
        slow_service()
    else:
        asyncio.run(verify(catalog_only="--catalog-only" in sys.argv))
