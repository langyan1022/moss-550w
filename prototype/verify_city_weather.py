"""Exercise saved-city precedence through HTTP with a local forecast fixture."""

import asyncio
from datetime import datetime
import json
import os
from unittest.mock import patch

import aiohttp
from aiohttp import web

from browser_lab import ROOT
from city_catalog import selected_city
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_weather_browser_location import sample_forecast
from weather_current import describe_current
from weather_forecast import format_weather


CITY_ID = "cn:110102"
AUTO = {"latitude": 31.218763, "longitude": 121.492174}
NAMED = {"name": "上海", "latitude": 31.23, "longitude": 121.47, "kind": "city"}
EXPLICIT = {"latitude": 35.25, "longitude": 120.50}


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("city-weather-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "cases": [], "current_calls": [], "forecast_calls": [], "model_connections": 0}
    data = folder / "data"
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    environment = {"MOSS_MEDIA_DB": str(catalog), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "LOCALAPPDATA": str(folder / "profile"), "MOSS_MEM0_ENABLED": "0",
                   "MOSS_WINDOWS_REMINDERS": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
    selected = selected_city(CITY_ID)
    assert selected and "西城" in selected["name"], selected

    async def reject_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def current(lat, lon):
        report["current_calls"].append([lat, lon])
        fixture = sample_forecast()
        fixture.update(timezone="Asia/Shanghai", utc_offset_seconds=28800)
        return describe_current(fixture, lat, lon)

    async def forecast(city, period, place, *, part_of_day=None, question=""):
        if place is None:
            assert city in {"上海", "上海市"}, city
            place = NAMED
        report["forecast_calls"].append({"kind": place["kind"], "name": place["name"], "period": period,
                                         "point": [place["latitude"], place["longitude"]]})
        return format_weather(sample_forecast(), place, period, part_of_day, question)

    async def model_fixture(**request):
        assert request["text"] == "请把蓝色和绿色描述成两种性格。"
        prompt = json.dumps({key: request[key] for key in ("text", "system", "history")}, ensure_ascii=False)
        assert all(value not in prompt for value in (CITY_ID, selected["name"], str(selected["latitude"])))
        report["unrelated_turn_omits_preference"] = True
        return "蓝色沉静，绿色明快。"

    try:
        async with await asyncio.start_server(reject_model, "127.0.0.1", 0) as guard:
            model = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            environment.update({prefix + "_BASE_URL": model for _, prefixes, _, _ in SPECS.values()
                                for prefix in prefixes})
            with (patch.dict(os.environ, environment), patch("weather_current.fetch_current", current),
                  patch("assistant_brain.fetch_weather", forecast), patch("assistant_brain.run_holo", model_fixture)):
                moss = Moss(data, model, "isolated")
                port = free_port()
                base = f"http://127.0.0.1:{port}"
                runner = web.AppRunner(application(moss, lambda: "standby", data, port, "fixture", asyncio.Event()),
                                       access_log=None, shutdown_timeout=2)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", port).start()
                    async with aiohttp.ClientSession(base_url=base) as client:
                        async def request(method, path, *, body=None, expected=200, same_origin=True):
                            headers = {"Origin": base} if same_origin else {}
                            async with client.request(method, path, json=body, headers=headers) as response:
                                assert response.status == expected, (path, response.status, await response.text())
                                if response.content_type == "application/json":
                                    return await response.json()

                        async def ask(text, city_id=CITY_ID, browser=None):
                            body = {"text": text, "chat_id": moss.chat_id}
                            if city_id is not None:
                                body["preferred_city_id"] = city_id
                            if browser is not None:
                                body["browser_location"] = browser
                            result = await request("POST", "/api/message", body=body)
                            started = result["turns"][-1]["id"]
                            async with asyncio.timeout(5):
                                while result["turns"][-1]["status"] == "thinking":
                                    await asyncio.sleep(.02)
                                    result = await request("GET", "/api/state")
                            turn = result["turns"][-1]
                            assert turn["id"] == started and turn["status"] == "replied", turn
                            assert not ({"preferred_city_id", "browser_location"} & turn.keys()), turn
                            report["cases"].append({"request": text, "reply": turn["reply"],
                                                    "weather": turn.get("weather")})
                            return turn

                        def check_weather(turn, kind, name, period="current"):
                            weather = turn["weather"]
                            assert (weather["location_kind"], weather["city"], weather["period"]) == (kind, name, period), turn

                        cities = await request("GET", "/api/cities")
                        assert selected in cities["domestic"] and selected_city("world:tokyo") in cities["international"]
                        assert len(cities["domestic"]) > 2800 and len(cities["international"]) >= 20
                        assert await request("GET", f"/api/cities?id={CITY_ID}") == {"city": selected}
                        await request("GET", "/api/cities?id=unknown", expected=404)
                        assert not (await request("GET", "/api/state"))["turns"]

                        ambient = await request("POST", "/api/weather/current", body={"city_id": CITY_ID})
                        rounded = {axis: round(selected[axis], 2) for axis in ("latitude", "longitude")}
                        assert ambient["city_id"] == CITY_ID and all(ambient[axis] == number for axis, number in rounded.items())
                        same_cell = await request("POST", "/api/weather/current", body=rounded)
                        assert same_cell == {key: val for key, val in ambient.items() if key != "city_id"}
                        assert report["current_calls"] == [list(rounded.values())]
                        await request("POST", "/api/weather/current", body={"city_id": CITY_ID}, expected=403,
                                      same_origin=False)
                        for invalid in ({"city_id": "unknown"}, {"city_id": []}, {"city_id": CITY_ID, **AUTO}):
                            await request("POST", "/api/weather/current", body=invalid, expected=400)
                        for invalid in ("unknown", [], None):
                            await request("POST", "/api/message", body={"text": "这里的天气", "preferred_city_id": invalid},
                                          expected=409)
                        assert not (await request("GET", "/api/state"))["turns"]

                        check_weather(await ask("天气怎么样？", browser=AUTO), "configured", selected["name"])
                        check_weather(await ask("这里明天会下雨吗？", browser=AUTO), "configured", selected["name"], "tomorrow")
                        check_weather(await ask("帮我查一下本地当前气温。"), "configured", selected["name"])
                        check_weather(await ask("这里现在几度？", city_id="world:tokyo"), "configured", "东京")
                        assert (await ask("我现在在哪里？", browser=AUTO))["reply"] == f"当前设定城市是{selected['name']}。"
                        check_weather(await ask("上海天气"), "city", NAMED["name"])
                        check_weather(await ask("那明天天气呢？"), "city", NAMED["name"], "tomorrow")
                        check_weather(await ask("再看看后天的天气呢？"), "city", NAMED["name"], "day_after_tomorrow")
                        by_point = await ask("纬度 35.25、经度 120.50 的天气怎么样？")
                        point_name = by_point["weather"]["city"]
                        check_weather(by_point, "coordinates", point_name)
                        assert all(by_point["weather"][key] == val for key, val in EXPLICIT.items())
                        check_weather(await ask("说回刚才那组坐标，明天的天气呢？"), "coordinates", point_name, "tomorrow")
                        check_weather(await ask("天气怎么样？"), "configured", selected["name"])
                        check_weather(await ask("这里明天的天气呢？"), "configured", selected["name"], "tomorrow")
                        auto = await ask("天气怎么样？", city_id=None, browser=AUTO)
                        check_weather(auto, "browser", "当前位置附近")
                        assert not (AUTO.keys() & auto["weather"].keys())
                        check_weather(await ask("那里明天的天气怎么样？", browser=AUTO), "browser", "当前位置附近", "tomorrow")
                        check_weather(await ask("这里天气怎么样？", browser=AUTO), "configured", selected["name"])
                        no_fix = await ask("天气怎么样？", city_id=None)
                        assert no_fix.get("weather_clarification") and not no_fix.get("weather"), no_fix
                        check_weather(await ask("我现在的位置"), "configured", selected["name"])

                        await request("POST", "/api/new-chat", body={})
                        other = await ask("请把蓝色和绿色描述成两种性格。", browser=AUTO)
                        assert other["reply"] == "蓝色沉静，绿色明快。" and report["unrelated_turn_omits_preference"]
                    assert not report["model_connections"], report
                    report["passed"] = True
                finally:
                    await runner.cleanup()
                    await moss.close()
    finally:
        (folder / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
