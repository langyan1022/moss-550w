import asyncio
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from time import perf_counter

from browser_lab import BrowserServer, ROOT


async def main(legacy=None):
    folder = ROOT / "runs" / datetime.now().strftime("weather-followup-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18734, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "cases": []}

    async def ask(text):
        start = perf_counter()
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200, state.get("error")
        key = state["turns"][-1]["id"]
        async with asyncio.timeout(100):
            while True:
                turn = (await server.state())["turns"][-1]
                assert turn["id"] == key
                if turn["status"] != "thinking":
                    break
                await asyncio.sleep(.08)
        report["cases"].append({"input": text, "status": turn["status"], "reply": turn["reply"], "error": turn.get("error"),
                                "weather": turn.get("weather"), "tools": turn.get("holo_tools", []),
                                "ms": round((perf_counter()-start)*1000)})
        return turn

    try:
        await server.start()
        assert (await server.post("/api/select-model", {"id": "deepseek"}))[0] == 200
        for prompt in ["北京今天的天气", "那明天呢", "那上海呢"]:
            await ask(prompt)
        await server.stop()
        await server.start()
        await ask("后天呢？")
        await ask("谢谢")
        assert (await server.post("/api/new-chat", {}))[0] == 200
        await ask("那明天呢？")
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for row, (city, period, offset) in zip(report["cases"], [
                ("北京", "today", 0), ("北京", "tomorrow", 1), ("上海", "tomorrow", 1),
                ("上海", "day_after_tomorrow", 2)]):
            stamp = (today + timedelta(days=offset))
            assert row["status"] == "replied" and row["tools"] == ["get_weather"], row
            data = row["weather"]
            assert data and city in data["city"] and data["period"] == period, row
            assert city in row["reply"] and any(date in row["reply"] for date in (
                stamp.isoformat(), f"{stamp.month}月{stamp.day}日")), row
        assert all("get_weather" not in row["tools"] for row in report["cases"][4:])
        english = await ask("用英文回答上海今天的天气")
        assert english.get("weather") and not english["weather"]["simple"], english
        assert re.search(r"\b(?:Shanghai|temperature|cloudy|sunny)\b", english["reply"], re.I), english["reply"]
        await ask("谢谢")
        advice = await ask("说回刚才那个城市，明天出门要带伞吗？")
        place = advice.get("weather") or {}
        assert (advice["holo_tools"] == ["get_weather"] and place.get("period") == "tomorrow"
                and place.get("latitude") == english["weather"]["latitude"]
                and place.get("longitude") == english["weather"]["longitude"] and "伞" in advice["reply"]), advice
        if legacy:
            with closing(sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                docs = (json.loads(row[0]) for row in source.execute("SELECT doc FROM turns ORDER BY rowid"))
                original = next(row for row in docs if row["user"] == "北京今天的天气" and
                                "get_weather" in row.get("holo_tools", []) and "weather" not in row)
            code, state = await server.post("/api/new-chat", {})
            assert code == 200
            await server.stop()
            original["chat_id"] = state["chat_id"]
            with closing(sqlite3.connect(server.data / "session.sqlite3")) as target, target:
                target.execute("INSERT INTO turns(id,doc,chat_id) VALUES(?,?,?)",
                               (original["id"], json.dumps(original, ensure_ascii=False), state["chat_id"]))
            await server.start()
            recovered = await ask("那明天呢？")
            assert recovered["weather"]["period"] == "tomorrow" and "北京" in recovered["weather"]["city"]
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", type=Path)
    asyncio.run(main(parser.parse_args().legacy))
