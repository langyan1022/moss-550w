import asyncio
from datetime import datetime, timezone
from functools import lru_cache
import re
import sqlite3
import sys
import unicodedata

from assistant_timers import parse_timer
from colloquial_zh import normalize_colloquial_days, normalize_weather_erhua
from desktop_actions import parse_desktop, run_desktop
from device_status import device_status, parse_device_status
from local_calendar import parse_calendar
from mcp_configured import parse_configured_mcp, run_configured_mcp
from system_media import run_system_media, system_media_intent
from weather_forecast import forecast_focus
from weather_location import (
    BROWSER_REFERENCE, DAY, FORECAST_RANGE_REPLY, LOCATION_REFERENCE, NO_LOOKUP, OUTSIDE_FORECAST_DAY,
    PART_OF_DAY, PREFIX as WEATHER_PREFIX, districts, forecast_details, period_from,
)


LOCAL_CLOCK = re.compile(r"(?:(?:你好|嗨|嘿|喂)?\s*MOSS\s*[，,、\s]*)?(?:(?:请|帮我|告诉我|查一下)\s*)*(?:读取本机时钟[，,]\s*(?:告诉我)?)?"
                         r"(?:(?:现在|这会儿|此刻)(?:的时间|几点(?:钟)?(?:了|啦)?|的日期和时间)"
                         r"|(?:今天|今日|今儿个?|今个)(?:是)?(?:几号|几月几[日号]|星期几|周几)(?:[，,、\s]*(?:星期几|周几|几号))?)[啊呀呢吗]?[？?。！!]*", re.I)
SHORT_CLOCK = re.compile(
    r"(?:(?:我的?|这台)?(?:本机|系统|电脑)的?)?(?:(?:当前|现在|这会儿|此刻)的?)?"
    r"(?:时间|日期和时间|日期|几点(?:钟)?(?:了|啦)?|星期几|周几)[啊呀呢吗么]?"
)
SPLIT_MARKS = re.compile(r"[,，、；;。！？!?\n]+")
LINK_WORD = re.compile(r"\s*(?:以及|还有|顺便|另外|然后|并且|并|再(?:查(?:一下)?|告诉我)|和|与|及)\s*")
MCP_PREFIX = re.compile(r"(?:用|通过)\s*MCP", re.I)
MCP_SEPARATOR = re.compile(rf"{SPLIT_MARKS.pattern}|{LINK_WORD.pattern}|\s*同时\s*")
IMPLICIT_QUERY_END = re.compile(
    r"(?:几点(?:钟)?(?:了|啦)?|几号|几月几[日号]|(?:星期|周)几|"
    r"静音(?:的|了)?|"
    r"(?:播放|放|播)(?:的)?(?:是)?(?:什么(?:音乐|歌曲|歌|曲目)?|哪(?:一)?首(?:歌曲|歌)?)|"
    r"(?:多少|多大|多久|在充电)(?:了)?|"
    r"(?<=多少|多大)(?:的?电(?:量)?|(?:可用)?内存|空间)(?:了)?)[啊呀呢吗么]?"
)
LEADING_WORD = re.compile(r"^(?:(?:还有(?!\s*(?:多少|什么|哪些))|以及|顺便|另外|然后|请你?|(?:再)?(?:帮我|告诉我|给我))\s*)+")
READ_WORD = re.compile(r"^(?:再)?(?:查询|查看|查查|看看|显示|(?:查|看|读)(?:一下|一眼)?)\s*")
SHARED_READ_END = re.compile(
    r"(?:(?:分别|各|都)(?:是|有|为)?(?:多少|多大)|都(?:给我|帮我)?(?:看|查)(?:一下|一眼))"
    r"[\s啊呀呢吗么？?。！!]*$"
)
LOCAL_WEATHER_PLACE = re.compile(rf"^((?:{DAY.pattern})?(?:{PART_OF_DAY.pattern})?)\s*(?:本地|当地|这边|我们这边)的?")
WEATHER_FOLLOWUP = re.compile(r"^(?:那(?:么)?|再看(?:看)?|再查|换回|回到|说回)")
WEATHER_NOUNS = {"天气", "天气预报", "天气情况", "气温", "温度"}
DEVICE_NAMES = {"cpu":"CPU 占用率", "内存":"可用内存", "电池":"电池状态"}
LABELS = {"clock":"时间", "volume":"系统音量", "cpu":"CPU", "memory":"内存", "battery":"电量", "mcp":"MCP 工具",
          "disk":"磁盘", "uptime":"开机时间", "device":"电脑状态", "reminders":"提醒", "calendar":"日程",
          "weather":"天气", "system_media":"系统播放器"}


def local_clock_reply():
    value = datetime.now().astimezone()
    return f"{value.year:04d}年{value.month:02d}月{value.day:02d}日，星期{'一二三四五六日'[value.weekday()]}，{value:%H:%M:%S}。"


def _weather_query(text):
    text = normalize_weather_erhua(text)
    if NO_LOOKUP.search(text) or LOCATION_REFERENCE.search(text):
        return None
    request = WEATHER_PREFIX.sub("", text)
    outside_range = OUTSIDE_FORECAST_DAY.search(request)
    # Check the remaining request with the same grammar used for a supported date.
    date_for_matching = OUTSIDE_FORECAST_DAY.sub("今天", request) if outside_range else request
    forecast = LOCAL_WEATHER_PLACE.sub(r"\1", date_for_matching)
    nearby = date_for_matching != forecast or BROWSER_REFERENCE.search(request)
    place, part, direct = forecast_details(forecast)
    if not direct:
        return None
    if outside_range:
        return {"kind":"weather", "place":place, "outside_forecast_range":True}
    if place:
        if nearby or len(districts(place)) > 1:
            return None
    elif not nearby and (text in WEATHER_NOUNS or WEATHER_FOLLOWUP.match(request)):
        return None
    focus = forecast_focus(text)
    period = period_from(text, "today" if part or focus else "current")
    return {"kind":"weather", "place":place, "part_of_day":part, "period":period, "focus":focus}


def _query(text, *, local_reading_only=False):
    text = LEADING_WORD.sub("", text.strip())
    reading = READ_WORD.sub("", text)
    if LOCAL_CLOCK.fullmatch(text) or SHORT_CLOCK.fullmatch(reading):
        return {"kind":"clock"}
    device = parse_device_status(DEVICE_NAMES.get(reading.casefold(), reading))
    if device and device["metric"] != "diagnosis":
        return {"kind":"device" if device["metric"] == "overview" else device["metric"], "intent":device}
    try:
        desktop = parse_desktop(reading)
    except ValueError:
        return None
    if desktop and desktop["action"] == "get_volume":
        return {"kind":"volume", "intent":desktop}
    if system_media_intent(reading, explicit_status=True) == "status":
        return {"kind":"system_media"}
    if local_reading_only:
        return None
    calendar = parse_calendar(text)
    if calendar and calendar["action"] == "calendar_list":
        return {"kind":"calendar", "intent":calendar}
    timer = parse_timer(text)
    if timer and timer["action"] == "list":
        return {"kind":"reminders", "intent":timer}
    return _weather_query(text)


def parse_local_overview(text, *, nearby_location=None):
    text = unicodedata.normalize("NFKC", text).strip()
    if re.search(r"[“”‘’「」『』`\"']", text):
        return None
    text = re.sub(r"^(?:MOSS|莫斯|摩斯)\s*[,，、]\s*", "", text, flags=re.I)

    @lru_cache(maxsize=None)
    def interpret(part):
        part = LEADING_WORD.sub("", SHARED_READ_END.sub("", part.strip()))
        if whole := _query(part):
            return [whole]
        # Prefer a complete left phrase, including terms such as 日期和时间.
        for cut in reversed(list(LINK_WORD.finditer(part))):
            if cut.start() and cut.end() < len(part) and (left := _query(part[:cut.start()])):
                if right := interpret(part[cut.end():]):
                    return [left, *right]
        # An omitted pause can follow a complete local reading, e.g. 现在几点这里会下雨吗.
        if len(part) <= 180:
            for ending in reversed(list(IMPLICIT_QUERY_END.finditer(part))):
                cut = ending.end()
                if cut < len(part) and (left := _query(part[:cut], local_reading_only=True)):
                    right = interpret(part[cut:])
                    if right and all(query["kind"] != "reminders" and not query.get("place") for query in right):
                        return [left, *right]
        return None

    raw = []
    for part in SPLIT_MARKS.split(text):
        if not part.strip():
            continue
        pieces = interpret(part)
        if not pieces:
            return None
        raw.extend(pieces)
    return _overview_intent(raw, nearby_location)


def _mcp_query_surface(text):
    # Keep the original character offsets so tool arguments are sent without normalization or splitting.
    surface = []
    closers = []
    quoted = escaped = False
    for char in text:
        if closers:
            surface.append("\0")
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                closers.append("]" if char == "[" else "}")
            elif char == closers[-1]:
                closers.pop()
        elif char in "[{":
            closers.append("]" if char == "[" else "}")
            surface.append("\0")
        elif char in "“”‘’「」『』`\"'":
            return ""
        else:
            surface.append(char)
    return "".join(surface)


def parse_mcp_overview(text, *, nearby_location=None):
    if not MCP_PREFIX.search(text):
        return None
    text = re.sub(r"^(?:MOSS|莫斯|摩斯)\s*[,，、]\s*", "", text, flags=re.I)
    surface = _mcp_query_surface(text)
    if not MCP_PREFIX.search(surface):
        return None
    cuts = tuple(MCP_SEPARATOR.finditer(surface))
    if not cuts:
        return None

    def reading(start, end):
        part = LEADING_WORD.sub("", text[start:end].strip())
        if not MCP_PREFIX.match(part):
            return _query(unicodedata.normalize("NFKC", part))
        intent = parse_configured_mcp(part)
        if not intent or intent["mode"] != "call":
            return None
        if args := intent["args"].strip():
            offset = text.rfind(args, start, end)
            if offset != -1 and MCP_SEPARATOR.search(surface[offset:end]):
                return None
        return {"kind":"mcp", "intent":intent}

    @lru_cache(maxsize=None)
    def read_from(start):
        for cut in cuts:
            if cut.start() > start and (left := reading(start, cut.start())):
                if right := read_from(cut.end()):
                    return [left, *right]
        if item := reading(start, len(text)):
            return [item]
        return None

    raw = read_from(0)
    if raw and any(row["kind"] == "mcp" for row in raw) and any(row["kind"] != "mcp" for row in raw):
        return _overview_intent(raw, nearby_location)
    return None


def _overview_intent(raw, nearby_location):
    if len(raw) < 2:
        return None
    queries = []
    volume_index = None
    for query in raw:
        if query["kind"] == "volume":
            if volume_index is not None:
                if query["intent"].get("mute_state"):
                    queries[volume_index] = query
                continue
            volume_index = len(queries)
        if query not in queries:
            queries.append(query)
    # A single weather lookup can share the panel with local readings.
    # City comparisons and requests without a device read keep their existing route.
    if all(query["kind"] == "weather" for query in queries):
        return None
    if sum(query["kind"] == "weather" and not query.get("outside_forecast_range") for query in queries) > 1:
        return None
    intent = {"action":"local_overview", "queries":queries}
    if nearby_location and any(query["kind"] == "weather" and not query.get("outside_forecast_range")
                               and not query["place"] for query in queries):
        intent["weather_location"] = nearby_location
    return intent


async def run_local_overview(intent, *, timers, chat_id, calendar_read=None, on_progress=None):
    reminder_lists = sum(query["kind"] == "reminders" and not query["intent"].get("countdown") for query in intent["queries"])
    mcp_slots = asyncio.Semaphore(2)

    async def read(query):
        kind = query["kind"]
        metadata = {}
        try:
            if kind == "clock":
                reply = local_clock_reply()
            elif kind == "reminders":
                if query["intent"].get("countdown"):
                    reply = timers.execute(query["intent"], chat_id=chat_id, listing=intent.get("reminder_listing"))
                else:
                    reply, rows = timers.listing(query["intent"])
                    if reminder_lists == 1:
                        metadata["_reminder_list_rows"] = rows
            elif kind == "calendar":
                reply = calendar_read(query["intent"])
            elif kind == "system_media":
                reply, _ = await run_system_media("status")
            elif kind == "mcp":
                call = query["intent"]
                async with mcp_slots:
                    reply = await run_configured_mcp({**call, "natural":True}, metadata)
                row = {"kind":kind, "label":f"MCP {call['server']} · {call['tool']}", "reply":reply,
                       "status":"ok" if "mcp_receipt" in metadata else "error", **metadata}
                return row, {}
            elif kind == "volume":
                async with asyncio.timeout(2):
                    reply = await run_desktop(query["intent"])
            elif kind == "weather":
                if query.get("outside_forecast_range"):
                    return {"kind":kind, "reply":FORECAST_RANGE_REPLY}, {}
                # Resolve via brain.weather_flow with dynamic assistant_brain test-patch hook (avoids circular import).
                from brain import weather_flow as _wf
                _ab = sys.modules.get("assistant_brain")
                BROWSER_POSITION_REQUIRED = getattr(_ab, "BROWSER_POSITION_REQUIRED", _wf.BROWSER_POSITION_REQUIRED)
                WEATHER_CLARIFICATION_LIFETIME = getattr(_ab, "WEATHER_CLARIFICATION_LIFETIME", _wf.WEATHER_CLARIFICATION_LIFETIME)
                WEATHER_TEMPORARILY_UNAVAILABLE = getattr(_ab, "WEATHER_TEMPORARILY_UNAVAILABLE", _wf.WEATHER_TEMPORARILY_UNAVAILABLE)
                get_weather = getattr(_ab, "get_weather", _wf.get_weather)
                weather_result = getattr(_ab, "weather_result", _wf.weather_result)

                place, part, period, focus = (query[key] for key in ("place", "part_of_day", "period", "focus"))
                location = intent.get("weather_location")
                if not place and not location:
                    return {"kind":kind, "reply":BROWSER_POSITION_REQUIRED}, {"weather_clarification": {
                        "period": period, "part_of_day": part, "focus": list(focus),
                        "expires_at": (datetime.now(timezone.utc) + WEATHER_CLARIFICATION_LIFETIME).isoformat(),
                    }}
                try:
                    async with asyncio.timeout(10):
                        reply, forecast = await get_weather(place, period, location=location,
                                                            part_of_day=part, question="、".join(focus))
                except TimeoutError:
                    reply, forecast = WEATHER_TEMPORARILY_UNAVAILABLE, None
                if forecast or reply == WEATHER_TEMPORARILY_UNAVAILABLE:
                    reply, metadata = weather_result(reply, forecast, place, period, location=location,
                                                     part_of_day=part, focus=focus)
            else:
                async with asyncio.timeout(2):
                    reply = await device_status(query["intent"])
        except ValueError as exc:
            reply = str(exc)
        except TimeoutError:
            reply = f"读取{LABELS[kind]}超时。"
        except (OSError, sqlite3.Error):
            reply = f"暂时无法读取{LABELS[kind]}。"
        return {"kind":kind, "reply":reply}, metadata

    queries = intent["queries"]
    results = [None] * len(queries)
    metadata = {}
    pending = {asyncio.create_task(read(query)): index for index, query in enumerate(queries)}
    try:
        while pending:
            finished, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                row, fields = task.result()
                results[pending.pop(task)] = row
                metadata.update(fields)
            if pending and on_progress:
                on_progress([row for row in results if row is not None])
    finally:
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    return "\n".join(row["reply"] for row in results), {**metadata, "local_overview":results}
