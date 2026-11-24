import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
import re
from time import perf_counter

import psutil
from pycaw.pycaw import AudioUtilities
import win32file

from browser_lab import ROOT
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated


QUESTION = "现在几点、电脑还有多少电、音量多大？"
METRICS = {
    "time": r"(?:[01]?\d|2[0-3])[:：](?:[0-5]\d)|\d{1,2}[点时]\d{1,2}分",
    "battery": r"(?:电量|电池)",
    "volume": r"音量",
}


def independent_readings():
    battery = psutil.sensors_battery()
    speaker = AudioUtilities.GetSpeakers()
    return {"battery": round(battery.percent) if battery else None,
            "volume": round(speaker.EndpointVolume.GetMasterVolumeLevelScalar() * 100) if speaker else None,
            "muted": bool(speaker.EndpointVolume.GetMute()) if speaker else None}


def check_live_values(items, start, end, initial, final):
    replies = {item["kind"]: item["reply"] for item in items}
    if "clock" in replies:
        match = re.search(r"(\d\d):(\d\d):(\d\d)", replies["clock"])
        assert match, replies["clock"]
        hour, minute, second = map(int, match.groups())
        candidates = [day.replace(hour=hour, minute=minute, second=second, microsecond=0) for day in (start, end)]
        assert any(start - timedelta(seconds=1) <= actual <= end for actual in candidates), (start, end, match[0])
    if "volume" in replies:
        if initial["volume"] is None and final["volume"] is None:
            assert re.search(r"没有|未能|无法|不可用", replies["volume"]), replies
        else:
            match = re.search(r"(\d+)%", replies["volume"])
            assert match and int(match[1]) in (initial["volume"], final["volume"]), (initial, replies, final)
            assert ("已静音" in replies["volume"]) in (initial["muted"], final["muted"])
    if "battery" in replies:
        if initial["battery"] is None and final["battery"] is None:
            assert "未报告" in replies["battery"], replies
        else:
            match = re.search(r"(\d+)%", replies["battery"])
            assert match and int(match[1]) in (initial["battery"], final["battery"]), (initial, replies, final)


async def run(baseline):
    folder = ROOT / "runs" / datetime.now().strftime("local-overview-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    catalog = folder / "空索引.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0")
    server = isolated(folder / "app")
    report = {"passed": False, "baseline": baseline, "model_connections": 0, "questions": []}
    result = folder / "result.json"
    print(result, flush=True)

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(question, expected=()):
        initial = independent_readings() if expected else None
        start = datetime.now()
        tick = perf_counter()
        code, state = await server.post("/api/message", {"text": question, "chat_id": chat})
        assert code == 200, state
        turn = state["turns"][-1]
        turn_id = turn["id"]
        first_readings = []
        first_ms = None
        async with asyncio.timeout(25):
            while turn["status"] in {"thinking", "saving"}:
                if first_ms is None and turn.get("local_overview_partial"):
                    first_readings = turn["local_overview_partial"]
                    first_ms = round((perf_counter() - tick) * 1000, 1)
                await asyncio.sleep(.03)
                turn = (await server.state())["turns"][-1]
                assert turn["id"] == turn_id
        reply = turn.get("reply") or ""
        row = {"text": question, "reply": reply, "status": turn["status"], "error": turn.get("error"),
               "source": turn.get("source"), "cloud_requests": turn["cloud_requests"],
               "present": [key for key, pattern in METRICS.items() if re.search(pattern, reply)],
               "items": turn.get("local_overview"), "first_readings": first_readings, "first_readings_ms": first_ms,
               "ms": round((perf_counter()-tick)*1000, 1)}
        report["questions"].append(row)
        if expected:
            end = datetime.now()
            final = independent_readings()
            row["windows_reads"] = {"before": initial, "after": final, "start": start.isoformat(), "end": end.isoformat()}
            assert turn["status"] == "replied" and turn["source"] == "context", row
            assert turn["cloud_requests"]["total"] == report["model_connections"] == 0, row
            assert not turn.get("desktop_confirmation") and not turn.get("media") and not turn.get("receipt"), row
            assert [item["kind"] for item in row["items"]] == list(expected), row
            assert all(item in row["items"] for item in first_readings), row
            assert all(item["reply"] and item["reply"] in reply for item in row["items"]), row
            assert [reply.index(item["reply"]) for item in row["items"]] == sorted(reply.index(item["reply"]) for item in row["items"])
            check_live_values(row["items"], start, end, initial, final)
        return row

    try:
        async with await asyncio.start_server(guard, "127.0.0.1", 0) as listener:
            server.model_url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = server.model_url
            await server.start()
            state = await server.state()
            assert state["model_id"] == "local" and state["memory_backend"] == "sqlite"
            chat = state["chat_id"]
            if baseline:
                row = await ask(QUESTION)
                assert row["status"] != "replied" or set(row["present"]) != set(METRICS), row
            else:
                from desktop_actions import parse_desktop
                from device_status import parse_device_status
                from local_overview import parse_local_overview
                await ask(QUESTION, ("clock", "battery", "volume"))
                conversational_queries = (
                    ("现在几点了？电脑还剩多少电？声音有多大？", ("clock", "battery", "volume")),
                    ("帮我看看现在几点了，我电量还剩多少，音量多大啊", ("clock", "battery", "volume")),
                    ("现在几点了？然后电脑电量和音量分别是多少？", ("clock", "battery", "volume")),
                    ("看看系统音量、电量、剩余内存", ("volume", "battery", "memory")),
                    ("现在几点顺便看一下我的电量和声音大小", ("clock", "battery", "volume")),
                    ("几点了，电还有多少，音量开到多少了", ("clock", "battery", "volume")),
                    ("我电脑现在几点，电量和音量呢", ("clock", "battery", "volume")),
                    ("系统时间、电量还有音量都给我看一眼", ("clock", "battery", "volume")),
                    ("我电脑电量和系统音量都是多少", ("battery", "volume")),
                    ("电量和音量分别是多少，再告诉我现在几点？", ("battery", "volume", "clock")),
                    ("电量和音量各是多少，顺便告诉我现在几点", ("battery", "volume", "clock")),
                    ("现在几点电脑还有多少电，音量多大？", ("clock", "battery", "volume")),
                    ("音量多大电脑还剩多少电现在几点了", ("volume", "battery", "clock")),
                    ("这会儿几点电脑电量多少？", ("clock", "battery")),
                    ("笔记本还剩百分之多少的电扬声器音量多大", ("battery", "volume")),
                    ("开机多久了现在几点啊内存还有多少", ("uptime", "clock", "memory")),
                    ("现在几点电脑电量多少和音量多大", ("clock", "battery", "volume")),
                )
                for question, kinds in conversational_queries:
                    await ask(question, kinds)
                await ask("电脑还有多少电和音量多大？", ("battery", "volume"))
                await ask("笔记本还剩百分之多少的电、扬声器音量多大？", ("battery", "volume"))
                await ask("我的笔记本电脑电量还有多少、默认音频输出设备的音量呢？", ("battery", "volume"))
                await ask("电量还有多少？这会儿系统音量多大？", ("battery", "volume"))
                await ask("现在几点了啊，笔记本还剩百分之多少的电，扬声器音量多大？", ("clock", "battery", "volume"))
                await ask("现在几点、电脑还有多少电，还有音量多大？", ("clock", "battery", "volume"))
                await ask("这会儿几点，电脑电量多少？", ("clock", "battery"))
                await ask("现在几点？电量还有多少？音量呢？", ("clock", "battery", "volume"))
                date = await ask("现在的日期和时间以及系统音量？", ("clock", "volume"))
                assert "年" in date["items"][0]["reply"] and "星期" in date["items"][0]["reply"], date
                await ask("现在的日期和时间和系统音量？", ("clock", "volume"))
                await ask("音量多大，顺便现在几点？", ("volume", "clock"))
                await ask("现在几点、系统音量、现在几点？", ("clock", "volume"))
                main_drive = os.environ["SystemDrive"][0].upper()
                disk = await ask(f"查看我的提醒、{main_drive}盘还有多少空间？", ("reminders", "disk"))
                assert "没有" in disk["items"][0]["reply"] and "提醒" in disk["items"][0]["reply"], disk
                available, total, free = win32file.GetDiskFreeSpaceEx(main_drive + ":\\")
                gib = 1 << 30
                reading = re.search(r"可用 ([\d.]+) GiB，总计 ([\d.]+) GiB", disk["items"][1]["reply"])
                assert reading and abs(float(reading[1]) - free/gib) < .3 and abs(float(reading[2]) - total/gib) < .1, disk
                missing = next(letter for letter in "ZYXWVUTSRQPONMLKJIHGFED" if
                               not win32file.GetLogicalDrives() & (1 << (ord(letter) - ord("A"))))
                missing_disk = await ask(f"{missing}盘还有多少空间、现在几点？", ("disk", "clock"))
                assert f"{missing} 盘" in missing_disk["items"][0]["reply"] and "不可用" in missing_disk["items"][0]["reply"]
                single = await ask("系统音量多大？")
                assert single["source"] == "desktop" and single["status"] == "replied" and not single["items"], single
                negatives = ("现在几点、把系统音量调到百分之十", "一分钟后提醒我看电量，音量多大？",
                             "取消所有提醒以及查看系统音量", "把‘现在几点、电脑还有多少电’翻译成英语",
                             "把现在几点和系统音量改写得更口语", "查询北京和上海的天气、系统音量多大？",
                             "现在几点，不用查电量和音量", "我之前说过电量和音量", "音量调到五十、电量是多少",
                             "天气、电量和音量都给我看一眼", "电脑有杂音、现在几点", "把电量和音量都写下",
                             "现在几点别查电量音量多大", "电脑还有多少电把音量调到十",
                             "把‘几点电脑电量多少’写进便签", "现在几点给我写一份上海天气预报",
                             "现在几点上海天气怎么样", "现在几点查看我的提醒",
                             "电脑还有多少电我准备关机", "现在几点音量多大顺便介绍一下北京")
                for text in negatives:
                    assert not parse_local_overview(text), text
                for text in ("笔记本电池怎么换", "把现在的电量写进便签"):
                    assert parse_device_status(text) is None, text
                for text in ("把扬声器音量调低", "怎么调整默认音频输出设备的音量", "解释声音大小的原理"):
                    assert (parse_desktop(text) or {}).get("action") != "get_volume", text
                assert parse_desktop("把系统音量调到百分之十") == {"action": "set_volume", "level": 10}
                report["parser_only_negatives"] = list(negatives)
                assert report["model_connections"] == 0
            report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        try:
            await server.stop()
        finally:
            result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true", help="验证旧入口是否漏答")
    asyncio.run(run(parser.parse_args().baseline))
