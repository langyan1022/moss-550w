import argparse
import asyncio
from datetime import datetime
import json
import os
from time import perf_counter

from browser_lab import ROOT
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated
from verify_weather_references import official_forecast
from weather_location import coordinates


DMS = "北纬31度14分15秒，东经121度28分30秒，今天天气"
EXPECTED = (31.2375, 121.475)


async def verify(baseline):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = ROOT / "runs" / ("spoken-coordinates-" + ("before-" if baseline else "") + stamp)
    folder.mkdir()
    print(folder, flush=True)
    result = {"passed":False, "baseline":baseline, "cases":[], "model_connections":0}
    outcome = folder / "result.json"
    database = folder / "catalog.sqlite3"
    MediaLibrary(database)
    os.environ.update(LOCALAPPDATA=str(folder / "local-profile"), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      MOSS_MEDIA_DB=str(database), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0")
    for _, prefixes, _, _ in SPECS.values():
        for prefix in prefixes:
            os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"
    server = isolated(folder / "app")

    async def model_guard(reader, writer):
        result["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(text):
        start = perf_counter()
        code, state = await server.post("/api/message", {"text":text})
        assert code == 200, state
        turn = state["turns"][-1]
        identifier = turn["id"]
        async with asyncio.timeout(30):
            while turn["status"] == "thinking":
                await asyncio.sleep(.05)
                turn = (await server.state())["turns"][-1]
        row = {"input":text, "reply":turn.get("reply"), "error":turn.get("error"), "status":turn["status"],
               "source":turn.get("source"),
               "weather":turn.get("weather"), "tools":turn.get("holo_tools", []),
               "cloud_requests":turn["cloud_requests"], "ms":round((perf_counter() - start) * 1000)}
        result["cases"].append(row)
        outcome.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        assert turn["id"] == identifier and turn["status"] == "replied", row
        assert turn["cloud_requests"] == {"complete":True, "total":0, "by_provider":{}} and result["model_connections"] == 0
        return row

    async def confirm_weather(row, expected, period):
        weather = row["weather"]
        if not weather and "无法连接" in row["reply"]:
            result["provider_diagnostic"] = await official_forecast(*expected)
            raise RuntimeError(str(result["provider_diagnostic"]))
        assert weather and weather["location_kind"] == "coordinates" and weather["source"] == "Open-Meteo", row
        assert row["tools"] == ["get_weather"] and weather["period"] == period and "°C" in row["reply"], row
        for value, key in zip(expected, ("latitude", "longitude")):
            assert abs(weather[key] - value) < 1e-6, row
        assert all(isinstance(weather[key], (float, int)) for key in ("grid_latitude", "grid_longitude")), weather

    async def compare_official(row, point, day):
        response = await official_forecast(*point)
        result.setdefault("independent_requests", []).append(response)
        assert response.get("status") == 200 and response["url"].startswith("https://api.open-meteo.com/v1/forecast?"), response
        weather = row["weather"]
        assert (response["grid_latitude"], response["grid_longitude"]) == (weather["grid_latitude"], weather["grid_longitude"]), row
        daily = response["daily"]
        assert all(str(daily[key][day]) in row["reply"] for key in (
            "time", "temperature_2m_min", "temperature_2m_max", "precipitation_probability_max")), row
        return daily

    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            server.model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            await server.start()
            first = await ask(DMS)
            if baseline:
                await confirm_weather(first, (31, 121), "today")
                result["confirmed_truncation"] = {"received":[first["weather"][k] for k in ("latitude", "longitude")],
                                                  "expected":EXPECTED}
            else:
                await confirm_weather(first, EXPECTED, "today")
                official_days = await compare_official(first, EXPECTED, 0)
                spoken = await ask("北纬三十一度十四分十五秒，东经一百二十一度二十八分三十秒，今天天气")
                await confirm_weather(spoken, EXPECTED, "today")
                tomorrow = await ask("明天呢？")
                await confirm_weather(tomorrow, EXPECTED, "tomorrow")
                assert official_days["time"][1] in tomorrow["reply"]
                other = (-(33 + 52 / 60), 151 + 12 / 60)
                south = await ask("南纬33度52分，东经151度12分，今天天气")
                await confirm_weather(south, other, "today")
                await compare_official(south, other, 0)
                decimal = await ask("经度 121.4737，纬度 31.2304 今天天气")
                await confirm_weather(decimal, (31.2304, 121.4737), "today")
                for text, clue in (("北纬31度80分，东经121度28分，今天天气", "分"),
                                   ("北纬31度14分60秒，东经121度28分30秒，今天天气", "秒"),
                                   ("南纬-31度14分15秒，东经121度28分30秒，今天天气", "正数"),
                                   ("北纬31度14分15秒，今天天气", "经度")):
                    invalid = await ask(text)
                    assert not invalid["weather"] and not invalid["tools"] and "°C" not in invalid["reply"], invalid
                    assert clue in invalid["reply"], invalid
                for text in ("十一月二十一日下午三点二十八分三十秒", "明天09:15提醒我核对天气资料"):
                    assert coordinates(text) is None, text
                reminder = await ask("明天09:15提醒我核对天气资料")
                assert reminder["source"] == "timer" and not reminder["weather"] and not reminder["tools"]
            result["passed"] = True
    finally:
        await server.stop()
        outcome.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, result["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", action="store_true")
    asyncio.run(verify(parser.parse_args().before))
