import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import re
import socket
import sqlite3
from time import perf_counter

from browser_lab import BrowserServer, ROOT
from local_media.library import DEFAULT_DB
from verify_configured_mcp import process_ids


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("natural-mcp-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    with closing(sqlite3.connect(DEFAULT_DB.resolve().as_uri() + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(folder / "catalog.sqlite3")) as copy:
            origin.backup(copy)
    os.environ["MOSS_MEDIA_DB"] = str(folder / "catalog.sqlite3")
    os.environ["MOSS_WINDOWS_REMINDERS"] = "0"
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    server = BrowserServer(folder, port, model_url="http://127.0.0.1:9/v1")
    server.data.mkdir()
    config_file = server.data / "mcp-servers.json"
    command = {"command":"project-python", "args":["-m","mcp_local_clock"], "server_name":"moss_local_clock"}
    clock = {**command, "tools":{"read_clock":"当前时间", "read_date":"当前日期"}}
    calendar = {**command, "tools":{"read_date":"当前日期"},
                "natural":[{"tool":"read_date", "phrases":["核对工作日期", "请确认工作日期吧。"]}]}

    def configure(options):
        config_file.write_text(json.dumps({"servers":options}, ensure_ascii=False), encoding="utf-8")

    configure({"时钟":clock, "日历":calendar})
    report = {"passed":False, "cases":[], "scope":"普通真实隔离 HTTP 入口，官方 SDK 实际本机只读时钟。模型地址为关闭的回环端口。"}

    async def send(text, *, expected_source="context", mcp=True):
        started = perf_counter()
        code, state = await server.post("/api/message", {"text":text})
        assert code == 200, (code, state)
        turn = state["turns"][-1]
        async with asyncio.timeout(12):
            while turn["status"] == "thinking":
                await asyncio.sleep(.04)
                turn = (await server.state())["turns"][-1]
        assert turn["status"] == "replied" and turn["source"] == expected_source, turn
        assert bool(turn.get("mcp_request")) is mcp
        assert not process_ids(server)
        report["cases"].append({"text":text, "reply":turn["reply"], "source":turn["source"], "mcp":mcp,
                                "wall_ms":round((perf_counter()-started)*1000, 1)})
        return turn

    def utc_time(text):
        match = re.fullmatch(r"协调世界时现在是 (\d{1,2}) 点 (\d{2}) 分。", text)
        assert match, text
        now = datetime.now(timezone.utc)
        assert any((int(match[1]), int(match[2])) == (t.hour,t.minute) for t in (now, now-timedelta(minutes=1))), text

    try:
        await server.start()
        for phrase in ("UTC 现在几点了？", "用时钟看看当前 UTC 时间。", "问一下时钟，世界时现在几点？",
                       "MOSS，请问协调世界时是多少？"):
            utc_time((await send(phrase))["reply"])
        stamp = datetime.now().date()
        expected = f"今天是 {stamp.year} 年 {stamp.month} 月 {stamp.day} 日。"
        assert (await send("用时钟看看今天日期。"))["reply"] == expected
        assert (await send("帮我核对工作日期。"))["reply"] == expected
        assert (await send("能否确认工作日期呢？"))["reply"] == expected
        assert "MCP 日历 · 当前日期" in (await send("用 MCP 日历调用 当前日期"))["reply"]
        assert "MCP 时钟 · 当前时间" in (await send("用 MCP 时钟调用 当前时间 {\"zone\":\"UTC\"}"))["reply"]

        # 配置改变后，只有本轮明确点名的服务才能说明权限，旧结果不会重复使用。
        configure({"时钟":{**clock, "tools":{"read_date":"当前日期"}}, "日历":calendar})
        assert "没有唯一可调用的该只读工具" in (await send("用时钟看看当前 UTC 时间。"))["reply"]
        configure({"时钟":clock, "日历":{**calendar, "natural":[{"tool":"read_date", "phrases":["今天日期"]}]}})
        assert "多个本机 MCP 工具" in (await send("今天日期"))["reply"]
        configure({"时钟":clock, "日历":calendar})
        assert (await send("现在几点？", expected_source="holo", mcp=False)).get("holo_tools") == ["get_time"]
        normal = await send("我想听刀郎的歌", expected_source="media", mcp=False)
        assert normal["media"]["autoplay"] and normal["media"]["online_calls"] == 0
        assert (await send("在吗", expected_source="smalltalk", mcp=False))["reply"] == "我在。"
        from context_tools import parse_context
        from mcp_configured import MCP_DATA
        mark = MCP_DATA.set(server.data)
        try:
            for text in ("解释“UTC几点”这个句子", "UTC几点，而且顺便改掉本机时钟", "用时钟更改今天日期"):
                assert parse_context(text) is None, text
            report["not_taken"] = 3
        finally:
            MCP_DATA.reset(mark)
        report["passed"] = True
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
