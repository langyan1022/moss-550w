import asyncio
from datetime import date, datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
from time import time
import unicodedata

from colloquial_zh import normalize_colloquial_erhua


DIGITS = "零一二三四五六七八九"
NUMBER = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)"
UNIT = r"(?:小时|钟头|分钟|分|秒钟|秒|天)"
PART = rf"(?:{NUMBER}\s*个半\s*(?:小时|钟头)|{NUMBER}\s*个?\s*{UNIT}|半\s*个?\s*(?:小时|钟头)|一刻钟)"
DURATION = rf"(?:{PART}\s*)+"
PERIOD = r"凌晨|早上|上午|中午|下午|晚上"
WEEK_PREFIX = r"(?:本|这|上(?:一)?|下(?:一)?)"
WEEK_UNIT = r"(?:周|星期|礼拜)"
WEEK_NUMBER = r"[一二三四五六七日天1-7]"
WEEKDAY = rf"{WEEK_PREFIX}?{WEEK_UNIT}\s*{WEEK_NUMBER}"
CALENDAR_YEAR = r"(?:[0-9]{4}|[零〇一二三四五六七八九]{4})"
CALENDAR_NUMBER = r"(?:[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})"
CHINESE_DATE = rf"(?:(?P<year>{CALENDAR_YEAR})年)?(?P<month>{CALENDAR_NUMBER})月(?P<date>{CALENDAR_NUMBER})[日号]"
DECIMAL_DATE = r"(?P<year>[0-9]{4})(?P<sep>[-/])(?P<month>[0-9]{1,2})(?P=sep)(?P<date>[0-9]{1,2})"
CHINESE_DATE_TOKEN = rf"(?:{CALENDAR_YEAR}年)?{CALENDAR_NUMBER}月{CALENDAR_NUMBER}[日号]"
ISO_DATE_TOKEN = r"[0-9]{4}(?:-[0-9]{1,2}-[0-9]{1,2}|/[0-9]{1,2}/[0-9]{1,2})"
DATE_TOKEN = rf"(?:{CHINESE_DATE_TOKEN}|{ISO_DATE_TOKEN})"
DAY = rf"(?:今天|明天|大后天|后天|{WEEKDAY}|{DATE_TOKEN})"
CLOCK = (rf"(?P<day>{DAY})?\s*(?P<period>{PERIOD})?\s*(?P<hour>{NUMBER})"
         rf"(?:(?P<half>点半)|点(?:(?P<minute>{NUMBER})分?)?|:(?P<digits>\d{{1,2}}))")
TIMER = r"(?:提醒|闹钟|倒计时|计时器)"
CHAT_REFERENCE = r"(?:刚(?:才|刚)(?:设(?:置)?)?的?|刚设(?:置)?的|上一个|这个|那个)"
TIMES = {"天": 86400, "小时": 3600, "钟头": 3600, "分钟": 60, "分": 60, "秒钟": 1, "秒": 1}
CREATE = r"(?:(?:设置|设定|设|定|开始|启动|来)(?:上)?(?:一?个)?\s*)?"
QUERY_DAY = rf"{DAY}|今早|今晚|明早|明晚|{WEEK_PREFIX}{WEEK_UNIT}"
QUERY_CLOCK = rf"(?:(?:{PERIOD})?\s*(?:\d{{1,2}}:\d{{2}}|{NUMBER}点(?:半|{NUMBER}分?)?))"
QUERY_WINDOW = re.compile(
    rf"(?P<day>{QUERY_DAY})(?:\s*(?P<part>{PERIOD})"
    rf"|\s*(?P<start>{QUERY_CLOCK})\s*(?:到|至|[-–—])\s*(?P<end>{QUERY_CLOCK}))?"
    rf"\s*的?\s*(?:我(?:的)?\s*)?(?:还?有没有|还?有(?:哪些|多少(?:个)?|几个|什么|啥))?\s*{TIMER}(?:列表|还?有没有|(?:有)?(?:哪些|什么|啥))?"
)
NUMBERED_TIMER = rf"(?:(?P<index_marker>第)\s*)?(?P<index>{NUMBER})\s*(?P<index_unit>号|个)"
COUNTDOWN = re.compile(
    rf"(?:{NUMBERED_TIMER}|(?P<latest>{CHAT_REFERENCE})|(?:下一个|最近的)?)"
    rf"\s*{TIMER}(?:\s*(?:#|编号|第)?\s*(?P<id>\d+)号?)?"
    r"\s*(?:还(?:有|剩)多久|多久(?:才|会)?到(?:时间)?|几点(?:钟)?(?:提醒|响)?|什么时候(?:到|响)?)"
)
DAY_OFFSETS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3, "今早": 0, "今晚": 0, "明早": 1, "明晚": 1}
WEEK_OFFSETS = {"本":0, "这":0, "上":-7, "下一":7, "下":7, "上一":-7}
DAY_NAMES = {value:index for index, value in enumerate("一二三四五六七")}
DAY_NAMES.update({"日":6, "天":6, **{str(index+1):index for index in range(7)}})
WEEKDAY_PATTERN = re.compile(rf"(?P<prefix>{WEEK_PREFIX})?{WEEK_UNIT}\s*(?P<weekday>{WEEK_NUMBER})")
DATE_PATTERN = re.compile(CHINESE_DATE)
ISO_DATE_PATTERN = re.compile(DECIMAL_DATE)
DAY_PARTS = {"凌晨": (0, 6), "早上": (6, 12), "上午": (6, 12), "中午": (11, 14),
             "下午": (12, 18), "晚上": (18, 24)}
ALIAS_PARTS = {"今早": "早上", "今晚": "晚上", "明早": "早上", "明晚": "晚上"}
SHORT_DAY_CLOCKS = {"今早":"今天早上", "今晚":"今天晚上", "明早":"明天早上", "明晚":"明天晚上",
                    "后天一早":"后天早上", "明天一早":"明天早上", "今天一早":"今天早上"}
SHORT_DAY_START = re.compile(r"^((?:提醒我|叫我|通知我)?\s*在?\s*)(" + "|".join(SHORT_DAY_CLOCKS) + ")")
CANCEL = re.compile(
    rf"(?:取消|撤销|删除|关掉)(?P<prefix>(?:(?:一下|我(?:的)?|最近的|{CHAT_REFERENCE})\s*)*)"
    rf"(?:(?P<all>全部|所有)\s*{TIMER}|{NUMBERED_TIMER}\s*{TIMER}"
    rf"|{TIMER}\s*(?:#|编号|第)?\s*(?P<id>\d+)(?:号)?|(?P<next>下一个)\s*{TIMER}"
    rf"|(?P<name>[^，,。！？?\n]{{1,80}}?)的?{TIMER}|{TIMER})"
)
BA_CANCEL = re.compile(
    r"(?:把|将)\s*(?P<target>[^，,。！？?\n]{1,90}?)\s*(?:取消掉?|撤销掉?|删(?:除)?掉?|关掉|关闭)"
)
RESCHEDULE = re.compile(
    r"(?P<marker>把|将)?\s*(?P<target>[^，,。！？?\n]{1,90}?)\s*"
    r"(?P<verb>改(?:到|成|为)|挪(?:到|至)|(?:调整|推迟|延后|延期|提前)(?:到|至)|推迟|延后|提前)"
    r"\s*(?P<when>[^，,。！？?\n]{1,90})"
)
CHAINED_TIMER = re.compile(
    rf"[，,。；;]\s*(?:顺便|然后|再|接着|另外|同时)\s*(?:请|帮我|给我|麻烦你?\s*)*\s*"
    rf"(?:(?:取消|撤销|删除|关掉|设置|设定|改期)[^，,。；;]{{0,80}}{TIMER}"
    rf"|{CLOCK}\s*(?:提醒我|叫我|通知我))"
)
COMPOUND_OTHER_ACTION = re.compile(
    r"[，,。；;]\s*(?:顺便|顺带|另外|然后|接着|再帮我|再给我|并帮我|并给我|同时帮我|同时)\s*"
    r"(?:请|帮我|给我|麻烦你?\s*)*"
    r"(?:查|看|算|计算|记|写|搜|搜索|读|打开|关|开|播|放|问|告诉|汇总|总结|列出|改|调)"
)


def number(text):
    if text[0].isascii():
        return float(text)
    total = 0
    digit = 0
    for char in text.replace("两", "二"):
        if char in DIGITS:
            digit = DIGITS.index(char)
        else:
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return total + digit


def duration_seconds(text):
    text = re.sub(rf"({NUMBER})\s*个半\s*(?:小时|钟头)", r"\1小时30分钟", text)
    text = re.sub(r"\s+|个", "", text).replace("半小时", "30分钟").replace("半钟头", "30分钟").replace("一刻钟", "15分钟")
    return sum(number(amount) * TIMES[unit] for amount, unit in re.findall(rf"({NUMBER})({UNIT})", text))


def clock_values(match, default_period=None):
    hour = number(match["hour"])
    minute = 30 if match["half"] else number(match["minute"] or match["digits"] or "0")
    period = match["period"] or default_period
    if period == "晚上" and hour in (0, 12):
        return 0, minute, 1
    if period in ("下午", "晚上", "中午") and hour < 12 and (period != "中午" or hour <= 2):
        hour += 12
    if period == "凌晨" and hour == 12:
        hour = 0
    return hour, minute, 0


def _reminder_number(match):
    if match["index_unit"] == "个":
        return {"id": None, "ordinal": number(match["index"]) if match["index_marker"] else 0}
    index = match["index"] or match["id"]
    return {"id": number(index) if index else None}


def reminder_target(match):
    return {"all":bool(match["all"]), **_reminder_number(match),
            "latest":not (match["index"] or match["id"]) and bool(re.search(CHAT_REFERENCE, match["prefix"])),
            "next":bool(match["next"]), "label":(match["name"] or "").strip("《》“” ")}


def expand_short_day(text):
    return SHORT_DAY_START.sub(lambda match: match[1] + SHORT_DAY_CLOCKS[match[2]], text)


def clock_intent(match):
    hour, minute, day_offset = clock_values(match)
    return {"day":match["day"], "hour":hour, "minute":minute, "day_offset":day_offset}


def _clean_timer_label(label):
    cleaned = str(label or "").strip().rstrip("。！!").strip()
    cleaned = re.sub(r"^(?:一下|一声|下)\s*[,，:：、]?\s*", "", cleaned)
    cleaned = cleaned.lstrip(",，:：、 ").strip()
    return cleaned or "倒计时"


def create_intent(label, **clock):
    cleaned = _clean_timer_label(label)
    if CHAINED_TIMER.search(cleaned):
        return {"action":"invalid", "reply":"这句话包含两项提醒操作，请分别提出。"}
    if COMPOUND_OTHER_ACTION.search(cleaned):
        return None
    return {"action":"create", "label":cleaned, **clock}


def reschedule_intent(move):
    match = CANCEL.fullmatch("取消" + move["target"].strip())
    if not match:
        return None
    target = reminder_target(match)
    if not move["marker"] and target["label"] and not target["latest"]:
        return None
    intent = {"action":"reschedule", **target}
    when = move["when"].strip()
    if move["verb"] in {"推迟", "延后", "提前"}:
        if re.fullmatch(DURATION, when):
            direction = -1 if move["verb"] == "提前" else 1
            return {**intent, "shift_seconds":direction * duration_seconds(when)}
        return None
    if relative := re.fullmatch(rf"(?P<delay>{DURATION})(?:之?后|以后)", when):
        return {**intent, "seconds":duration_seconds(relative["delay"])}
    if absolute := re.fullmatch(rf"(?:在)?{CLOCK}", expand_short_day(when)):
        return {**intent, **clock_intent(absolute)}
    if date_only := re.fullmatch(rf"在?\s*({DAY})", when):
        return {**intent, "day":date_only[1], "retain_clock":True}
    return None


def parse_timer(text):
    text = unicodedata.normalize("NFKC", text).strip().rstrip("。！!").strip()
    text = normalize_colloquial_erhua(text)
    text = re.sub(r"^(?:MOSS\s*[,，]?\s*)?(?:(?:请(?:你)?|麻烦(?:你)?|帮我|给我)\s*)*", "", text, flags=re.I)
    if re.search(r"每天|每日|工作日|每(?:个?)?(?:周|星期|礼拜)|每隔|每月", text):
        return None
    query = text.rstrip("?？").strip()
    short = re.sub(r"^(?:(?:查看|查(?:看|询)?|列出|显示|看看|看)(?:一下)?\s*)?(?:我(?:的)?\s*)?", "", query)
    window = QUERY_WINDOW.fullmatch(short)
    if window:
        return {"action": "list", "window": window.groupdict()}
    countdown = COUNTDOWN.fullmatch(short)
    if countdown and not (countdown["id"] and (countdown["index"] or countdown["latest"])):
        return {"action": "list", "countdown": True, **_reminder_number(countdown),
                "latest": bool(countdown["latest"])}
    if re.fullmatch(rf"(?:查看|查询|查|列出|显示|看看)(?:一下)?(?:我(?:的)?)?(?:(?:当前|待办|全部|所有)\s*)?(?:还?有没有\s*)?{TIMER}(?:列表)?"
                    rf"|(?:我)?(?:现在|当前)?(?:还?有没有|还有(?:哪些|多少(?:个)?|几个|什么)){TIMER}|{TIMER}列表", query):
        return {"action": "list"}
    if cancel := CANCEL.fullmatch(text):
        return {"action":"cancel", **reminder_target(cancel)}
    if any(mark in text for mark in ("?", "？", "\n")) or text.endswith(("吗", "么", "会怎样", "会怎么样", "是什么意思", "怎么说", "怎么翻译", "如何翻译")):
        return None
    if ba := BA_CANCEL.fullmatch(text):
        if cancel := CANCEL.fullmatch("取消" + ba["target"].strip()):
            return {"action": "cancel", **reminder_target(cancel)}
    if move := RESCHEDULE.fullmatch(text):
        if intent := reschedule_intent(move):
            return intent
    relative = re.fullmatch(rf"(?:在|过)?\s*(?P<delay>{DURATION})(?:之?后|以后)\s*[,，]?\s*(?:提醒我|叫我|通知我)\s*(?P<label>.+)", text)
    if not relative:
        relative = re.fullmatch(rf"过\s*(?P<delay>{DURATION})\s*[,，]?\s*(?:提醒我|叫我|通知我)\s*(?P<label>.+)", text)
    if not relative:
        relative = re.fullmatch(rf"(?:提醒我|叫我|通知我)\s*(?:在|过)?\s*(?P<delay>{DURATION})(?:之?后|以后)\s*[,，]?\s*(?P<label>.+)", text)
    if not relative:
        relative = re.fullmatch(rf"{CREATE}(?P<delay>{DURATION})的?\s*{TIMER}(?:(?:[,，]?\s*(?:到时(?:候)?|结束时)?"
                                rf"\s*(?:提醒我|叫我|通知我)|[:：,，])\s*(?P<label>.+))?", text)
    if not relative:
        relative = re.fullmatch(rf"{CREATE}(?:倒计时|计时)\s*(?P<delay>{DURATION})(?:[,，]\s*(?:提醒我)?\s*(?P<label>.+))?", text)
    if relative:
        return create_intent((relative["label"] or "倒计时").strip().rstrip("。"),
                             seconds=duration_seconds(relative["delay"]))
    text = expand_short_day(text)
    absolute = re.fullmatch(rf"(?:在)?{CLOCK}\s*[,，]?\s*(?:提醒我|叫我|通知我)\s*(?P<label>.+)", text)
    if not absolute:
        absolute = re.fullmatch(rf"(?:提醒我|叫我|通知我)\s*(?:在)?{CLOCK}\s*[,，]?\s*(?P<label>.+)", text)
    if absolute:
        return create_intent(absolute["label"].strip(), **clock_intent(absolute))
    return None


def display_time(epoch):
    moment = datetime.fromtimestamp(epoch)
    prefix = "" if moment.year == datetime.now().year else f"{moment.year:04d}年"
    return f"{prefix}{moment.month:02d}月{moment.day:02d}日 {moment:%H:%M:%S}"


def calendar_number(value):
    if value.isascii():
        return int(value)
    value = value.replace("〇", "零").replace("两", "二")
    if all(character in DIGITS for character in value):
        return int("".join(str(DIGITS.index(character)) for character in value))
    return number(value)


def calendar_day(value, today):
    if value is None or value in DAY_OFFSETS:
        return today + timedelta(days=DAY_OFFSETS.get(value, 0))
    if weekday := WEEKDAY_PATTERN.fullmatch(value):
        index = DAY_NAMES[weekday["weekday"]]
        prefix = weekday["prefix"]
        days = (WEEK_OFFSETS[prefix] + index - today.weekday()) if prefix else (index - today.weekday()) % 7
        return today + timedelta(days=days)
    exact = DATE_PATTERN.fullmatch(value) or ISO_DATE_PATTERN.fullmatch(value)
    if exact:
        month, day = calendar_number(exact["month"]), calendar_number(exact["date"])
        # 先用闰年核对月份和日期。没有年份时，选择最近的同一日历日期。
        try:
            if exact["year"]:
                return date(calendar_number(exact["year"]), month, day)
            date(2000, month, day)
            for year in range(today.year, min(today.year + 9, 10000)):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate >= today:
                    return candidate
        except ValueError:
            pass
    raise ValueError("提醒日期无效，请核对年月日。")


def scheduled_epoch(intent, current, *, previous=None):
    if "shift_seconds" in intent:
        epoch = previous + intent["shift_seconds"]
        if not math.isfinite(epoch) or epoch <= current.timestamp():
            raise ValueError("改期后的时间必须晚于现在。")
        try:
            datetime.fromtimestamp(epoch)
        except (OSError, ValueError, OverflowError) as error:
            raise ValueError("这个日期超出本机支持的时间范围，请重新设置。") from error
        return epoch
    if "seconds" in intent:
        delay = intent["seconds"]
        if not 0 < delay <= (datetime.max - current).total_seconds() - 86400:
            raise ValueError("请给提醒一个有效的正数时长。")
        due = current + timedelta(seconds=delay)
    else:
        day = intent["day"]
        try:
            date_at = calendar_day(day, current.date()) + timedelta(days=intent.get("day_offset", 0))
            if intent.get("retain_clock"):
                clock = datetime.fromtimestamp(previous).time()
            else:
                hour, minute = intent["hour"], intent["minute"]
                if not (0 <= hour < 24 and 0 <= minute < 60) or int(hour) != hour or int(minute) != minute:
                    raise ValueError("提醒时间无效，请使用 0 至 23 时、0 至 59 分。")
                clock = current.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0).time()
            due = datetime.combine(date_at, clock)
        except (OverflowError, OSError) as error:
            raise ValueError("这个日期超出本机支持的时间范围，请重新设置。") from error
        if due <= current:
            weekday = WEEKDAY_PATTERN.fullmatch(day) if day else None
            if day is not None and not (weekday and weekday["prefix"] is None):
                operation = "改期" if previous is not None else "创建"
                prefix = "今天的" if day == "今天" else "指定日期的"
                raise ValueError(f"{prefix}这个时间已经过去，提醒未{operation}。")
            try:
                due += timedelta(days=7 if weekday else 1)
            except OverflowError as error:
                raise ValueError("这个日期超出本机支持的时间范围，请重新设置。") from error
    try:
        return due.timestamp()
    except (OSError, ValueError, OverflowError) as error:
        raise ValueError("这个日期超出本机支持的时间范围，请重新设置。") from error


def query_window(window, current):
    day, part, start, end = (window[key] for key in ("day", "part", "start", "end"))
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week = re.fullmatch(rf"(?P<prefix>{WEEK_PREFIX}){WEEK_UNIT}", day)
    if week:
        if part or start:
            raise ValueError("请给出具体日期，再查询当天的时段。")
        monday = midnight + timedelta(days=WEEK_OFFSETS[week["prefix"]] - current.weekday())
        return monday, monday + timedelta(days=7), day
    midnight = datetime.combine(calendar_day(day, current.date()), midnight.time())
    if start:
        begin, end = (re.fullmatch(CLOCK, value) for value in (start, end))
        inherited = begin["period"] or ALIAS_PARTS.get(day)
        begin_h, begin_m, begin_offset = clock_values(begin, inherited)
        end_default = None if end["digits"] is not None and end["hour"].startswith("0") else inherited
        end_h, end_m, end_offset = clock_values(end, end_default)
        message = "请提供两个不同的有效时刻，例如 09:00 到 12:00。"
        invalid = (not all(0 <= h < 24 and int(h) == h for h in (begin_h, end_h)) or
                   not all(0 <= m < 60 and int(m) == m for m in (begin_m, end_m)))
        if invalid:
            raise ValueError(message)
        first = midnight + timedelta(days=begin_offset, hours=begin_h, minutes=begin_m)
        last = midnight + timedelta(days=end_offset, hours=end_h, minutes=end_m)
        if last < first:
            bare_hour = number(end["hour"])
            if end_default == "晚上" and not end["period"] and 0 <= bare_hour < 12:
                last = midnight + timedelta(days=max(1, begin_offset), hours=bare_hour, minutes=end_m)
            if last < first:
                last += timedelta(days=1)
        if last == first:
            raise ValueError(message)
        return first, last, f"{first.month:02d}月{first.day:02d}日 {first:%H:%M} 到 {last.month:02d}月{last.day:02d}日 {last:%H:%M}"
    part = part or ALIAS_PARTS.get(day)
    first_hour, last_hour = DAY_PARTS[part] if part else (0, 24)
    if DATE_PATTERN.fullmatch(day) or ISO_DATE_PATTERN.fullmatch(day):
        label = (f"{midnight.year}年" if midnight.year != current.year or "年" in day or "-" in day or "/" in day else "")
        label += f"{midnight.month}月{midnight.day}日"
    elif WEEKDAY_PATTERN.fullmatch(day):
        label = f"{day}（{midnight.month}月{midnight.day}日）"
    else:
        label = day
    if day not in ALIAS_PARTS:
        label += part or ""
    return midnight + timedelta(hours=first_hour), midnight + timedelta(hours=last_hour), label


def remaining_text(seconds):
    units = []
    seconds = math.ceil(seconds)
    for size, unit in ((86400, "天"), (3600, "小时"), (60, "分钟"), (1, "秒")):
        count, seconds = divmod(seconds, size)
        if count:
            units.append(f"{count}{unit}")
    return "".join(units)


class TimerStore:
    def __init__(self, db):
        self.db = db
        self.windows = None
        self.windows_error = None
        self._windows_lock = asyncio.Lock()
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS assistant_timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, due_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open')""")
            db.execute("CREATE INDEX IF NOT EXISTS open_timers ON assistant_timers(status, due_at)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(assistant_timers)")}
            for name, kind in (("chat_id", "TEXT"), ("turn_id", "TEXT"), ("media", "TEXT"), ("notified_at", "REAL"),
                               ("generation", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE assistant_timers ADD COLUMN {name} {kind}")

    def snapshot(self, *, include_generation=False):
        return [{"id": key, "label": label, "due_at": due_at, "chat_id": chat, "turn_id": turn,
                 "media": json.loads(media) if media else None, **({"generation":generation} if include_generation else {})}
                for key, label, due_at, chat, turn, media, generation in self.db.execute(
                    "SELECT id,label,due_at,chat_id,turn_id,media,generation FROM assistant_timers "
                    "WHERE status='open' ORDER BY due_at,id")]

    def enable_windows(self, data, port, *, startup=None):
        setting = os.environ.get("MOSS_WINDOWS_REMINDERS")
        directory = Path(data).resolve()
        known = directory == Path(__file__).resolve().parent / "data" or (directory / "windows-reminder-launch.json").is_file()
        if os.name == "nt" and (setting == "1" or setting is None and known):
            from windows_reminders import WindowsReminders
            self.windows = WindowsReminders(data, port, startup=startup)

    async def sync_windows(self):
        if self.windows is None:
            return
        async with self._windows_lock:
            rows = self.db.execute("SELECT id,due_at,notified_at,generation FROM assistant_timers WHERE status='open'").fetchall()
            try:
                await asyncio.to_thread(self.windows.reconcile, rows)
                self.windows_error = None
            except Exception as error:
                logging.getLogger(__name__).warning("Windows reminder scheduling failed: %s", error)
                self.windows_error = "Windows 后台提醒未就绪，打开 MOSS 后仍可查看到期提醒。"

    def notification(self, key, *, delivered=False, generation=0):
        if (type(key) is not int or not 0 < key < 2**63 or type(delivered) is not bool
                or type(generation) is not int or not 0 <= generation < 2**63):
            raise ValueError("提醒编号或回执无效")
        stamp = time()
        row = self.db.execute(
            "SELECT label,due_at,notified_at FROM assistant_timers WHERE id=? AND generation=? AND status='open'",
            (key, generation)
        ).fetchone()
        if not row or row[1] > stamp:
            return None
        if delivered:
            if not self.db.execute(
                    "UPDATE assistant_timers SET notified_at=COALESCE(notified_at,?) "
                    "WHERE id=? AND generation=? AND status='open' AND due_at<=?",
                    (stamp, key, generation, stamp)).rowcount:
                return None
        return {"id":key, "label":row[0], "due_at":row[1], "generation":generation, "delivered":bool(delivered or row[2])}

    def query(self, intent, items):
        if not intent.get("countdown"):
            return self._list_reply(intent, items)[0]
        current = datetime.now()
        stamp = current.timestamp()
        key = intent["id"]
        if key is not None:
            if not 1 <= key < 2**63 or key != int(key):
                return "请提供有效的提醒编号。"
            row = self.db.execute(
                "SELECT id,label,due_at,status FROM assistant_timers WHERE id=?", (int(key),)
            ).fetchone()
            if not row:
                return f"找不到 {int(key)} 号提醒。"
            key, label, due_at, status = row
            if status != "open":
                return f"{key} 号提醒已{'取消' if status == 'cancelled' else '处理'}。"
        else:
            if not items:
                return "目前没有待处理的提醒。"
            item = next((row for row in items if row["due_at"] > stamp), items[-1])
            key, label, due_at = item["id"], item["label"], item["due_at"]
        when = display_time(due_at)
        if due_at <= stamp:
            return f"{key} 号提醒「{label}」已到时间，原定 {when}。"
        return f"{key} 号提醒「{label}」还有 {remaining_text(due_at - stamp)}，定于 {when}。"

    def _list_reply(self, intent, items):
        current = datetime.now()
        stamp = current.timestamp()
        label = "目前"
        if window := intent.get("window"):
            try:
                first, last, label = query_window(window, current)
                lower, upper = first.timestamp(), last.timestamp()
            except ValueError as error:
                return str(error), []
            except (OSError, OverflowError):
                return "这个日期超出本机支持的查询范围。", []
            items = [item for item in items if lower <= item["due_at"] < upper]
        if not items:
            return f"{label}没有待处理的提醒。", []
        prefix = "共有" if label == "目前" else f"{label}有"
        lines = [f"{prefix} {len(items)} 个待处理的提醒。"]
        for item in items:
            when = display_time(item["due_at"])
            state = "（已到时间）" if item["due_at"] <= stamp else ""
            lines.append(f"{item['id']} 号 · {when}{state} · {item['label']}")
        return "\n".join(lines), items

    def listing(self, intent):
        reply, items = self._list_reply(intent, self.snapshot(include_generation=True))
        return reply, [[item["id"], item["due_at"], item["generation"]] for item in items]

    def _ordinal_target(self, ordinal, listing):
        if not ordinal or ordinal % 1 != 0:
            raise ValueError("请说清列表中的第几个提醒，或说出“1号提醒”这样的编号。")
        if listing is None:
            raise ValueError("请先查看提醒列表，再按列表顺序选择。")
        if not 1 <= ordinal <= len(listing):
            raise ValueError("刚才的提醒列表没有这一项。")
        expected = tuple(listing[int(ordinal) - 1])
        current = self.db.execute("SELECT id,due_at,generation FROM assistant_timers WHERE id=? AND status='open'",
                                  (expected[0],)).fetchone()
        if current != expected:
            raise ValueError("这条提醒在查看后已变化，请重新查看提醒列表。")
        return expected

    def execute(self, intent, *, chat_id=None, turn_id=None, media=None, listing=None):
        action = intent["action"]
        if action == "invalid":
            return intent["reply"]
        expected_generation = None
        if "ordinal" in intent:
            try:
                key, _, expected_generation = self._ordinal_target(intent["ordinal"], listing)
            except ValueError as error:
                return str(error)
            intent = {**intent, "id": key}
        if action == "create":
            try:
                epoch = scheduled_epoch(intent, datetime.now())
            except ValueError as error:
                return str(error)
            label = intent["label"]
            key = self.db.execute("INSERT INTO assistant_timers(label,due_at,chat_id,turn_id,media) VALUES(?,?,?,?,?)",
                                  (label, epoch, chat_id, turn_id, json.dumps(media, ensure_ascii=False) if media else None)).lastrowid
            return f"已设置 {key} 号提醒，{display_time(epoch)} 提醒你：{label}。"
        operation = "改期" if action == "reschedule" else "取消"
        if intent.get("latest"):
            if intent.get("all"):
                return f"请指定想{operation}的提醒编号。"
            sql = "SELECT id,status FROM assistant_timers WHERE chat_id=?"
            args = [chat_id]
            if label := intent.get("label"):
                sql += " AND label=?"
                args.append(label)
            row = self.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
            if not row:
                return "这段对话没有找到同名提醒。" if label else "这段对话还没有设置过提醒。"
            key, status = row
            if status != "open":
                return f"{key} 号提醒已{'取消' if status == 'cancelled' else '处理'}。"
            intent = {**intent, "id": key}
        items = self.snapshot()
        if action == "list":
            return self.query(intent, items)
        if action not in {"cancel", "reschedule"}:
            raise ValueError("未知的提醒操作")
        if intent["all"]:
            if action == "reschedule":
                return "请指定想改期的提醒编号。"
            self.db.execute("UPDATE assistant_timers SET status='cancelled' WHERE status='open'")
            return f"已取消全部 {len(items)} 个提醒。" if items else "目前没有可以取消的提醒。"
        if intent["id"] is not None:
            items = [item for item in items if item["id"] == intent["id"]]
        elif label := intent["label"]:
            exact = [item for item in items if item["label"] == label]
            items = exact or [item for item in items if label.casefold() in item["label"].casefold()]
            local = [item for item in items if item["chat_id"] == chat_id]
            if local:
                items = local
            elif len(items) > 1:
                numbers = "、".join(f"{key} 号" for key in sorted(item["id"] for item in items))
                return f"「{label}」有多个待处理的提醒（{numbers}），请指定编号。"
        else:
            items = [item for item in items if item["chat_id"] == chat_id]
            if intent.get("next"):
                stamp = time()
                items = [item for item in items if item["due_at"] > stamp]
                if not items:
                    return "这段对话没有尚未到时间的提醒。"
            elif not items:
                return f"这段对话没有可以{operation}的提醒。"
        if not items:
            return f"没有找到可以{operation}的对应提醒。"
        item = items[0] if intent.get("next") else max(items, key=lambda row: row["id"])
        if action == "reschedule":
            previous = item["due_at"]
            try:
                epoch = scheduled_epoch(intent, datetime.now(), previous=previous)
            except ValueError as error:
                return str(error)
            if epoch == previous:
                return f"{item['id']} 号提醒已经定在 {display_time(epoch)}。"
            if not self.db.execute(
                    "UPDATE assistant_timers SET due_at=?,notified_at=NULL,generation=generation+1 "
                    "WHERE id=? AND status='open' AND due_at=? AND (generation=? OR ? IS NULL)",
                    (epoch, item["id"], previous, expected_generation, expected_generation)).rowcount:
                return f"{item['id']} 号提醒已变化，请查看当前提醒。"
            return f"已改期 {item['id']} 号提醒，{display_time(epoch)} 提醒你：{item['label']}。"
        if not self.db.execute(
                "UPDATE assistant_timers SET status='cancelled' WHERE id=? AND status='open' AND (generation=? OR ? IS NULL)",
                (item["id"], expected_generation, expected_generation)).rowcount:
            return f"{item['id']} 号提醒已结束。"
        return f"已取消 {item['id']} 号提醒：{item['label']}。"

    def dismiss(self, key):
        if type(key) is not int or not 0 < key < 2**63:
            raise ValueError("提醒编号无效")
        row = self.db.execute("SELECT status,due_at FROM assistant_timers WHERE id=?", (key,)).fetchone()
        if not row or row[0] == "cancelled" or row[1] > time():
            raise ValueError("没有到时的对应提醒")
        self.db.execute("UPDATE assistant_timers SET status='seen' WHERE id=? AND status='open'", (key,))
