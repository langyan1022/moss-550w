"""Exercise configured MCP plus device readings through isolated HTTP and SSE."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from typing_extensions import TypedDict
from unittest.mock import patch


BATTERY = "电脑还有多少电"
CLOCK = "用 MCP 时钟调用 当前时间"
UTC_CLOCK = CLOCK + ' {"zone":"UTC"}'
LABEL = "用 MCP 标签调用 查看标签"
VALUE = {"label": '读数，顺便核对 {A,B} 和 "C"', "tags": ["温度,湿度", "以及音量", "甲、乙"]}
DELAYED = "用 MCP 慢查询调用 延时读取"
CLOCK_HISTORY = "用 MCP 时钟查询当前 UTC 时间了吗？"
TOOLS = {"read_label": "查看标签", "read_delayed": "延时读取", "delete_label": "删除标签"}
TIMEOUT = 4.0


class LabelResult(TypedDict):
    label: str
    tags: list[str]


def fixture_server(journal):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("moss_compound_fixture")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def record(tool, **args):
        with journal.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps({"tool": tool, "args": args, "pid": os.getpid()}, ensure_ascii=False) + "\n")

    @server.tool(annotations=read_only, structured_output=True)
    def read_label(label: str, tags: list[str]) -> LabelResult:
        record("read_label", label=label, tags=tags)
        return {"label": label, "tags": tags}

    @server.tool(annotations=read_only, structured_output=True)
    async def read_delayed() -> str:
        record("read_delayed")
        await asyncio.sleep(20)
        return "这条晚到结果不可显示。"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=True), structured_output=True)
    def delete_label() -> str:
        record("delete_label")
        return "此调用必须被拦截。"

    server.run(transport="stdio")


def write_config(path, journal):
    clock = {"command": "project-python", "args": ["-m", "mcp_local_clock"],
             "server_name": "moss_local_clock", "tools": {"read_clock": "当前时间", "read_date": "当前日期"}}
    fixture = {"command": "project-python", "args": [str(Path(__file__).resolve()), "--fixture-server", str(journal)],
               "server_name": "moss_compound_fixture"}
    label = {**fixture, "tools": {key: TOOLS[key] for key in ("read_label", "delete_label")}}
    delayed = {**fixture, "tools": {"read_delayed": TOOLS["read_delayed"]}, "timeout_seconds": TIMEOUT}
    path.mkdir()
    (path / "mcp-servers.json").write_text(json.dumps({"servers": {"时钟": clock, "标签": label, "慢查询": delayed}},
                                                      ensure_ascii=False), encoding="utf-8")


def response_json(reading):
    reply = reading["reply"]
    return json.loads(reply if reply.startswith("{") else reply.split("\n", 1)[1])


def check_clock(result, *, utc):
    stamp = datetime.fromisoformat(result["时间"])
    assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 15, result
    if utc:
        assert stamp.utcoffset() == timedelta(0), result
    assert "现在是" in result["答复"], result


def check_clock_reading(reading, *, utc):
    label = "协调世界时" if utc else "本机时间"
    clock = re.fullmatch(rf"{label}现在是 (\d{{1,2}}) 点 (\d{{2}}) 分。", reading["reply"])
    now = datetime.now(timezone.utc) if utc else datetime.now()
    assert clock and any(tuple(map(int, clock.groups())) == (stamp.hour, stamp.minute)
                         for stamp in (now, now - timedelta(minutes=1))), reading


async def verify(server, stream, journal, protocol, report):
    import psutil
    from mcp_configured import receipt_key
    from verify_local_overview_progressive import wait_turn

    def calls():
        if not journal.is_file():
            return []
        return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]

    async def begin(text):
        start = perf_counter()
        code, state = await server.post("/api/message", {"text": text})
        assert code == 200, state
        return state["turns"][-1], start

    async def finish(turn):
        if turn["status"] == "thinking":
            turn = await wait_turn(stream, turn["id"], "replied")
        assert turn["status"] == "replied" and not turn["cloud_requests"]["total"], turn
        return turn

    def assert_rows(turn, kinds):
        assert turn["source"] == "context" and turn["mcp_request"] is True and "mcp_receipt" not in turn, turn
        rows = turn.get("local_overview", [])
        assert [row["kind"] for row in rows] == kinds, turn
        assert turn["reply"] == "\n".join(row["reply"] for row in rows), turn
        assert not turn.get("local_overview_partial") and not turn.get("partial_reply"), turn
        return rows

    async def ask(text, kinds):
        initial, start = await begin(text)
        turn = await finish(initial)
        rows = assert_rows(turn, kinds)
        report["cases"].append({"text": text, "turn_id": turn["id"], "rows": rows, "wall_ms": round((perf_counter()-start)*1000, 1)})
        return rows

    async def wait_started(before):
        async with asyncio.timeout(3.75):
            while len(calls()) == before:
                await asyncio.sleep(.025)
        row = calls()[-1]
        assert row["tool"] == "read_delayed" and psutil.pid_exists(row["pid"]), row
        return row["pid"]

    async def assert_exited(pid):
        async with asyncio.timeout(2):
            while psutil.pid_exists(pid):
                await asyncio.sleep(.025)

    def battery(reading):
        assert any(word in reading["reply"] for word in ("电量", "电池")), reading

    def mcp_row(reading, service, alias, tool=None, args=None):
        assert reading["kind"] == "mcp" and reading["label"] == f"MCP {service} · {alias}", reading
        assert reading["status"] == ("ok" if tool else "error"), reading
        if tool:
            assert reading["mcp_receipt"] == receipt_key(service, tool, args or {}), reading
        else:
            assert not reading.get("mcp_receipt"), reading

    separate = await finish((await begin(CLOCK + " " + json.dumps({"zone":"UTC"}, indent=2)))[0])
    assert separate["source"] == "context" and separate.get("mcp_receipt"), separate
    check_clock(response_json(separate), utc=True)
    report["standalone_clock"] = separate["reply"]

    rows = await ask(CLOCK + "，顺便告诉我" + BATTERY, ["mcp", "battery"])
    mcp_row(rows[0], "时钟", "当前时间", "read_clock")
    check_clock_reading(rows[0], utc=False)
    battery(rows[1])

    rows = await ask(BATTERY + "，顺便" + UTC_CLOCK, ["battery", "mcp"])
    mcp_row(rows[1], "时钟", "当前时间", "read_clock", {"zone":"UTC"})
    battery(rows[0])
    check_clock_reading(rows[1], utc=True)

    rows = await ask(CLOCK + "顺便告诉我" + BATTERY, ["mcp", "battery"])
    mcp_row(rows[0], "时钟", "当前时间", "read_clock")
    check_clock_reading(rows[0], utc=False)
    battery(rows[1])

    literal = LABEL + " " + json.dumps(VALUE, ensure_ascii=False)
    rows = await ask(literal + "，现在几点", ["mcp", "clock"])
    mcp_row(rows[0], "标签", "查看标签", "read_label", VALUE)
    assert response_json(rows[0]) == VALUE and calls()[-1]["args"] == VALUE, rows
    assert calls()[-1]["tool"] == "read_label", calls()
    await assert_exited(calls()[-1]["pid"])
    rows = await ask("现在几点，然后" + literal, ["clock", "mcp"])
    mcp_row(rows[1], "标签", "查看标签", "read_label", VALUE)
    assert response_json(rows[1]) == VALUE and calls()[-1]["args"] == VALUE, rows
    await assert_exited(calls()[-1]["pid"])

    pretty = LABEL + " " + json.dumps(VALUE, ensure_ascii=False, indent=2)
    for text, order, index in (
        (pretty + "，" + BATTERY, ["mcp", "battery"], 0),
        (BATTERY + "，" + pretty, ["battery", "mcp"], 1),
    ):
        rows = await ask(text, order)
        mcp_row(rows[index], "标签", "查看标签", "read_label", VALUE)
        assert response_json(rows[index]) == VALUE and calls()[-1]["args"] == VALUE, rows
        await assert_exited(calls()[-1]["pid"])

    rows = await ask(literal + "、" + BATTERY + "、" + UTC_CLOCK, ["mcp", "battery", "mcp"])
    mcp_row(rows[0], "标签", "查看标签", "read_label", VALUE)
    mcp_row(rows[2], "时钟", "当前时间", "read_clock", {"zone":"UTC"})
    assert response_json(rows[0]) == VALUE and calls()[-1]["args"] == VALUE, rows
    battery(rows[1])
    check_clock_reading(rows[2], utc=True)
    combined_id = report["cases"][-1]["turn_id"]
    before_history = protocol.read_bytes().count(b"CallToolRequest")
    history = await finish((await begin(CLOCK_HISTORY))[0])
    assert history["mcp_status"]["matched_turn_id"] == combined_id, history
    assert rows[2]["reply"] in history["reply"] and rows[0]["reply"] not in history["reply"], history
    assert rows[1]["reply"] not in history["reply"] and not history.get("mcp_receipt"), history
    assert protocol.read_bytes().count(b"CallToolRequest") == before_history, history
    report["history"] = {"matched_turn_id": combined_id, "reply": history["reply"], "extra_calls": 0}

    before_bad = len(calls())
    for args in ('{label:"不合法"}', '{\n  "label": "带尾逗号",\n  "tags": ["温度"],\n}'):
        rows = await ask(LABEL + " " + args + "，" + BATTERY, ["mcp", "battery"])
        mcp_row(rows[0], "标签", "查看标签")
        assert "JSON" in rows[0]["reply"] and len(calls()) == before_bad, rows
        battery(rows[1])
    rows = await ask(BATTERY + "，用 MCP 标签调用 删除标签", ["battery", "mcp"])
    mcp_row(rows[1], "标签", "删除标签")
    assert "没有提供这个只读工具" in rows[1]["reply"] and len(calls()) == before_bad, rows
    battery(rows[0])
    report["invalid_fences"] = []
    for args in ('{"label":"未闭合"', '{\n  "label": "未闭合",\n  "tags": ["温度"]',
                 '```json\n' + json.dumps(VALUE, ensure_ascii=False, indent=2) + '\n```'):
        bad_tail = await finish((await begin(LABEL + " " + args + "，现在几点"))[0])
        assert "JSON" in bad_tail["reply"] and len(calls()) == before_bad, bad_tail
        assert not bad_tail.get("mcp_receipt"), bad_tail
        report["invalid_fences"].append(bad_tail["reply"])

    before_protected = protocol.read_bytes().count(b"CallToolRequest")
    for text in (UTC_CLOCK + "，然后锁屏", UTC_CLOCK + "，顺便执行 PowerShell：Get-Date"):
        guarded = await finish((await begin(text))[0])
        assert not any(guarded.get(field) for field in ("local_overview", "mcp_receipt", "desktop_confirmation")), guarded
        assert "JSON" in guarded["reply"], guarded
    assert protocol.read_bytes().count(b"CallToolRequest") == before_protected
    report["mutating_suffix_extra_calls"] = 0

    before_slow = len(calls())
    initial, start = await begin(DELAYED + "，" + BATTERY)
    early = await wait_turn(stream, initial["id"], "thinking", with_partial=True)
    partial_ms = round((perf_counter() - start)*1000, 1)
    assert [row["kind"] for row in early["local_overview_partial"]] == ["battery"], early
    assert not early.get("reply") and partial_ms < 1000, (partial_ms, early)
    battery(early["local_overview_partial"][0])
    pid = await wait_started(before_slow)
    timed = await finish(initial)
    timed_ms = round((perf_counter() - start)*1000, 1)
    rows = assert_rows(timed, ["mcp", "battery"])
    mcp_row(rows[0], "慢查询", "延时读取")
    assert "未在 4 秒内返回" in rows[0]["reply"] and "晚到结果" not in timed["reply"], timed
    assert rows[1] == early["local_overview_partial"][0] and partial_ms < timed_ms < 5600, timed
    await assert_exited(pid)
    report["timeout"] = {"partial_ms": partial_ms, "wall_ms": timed_ms, "rows": rows, "process_exited": True}

    before_cancel = len(calls())
    initial, _ = await begin("现在几点，" + DELAYED)
    early = await wait_turn(stream, initial["id"], "thinking", with_partial=True)
    assert [row["kind"] for row in early["local_overview_partial"]] == ["clock"], early
    pid = await wait_started(before_cancel)
    stamp = perf_counter()
    code, state = await server.post("/api/cancel", {"turn_id": initial["id"]})
    cancelled_ms = round((perf_counter() - stamp)*1000, 1)
    assert code == 200 and state["turns"][-1]["status"] == "cancelled", state
    await assert_exited(pid)
    final = await wait_turn(stream, initial["id"], "cancelled")
    assert not final.get("local_overview") and not final.get("mcp_receipt"), final
    assert (await finish((await begin("现在几点"))[0]))["reply"], "取消后应能继续查询"
    report["cancel"] = {"wall_ms": cancelled_ms, "process_exited": True}
    report["fixture_calls"] = calls()
    assert [row["tool"] for row in calls()] == ["read_label"] * 5 + ["read_delayed"] * 2, calls()


async def run():
    from aiohttp import ClientSession

    from browser_lab import BrowserServer, ROOT
    from local_media.library import MediaLibrary
    from local_overview import parse_local_overview, parse_mcp_overview
    from mcp_configured import MCP_DATA
    from model_catalog import SPECS
    from verify_local_overview_progressive import available_port
    from verify_named_natural_mcp import protocol_log
    from verify_reminder_ordinals import serve_profile

    folder = ROOT / "runs" / datetime.now().strftime("compound-configured-mcp-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result)
    report = {"passed": False, "model_connections": 0, "cases": []}
    journal = folder / "calls.jsonl"
    protocol = folder / "mcp-stdio.log"

    async def model_guard(_, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        report["protected"] = [
            "不要" + CLOCK + "，" + BATTERY,
            "把“" + CLOCK + "”翻译成英文，" + BATTERY,
            "文案里的提示是用 MCP 时钟调用 当前时间，" + BATTERY,
            UTC_CLOCK + "，然后锁屏",
            "现在几点，" + UTC_CLOCK + "，然后取消全部提醒",
            UTC_CLOCK + "，顺便执行 PowerShell：Get-Date",
            "通过 MCP 时钟查我下一站的气候，" + BATTERY,
        ]
        catalog = folder / "empty-catalog.sqlite3"
        MediaLibrary(catalog)
        async with AsyncExitStack() as stack:
            guard = await stack.enter_async_context(await asyncio.start_server(model_guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    env[prefix + "_BASE_URL"] = model_url
                    env[prefix + "_API_KEY"] = "isolated-no-real-provider"
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch("model_catalog.allowed", side_effect=lambda _, url: url == model_url))
            server = BrowserServer(folder, available_port(), model_url=model_url)
            write_config(server.data, journal)
            stack.callback(MCP_DATA.reset, MCP_DATA.set(server.data))
            for text in report["protected"]:
                assert parse_local_overview(text) is None and parse_mcp_overview(text) is None, text
            stack.enter_context(protocol_log(protocol))
            async with serve_profile(server, model_url) as moss:
                code, _ = await server.post("/api/select-model", {"id": "deepseek"})
                assert code == 200 and moss.model_id == "deepseek"
                async with ClientSession() as client, client.get(server.url + "/api/events") as events:
                    assert events.status == 200 and events.content_type == "text/event-stream"
                    await verify(server, events.content, journal, protocol, report)
        assert not report["model_connections"], report
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture-server"]:
        fixture_server(Path(sys.argv[2]))
    else:
        asyncio.run(run())
