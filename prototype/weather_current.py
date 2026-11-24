import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic

import aiohttp

from weather_forecast import WEATHER_CODES, _number


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = {"temperature":"temperature_2m", "apparent_temperature":"apparent_temperature",
                  "humidity":"relative_humidity_2m", "wind_speed":"wind_speed_10m"}
CACHE_SECONDS = 600
CACHE_CELLS = 32


def describe_current(forecast, latitude, longitude):
    current = forecast.get("current") if isinstance(forecast, dict) else None
    if not isinstance(current, dict):
        raise ValueError("天气服务没有返回当前读数。")
    stamp, zone = current.get("time"), forecast.get("timezone")
    if not isinstance(stamp, str) or "T" not in stamp or not isinstance(zone, str) or not zone.strip():
        raise ValueError("天气服务没有返回有效时刻。")
    sampled = datetime.fromisoformat(stamp)
    if sampled.tzinfo is None:
        offset = forecast.get("utc_offset_seconds")
        if type(offset) is not int or abs(offset) >= 86400:
            raise ValueError("天气服务没有返回有效的时区偏移。")
        sampled = sampled.replace(tzinfo=timezone(timedelta(seconds=offset)))

    values = {key:value for key, field in CURRENT_FIELDS.items() if (value := _number(current.get(field))) is not None}
    code = _number(current.get("weather_code"))
    condition = WEATHER_CODES.get(code)
    if condition:
        values["code"] = int(code)
    if not values:
        raise ValueError("天气服务没有返回当前读数。")
    return {**values, "condition":condition, "sampled_at":sampled.isoformat(timespec="seconds"),
            "time_zone":zone, "latitude":latitude, "longitude":longitude}


async def fetch_current(latitude, longitude):
    params = {"latitude":f"{latitude:.2f}", "longitude":f"{longitude:.2f}", "timezone":"auto",
              "temperature_unit":"celsius", "wind_speed_unit":"kmh",
              "current":",".join([*CURRENT_FIELDS.values(), "weather_code"])}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=True) as client:
        async with client.get(FORECAST_URL, params=params, allow_redirects=False) as response:
            response.raise_for_status()
            return describe_current(await response.json(), latitude, longitude)


@dataclass
class PendingReading:
    task: asyncio.Task
    readers: int = 0


class CurrentWeatherCache:
    def __init__(self):
        self.samples = {}
        self.pending = {}

    async def _fetch(self, cell):
        reading = await fetch_current(*cell)
        pending = self.pending.get(cell)
        if pending is None or pending.task is not asyncio.current_task():
            return reading
        if len(self.samples) >= CACHE_CELLS:
            self.samples.pop(next(iter(self.samples)))
        self.samples[cell] = (monotonic() + CACHE_SECONDS, reading)
        return reading

    async def read(self, location):
        cell = tuple(round(location[axis], 2) or 0.0 for axis in ("latitude", "longitude"))
        now = monotonic()
        self.samples = {key:entry for key, entry in self.samples.items() if entry[0] > now}
        if cell in self.samples:
            return self.samples[cell][1]
        pending = self.pending.get(cell)
        if pending is None:
            pending = self.pending[cell] = PendingReading(asyncio.create_task(self._fetch(cell)))
        pending.readers += 1
        try:
            return await asyncio.shield(pending.task)
        finally:
            pending.readers -= 1
            if not pending.readers:
                del self.pending[cell]
                if not pending.task.done():
                    pending.task.cancel()
                    await asyncio.gather(pending.task, return_exceptions=True)
