import asyncio
from datetime import datetime
import json
from time import perf_counter

from browser_lab import BrowserServer, ROOT
from weather_location import coordinates, forecast_reference


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("weather-locations-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18794, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "cases": []}

    async def ask(text):
        started = perf_counter()
        code, snapshot = await server.post("/api/message", {"text": text})
        assert code == 200, snapshot
        key = snapshot["turns"][-1]["id"]
        async with asyncio.timeout(35):
            while True:
                turn = (await server.state())["turns"][-1]
                assert turn["id"] == key
                if turn["status"] != "thinking":
                    break
                await asyncio.sleep(.05)
        report["cases"].append({"input": text, "reply": turn["reply"], "weather": turn.get("weather"),
                                "error": turn["error"], "ms": round((perf_counter() - started) * 1000)})
        assert turn["status"] == "replied", turn.get("error")
        return turn

    async def check(text, name, period, lat, lon, tolerance=.4):
        turn = await ask(text)
        data = turn.get("weather")
        assert data and name in data["city"] and name in turn["reply"] and data["period"] == period, turn["reply"]
        assert abs(data["latitude"]-lat) < tolerance and abs(data["longitude"]-lon) < tolerance, data
        assert "°C" in turn["reply"] and data["source"] == "Open-Meteo" and "grid_latitude" in data
        assert turn["holo_tools"] == ["get_weather"]
        return data

    try:
        for text in ("刚才下雨吗", "那里明天要开会吗", "刚才那个区和深圳哪个热", "深圳明天会下雨吗"):
            assert not forecast_reference(text)[1], text
        await server.start()
        assert (await server.state())["model_id"] == "local"
        await check("北京今天天气", "北京", "today", 39.9, 116.4)
        await check("那上海呢", "上海", "today", 31.23, 121.47)
        await check("北京海淀区今天天气", "海淀区", "today", 39.96, 116.30)
        await check("那朝阳区呢", "北京市朝阳区", "today", 39.92, 116.44)
        await check("查一下北京朝阳区今天的天气", "朝阳区", "today", 39.92, 116.44)
        await check("明天呢", "朝阳区", "tomorrow", 39.92, 116.44)
        await check("那长春市朝阳区呢", "朝阳区", "tomorrow", 43.83, 125.28)
        await server.stop()
        await server.start()
        await check("后天呢", "朝阳区", "day_after_tomorrow", 43.83, 125.28)
        for text, region, lat, lon in [
                ("上海浦东新区天气", "浦东新区", 31.23, 121.56),
                ("深圳市南山区天气", "南山区", 22.53, 113.93)]:
            await check(text, region, "current", lat, lon)
        assert (await server.post("/api/new-chat", {}))[0] == 200
        ambiguous = await ask("朝阳区明天气温")
        assert not ambiguous.get("weather") and all(name in ambiguous["reply"] for name in ("北京", "长春"))
        await ask("谢谢")
        ambiguous = await ask("刚才那个区后天的天气呢？")
        assert not ambiguous.get("weather") and all(name in ambiguous["reply"] for name in ("北京", "长春"))
        await check("长春的", "朝阳区", "day_after_tomorrow", 43.83, 125.28)
        await check("经度 121.4737，纬度 31.2304 今天天气", "纬度", "today", 31.2304, 121.4737, .00001)
        await check("明天呢", "纬度", "tomorrow", 31.2304, 121.4737, .00001)
        await ask("谢谢")
        await server.stop()
        await server.start()
        await check("回到刚才那个地点，后天的天气呢？", "纬度", "day_after_tomorrow", 31.2304, 121.4737, .00001)
        await check("明天会下雨吗", "纬度", "tomorrow", 31.2304, 121.4737, .00001)
        pending = await ask("朝阳区明天气温")
        assert len(pending["weather_choices"]["choices"]) >= 2
        pending = await ask("那今天呢？")
        assert pending["weather_choices"]["period"] == "today"
        pending = await ask("刚才那个区后天的天气呢？")
        assert len(pending["weather_choices"]["choices"]) >= 2 and pending["weather_choices"]["period"] == "day_after_tomorrow"
        await check("刚才那组坐标明天天气呢？", "纬度", "tomorrow", 31.2304, 121.4737, .00001)
        await check("深圳市南山区天气", "南山区", "current", 22.53, 113.93)
        await ask("谢谢")
        await check("刚才那个区明天气温多少？", "南山区", "tomorrow", 22.53, 113.93)
        assert (await server.post("/api/new-chat", {}))[0] == 200
        missing = await ask("刚才那个区明天的天气呢？")
        assert not missing.get("weather") and "地点" in missing["reply"] and "°C" not in missing["reply"]
        await check("深圳市南山区天气", "南山区", "current", 22.53, 113.93)
        await ask("并不存在的某某区天气")
        missing = await ask("刚才那个区后天天气呢？")
        assert not missing.get("weather") and "°C" not in missing["reply"]
        await check("南纬 33.87、东经 151.21 的天气", "纬度", "current", -33.87, 151.21, .00001)
        for text, fragment in [("纬度 95，经度 40 的天气", "纬度需要"), ("北纬 31 度的天气", "同时提供"),
                               ("并不存在的某某区天气", "没有查到")]:
            turn = await ask(text)
            assert fragment in turn["reply"] and not turn.get("weather") and "°C" not in turn["reply"]
        for sample in ("坐标 39.92,116.44", "经纬度 116.44,39.92", "39.92°N,116.44°E天气"):
            place = coordinates(sample)
            assert (place["latitude"], place["longitude"]) == (39.92, 116.44)
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
