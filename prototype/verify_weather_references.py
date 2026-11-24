import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from time import perf_counter

import aiohttp

from browser_lab import ROOT
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated


COORDINATES = (31.2304, 121.4737)
DISTRICT = (22.53, 113.93)
FORECAST = "https://api.open-meteo.com/v1/forecast"


async def official_forecast(latitude, longitude):
    params = {"latitude":latitude, "longitude":longitude, "timezone":"auto", "forecast_days":"3",
              "current":"temperature_2m,precipitation,wind_speed_10m,weather_code",
              "daily":"temperature_2m_min,temperature_2m_max,precipitation_probability_max,weather_code"}
    try:
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.get(FORECAST, params=params) as response:
                result = {"url":str(response.request_info.url), "status":response.status,
                          "reason":response.reason, "type":response.content_type}
                if response.status == 200:
                    payload = await response.json()
                    result.update(grid_latitude=payload["latitude"], grid_longitude=payload["longitude"],
                                  daily=payload["daily"], timezone=payload["timezone"])
                return result
    except (aiohttp.ClientError, TimeoutError) as exc:
        return {"url":FORECAST, "error":type(exc).__name__, "message":str(exc)}


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("weather-references-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    outcome = folder / "result.json"
    report = {"passed":False, "provider_url":FORECAST, "model_connections":0,
              "application_pids":[], "cases":[]}
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0")
    for _, prefixes, _, _ in SPECS.values():
        for prefix in prefixes:
            os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"
    server = isolated(folder / "app")

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(text):
        before = perf_counter()
        code, state = await server.post("/api/message", {"text":text})
        assert code == 200, state
        key = state["turns"][-1]["id"]
        async with asyncio.timeout(40):
            while state["turns"][-1]["status"] == "thinking":
                await asyncio.sleep(.06)
                state = await server.state()
        turn = state["turns"][-1]
        case = {"input":text, "reply":turn["reply"], "status":turn["status"], "source":turn.get("source"),
                "error":turn.get("error"), "chat_id":turn["chat_id"], "weather":turn.get("weather"),
                "weather_choices":turn.get("weather_choices"), "tools":turn.get("holo_tools", []),
                "cloud_requests":turn["cloud_requests"], "ms":round((perf_counter() - before) * 1000)}
        report["cases"].append(case)
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        assert turn["id"] == key and turn["status"] == "replied", case
        assert turn["cloud_requests"] == {"complete":True, "total":0, "by_provider":{}} and not report["model_connections"], case
        return case

    async def check(text, kind, period):
        lat, lon = COORDINATES if kind == "coordinates" else DISTRICT
        case = await ask(text)
        if not case["weather"] and "无法连接" in case["reply"]:
            report["provider_diagnostic"] = await official_forecast(lat, lon)
            raise RuntimeError(str(report["provider_diagnostic"]))
        weather = case["weather"]
        assert weather and weather["location_kind"] == kind and weather["period"] == period, case
        tolerance = .00001 if kind == "coordinates" else .06
        assert abs(weather["latitude"] - lat) < tolerance and abs(weather["longitude"] - lon) < tolerance, case
        assert case["tools"] == ["get_weather"] and "°C" in case["reply"] and weather["source"] == "Open-Meteo", case
        assert weather["city"] in case["reply"] and (kind != "district" or "南山区" in weather["city"])
        assert all(isinstance(weather[key], (float, int)) for key in ("grid_latitude", "grid_longitude"))
        day = datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days={
            "today":0, "tomorrow":1, "day_after_tomorrow":2}[period])
        assert day.isoformat() in case["reply"], case
        return weather

    async def unavailable(text, *words):
        case = await ask(text)
        assert not case["weather"] and "°C" not in case["reply"], case
        assert all(word in case["reply"] for word in words), case
        return case

    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            server.model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            await server.start()
            report["application_pids"].append(server.pid)
            state = await server.state()
            chat = state["chat_id"]
            assert state["model_id"] == "local" and state["memory_backend"] == "sqlite"
            first_forecast = await check("经度 121.4737，纬度 31.2304 今天天气", "coordinates", "today")
            official = await official_forecast(*COORDINATES)
            report["independent_official_request"] = official
            assert official.get("status") == 200 and official["url"].startswith(FORECAST + "?"), official
            assert (official["grid_latitude"], official["grid_longitude"]) == (
                first_forecast["grid_latitude"], first_forecast["grid_longitude"])
            day = official["daily"]
            reply = report["cases"][-1]["reply"]
            assert all(str(day[key][0]) in reply for key in (
                "time", "temperature_2m_min", "temperature_2m_max", "precipitation_probability_max")), reply
            assert (await ask("查看我的便签"))["source"] == "notes"
            await check("说回刚才那组坐标，明天的天气呢？", "coordinates", "tomorrow")
            await check("深圳市南山区后天的天气", "district", "day_after_tomorrow")
            assert (await ask("查看我的便签"))["source"] == "notes"
            await server.stop()
            await server.start()
            report["application_pids"].append(server.pid)
            assert len(set(report["application_pids"])) == 2 and (await server.state())["chat_id"] == chat
            await check("说回刚才那组坐标", "coordinates", "tomorrow")
            await check("回到刚才那个区，今天的天气呢？", "district", "today")
            code, empty = await server.post("/api/new-chat", {})
            assert code == 200 and empty["chat_id"] != chat
            await unavailable("说回刚才那组坐标")
            await unavailable("刚才那个区明天的天气呢？")
            assert (await server.post("/api/switch-chat", {"id":chat}))[0] == 200
            await unavailable("纬度 95，经度 40 今天天气", "纬度")
            assert (await ask("查看我的便签"))["source"] == "notes"
            await unavailable("说回刚才那组坐标，明天的天气呢？")
            first = await unavailable("朝阳区明天的天气", "北京", "长春")
            assert len(first["weather_choices"]["choices"]) >= 2
            assert (await ask("查看我的便签"))["source"] == "notes"
            await unavailable("刚才那个区后天的天气呢？", "北京", "长春")
        report["passed"] = True
    finally:
        await server.stop()
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
