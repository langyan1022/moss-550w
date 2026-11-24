import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from time import perf_counter
from unittest.mock import patch

from core import Moss
from everyday import readback_source
from local_media.library import MediaLibrary
from local_overview import device_status, run_desktop
from verify_weather_browser_location import sample_forecast
from weather_forecast import format_weather


ROOT = Path(__file__).resolve().parent


async def run(archive):
    directory = ROOT / "runs" / datetime.now().strftime("readback-%Y%m%d-%H%M%S-%f")
    data = directory / "data"
    data.mkdir(parents=True)
    catalog = directory / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(LOCALAPPDATA=str(directory / "profile"), MOSS_PROVIDER_DIR=str(directory / "providers"),
                      MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0")
    report = {"archive": str(archive.resolve()), "cases": [], "network_events": [], "passed": False}
    source_uri = (archive / "data" / "session.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as src, closing(sqlite3.connect(data / "session.sqlite3")) as dst:
        src.backup(dst)
        first, doc = dst.execute("SELECT rowid, doc FROM turns ORDER BY rowid LIMIT 1").fetchone()
        original = json.loads(doc)
        assert original["source"] == "holo" and original["model_id"] == "deepseek"
        assert original["reply"] == json.loads((archive / "result.json").read_text(encoding="utf-8"))["cases"][0]["reply"]
        with dst:
            dst.execute("DELETE FROM turns WHERE rowid > ?", (first,))
    phase = ""

    def audit(event, args):
        if phase and event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen"}:
            report["network_events"].append({"phase": phase, "event": event})
            raise RuntimeError("隔离验证阻止了连接或子进程")

    sys.addaudithook(audit)
    phase = "核实审计钩子"
    try:
        socket.getaddrinfo("localhost", 1)
    except RuntimeError:
        pass
    assert len(report["network_events"]) == 1
    phase = "恢复本机测试会话"

    def restore():
        return Moss(data, "http://127.0.0.1:9/v1", "isolated-no-model")

    moss = restore()
    replies = []
    tool_speech = []

    async def message(question, *, expect=None, same_as=None, category=None):
        nonlocal phase
        phase = question
        previous = next(moss._recent_turns(moss.chat_id), {})
        start = perf_counter()
        turn_id = await moss.message(question, chat_id=moss.chat_id)
        if moss.job and not moss.job.done():
            await moss.job
        turn = moss._get(turn_id)
        row = {"question": question, "source": turn.get("source"), "ms": round((perf_counter()-start)*1000, 2)}
        report["cases"].append(row)
        assert turn["status"] == "replied" and (not expect or row["source"] == expect), row
        if expect == "readback":
            assert turn["readback_of"] == previous["id"] and not any(turn.get(k) for k in ("receipt", "call_id", "transport", "media", "holo_tools"))
            assert turn["reply"].encode("utf-8") == same_as.encode("utf-8")
            assert not (moss.job and not moss.job.done())
            row["sha256"] = hashlib.sha256(turn["reply"].encode("utf-8")).hexdigest()
            row["readback_of"] = previous["id"]
        elif expect == "readback_unavailable":
            assert not turn.get("readback_of") and turn["reply"] != original["reply"]
        if category:
            replies.append((turn, category, row))
        return turn

    async def verify_tool_readback():
        forecast_calls = []

        async def forecast(city, period, place, *, part_of_day=None, question=""):
            forecast_calls.append((city, period))
            place = place or {"name":city, "latitude":39.9, "longitude":116.4, "kind":"city"}
            return format_weather(sample_forecast(), place, period, part_of_day, question)

        with (patch("assistant_brain.fetch_weather", forecast),
              patch("local_overview.device_status", wraps=device_status) as battery,
              patch("local_overview.run_desktop", wraps=run_desktop) as volume):
            local = await message("现在几点、电脑还有多少电、音量多大？", expect="context")
            assert [row["kind"] for row in local["local_overview"]] == ["clock", "battery", "volume"]
            assert battery.await_count == volume.await_count == 1
            again = await message("再念一遍。", expect="readback", same_as=local["reply"])
            warm = await message("更温柔一点，再念一遍。", expect="readback", same_as=local["reply"])
            exact = await message("把原话再念一遍。", expect="readback", same_as=local["reply"])
            tool_speech.extend([([local, again], "reply", None), ([local, again, warm], "warm", None),
                                ([local, again, warm, exact], "warm", local["reply"])])
            assert battery.await_count == volume.await_count == 1 and not forecast_calls

            weather = await message("明天北京的天气怎么样？", expect="holo")
            assert weather.get("holo_tools") == ["get_weather"] and weather["weather"]["simple"] is True
            again = await message("再读一遍。", expect="readback", same_as=weather["reply"])
            spoken = await message("刚才那句我没听清，能再说一遍吗？", expect="readback", same_as=weather["reply"])
            tool_speech.append(([weather, again, spoken], "reply", weather["reply"]))
            assert forecast_calls == [("北京", "tomorrow")]

        for field in ("mcp_request", "mcp_receipt", "desktop_confirmation", "file_move_receipt", "media", "receipt", "transport"):
            assert readback_source({**local, field:True}) is None, field
        assert readback_source({**local, "local_overview":None}) is None
        assert readback_source({**local, "status":"thinking"}) is None
        assert readback_source({**local, "reply":"没有来自原始读数的正文。"}) is None
        assert readback_source({**local, "local_overview":[{"kind":"reminders", "reply":local["reply"]}]}) is None
        assert readback_source({**weather, "_weather_memory_versions":{"fixture":1}}) is None
        assert readback_source({**weather, "weather":{**weather["weather"], "simple":False}}) is None
        report["tool_readback_calls"] = {"battery":battery.await_count, "volume":volume.await_count,
                                         "forecast":len(forecast_calls)}

    try:
        assert moss.chat_id == original["chat_id"] and moss.model_id == "deepseek"
        await message("再轻柔一点，念一遍。", expect="readback", same_as=original["reply"], category="verse-warm")
        await moss.close()
        moss = restore()
        await message("再读一遍，读得有气势一点。", expect="readback", same_as=original["reply"], category="verse")
        await message("再读一遍。", expect="readback", same_as=original["reply"], category="verse")
        await message("刚才那阕再吟一遍。", expect="readback", same_as=original["reply"], category="verse")
        await message("轻柔点，能不能再给我读一遍？", expect="readback", same_as=original["reply"], category="verse-warm")
        for phrase in ("再说一遍。", "没听清，能再说一遍吗？", "刚才我没有听清楚，麻烦您重复一下。",
                       "你能把原话再讲一次吗？"):
            await message(phrase, expect="readback", same_as=original["reply"])
        await moss.new_chat()
        await message("再读一遍。", expect="readback_unavailable")
        await moss.switch_chat(original["chat_id"])
        smalltalk = await message("在吗", expect="smalltalk")
        await message("再念一遍。", expect="readback", same_as=smalltalk["reply"], category="warm")
        tools = await message("你现在有哪些工具？", expect="holo")
        assert tools.get("holo_tools") == ["list_capabilities"]
        assert all(feature in tools["reply"] for feature in ("查询或取消提醒", "窗口识别", "拟回复", "朗诵"))
        await message("再读一遍。", expect="readback_unavailable")
        await message("暂停播放。", expect="transport")
        await message("再读一遍。", expect="readback_unavailable")
        await message("再读一遍。", expect="readback_unavailable")
        await verify_tool_readback()

        first = "潮水刚退，岸边还很亮！"
        second = "「气温 −1.1 °C 到 8.0 °C，原文照读。」"
        script = first + "[pause 375ms]\n\n" + second
        for prefix, category in (("请帮我朗读以下文字：", "reply"), ("请把下面这段读给我听：", "reply"),
                                 ("麻烦您帮我朗诵这段正文:", "verse")):
            turn = await message(prefix + script, expect="narration")
            assert turn["reply"].encode("utf-8") == (first + "\n\n" + second).encode("utf-8"), turn
            assert turn["narration"] == {"category":category, "cues":[
                {"text":first}, {"pause_ms":375}, {"text":second}]}, turn
            assert not any(turn.get(key) for key in ("holo_tools", "receipt", "transport", "media")), turn

        for q in ("再读一遍《静夜思》。", "把上一段改短一点再读一遍。", "解释一下再读一遍的用意。",
                  "“请帮我朗读以下文字：潮水刚退。”", "请不要把下面这段读给我听：潮水刚退。",
                  "请先翻译下面这段再读给我听：潮水刚退。", "请把下面这段读给我听",
                  "“再说一遍”是什么意思？", "不用再说一遍。", "把我刚才那句再说一遍。",
                  "重复一遍我刚才说的话。", "再说一遍为什么会下雨？", "再说一遍，然后解释原因。",
                  "说一遍。", "没听清。"):
            phase = q
            turn_id = await moss.message(q, chat_id=moss.chat_id)
            turn = moss._get(turn_id)
            assert turn["status"] == "thinking" and turn["source"] == "holo" and turn["reply"] is None
            report["cases"].append({"question": q, "source": turn["source"], "state_before_cancel": turn["status"]})
            await moss.cancel(turn_id)
            await asyncio.gather(moss.job, return_exceptions=True)
        phase = ""
        plans = """import fs from 'node:fs'; import {conversationSpeech, speechDelivery} from './static/speech-delivery.js';
          const {replies, tools} = JSON.parse(fs.readFileSync(0,'utf8'));
          console.log(JSON.stringify({replies:replies.map(t=>speechDelivery(t).category),
            tools:tools.map(history=>conversationSpeech(history.at(-1),history)),
            toolOrigins:tools.map(history=>speechDelivery(history[0]).text)}));"""
        command = subprocess.run(["node", "--input-type=module", "-e", plans], cwd=ROOT,
                                 input=json.dumps({"replies":[t for t, _, _ in replies],
                                                   "tools":[history for history, _, _ in tool_speech]}, ensure_ascii=False),
                                 text=True, encoding="utf-8", capture_output=True, check=True)
        spoken = json.loads(command.stdout)
        for actual, (_, expected, row) in zip(spoken["replies"], replies, strict=True):
            row["delivery"] = actual
            assert actual == expected
        for actual, origin, (_, category, text) in zip(spoken["tools"], spoken["toolOrigins"], tool_speech, strict=True):
            assert actual == {"category":category, "text":origin if text is None else text}, actual
        report["tool_readback_speech"] = spoken["tools"]
        report["outbound_attempts"] = sum(e["phase"] != "核实审计钩子" for e in report["network_events"])
        assert report["outbound_attempts"] == 0
        report["passed"] = True
    finally:
        phase = ""
        await moss.close()
        (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(directory, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    asyncio.run(run(parser.parse_args().archive))
