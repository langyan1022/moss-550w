import asyncio
from datetime import datetime
import json
import os
import re
import socket
from time import perf_counter, time

import psutil
import win32file

from browser_lab import BrowserServer, ROOT
from context_tools import parse_context
from device_status import GIB


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("device-status-%Y%m%d-%H%M%S-%f")
    os.environ["MOSS_WINDOWS_REMINDERS"] = "0"
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    server = BrowserServer(folder, port, model_url="http://127.0.0.1:9/v1")
    report = {"passed":False, "cases":[], "independent_windows_reads":{}}
    system = (os.environ.get("SystemDrive") or "C:").upper().removesuffix(":")
    missing = next((letter for letter in "ZYXWVUTSRQPONMLKJIHGFED" if
                    not win32file.GetLogicalDrives() & (1 << (ord(letter) - ord("A")))), None)

    async def send(text):
        started = perf_counter()
        status, state = await server.post("/api/message", {"text":text})
        assert status == 200
        turn = state["turns"][-1]
        async with asyncio.timeout(8):
            while turn["status"] == "thinking":
                await asyncio.sleep(.025)
                turn = (await server.state())["turns"][-1]
        assert turn["status"] == "replied" and turn["source"] == "context"
        assert not any(turn.get(field) for field in ("holo_tools","receipt","mcp_request","partial_reply"))
        reply = turn["reply"]
        report["cases"].append({"text":text, "reply":reply, "wall_ms":round((perf_counter()-started)*1000, 1)})
        return reply

    def disk_check(result, letter):
        reads = win32file.GetDiskFreeSpaceEx(f"{letter}:\\")
        match = re.search(rf"{letter} 盘可用 ([\d.]+) GiB，总计 ([\d.]+) GiB。", result)
        assert match, result
        free, total = map(float, match.groups())
        assert abs(free - (reads[2] / GIB)) <= .2 and abs(total - (reads[1] / GIB)) <= .10
        report["independent_windows_reads"][f"{letter}_disk_gib"] = {"free":round(reads[2]/GIB,2),"total":round(reads[1]/GIB,2)}

    try:
        await server.start()
        summary = await send("查看系统状态")
        for indicator in ("CPU", "内存可用", f"{system} 盘可用", "开机已运行"):
            assert indicator in summary, summary
        assert "电量" in summary or "电池" in summary
        diagnosis = await send("电脑怎么这么卡？")
        assert "短时读数" in diagnosis and "CPU" in diagnosis and "内存" in diagnosis
        disk_check(summary, system)
        cpu, paired = await asyncio.gather(send("现在 CPU 占用率是多少？"), asyncio.to_thread(psutil.cpu_percent, .20))
        percent = int(re.search(r"占用 (\d+)%", cpu)[1])
        assert 0 <= percent <= 100 and 0 <= paired <= 100 and abs(percent - paired) <= 40
        report["independent_windows_reads"]["cpu_percent_parallel"] = paired

        memory = await send("电脑内存现在还剩多少？")
        values = re.search(r"内存可用 ([\d.]+) GiB，总计 ([\d.]+) GiB", memory)
        real = psutil.virtual_memory()
        available, total = map(float, values.groups())
        assert abs(available - real.available / GIB) <= .75 and abs(total - real.total / GIB) <= .1
        report["independent_windows_reads"]["memory_gib"] = {"available":round(real.available/GIB,2),"total":round(real.total/GIB,2)}
        disk_check(await send("系统盘还有多少空间？"), system)
        disk_check(await send("G 盘剩余空间"), "G")
        if missing:
            assert "当前不可用" in await send(f"{missing}盘还有多少空间")

        battery = await send("电脑还有多少电？")
        battery_data = psutil.sensors_battery()
        if battery_data:
            assert "电量" in battery
            found = re.search(r"电量 (\d+)%", battery)
            if found:
                assert abs(int(found[1]) - battery_data.percent) <= 2
            if battery_data.power_plugged is not None:
                assert ("已接电" if battery_data.power_plugged else "未接电") in battery
            report["independent_windows_reads"]["battery"] = {"percent":battery_data.percent,
                                                              "power_plugged":battery_data.power_plugged}
        else:
            report["independent_windows_reads"]["battery"] = None

        uptime = await send("这台电脑开机多久了？")
        elapsed = re.search(r"开机已运行 (?:(\d+) 天 )?(\d+) 小时 (\d+) 分钟", uptime)
        days, hours, minutes = (int(value or 0) for value in elapsed.groups())
        win_seconds = time() - psutil.boot_time()
        assert abs(days*86400 + hours*3600 + minutes*60 - win_seconds) <= 90
        report["independent_windows_reads"]["uptime_seconds"] = round(win_seconds,1)

        blocked = ("打开 G 盘", "删除 G 盘的临时文件", "把系统盘切换到 G 盘",
                   "G 盘还剩多少空间，然后把下载目录清空", "解释 CPU 占用率这个概念",
                   "查看 G:\\音乐 的目录内容", "手机还剩多少电")
        for text in blocked:
            assert (parse_context(text) or {}).get("action") != "device_status", text
        report["not_intercepted"] = list(blocked)
        report["passed"] = True
    finally:
        await server.stop()
        folder.mkdir(exist_ok=True)
        (folder/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
