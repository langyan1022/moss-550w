from math import isfinite
from time import monotonic

import aiohttp
from aiohttp import web

from weather_current import CACHE_CELLS, CACHE_SECONDS, CURRENT_FIELDS, FORECAST_URL, describe_current


DAILY_FIELDS = {"code": "weather_code", "temperature_min": "temperature_2m_min",
                "temperature_max": "temperature_2m_max", "precipitation_probability": "precipitation_probability_max",
                "wind_speed": "wind_speed_10m_max"}
HOURLY_FIELDS = {"code": "weather_code", "temperature": "temperature_2m",
                 "precipitation_probability": "precipitation_probability", "wind_speed": "wind_speed_10m"}


def forecast_rows(series, time_key, fields):
    def value(field, index):
        items = series.get(field, [])
        return items[index] if index < len(items) else None

    return [{time_key: stamp, **{key: value(field, index) for key, field in fields.items()}}
            for index, stamp in enumerate(series.get("time", []))]


async def fetch_console_forecast(latitude, longitude):
    params = {"latitude": str(latitude), "longitude": str(longitude), "timezone": "auto", "forecast_days": "3",
              "current": ",".join([*CURRENT_FIELDS.values(), "weather_code", "precipitation"]),
              "daily": ",".join(DAILY_FIELDS.values()), "hourly": ",".join(HOURLY_FIELDS.values())}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=True) as client:
        async with client.get(FORECAST_URL, params=params, allow_redirects=False) as response:
            response.raise_for_status()
            raw = await response.json()

    reading = describe_current(raw, latitude, longitude)
    current = raw["current"]
    days = forecast_rows(raw.get("daily", {}), "date", DAILY_FIELDS)
    hourly = [row for row in forecast_rows(raw.get("hourly", {}), "time", HOURLY_FIELDS)
              if row["time"] >= current["time"]][:12]
    probability = (hourly or days or [{}])[0].get("precipitation_probability")
    return {"source": "Open-Meteo", "latitude": latitude, "longitude": longitude,
            "timezone": reading["time_zone"], "sampled_at": reading["sampled_at"],
            "current": {**{key: reading.get(key) for key in (*CURRENT_FIELDS, "code", "condition")},
                        "time": current["time"], "precipitation": current.get("precipitation"),
                        "precipitation_probability": probability}, "days": days, "hourly": hourly}


def install_fui(app, public):
    cache = {}

    async def weather(request):
        try:
            lat, lon = (float(request.query[axis]) for axis in ("lat", "lon"))
            if not isfinite(lat) or not isfinite(lon) or abs(lat) > 90 or abs(lon) > 180:
                raise ValueError
        except (KeyError, ValueError):
            return web.json_response({"error": "无效的经纬度。"}, status=400)
        cell = (round(lat, 2), round(lon, 2))
        expires, reading = cache.get(cell, (0, None))
        if expires <= monotonic():
            try:
                reading = await fetch_console_forecast(*cell)
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError, OverflowError):
                return web.json_response({"error": "当前位置天气暂时无法读取。"}, status=502)
            if len(cache) >= CACHE_CELLS:
                cache.pop(next(iter(cache)))
            cache[cell] = (monotonic() + CACHE_SECONDS, reading)
        return web.json_response(reading, headers={"Cache-Control": "no-store"})

    async def portrait_model(request):
        return web.FileResponse(public / "vendor/models/selfie_segmenter.tflite",
                                headers={"Cache-Control": "public, max-age=86400"})

    app.add_routes([web.get("/fui/local-weather", weather), web.get("/fui/model/selfie_segmenter.tflite", portrait_model),
                    *(web.static(f"/{name}", public / name) for name in ("js", "styles", "vendor"))])
