"""Run reminder list references through the isolated message API."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import json
import os
from unittest.mock import patch

from aiohttp import web

from browser_lab import BrowserServer, ROOT
from core import Moss
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_cross_domain_weather_media import BEIJING, catalog_at
from verify_weather_browser_location import sample_forecast
from weather_forecast import format_weather


MORNING = "明天上午有哪些提醒"
WEATHER = "北京今天天气"
MUSIC = "只看看测试歌手的歌，不要播放"
FIRST_CANCEL = "取消第一个提醒"
NO_LIST = "请先查看提醒列表"
STALE = "这条提醒在查看后已变化"


@asynccontextmanager
async def serve_profile(server, model_url):
    moss = Moss(server.data, model_url, "isolated-reminder-ordinals")
    stopped = asyncio.Event()
    runner = web.AppRunner(application(moss, lambda: "standby", server.data, server.port,
                                       "isolated-reminder-ordinals", stopped), access_log=None)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(moss.close)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", server.port).start()
        try:
            yield moss
        finally:
            stopped.set()
            moss._notify()


def timer_rows(moss):
    return {row[0]: row[1:] for row in moss.db.execute(
        "SELECT id,status,label,due_at,generation,chat_id FROM assistant_timers ORDER BY id")}


async def run_cases(server, moss, report):
    async def ask(text, prefix=None):
        code, state = await server.post("/api/message", {"text": text})
        assert code == 200, state
        turn = state["turns"][-1]
        turn_id = turn["id"]
        async with asyncio.timeout(5):
            while turn["status"] in {"thinking", "saving"}:
                await asyncio.sleep(0.03)
                turn = next(row for row in (await server.state())["turns"] if row["id"] == turn_id)
        assert turn["status"] == "replied" and turn["user"] == text, turn
        assert not turn["cloud_requests"]["total"] and "_reminder_list_rows" not in turn, turn
        if prefix:
            assert turn["reply"].startswith(prefix), turn
        report["cases"].append({"text": text, "source": turn["source"], "reply": turn["reply"]})
        return turn

    async def switch(chat=None):
        path = "/api/switch-chat" if chat else "/api/new-chat"
        status, state = await server.post(path, {"id": chat} if chat else {})
        assert status == 200 and (not chat or state["chat_id"] == chat), state
        return state["chat_id"]

    async def list_ids(text, expected):
        turn = await ask(text)
        captured = moss._get(turn["id"]).get("_reminder_list_rows")
        assert [row[0] for row in captured] == expected, (turn, captured)
        reply = turn["reply"]
        positions = [reply.index(f"{key} 号 ·") for key in expected]
        assert positions == sorted(positions), turn

    async def no_mutation(text, prefix):
        before = timer_rows(moss)
        await ask(text, prefix)
        assert timer_rows(moss) == before, text

    other = (await server.state())["chat_id"]
    await ask("明天晚上八点提醒我检查外部对话", "已设置 1 号提醒")
    main = await switch()
    await ask("明天上午十点提醒我核对物品", "已设置 2 号提醒")
    await ask("明天上午九点提醒我核对物品", "已设置 3 号提醒")
    await ask("后天上午八点提醒我检查日期范围", "已设置 4 号提醒")
    initial = timer_rows(moss)
    assert initial[1][-1] == other and all(initial[key][-1] == main for key in (2, 3, 4))

    await list_ids(MORNING, [3, 2])
    await no_mutation("第一个提醒还有多久？", "3 号提醒「核对物品」还有")
    await list_ids(MORNING, [3, 2])
    await no_mutation("第2个提醒几点响？", "2 号提醒「核对物品」还有")
    await no_mutation("第1号提醒还有多久？", "1 号提醒「检查外部对话」还有")

    await list_ids(MORNING, [3, 2])
    await no_mutation("取消第99个提醒", "刚才的提醒列表没有这一项")
    await no_mutation("取消两个提醒", "请说清列表中的第几个提醒")
    await list_ids(MORNING, [3, 2])
    empty = await switch()
    await no_mutation(FIRST_CANCEL, NO_LIST)
    await switch(main)
    await ask("把第一个提醒改到后天上午九点", "已改期 3 号提醒")
    moved = timer_rows(moss)
    assert {key for key in moved if initial[key] != moved[key]} == {3}, moved
    assert moved[3][3] == initial[3][3] + 1 and kept_hour(moved[3], 9)

    await switch(other)
    await ask("把3号提醒改到明天上午九点", "已改期 3 号提醒")
    await switch(main)
    await list_ids(MORNING, [3, 2])
    await switch(other)
    await ask("把3号提醒改到明天上午十一点", "已改期 3 号提醒")
    await ask("把3号提醒改到明天上午九点", "已改期 3 号提醒")
    retimed = timer_rows(moss)
    assert retimed[3][2] == initial[3][2] and retimed[3][3] == initial[3][3] + 4
    await switch(main)
    await no_mutation(FIRST_CANCEL, STALE)

    await list_ids(MORNING, [3, 2])
    await switch(other)
    await ask("取消3号提醒", "已取消 3 号提醒")
    await switch(main)
    await no_mutation(FIRST_CANCEL, STALE)
    assert timer_rows(moss)[2] == initial[2]

    for text, source in ((WEATHER, "holo"), ("明天有哪些日程", "calendar"),
                         ("记住：我喜欢用等宽字体", "memory"), (MUSIC, "media")):
        await list_ids(MORNING, [2])
        turn = await ask(text)
        assert turn["source"] == source, turn
        if text == WEATHER:
            assert turn["weather"]["city"] == "北京", turn
        if text == MUSIC:
            assert turn["media"]["items"] and not turn["media"].get("autoplay"), turn
        await no_mutation(FIRST_CANCEL, NO_LIST)

    overview = await ask("明天上午有哪些提醒、明天晚上有哪些提醒、现在几点")
    assert [row["kind"] for row in overview["local_overview"]] == ["reminders", "reminders", "clock"], overview
    assert "_reminder_list_rows" not in moss._get(overview["id"])
    await no_mutation(FIRST_CANCEL, NO_LIST)

    await list_ids("北京今天天气、明天上午有哪些提醒", [2])
    await no_mutation("第一个提醒还有多久？", "2 号提醒「核对物品」还有")
    await list_ids("后天晚上有哪些提醒", [])
    await no_mutation(FIRST_CANCEL, "刚才的提醒列表没有这一项")
    await list_ids(MORNING, [2])
    report["chats"] = {"other": other, "main": main, "empty": empty}
    return timer_rows(moss)


def kept_hour(row, hour):
    return datetime.fromtimestamp(row[2]).hour == hour


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("reminder-ordinals-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result)
    report = {"passed": False, "model_connections": 0, "forecast_calls": 0, "cases": []}

    async def model_guard(_, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def forecast(city, period, place, *, part_of_day=None, question=""):
        assert city == "北京" or (place or {}).get("name") == "北京", (city, place)
        report["forecast_calls"] += 1
        return format_weather(sample_forecast(), place or BEIJING, period, part_of_day, question)

    try:
        async with AsyncExitStack() as stack:
            guard = await stack.enter_async_context(await asyncio.start_server(model_guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog_at(folder)), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    env[prefix + "_BASE_URL"] = model_url
                    env[prefix + "_API_KEY"] = "isolated-qa"
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch("assistant_brain.fetch_weather", forecast))

            server = BrowserServer(folder, free_port(), model_url=model_url)
            async with serve_profile(server, model_url) as moss:
                before = await run_cases(server, moss, report)
            async with serve_profile(server, model_url) as moss:
                code, state = await server.post("/api/message", {"text": FIRST_CANCEL})
                turn = state["turns"][-1]
                assert code == 200 and turn["reply"].startswith("已取消 2 号提醒"), state
                assert turn["status"] == "replied" and not turn["cloud_requests"]["total"], turn
                assert "_reminder_list_rows" not in turn
                after = timer_rows(moss)
                assert after == {**before, 2: ("cancelled", *before[2][1:])}, after
                report["restart_cancel_reply"] = turn["reply"]
            assert report["model_connections"] == 0 and report["forecast_calls"] == 2, report
            report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(run())
