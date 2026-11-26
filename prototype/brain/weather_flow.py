"""Unified weather forecasting, multi-turn disambiguation, and retry state helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import sys
import unicodedata

import aiohttp

from brain.prompts import DAY_FOLLOWUP, SHORT_FOLLOWUP
from brain.registry import ab as _ab
from weather_forecast import DAILY_FIELDS, HOURLY_FIELDS, WEATHER_DAYS, forecast_focus, format_weather
from weather_places import known_city
from weather_location import (
    BROWSER_REFERENCE,
    DEVICE_LOCATION,
    FACT_END,
    FORECAST_RANGE_REPLY,
    NO_LOOKUP,
    OTHER_REQUEST,
    OUTSIDE_FORECAST_DAY,
    PART_OF_DAY,
    PREFIX,
    city_candidates,
    coordinates,
    districts,
    forecast_details,
    forecast_reference,
    mentioned_district,
    period_from,
    place_text,
    referenced_kind,
    weather_coordinate_answer,
    weather_place_answer,
)


WEATHER_TOPIC = re.compile(
    r"天气|气温|温度|下雨|降水|降雨|带伞|会冷|会热|冷不冷|冷吗|热不热|热吗|暖不暖|凉不凉|晴天?|阴天?|"
    r"刮风|刮不刮风|预报|穿衣|穿什么|怎么穿|几度|多少度|下雪"
)
FORECAST_ONLY = re.compile(
    r"(?:(?:请你?|帮我|麻烦你?|查(?:一下|一查)?|看看|告诉我)\s*)*"
    r"(?:今天|明天|后天|现在|未来三天|这三天)?的?天气(?:预报)?(?:怎么样|如何|呢|怎样)?[？?。！!]*"
)
WEATHER_PLACE_REQUIRED = "要查哪个地点的天气？可以直接说城市、区县或经纬度。"
BROWSER_POSITION_REQUIRED = "我还没拿到当前的位置。可以点「天气定位」重试，或者告诉我城市或区县。"
WEATHER_TEMPORARILY_UNAVAILABLE = "天气服务暂时无法连接，请稍后重试。"
WEATHER_RETRY_OFFER = "天气服务暂时连不上。要我再查一次吗？"
WEATHER_CLARIFICATION_LIFETIME = timedelta(seconds=90)
WEATHER_RETRY_REPLY = re.compile(
    r"(?:需要|要|要的|好|好的|好啊|行|可以|可以啊|"
    r"(?:请|那就)?(?:再查|重查|再试|重试)(?:一次|一遍|一下)?)(?:吧|呀)?[。！!？?]*"
)
WEATHER_RETRY_DECLINE = re.compile(
    r"(?:不需要|不用了|不用重试|不要重试|(?:先)?(?:不用|别|不)查(?:了)?|算了|取消)(?:吧)?[。！!？?]*"
)
_VALID_PERIODS = frozenset({"current", "today", "tomorrow", "day_after_tomorrow", "three_days"})


def simple_forecast(text, city, previous):
    text = text.strip()
    if DAY_FOLLOWUP.fullmatch(text):
        return bool(previous and previous.get("simple", True))
    name = city.removesuffix("市")
    if not name or name not in text:
        return False
    remainder = re.sub(re.escape(name) + "市?", "", text, count=1).strip()
    if previous and re.fullmatch(r"(?:那|那么|换成|换到)?(?:呢|怎么样|如何)[？?。！!]*", remainder):
        return previous.get("simple", True)
    return bool(FORECAST_ONLY.fullmatch(remainder))


def weather_context(turn):
    if turn.get("status") != "replied":
        return None
    if turn.get("weather"):
        return turn["weather"]
    reply = turn.get("reply") or ""
    if "get_weather" not in (turn.get("holo_tools") or []) or not re.search(
        r"-?\d+(?:\.\d+)?\s*(?:°\s*C|℃|摄氏度|度)", reply
    ):
        return None
    request = turn["user"]
    periods = [value for label, value in WEATHER_DAYS.items() if label in request]
    period = "three_days" if "三天" in request or len(periods) > 1 else next(iter(periods), "current")
    return {"period": period, "city": "", "previous_reply": reply}


def weather_input(moss, turn, ignore_memory):
    context = weather_context(turn)
    choices = turn.get("weather_choices") or {}
    if not (context or choices):
        return context, choices, {}
    versions = moss._memory_provenance(turn, "_weather_memory_versions")
    if versions is None or (versions and ignore_memory):
        return None, {}, None
    return context, choices, versions


def active_weather_clarification(earlier, turn):
    pending = earlier.get("weather_clarification")
    if earlier.get("status") == "replied" and pending:
        if datetime.fromisoformat(turn["created_at"]) <= datetime.fromisoformat(pending["expires_at"]):
            return pending
    return None


def weather_retry_intent(earlier, turn):
    if earlier.get("status") != "replied" or earlier.get("chat_id") != turn["chat_id"]:
        return None
    retry = earlier.get("_weather_retry")
    if retry and datetime.fromisoformat(turn["created_at"]) <= datetime.fromisoformat(retry["expires_at"]):
        text = unicodedata.normalize("NFKC", turn["user"]).strip()
        if WEATHER_RETRY_REPLY.fullmatch(text):
            return "retry"
        if WEATHER_RETRY_DECLINE.fullmatch(text):
            return "decline"
    return None


def weather_result(reply, context, city, period, *, location=None, previous=None, part_of_day=None, focus=(), simple=True):
    kind = (context or {}).get("location_kind")
    if not kind:
        kind = location["kind"] if location else "district" if city.endswith(("区", "县", "旗")) else "city"
    changes = {"weather_request_kind": kind}
    if context and "choices" in context:
        changes["weather_choices"] = {**context, "part_of_day": part_of_day, "focus": list(focus)}
    elif context:
        changes["weather"] = {**context, "simple": simple}
    elif reply == WEATHER_TEMPORARILY_UNAVAILABLE:
        lifetime = _ab("WEATHER_CLARIFICATION_LIFETIME", WEATHER_CLARIFICATION_LIFETIME)
        changes["_weather_retry"] = {
            "kind": kind, "city": city, "period": period, "part_of_day": part_of_day, "focus": list(focus),
            "expires_at": (datetime.now(timezone.utc) + lifetime).isoformat(),
        }
        if location and kind != "browser":
            changes["_weather_retry"]["location"] = {
                k: location[k] for k in ("name", "kind", "latitude", "longitude", "parent") if k in location
            }
        if previous:
            changes["_weather_retry"]["previous"] = {k: previous[k] for k in ("city", "parent") if k in previous}
        reply = WEATHER_RETRY_OFFER
    return reply, changes


async def fetch_weather(city, period, place, *, part_of_day=None, question=""):
    http_mod = _ab("aiohttp", aiohttp)
    async with http_mod.ClientSession(timeout=http_mod.ClientTimeout(total=10), trust_env=True) as http:
        if place is None:
            params = {"name": city.strip().removesuffix("市"), "language": "zh", "count": "5"}
            async with http.get("https://geocoding-api.open-meteo.com/v1/search", params=params) as response:
                response.raise_for_status()
                places = (await response.json()).get("results") or []
            if not places:
                return f"天气服务没有找到「{city}」。", None
            places = city_candidates(city, places)
            if len(places) > 1:
                names = "；".join(item["name"] for item in places[:5])
                return f"查到多个同名地点：{names}。请补充省份或城市。", {"choices": places, "period": period}
            place = places[0]
        lat, lon = (place[axis] for axis in ("latitude", "longitude"))
        if place.get("kind") == "browser":
            lat, lon = round(lat, 2), round(lon, 2)
        params = {
            "latitude": str(lat), "longitude": str(lon), "timezone": "auto",
            "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
            "daily": ",".join(DAILY_FIELDS.values()), "forecast_days": "3",
        }
        if part_of_day:
            params["hourly"] = ",".join(HOURLY_FIELDS.values())
        async with http.get("https://api.open-meteo.com/v1/forecast", params=params) as response:
            response.raise_for_status()
            forecast = await response.json()
    return format_weather(forecast, place, period, part_of_day, question)


async def get_weather(city, period, *, location=None, previous=None, part_of_day=None, question=""):
    if period not in _VALID_PERIODS:
        return "天气服务支持当前天气和从今天起三天的预报。", None
    if location is None:
        if not isinstance(city, str) or not re.fullmatch(r"[\w\u3400-\u9fff .·-]{1,50}", city.strip()) or not city.strip():
            return "请提供城市、区县或经纬度。", None
        matches = districts(city.strip(), previous)
        if len(matches) > 1:
            names = "、".join(item["name"] for item in matches[:5])
            return f"有多个地方叫{city}，包括{names}。请补充城市名。", {"choices": matches, "period": period}
        if matches:
            location = {**matches[0], "kind": "district"}
        elif city.endswith(("区", "县", "旗")):
            return f"没有查到「{city}」的区县坐标。可以提供完整地名或经纬度。", None
    try:
        # Respect test patches applied directly on assistant_brain.fetch_weather
        facade = sys.modules.get("assistant_brain")
        fetcher = getattr(facade, "fetch_weather", None) if facade else None
        if fetcher is None:
            fetcher = fetch_weather
        return await fetcher(city, period, location, part_of_day=part_of_day, question=question)
    except aiohttp.ClientResponseError as error:
        if 400 <= error.status < 500 and error.status not in (408, 429):
            return "天气服务无法处理这次查询。", None
    except (aiohttp.ClientError, TimeoutError):
        pass
    return WEATHER_TEMPORARILY_UNAVAILABLE, None
