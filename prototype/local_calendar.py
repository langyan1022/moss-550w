from datetime import datetime, time as clock_time, timedelta
import json
import re
from time import time
import unicodedata

from assistant_timers import ALIAS_PARTS, CLOCK, DAY, PERIOD, QUERY_CLOCK, QUERY_DAY, calendar_day, clock_values, number, query_window
from colloquial_zh import normalize_colloquial_days


CREATE = r"(?:(?:安排|新增|创建|添加|登记)(?:一?个|一条)?\s*(?:日程|会议))"
RANGE = (rf"(?P<day>{DAY}|今早|今晚|明早|明晚)\s*(?P<start>{QUERY_CLOCK})\s*(?:到|至|[-–—~～])\s*"
         rf"(?P<end>{QUERY_CLOCK})")
TITLE = r"(?P<title>[^，,。！？?；;\r\n]{1,100})"
TIME_LEAD = rf"(?:在\s*)?{RANGE}\s*[,，]?\s*"
CREATIONS = tuple(re.compile(pattern) for pattern in (
    rf"{TIME_LEAD}{CREATE}\s*[:：,，]\s*{TITLE}",
    rf"{CREATE}\s*[:：]\s*{TITLE}\s*[,，]\s*(?:在\s*)?{RANGE}",
    rf"{TIME_LEAD}安排(?:一?个|一场)\s*{TITLE}(?:会议|会)",
    rf"{TIME_LEAD}日程(?:记为|命名为|叫)\s*{TITLE}",
))
CREATE_LEAD = re.compile(rf"(?:(?:在\s*)?{DAY}\s*(?:{QUERY_CLOCK}|{PERIOD})?\s*[,，]?\s*)?"
                         rf"{CREATE}(?:\s*[:：,，]|\s*$)")
QUERY_PREFIX = r"(?:(?:查看|查询|查|列出|显示|看看|看)(?:一下)?\s*)?(?:我(?:的)?\s*)?"
QUERY = re.compile(
    rf"{QUERY_PREFIX}(?P<day>{QUERY_DAY})"
    rf"(?:\s*(?P<part>{PERIOD})|\s*(?P<start>{QUERY_CLOCK})\s*(?:到|至|[-–—])\s*(?P<end>{QUERY_CLOCK}))?"
    r"\s*的?\s*(?:我(?:的)?\s*)?(?:(?:(?:还?有没有|(?:都)?有(?:哪些|什么|啥)|还?有(?=(?:本机)?日程[吗么]))\s*)?(?:本机)?日程(?:列表|还?有没有|有(?:哪些|什么|啥))?"
    r"|(?:还?有没有|(?:都)?有(?:哪些|什么|啥)|还?有(?=安排[吗么]))\s*安排)(?:呢|呀|啊|吗|么)?"
)
FOLLOWUP_DAYS = {"前天": -2, "昨天": -1, "今天": 0, "明天": 1, "后天": 2, "大后天": 3}
CALENDAR_FOLLOWUP = re.compile(
    rf"(?:那(?:么)?|再(?:查|看看?|看一下)|换到)?\s*(?P<day>{'|'.join(FOLLOWUP_DAYS)})?\s*"
    rf"(?P<part>{PERIOD})?\s*(?:的?\s*(?P<topic>(?:本机)?日程|安排))?(?:的话)?(?:呢|怎么样|如何)?"
)
CREATE_HINT = "请告诉我日程名称、日期和起止时间，例如：明天上午10点到11点安排日程：产品评审。"
CHANGE_HINT = "请给出新日期和起止时间，例如：把刚才的日程改到明天下午3点到4点。"
TARGET_HINT = "请点明一条本机日程的完整名称。重名时同时给出日期和时段。"
STALE_EVENT = "日程在预览后已变化，请重新查询并确认。"
CALENDAR_MUTATIONS = frozenset({"calendar_reschedule", "calendar_cancel"})
CALENDAR_CONFIRMATIONS = frozenset({"calendar_create", *CALENDAR_MUTATIONS})
MUTATION_TITLES = {"calendar_reschedule": "改期本机日程", "calendar_cancel": "取消本机日程"}
RESCHEDULE_TARGET = r"(?:把|将)?\s*(?P<target>[^，,。！？?；;\r\n]{1,120}?)\s*(?:改期到|改到|改成|挪到|调整到|移到)\s*"
RESCHEDULE = re.compile(rf"{RESCHEDULE_TARGET}(?:在\s*)?{RANGE}")
CANCEL_EVENT = re.compile(
    r"(?:(?:取消|撤销|删除|删掉|移除)\s*(?P<target>[^，,。！？?；;\r\n]{1,120})"
    r"|(?:把|将)\s*(?P<ba_target>[^，,。！？?；;\r\n]{1,120}?)\s*(?:取消掉?|撤销掉?|删(?:除)?掉?|移除掉?))"
)
RECENT_EVENT = re.compile(r"(?:刚才|刚刚|上一个|这条|这个|那条|那个)(?:创建|安排|改期)?的?(?:日程|会议)")
EXPLICIT_EVENT = re.compile(r"(?:本机)?(?:日程|会议)\s*[:：]\s*(.+)")
NAMED_EVENT = re.compile(rf"(?:(?P<day>{QUERY_DAY})\s*(?:(?P<start>{QUERY_CLOCK})"
                         rf"(?:\s*(?:到|至|[-–—~～])\s*(?P<end>{QUERY_CLOCK}))?|(?P<part>{PERIOD}))?\s*的?\s*)?"
                         r"(?:(?:那个|那条|那场|这个|这条|这场)\s*)?"
                         r"(?P<name>.+?)\s*(?P<noun>日程|会议)")
EVENT_FIELDS = ("operation_id", "title", "start", "end", "slot")
LIST_LIMIT = 20
CALENDAR_COMPOUND_OTHER = re.compile(
    r"[，,。；;]\s*(?:顺便|顺带|另外|然后|接着|再帮我|再给我|并帮我|并给我|同时帮我|同时|以及)\s*"
    r"(?:请|帮我|给我|麻烦你?\s*)*"
    r"(?:查|看|算|计算|记|写|搜|搜索|读|打开|关|开|播|放|问|告诉|汇总|总结|列出|设|提醒|调|删)"
)


def _invalid(message=CREATE_HINT):
    return {"action": "calendar_invalid", "detail": message}


def _epoch(moment):
    epoch = int(moment.timestamp())
    if datetime.fromtimestamp(epoch, moment.tzinfo).replace(tzinfo=None) != moment.replace(tzinfo=None):
        raise ValueError("该时刻在本机时区不可用")
    return epoch


def _fmt_ymd_hm(moment):
    return f"{moment.year:04d}年{moment.month:02d}月{moment.day:02d}日 {moment:%H:%M}"


def _slot(first, last):
    end = f"{last:%H:%M}" if first.date() == last.date() else _fmt_ymd_hm(last)
    return f"{_fmt_ymd_hm(first)} 至 {end}"


def _calendar_interval(match, current, *, future=True):
    start_clock, end_clock = (re.fullmatch(CLOCK, match[key]) for key in ("start", "end"))
    try:
        first, last, _ = query_window({"day": match["day"], "part": None, "start": match["start"],
                                      "end": match["end"]}, current.replace(tzinfo=None))
        end_hour = number(end_clock["hour"])
        start_period = start_clock["period"] or ALIAS_PARTS.get(match["day"])
        overnight = (start_period == "晚上" and end_clock["period"] in {None, "凌晨"} and 0 <= end_hour <= 6)
        midnight = end_clock["period"] == "晚上" and end_hour in (0, 12) and last.hour == 0
        crosses_day = last.date() != first.date() and not (overnight or midnight)
        first, last = (moment.replace(tzinfo=current.tzinfo) for moment in (first, last))
        start, end = _epoch(first), _epoch(last)
    except (OSError, ValueError, OverflowError):
        raise ValueError("日程的日期或起止时间有误，请核对后再试。") from None
    if crosses_day or start >= end:
        raise ValueError("日程结束时间应晚于开始时间。请核对两个时刻。")
    if future and start < current.timestamp():
        raise ValueError("这个日程的开始时间已经过去。请给出未来的起止时间。")
    return {"start": start, "end": end, "slot": _slot(first, last)}


def _calendar_target(value, current):
    value = value.strip()
    if RECENT_EVENT.fullmatch(value):
        return {"recent": True}
    explicit = EXPLICIT_EVENT.fullmatch(value)
    named = None if explicit else NAMED_EVENT.fullmatch(value)
    if not (explicit or named):
        if value in {"日程", "会议", "本机日程"}:
            raise ValueError(TARGET_HINT)
        return None
    title = (explicit[1] if explicit else named["name"]).strip().strip('「」“”"').strip()
    if not title or len(title) > 100 or title in {"全部", "所有", "我的", "本机", "今天", "明天", "后天"}:
        raise ValueError(TARGET_HINT)
    target = {"titles": [title]}
    if named:
        target["titles"].append(title + named["noun"])
        if named["end"]:
            interval = _calendar_interval(named, current, future=False)
            target.update(exact_start=interval["start"], exact_end=interval["end"])
        elif named["start"]:
            clock = re.fullmatch(CLOCK, named["start"])
            hour, minute, offset = clock_values(clock, ALIAS_PARTS.get(named["day"]))
            if not (0 <= hour < 24 and 0 <= minute < 60 and int(hour) == hour and int(minute) == minute):
                raise ValueError("请提供有效的原开始时间。")
            day = calendar_day(named["day"], current.date()) + timedelta(days=offset)
            first = datetime.combine(day, datetime.min.time()).replace(hour=int(hour), minute=int(minute), tzinfo=current.tzinfo)
            target["exact_start"] = _epoch(first)
        elif named["day"]:
            first, last, _ = query_window({"day": named["day"], "part": named["part"], "start": None, "end": None},
                                          current.replace(tzinfo=None))
            target.update(start=_epoch(first.replace(tzinfo=current.tzinfo)), end=_epoch(last.replace(tzinfo=current.tzinfo)))
    return target


def _parse_mutation(text, current):
    move = RESCHEDULE.fullmatch(text)
    incomplete = None if move else re.match(RESCHEDULE_TARGET, text)
    cancel = CANCEL_EVENT.fullmatch(text) if not (move or incomplete) else None
    command = move or incomplete or cancel
    if command is None:
        return None
    try:
        raw_target = (cancel["target"] or cancel["ba_target"]) if command is cancel else command["target"]
        target = _calendar_target(raw_target, current)
        if target is None:
            return None
        if incomplete:
            return _invalid(CHANGE_HINT)
        if move:
            return {"action": "calendar_reschedule", "selector": target, "after": _calendar_interval(move, current)}
        return {"action": "calendar_cancel", "selector": target}
    except (ValueError, OSError, OverflowError) as exc:
        return _invalid(str(exc) or TARGET_HINT)


def _list_intent(first, last):
    whole_day = first.time() == last.time() == clock_time.min and (last - first).days == 1
    detail = f"{first.year:04d}年{first.month:02d}月{first.day:02d}日" if whole_day else _slot(first, last)
    return {"action": "calendar_list", "start": _epoch(first), "end": _epoch(last), "detail": detail}


def calendar_list_window(intent):
    return {key: intent[key] for key in ("start", "end")}


def calendar_followup(text, previous, *, current=None):
    if not previous or previous.get("status") != "replied" or not (windows := previous.get("_calendar_list_windows")):
        return None
    phrase = normalize_colloquial_days(unicodedata.normalize("NFKC", text).strip().rstrip("。!?").strip())
    match = CALENDAR_FOLLOWUP.fullmatch(phrase)
    if not match or not (match["day"] or match["part"]):
        return None
    day, part = match["day"], match["part"]
    result = {"day": day, "part": part, "explicit": bool(match["topic"])}
    if len(windows) != 1 and not (day and part):
        return {**result, "intent": _invalid("上一轮有多个日程时段。请说出具体日期和时段，例如“明天下午的日程”。")}
    current = datetime.now() if current is None else current
    try:
        start = datetime.fromtimestamp(windows[0]["start"], current.tzinfo)
        end = datetime.fromtimestamp(windows[0]["end"], current.tzinfo)
        spans_multiple_dates = end.date() != start.date() and not (
            end.date() - start.date() == timedelta(days=1) and end.time() == clock_time.min)
        if not day and spans_multiple_dates:
            return {**result, "intent": _invalid("上一轮的日程跨日期。请说出要查询的日期和时段。")}
        date = current.date() + timedelta(days=FOLLOWUP_DAYS[day]) if day else start.date()
        if part or end - start > timedelta(days=1):
            first, last, _ = query_window({"day": date.isoformat(), "part": part, "start": None, "end": None}, current)
            first, last = (moment.replace(tzinfo=current.tzinfo) for moment in (first, last))
        else:
            shift = date - start.date()
            first, last = start + shift, end + shift
        return {**result, "intent": _list_intent(first, last)}
    except (OSError, ValueError, OverflowError):
        return {**result, "intent": _invalid("日程查询的日期或时间有误，请核对后再试。")}


def parse_calendar(text, *, current=None):
    text = normalize_colloquial_days(unicodedata.normalize("NFKC", text).strip().rstrip("。！!").strip())
    text = re.sub(r"^(?:(?:MOSS|莫斯|摩斯)\s*[,，]\s*)?(?:(?:请(?:你)?|麻烦(?:你)?|帮我|给我)\s*)*", "", text, flags=re.I)
    query = text.rstrip("?？").strip()
    current = datetime.now() if current is None else current
    lookup = QUERY.fullmatch(query)
    if lookup or re.fullmatch(r"(?:查看|查询|查|列出|显示|看看|看)(?:一下)?(?:我的)?日程(?:列表)?", query):
        window = lookup.groupdict() if lookup else {"day": "今天", "part": None, "start": None, "end": None}
        try:
            first, last, _ = query_window(window, current.replace(tzinfo=None))
            first, last = (moment.replace(tzinfo=current.tzinfo) for moment in (first, last))
            return _list_intent(first, last)
        except (OSError, ValueError, OverflowError):
            return _invalid("日程查询的日期或时间有误，请核对后再试。")

    if any(mark in text for mark in ("?", "\r", "\n")) or text.endswith(("吗", "么", "是什么意思", "怎么说")):
        return None
    if CALENDAR_COMPOUND_OTHER.search(text):
        return None
    if mutation := _parse_mutation(text, current):
        return mutation
    match = next((match for pattern in CREATIONS if (match := pattern.fullmatch(text)) is not None), None)
    if match is None:
        return _invalid() if CREATE_LEAD.match(text) else None

    title = match["title"].strip()
    if not title:
        return _invalid()
    try:
        interval = _calendar_interval(match, current)
    except ValueError as exc:
        return _invalid(str(exc))
    return {"action": "calendar_create", "title": title, **interval,
            "detail": f"{title}\n{interval['slot']}"}


def init_calendar(db):
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS calendar_events (
            operation_id TEXT PRIMARY KEY NOT NULL,
            title TEXT NOT NULL,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL CHECK (end > start),
            slot TEXT NOT NULL,
            UNIQUE (start, end, title)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS calendar_operations (
            operation_id TEXT PRIMARY KEY NOT NULL,
            event_id TEXT NOT NULL REFERENCES calendar_events(operation_id),
            slot TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS calendar_receipts (
            operation_id TEXT PRIMARY KEY NOT NULL,
            event_id TEXT NOT NULL,
            action TEXT NOT NULL,
            expected TEXT NOT NULL,
            receipt TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS calendar_receipts_event ON calendar_receipts (event_id)")


def _receipt(row):
    operation_id, title, start, end, slot = row
    return {"operation_id": operation_id, "title": title, "start": start, "end": end,
            "slot": slot, "detail": f"{title}\n{slot}\n本机日历"}


def _expected(intent):
    fields = EVENT_FIELDS[1:] if intent["action"] == "calendar_create" else ("before", "after")
    return {key: intent[key] for key in fields if key in intent}


def _read_receipt(db, operation_id, intent):
    row = db.execute("SELECT action, expected, receipt FROM calendar_receipts WHERE operation_id=?",
                     (operation_id,)).fetchone()
    if row is None:
        return None
    if row[0] != intent["action"] or json.loads(row[1]) != _expected(intent):
        raise ValueError("日程回执与本次确认的内容不同。")
    return json.loads(row[2])


def _store_receipt(db, operation_id, intent, receipt):
    db.execute("INSERT INTO calendar_receipts VALUES (?,?,?,?,?)",
               (operation_id, receipt["operation_id"], intent["action"],
                json.dumps(_expected(intent), ensure_ascii=False), json.dumps(receipt, ensure_ascii=False)))


def _event_snapshot(db, row):
    event = dict(zip(EVENT_FIELDS, row))
    version = db.execute("""SELECT operation_id FROM calendar_receipts
                            WHERE event_id=? AND action='calendar_reschedule' ORDER BY rowid DESC LIMIT 1""",
                         (event["operation_id"],)).fetchone()
    event["version"] = version[0] if version else event["operation_id"]
    return event


def calendar_event_snapshot(db, event_id):
    row = db.execute("SELECT operation_id, title, start, end, slot FROM calendar_events WHERE operation_id=?",
                     (event_id,)).fetchone()
    return _event_snapshot(db, row) if row else None


def prepare_mutation(db, intent, recent):
    selector = intent["selector"]
    if selector.get("recent"):
        if not recent or recent.get("action") == "calendar_cancel":
            return _invalid(TARGET_HINT)
        before = calendar_event_snapshot(db, recent["operation_id"])
        if (not before or before["version"] != recent.get("version", recent["operation_id"])
                or any(before[key] != recent[key] for key in EVENT_FIELDS)):
            return _invalid(STALE_EVENT)
    else:
        end = selector.get("end")
        exact_start, exact_end = selector.get("exact_start"), selector.get("exact_end")
        rows = db.execute("""SELECT operation_id, title, start, end, slot FROM calendar_events
                             WHERE title IN (?, ?) AND end > ? AND (? IS NULL OR start < ?)
                               AND (? IS NULL OR start=?) AND (? IS NULL OR end=?) ORDER BY start LIMIT 2""",
                          (selector["titles"][0], selector["titles"][-1], max(time(), selector.get("start", 0)),
                           end, end, exact_start, exact_start, exact_end, exact_end)).fetchall()
        if len(rows) > 1:
            return _invalid("有多条同名日程，请补充原日期和时段。")
        if not rows:
            return _invalid("没找到这条本机日程，请核对完整名称和日期。")
        before = _event_snapshot(db, rows[0])
    if before["end"] <= time():
        return _invalid("这条日程已经结束。")
    prepared = {"action": intent["action"], "before": before,
                "detail": f"{before['title']}\n原时间 {before['slot']}"}
    if after := intent.get("after"):
        if (after["start"], after["end"]) == (before["start"], before["end"]):
            return _invalid("这条日程已经安排在该时段。")
        prepared.update(after=after, detail=prepared["detail"] + f"\n新时间 {after['slot']}")
    return prepared


def recover_event(db, operation_id, intent):
    saved = _read_receipt(db, operation_id, intent)
    if saved is not None or intent["action"] in CALENDAR_MUTATIONS:
        return saved
    row = db.execute("""SELECT event.operation_id, event.title, event.start, event.end, attempt.slot, original.slot
                        FROM calendar_operations AS attempt
                        LEFT JOIN calendar_events AS event ON event.operation_id=attempt.event_id
                        LEFT JOIN calendar_operations AS original ON original.operation_id=event.operation_id
                            AND original.event_id=event.operation_id AND original.slot=event.slot
                        WHERE attempt.operation_id=?""", (operation_id,)).fetchone()
    if row is None:
        return None
    expected = (intent["title"], intent["start"], intent["end"], intent["slot"])
    if tuple(row[1:5]) != expected or row[5] is None:
        raise ValueError("已保存的日程与本次确认的内容不同。")
    return _receipt(row[:5])


def create_event(db, operation_id, intent):
    receipt = recover_event(db, operation_id, intent)
    if receipt is not None:
        return receipt
    with db:
        db.execute("""INSERT INTO calendar_events (operation_id, title, start, end, slot)
                      SELECT ?, ?, ?, ?, ? WHERE ? > ?
                      ON CONFLICT (start, end, title) DO NOTHING""",
                   (operation_id, intent["title"], intent["start"], intent["end"], intent["slot"],
                    intent["start"], time()))
        row = db.execute("""SELECT operation_id, title, start, end, slot FROM calendar_events
                            WHERE start=? AND end=? AND title=?""",
                         (intent["start"], intent["end"], intent["title"])).fetchone()
        if row is None:
            if intent["start"] <= time():
                raise ValueError("预览里的开始时间已过去，请重新安排日程。")
            raise ValueError("写入结束后无法在本机日历读回这条日程。")
        if row[4] != intent["slot"]:
            raise ValueError("已保存的日程与本次确认的内容不同。")
        receipt = _receipt(row)
        version = _event_snapshot(db, row)["version"]
        if version != row[0]:
            receipt["version"] = version
        _store_receipt(db, operation_id, intent, receipt)
    return receipt


def _archive_legacy_receipts(db, before):
    event_id = before["operation_id"]
    rows = db.execute("SELECT operation_id, slot FROM calendar_operations WHERE event_id=?", (event_id,)).fetchall()
    if not rows:
        return
    receipt = _receipt(tuple(before[key] for key in EVENT_FIELDS))
    create = {**before, "action": "calendar_create"}
    for operation_id, slot in rows:
        if slot != before["slot"]:
            raise ValueError("无法核对原日程的创建回执。")
        _store_receipt(db, operation_id, create, receipt)
    db.execute("DELETE FROM calendar_operations WHERE event_id=?", (event_id,))


def apply_mutation(db, operation_id, intent):
    action, before = intent["action"], intent["before"]
    if action not in CALENDAR_MUTATIONS:
        raise ValueError("未知的日程变更操作。")
    event_id = before["operation_id"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        if receipt := _read_receipt(db, operation_id, intent):
            return receipt
        if calendar_event_snapshot(db, event_id) != before or before["end"] <= time():
            raise ValueError(STALE_EVENT)
        _archive_legacy_receipts(db, before)
        if action == "calendar_reschedule":
            after = intent["after"]
            if after["start"] <= time():
                raise ValueError("预览里的新时间已过去，请重新改期。")
            duplicate = db.execute("SELECT 1 FROM calendar_events WHERE title=? AND start=? AND end=? AND operation_id<>?",
                                   (before["title"], after["start"], after["end"], event_id)).fetchone()
            if duplicate:
                raise ValueError("新时段已有同名日程，请核对后重新改期。")
            db.execute("UPDATE calendar_events SET start=?,end=?,slot=? WHERE operation_id=?",
                       (after["start"], after["end"], after["slot"], event_id))
            result = calendar_event_snapshot(db, event_id)
            if not result or any(result[key] != after[key] for key in after):
                raise ValueError("无法读回改期后的日程。")
        else:
            db.execute("DELETE FROM calendar_events WHERE operation_id=?", (event_id,))
            if calendar_event_snapshot(db, event_id) is not None:
                raise ValueError("无法核实日程已取消。")
            result = before
        receipt = _receipt(tuple(result[key] for key in EVENT_FIELDS))
        receipt.update(action=action, previous_slot=before["slot"], version=operation_id)
        _store_receipt(db, operation_id, intent, receipt)
    return receipt


def list_events(db, intent):
    rows = db.execute("""SELECT operation_id, title, start, end, slot FROM calendar_events
                        WHERE start < ? AND end > ? ORDER BY start, end, title, operation_id LIMIT ?""",
                      (intent["end"], intent["start"], LIST_LIMIT + 1)).fetchall()
    return [_receipt(row) for row in rows]


def calendar_reply(receipt):
    if receipt.get("action") == "calendar_reschedule":
        return f"本机日历已改期并核对「{receipt['title']}」，新时间为{receipt['slot']}。"
    if receipt.get("action") == "calendar_cancel":
        return f"本机日程「{receipt['title']}」已取消并核对，原时间为{receipt['slot']}。"
    return f"本机日历已保存并核对「{receipt['title']}」，{receipt['slot']}。"


def calendar_list_reply(intent, events):
    if not events:
        return f"{intent['detail']}没有本机日程。"
    lines = [f"{intent['detail']}的本机日程"]
    lines += [f"{item['slot']}　{item['title']}" for item in events[:LIST_LIMIT]]
    if len(events) > LIST_LIMIT:
        lines.append(f"这里只显示前 {LIST_LIMIT} 项，可以按时段查询。")
    return "\n".join(lines)
