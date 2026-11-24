import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from time import perf_counter
from unittest.mock import patch

import aiohttp
from aiohttp import web

from assistant_brain import fetch_weather
from browser_lab import ROOT
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from weather_forecast import format_weather
from weather_location import browser_position, point


BROWSER = {"latitude": 39.931572, "longitude": 116.437628}
MOVED_BROWSER = {"latitude": 34.766293, "longitude": 113.628463}
COORDINATE_KEYS = {"latitude", "longitude", "grid_latitude", "grid_longitude"}


def sample_forecast():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    days = [(today + timedelta(days=offset)).isoformat() for offset in range(3)]
    return {"latitude": 39.9, "longitude": 116.4,
            "current": {"time": f"{days[0]}T17:00", "weather_code": 1,
                        "temperature_2m": 13.3, "precipitation": 0, "wind_speed_10m": 9.2},
            "daily": {"time": days, "weather_code": [1, 61, 3],
                      "temperature_2m_min": [7.1, 7.5, 6.3], "temperature_2m_max": [15.8, 12.8, 11.8],
                      "precipitation_probability_max": [0, 75, 5], "rain_sum": [0, 2.3, 0], "showers_sum": [0, 0, 0]},
            "hourly": {"time": [f"{day}T{hour:02d}:00" for day in days for hour in range(24)],
                       "temperature_2m": [8 + hour / 4 for hour in range(24)] * 3, "weather_code": [1] * 72,
                       "precipitation_probability": [0] * 72, "rain": [0] * 72, "wind_speed_10m": [7.0] * 72}}


async def verify_provider_coordinates():
    calls = []

    class ForecastResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return sample_forecast()

        def get(self, url, *, params):
            assert url == "https://api.open-meteo.com/v1/forecast"
            calls.append((params["latitude"], params["longitude"]))
            return self

    with patch("assistant_brain.aiohttp.ClientSession", return_value=ForecastResponse()):
        await fetch_weather("", "today", browser_position(BROWSER))
        await fetch_weather("", "today", point(*BROWSER.values()))
    assert calls == [("39.93", "116.44"), tuple(map(str, BROWSER.values()))], calls


async def run(live):
    folder = ROOT / "runs" / datetime.now().strftime("weather-browser-location-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "provider": "Open-Meteo" if live else "fixed forecast", "model_connections": 0,
              "cases": [], "requests": []}
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0")

    async def reject_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    failure = None
    checked_position = None

    async def fixture(city, period, place, *, part_of_day=None, question=""):
        nonlocal failure
        assert place, city
        report["requests"].append({"kind": place["kind"], "period": period, "part": part_of_day})
        if checked_position is not None:
            assert all(place[axis] == value for axis, value in checked_position.items()), place
        if failure is not None:
            error, failure = failure, None
            raise error
        return format_weather(sample_forecast(), place, period, part_of_day, question)

    async def check_no_leak(**request):
        data = json.dumps({key: request[key] for key in ("system", "text", "history")}, ensure_ascii=False)
        assert all(str(value) not in data for value in BROWSER.values()), data
        report["checked_model_context"] = True
        report["model_fixture_calls"] = report.get("model_fixture_calls", 0) + 1
        assert await request["dispatch"]("get_weather", {"city": "北京", "period": "today"}) == "本轮没有要求查询天气。"
        return "这是隔离对话回复。"

    try:
        if not live:
            await verify_provider_coordinates()
            report["device_precision"] = "round browser-only forecast coordinates before provider request"
        async with await asyncio.start_server(reject_model, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            moss = Moss(folder / "data", model_url, "isolated-unused")
            port = free_port()
            app = application(moss, lambda: "standby", folder / "data", port, "isolation", asyncio.Event())
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
                    async def post(path, body, expected=200):
                        async with client.post(path, json=body) as answer:
                            payload = await answer.json()
                            assert answer.status == expected, payload
                            return payload

                    async def ask(text, position=None, *, city=None, period=None, part=None):
                        body = {"text": text, "chat_id": moss.chat_id}
                        if position is not None:
                            body["browser_location"] = position
                        started = perf_counter()
                        result = await post("/api/message", body)
                        key = result["turns"][-1]["id"]
                        async with asyncio.timeout(24):
                            while result["turns"][-1]["status"] == "thinking":
                                await asyncio.sleep(.025)
                                async with client.get("/api/state") as response:
                                    result = await response.json()
                        turn = result["turns"][-1]
                        case = {"user": text, "reply": turn["reply"], "status": turn["status"],
                                "weather": turn.get("weather"), "ms": round((perf_counter() - started) * 1000, 1)}
                        report["cases"].append(case)
                        assert turn["id"] == key and turn["status"] == "replied" and not turn["cloud_requests"]["total"], case
                        assert "browser_location" not in turn and "_weather_retry" not in turn
                        if city:
                            weather = turn["weather"]
                            assert weather["city"] == city and weather["period"] == period and weather.get("date"), case
                            assert turn["holo_tools"] == ["get_weather"] and weather.get("condition"), case
                            if city == "当前位置附近":
                                assert weather["location_kind"] == "browser" and not (COORDINATE_KEYS & weather.keys()), case
                            if part:
                                assert weather["part_of_day"] == part and weather.get("hourly"), case
                        return turn

                    with ExitStack() as patches:
                        if not live:
                            patches.enter_context(patch("assistant_brain.fetch_weather", fixture))
                        patches.enter_context(patch("assistant_brain.run_holo", check_no_leak))
                        for bad in (None, {"latitude": True, "longitude": 1}, {"latitude": 91, "longitude": 2},
                                    {"latitude": 1, "longitude": 2, "accuracy": 10}):
                            await post("/api/message", {"text": "我当前位置的天气", "browser_location": bad}, 409)
                        assert not moss.state()["turns"]
                        missing = await ask("我当前位置的天气")
                        assert "位置" in missing["reply"] and not missing.get("weather")
                        await ask("今天天气怎么样", BROWSER, city="当前位置附近", period="today")
                        named = await ask("北京朝阳明天会下雨吗，带伞吗", BROWSER, city="北京市朝阳区", period="tomorrow")
                        assert named["weather"]["location_kind"] == "district" and "伞" in named["reply"]
                        after = await ask("后天呢？", BROWSER, city="北京市朝阳区", period="day_after_tomorrow")
                        assert "伞" in after["reply"], after
                        await ask("明天下午会下雨吗", BROWSER, city="北京市朝阳区", period="tomorrow", part="下午")
                        await ask("我当前位置的天气", BROWSER, city="当前位置附近", period="current")
                        await ask("那明天呢", BROWSER, city="当前位置附近", period="tomorrow")
                        unavailable = await ask("后天呢")
                        assert "位置" in unavailable["reply"] and not unavailable.get("weather")
                        unrelated = await ask("讲一个轻松的故事", BROWSER)
                        assert not unrelated.get("weather") and report["checked_model_context"]
                        if not live:
                            await post("/api/new-chat", {})
                            uncertain = await ask("朝阳区明天气温")
                            assert not uncertain.get("weather") and uncertain.get("weather_choices")
                            await ask("谢谢")
                            offered = await ask("刚才那个区后天的天气呢？")
                            assert offered["weather_choices"]["period"] == "day_after_tomorrow"
                            assert "北京" in offered["reply"] and "长春" in offered["reply"]
                            await ask("长春的", city="长春市朝阳区", period="day_after_tomorrow")
                            await post("/api/new-chat", {})
                            fallback_before = report["model_fixture_calls"]
                            need_place = await ask("今天天气怎么样")
                            assert "地点" in need_place["reply"] and need_place["weather_clarification"]["period"] == "today"
                            where = await ask("我当前的定位点是在哪里，你不知道么")
                            assert "定位" in where["reply"] and "还没收到" in where["reply"] and not where.get("weather")
                            assert where["weather_clarification"] == need_place["weather_clarification"]
                            await ask("河南郑州市中原区", city="郑州市中原区", period="today")
                            located = await ask("我当前的定位点是在哪里，你不知道么", BROWSER)
                            assert located["browser_position_available"] and not located.get("weather") and not located.get("holo_tools")
                            assert "39.9" in located["reply"] and "116.4" in located["reply"]
                            assert fallback_before == report["model_fixture_calls"]

                            await post("/api/new-chat", {})
                            pending = await ask("明天下午出门要不要带伞？")
                            assert pending["weather_clarification"]["part_of_day"] == "下午"
                            date_revision = await ask("后天呢？")
                            assert date_revision["weather_clarification"]["period"] == "day_after_tomorrow"
                            inherited = await ask("河南郑州市中原区", city="郑州市中原区", period="day_after_tomorrow", part="下午")
                            assert "伞" in inherited["reply"] and fallback_before == report["model_fixture_calls"]

                            await post("/api/new-chat", {})
                            await ask("明天的天气怎么样？")
                            await ask("当前位置", BROWSER, city="当前位置附近", period="tomorrow")
                            await post("/api/new-chat", {})
                            await ask("今天天气怎么样")
                            await ask("39.92,116.44", city="纬度 39.92°、经度 116.44°", period="today")

                            await post("/api/new-chat", {})
                            await ask("今天天气怎么样")
                            await ask("现在几点")
                            calls = len(report["requests"])
                            assert not (await ask("河南郑州市中原区")).get("weather") and calls == len(report["requests"])

                            await post("/api/new-chat", {})
                            with patch("assistant_brain.WEATHER_CLARIFICATION_LIFETIME", timedelta(seconds=0)):
                                await ask("今天天气怎么样")
                            assert not (await ask("河南郑州市中原区")).get("weather") and calls == len(report["requests"])

                            await post("/api/new-chat", {})
                            await ask("今天天气怎么样")
                            await post("/api/new-chat", {})
                            assert not (await ask("河南郑州市中原区")).get("weather") and calls == len(report["requests"])

                            await post("/api/new-chat", {})
                            failure = TimeoutError()
                            interrupted = await ask("当前位置明天下午的天气", BROWSER)
                            assert interrupted.get("weather_retry_browser_until") and not interrupted.get("weather")
                            calls = len(report["requests"])
                            denied = await ask("需要")
                            assert "定位" in denied["reply"] and len(report["requests"]) == calls
                            checked_position = MOVED_BROWSER
                            found = await ask("那就重试一遍", MOVED_BROWSER, city="当前位置附近", period="tomorrow", part="下午")
                            checked_position = None
                            assert not found.get("weather_retry_browser_until") and len(report["requests"]) == calls + 1

                            await post("/api/new-chat", {})
                            failure = TimeoutError()
                            await ask("北京市朝阳区明天天气")
                            await ask("好的", city="北京市朝阳区", period="tomorrow")

                            for boundary in ("timeout", "new-chat", "other-topic", "cancel-decline", "permanent"):
                                await post("/api/new-chat", {})
                                failure = (aiohttp.ClientResponseError(None, (), status=400) if boundary == "permanent" else TimeoutError())
                                if boundary == "timeout":
                                    with patch("assistant_brain.WEATHER_CLARIFICATION_LIFETIME", timedelta(seconds=0)):
                                        await ask("北京市朝阳区明天天气")
                                else:
                                    previous = await ask("北京市朝阳区明天天气")
                                    if boundary == "permanent":
                                        assert "再查一次" not in previous["reply"]
                                if boundary == "new-chat":
                                    await post("/api/new-chat", {})
                                elif boundary == "other-topic":
                                    await ask("现在几点")
                                elif boundary == "cancel-decline":
                                    fallback_before = report["model_fixture_calls"]
                                    stopped = await ask("不需要")
                                    assert stopped["reply"] == "好，先不查了。" and fallback_before == report["model_fixture_calls"]
                                calls = len(report["requests"])
                                assert not (await ask("需要")).get("weather") and len(report["requests"]) == calls, boundary
                        persisted = "\n".join(value for value, in moss.db.execute("SELECT doc FROM turns"))
                        assert "browser_location" not in persisted and all(str(value) not in persisted for value in
                               (*BROWSER.values(), *MOVED_BROWSER.values()))
                        assert not report["model_connections"], report
                        report["passed"] = True
            finally:
                await runner.cleanup()
                await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="使用当前 Open-Meteo 回包")
    asyncio.run(run(parser.parse_args().live))
