import asyncio
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
import re
import sqlite3
from time import perf_counter, time
from unittest.mock import patch

from aiohttp import web

from assistant_timers import TimerStore, parse_timer, query_window, scheduled_epoch
from browser_lab import BrowserServer, ROOT
from local_media.library import MediaLibrary
from model_catalog import SPECS


def saved_rows(path):
    # 只读连接，检查查询前后每条提醒及通知状态均不变。
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT * FROM assistant_timers ORDER BY id").fetchall()


def verify_legacy_reminder():
    with closing(sqlite3.connect(":memory:")) as db, db:
        db.execute("CREATE TABLE assistant_timers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "label TEXT NOT NULL, due_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open', "
                   "chat_id TEXT, turn_id TEXT, media TEXT, notified_at REAL)")
        db.execute("INSERT INTO assistant_timers (label,due_at,chat_id,turn_id) VALUES (?,?,?,?)",
                   ("检查旧备份", time()-2, "legacy-chat", "legacy-turn"))
        reminders = TimerStore(db)
        assert reminders.notification(1, delivered=True)["delivered"] is True
        old = reminders.snapshot()[0]
        assert "已改期 1 号提醒" in reminders.execute(parse_timer("把1号提醒改到五分钟后"), chat_id="legacy-chat")
        current = reminders.snapshot()[0]
        assert {**old, "due_at":current["due_at"]} == current
        with patch("assistant_timers.time", return_value=current["due_at"] + 1):
            assert reminders.notification(1, delivered=True) is None
            assert reminders.notification(1, generation=1)["delivered"] is False
            assert reminders.notification(1, delivered=True, generation=1)["delivered"] is True
            assert reminders.notification(1, generation=1)["delivered"] is True


def verify_midnight_clock():
    morning = datetime(2026, 12, 31, 9, 0)
    late = datetime(2026, 12, 31, 23, 59, 59)
    cases = (
        ("今晚12点提醒我交接", morning, datetime(2027, 1, 1, 0, 0)),
        ("晚上十二点提醒我交接", late, datetime(2027, 1, 1, 0, 0)),
        ("今晚0点提醒我交接", late, datetime(2027, 1, 1, 0, 0)),
        ("明晚十二点半提醒我交接", morning, datetime(2027, 1, 2, 0, 30)),
        ("明天上午12点提醒我交接", morning, datetime(2027, 1, 1, 12, 0)),
        ("明天中午12点提醒我交接", morning, datetime(2027, 1, 1, 12, 0)),
        ("明天凌晨12点提醒我交接", morning, datetime(2027, 1, 1, 0, 0)),
        ("把刚才的提醒改到明晚12点", morning, datetime(2027, 1, 2, 0, 0)),
    )
    results = []
    for text, current, expected in cases:
        intent = parse_timer(text)
        previous = (current + timedelta(hours=1)).timestamp() if intent["action"] == "reschedule" else None
        due = datetime.fromtimestamp(scheduled_epoch(intent, current, previous=previous))
        assert due == expected, (text, due, expected)
        results.append({"text":text, "current":current.isoformat(), "due":due.isoformat()})
    for text in ("今晚24:00提醒我交接", "今晚12点60分提醒我交接", "今天中午12点提醒我交接"):
        try:
            scheduled_epoch(parse_timer(text), late)
        except ValueError:
            pass
        else:
            raise AssertionError(text)
    overnight = parse_timer("明晚十一点到十二点的提醒有哪些？")
    first, last, _ = query_window(overnight["window"], morning)
    assert (first, last) == (datetime(2027, 1, 1, 23), datetime(2027, 1, 2))
    return results


def verify_calendar_clock():
    sunday = datetime(2026, 11, 22, 17)
    monday = datetime(2026, 11, 23, 17)
    new_year_eve = datetime(2026, 12, 31, 9)
    fixed = (
        ("下周三上午九点提醒我读报告", sunday, datetime(2026, 11, 25, 9)),
        ("下星期一九点提醒我交接", sunday, datetime(2026, 11, 23, 9)),
        ("下礼拜一九点提醒我交接", monday, datetime(2026, 11, 30, 9)),
        ("星期一六点提醒我关机", monday, datetime(2026, 11, 30, 6)),
        ("周一18:00提醒我签字", monday, datetime(2026, 11, 23, 18)),
        ("本周日晚上十点提醒我锁门", sunday, datetime(2026, 11, 22, 22)),
        ("12月3日下午三点提醒我交接", sunday, datetime(2026, 12, 3, 15)),
        ("一月二号九点提醒我收书", new_year_eve, datetime(2027, 1, 2, 9)),
        ("二〇二七年一月二号下午三点提醒我到访", new_year_eve, datetime(2027, 1, 2, 15)),
        ("2027/01/02 15:36提醒我签字", new_year_eve, datetime(2027, 1, 2, 15, 36)),
        ("2月29号上午10点提醒我复查", sunday, datetime(2028, 2, 29, 10)),
    )
    results = []
    for text, current, expected in fixed:
        actual = datetime.fromtimestamp(scheduled_epoch(parse_timer(text), current))
        assert actual == expected, (text, actual, expected)
        results.append({"text":text, "due":actual.isoformat()})
    moved = parse_timer("把那个提醒改到下周五")
    previous = datetime(2026, 11, 22, 19, 12, 38, 200000)
    actual = datetime.fromtimestamp(scheduled_epoch(moved, sunday, previous=previous.timestamp()))
    assert actual == previous.replace(day=27)
    results.append({"text":"把那个提醒改到下周五", "due":actual.isoformat()})
    for text in (
        "2026年11月20日九点提醒我签收", "本周日早上九点提醒我签收", "上周五10点提醒我签到",
        "2027-02-29 10:00提醒我交接", "四月三十一号10:00提醒我交接", "0000-01-01 10:00提醒我交接",
    ):
        intent = parse_timer(text)
        assert intent and intent["action"] == "create", text
        try:
            scheduled_epoch(intent, sunday)
        except ValueError:
            results.append({"text":text, "refused":True})
        else:
            raise AssertionError(text)
    for text, begin, end in (
        ("下周有哪些提醒？", datetime(2026, 11, 23), datetime(2026, 11, 30)),
        ("下周三我有什么提醒？", datetime(2026, 11, 25), datetime(2026, 11, 26)),
        ("2027-01-02上午的提醒", datetime(2027, 1, 2, 6), datetime(2027, 1, 2, 12)),
        ("明晚十一点到一点的提醒", datetime(2027, 1, 1, 23), datetime(2027, 1, 2, 1)),
        ("明晚十一点到01:00的提醒", datetime(2027, 1, 1, 23), datetime(2027, 1, 2, 1)),
        ("明晚十一点到13:00的提醒", datetime(2027, 1, 1, 23), datetime(2027, 1, 2, 13)),
        ("明晚十一点到凌晨一点的提醒", datetime(2027, 1, 1, 23), datetime(2027, 1, 2, 1)),
        ("明晚八点到九点的提醒", datetime(2027, 1, 1, 20), datetime(2027, 1, 1, 21)),
    ):
        a, b, _ = query_window(parse_timer(text)["window"], new_year_eve if text.startswith("明晚") else sunday)
        assert (a, b) == (begin, end), (text, a, b)
        results.append({"text":text, "start":a.isoformat(), "end":b.isoformat()})
    assert parse_timer("下周三九点提醒我继续听这首歌")["label"] == "继续听这首歌"
    assert parse_timer("下周三九点提醒我核对清单、签字")["label"] == "核对清单、签字"
    assert parse_timer("下周三九点提醒我交接，再帮我取消所有提醒")["action"] == "invalid"
    assert parse_timer("明天九点提醒我交接。再帮我关掉闹钟")["action"] == "invalid"
    assert parse_timer("如果我说下周三九点提醒我开会，会怎么样？") is None
    return results


async def chat_scopes(server, message, create, report):
    path = server.data / "session.sqlite3"
    empty = "这段对话还没有设置过提醒。"

    async def switch(chat=None):
        status, state = await (server.post("/api/switch-chat", {"id":chat}) if chat else
                               server.post("/api/new-chat", {}))
        assert status == 200 and (not chat or state["chat_id"] == chat)
        return state["chat_id"]

    def ownership():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return {key:(chat, status) for key, chat, status in db.execute(
                "SELECT id,chat_id,status FROM assistant_timers ORDER BY id")}

    async def pending():
        return (await server.state())["timers"]

    chat_a = (await server.state())["chat_id"]
    a = await create("2小时后提醒我检查茶水", chat_a)
    chat_b = await switch()
    b = await create("3小时后提醒我检查书架", chat_b)
    chat_c = await switch()
    assert len({chat_a, chat_b, chat_c}) == 3 and (a, b) == (1, 2)
    before = await pending()
    assert await message("上一个提醒还剩多久？", chat_c) == empty
    assert await message("取消刚才的提醒", chat_c) == empty
    assert await message("取消提醒", chat_c) == "这段对话没有可以取消的提醒。"
    assert await message("取消下一个提醒", chat_c) == "这段对话没有尚未到时间的提醒。"
    assert await pending() == before
    listed = await message("查看全部提醒", chat_c)
    assert all(f"{key} 号 ·" in listed for key in (a, b))
    assert f"{b} 号提醒「检查书架」还有" in await message(f"{b}号提醒还有多久？", chat_c)
    assert await message("这个提醒还剩多久？", chat_c) == empty
    await switch(chat_a)
    assert f"{a} 号提醒「检查茶水」还有" in await message("上一个提醒还剩多久？", chat_a)
    assert await message("取消刚才设置的所有提醒", chat_a) == "请指定想取消的提醒编号。"
    assert await pending() == before
    assert f"已取消 {a} 号提醒" in await message("取消刚才的提醒", chat_a)
    assert [(row["id"], row["chat_id"]) for row in await pending()] == [(b, chat_b)]

    earlier = await create("4小时后提醒我整理照片", chat_a)
    later = await create("7小时后提醒我检查窗帘", chat_a)
    latest = await create("5小时后提醒我收拾桌面", chat_a)
    await switch(chat_b)
    due = await create("0.1秒后提醒我记录时间", chat_b)
    before_restart = await pending()
    old_pid = server.pid
    await server.stop()
    checkpoint = ownership()
    assert checkpoint == {a:(chat_a, "cancelled"), b:(chat_b, "open"), earlier:(chat_a, "open"), later:(chat_a, "open"),
                          latest:(chat_a, "open"), due:(chat_b, "open")}
    await server.start()
    assert server.pid != old_pid and await pending() == before_restart
    await switch(chat_a)
    assert f"{latest} 号提醒「收拾桌面」还有" in await message("刚才设置的提醒还剩多久？", chat_a)
    assert f"已取消 {latest} 号提醒" in await message("取消提醒", chat_a)
    after_cancel = await pending()
    assert await message("这个提醒还剩多久？", chat_a) == f"{latest} 号提醒已取消。"
    assert await message("取消刚才设置的提醒", chat_a) == f"{latest} 号提醒已取消。"
    assert await pending() == after_cancel
    assert f"已取消 {earlier} 号提醒" in await message("取消下一个提醒", chat_a)
    assert {item["id"] for item in await pending()} == {b, later, due}
    await switch(chat_b)
    state = await server.state()
    due_at = next(item["due_at"] for item in state["timers"] if item["id"] == due)
    await asyncio.sleep(max(0, due_at - time()))
    assert (await server.post("/api/timer-dismiss", {"id":due})) == (200, {"ok":True})
    after_dismiss = await pending()
    assert await message("上一个提醒还剩多久？", chat_b) == f"{due} 号提醒已处理。"
    assert await message("取消刚才的提醒", chat_b) == f"{due} 号提醒已处理。"
    assert await pending() == after_dismiss
    await switch(chat_c)
    assert f"已取消 {b} 号提醒" in await message(f"取消提醒 {b}", chat_c)
    assert await message("取消刚才的提醒", chat_c) == empty
    listed = await message("提醒列表", chat_c)
    assert f"{later} 号 ·" in listed and "共有 1 个" in listed
    expired = await create("0.1秒后提醒我检查到时边界", chat_c)
    before_expired = await pending()
    due_at = next(item["due_at"] for item in before_expired if item["id"] == expired)
    await asyncio.sleep(max(0, due_at - time()))
    assert await message("取消下一个提醒", chat_c) == "这段对话没有尚未到时间的提醒。"
    assert await pending() == before_expired
    assert f"已取消 {expired} 号提醒" in await message("取消提醒", chat_c)

    await switch(chat_a)
    named_a = await create("2小时后提醒我检查标签", chat_a)
    newer_a = await create("3小时后提醒我检查标签", chat_a)
    await switch(chat_b)
    named_b = await create("4小时后提醒我检查标签", chat_b)
    await switch(chat_c)
    before_named = await pending()
    reply = await message("取消检查标签的提醒", chat_c)
    assert reply == f"「检查标签」有多个待处理的提醒（{named_a} 号、{newer_a} 号、{named_b} 号），请指定编号。"
    assert await pending() == before_named
    await switch(chat_a)
    assert f"已取消 {newer_a} 号提醒" in await message("取消检查标签的提醒", chat_a)
    assert {item["id"] for item in await pending()} == {later, named_a, named_b}
    assert f"已取消 {named_a} 号提醒" in await message("取消检查标签的提醒", chat_a)
    await switch(chat_c)
    assert f"已取消 {named_b} 号提醒" in await message("取消检查标签的提醒", chat_c)
    assert {item["id"] for item in await pending()} == {later}
    power = await create("8小时后提醒我检查电源", chat_c)
    await switch(chat_b)
    assert await message("取消全部提醒", chat_b) == "已取消全部 2 个提醒。"
    assert await pending() == []
    await server.stop()
    final = ownership()
    assert final == {a:(chat_a, "cancelled"), b:(chat_b, "cancelled"), earlier:(chat_a, "cancelled"),
                     later:(chat_a, "cancelled"), latest:(chat_a, "cancelled"), due:(chat_b, "seen"),
                     expired:(chat_c, "cancelled"), named_a:(chat_a, "cancelled"),
                     newer_a:(chat_a, "cancelled"), named_b:(chat_b, "cancelled"), power:(chat_c, "cancelled")}
    report["chat_scopes"] = {"A":chat_a, "B":chat_b, "C":chat_c, "before_restart":checkpoint, "reminders":final,
                             "sqlite":str(path), "restarts":1}


async def reschedule_cases(server, message, create, report):
    path = server.data / "session.sqlite3"

    def generations():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return {key: (generation, notified) for key, generation, notified in db.execute(
                "SELECT id,generation,notified_at FROM assistant_timers ORDER BY id")}

    async def open_rows():
        return {row["id"]: row for row in (await server.state())["timers"]}

    chat_a = (await server.state())["chat_id"]
    first = await create("2小时后提醒我检查茶水", chat_a)
    recent = await create("4小时后提醒我检查书架", chat_a)
    status, state = await server.post("/api/new-chat", {})
    assert status == 200
    chat_b = state["chat_id"]
    other = await create("1小时后提醒我检查茶水", chat_b)
    before = await open_rows()
    assert (await server.post("/api/switch-chat", {"id":chat_a}))[0] == 200
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    move = "把刚才的提醒改到明早九点"
    assert f"已改期 {recent} 号提醒" in await message(move, chat_a)
    after = await open_rows()
    assert set(after) == set(before) and after[first] == before[first] and after[other] == before[other]
    assert after[recent] == {**before[recent], "due_at":tomorrow}
    assert "已经" in await message(move, chat_a)
    assert await open_rows() == after
    assert "时间无效" in await message(f"把{recent}号提醒改到明天二十五点", chat_a)
    assert await open_rows() == after
    shifted = min((first, recent), key=lambda key: after[key]["due_at"])
    assert f"已改期 {shifted} 号提醒" in await message("把下一个提醒推迟半小时", chat_a)
    latest = await open_rows()
    assert latest[shifted]["due_at"] == after[shifted]["due_at"] + 1800
    assert all(latest[key] == after[key] for key in (first, recent, other) if key != shifted)
    status, state = await server.post("/api/new-chat", {})
    assert status == 200
    chat_c = state["chat_id"]
    assert "这段对话还没有设置过提醒" in await message("把刚才的提醒改到明晚七点", chat_c)
    assert await open_rows() == latest
    assert "多个" in await message("把检查茶水的提醒改到明晚七点", chat_c)
    assert await open_rows() == latest
    assert f"已改期 {other} 号提醒" in await message(f"把{other}号提醒改到二十分钟后", chat_c)
    moved = await open_rows()
    assert moved[other]["chat_id"] == chat_b and moved[other]["turn_id"] == before[other]["turn_id"]
    assert 1195 <= moved[other]["due_at"] - time() <= 1201
    for text in ("不要把刚才的提醒改到明早九点", "怎么把刚才的提醒改到明早九点？",
                 "把「取消检查茶水的提醒」翻译成英语", "把刚才的提醒改到明早九点，再帮我取消所有提醒"):
        assert parse_timer(text) is None, text
    scheduled = await create("0.1秒后提醒我等通知", chat_c)
    await asyncio.sleep(.12)
    token = json.loads((server.data / "instance.json").read_text(encoding="utf-8"))["token"]
    status, receipt = await server.post("/api/timer-notification", {"token":token, "id":scheduled, "delivered":True})
    assert status == 200 and receipt["timer"]["delivered"] is True
    assert f"已改期 {scheduled} 号提醒" in await message("把刚才的提醒改到十五分钟后", chat_c)
    checkpoint = await open_rows()
    old_pid = server.pid
    await server.stop()
    assert generations() == {key:(1, None) for key in (first, recent, other, scheduled)}
    await server.start()
    assert server.pid != old_pid and await open_rows() == checkpoint
    assert f"已取消 {scheduled} 号提醒" in await message(f"取消提醒{scheduled}", chat_c)
    assert "没有找到" in await message(f"把{scheduled}号提醒改到明早九点", chat_c)
    assert (await open_rows()) == {key:row for key,row in checkpoint.items() if key != scheduled}
    report["reschedule"] = {"kept_original_identity":True, "same_time_idempotent":True, "restarts":1,
                            "already_notified_reopened":True, "cross_chat_scope_preserved":True}


async def midnight_cases(server, message, create, report):
    status, state = await server.post("/api/new-chat", {})
    assert status == 200
    chat = state["chat_id"]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    async def records():
        return {row["id"]:row for row in (await server.state())["timers"] if row["chat_id"] == chat}

    midnight = await create("今晚12点提醒我核对零点", chat)
    noon = await create("明天上午12点提醒我核对中午", chat)
    before = await records()
    assert before[midnight]["due_at"] == (today + timedelta(days=1)).timestamp()
    assert before[noon]["due_at"] == (today + timedelta(days=1, hours=12)).timestamp()

    assert f"已改期 {midnight} 号提醒" in await message(f"把{midnight}号提醒改到明晚12点17分", chat)
    after = await records()
    due = today + timedelta(days=2, minutes=17)
    assert after == {**before, midnight:{**before[midnight], "due_at":due.timestamp()}}
    listing = await message("后天00:00到01:00有哪些提醒？", chat)
    assert f"{midnight} 号 ·" in listing and f"{noon} 号 ·" not in listing
    assert "提醒时间无效" in await message(f"把{midnight}号提醒改到今晚12点60分", chat)
    assert await records() == after
    report["midnight_http"] = {"created":datetime.fromtimestamp(before[midnight]["due_at"]).isoformat(),
                               "rescheduled":due.isoformat(), "noon":datetime.fromtimestamp(before[noon]["due_at"]).isoformat(),
                               "original_id_preserved":True, "invalid_change_rejected":True}


async def calendar_cases(server, message, create, report):
    status, state = await server.post("/api/new-chat", {})
    assert status == 200
    chat = state["chat_id"]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today + timedelta(days=7-today.weekday())
    wednesday = monday + timedelta(days=2, hours=9)
    friday = wednesday + timedelta(days=2)
    saturday = monday + timedelta(days=5, hours=14, minutes=35)
    outside = monday + timedelta(days=7, hours=10)

    async def records():
        return {item["id"]:item for item in (await server.state())["timers"] if item["chat_id"] == chat}

    weekday = await create("下周三上午九点提醒我核对日历记录", chat)
    first = (await records())[weekday]
    assert first["due_at"] == wednesday.timestamp()
    assert f"{weekday} 号 ·" in await message("下周三我有什么提醒？", chat)
    assert f"已改期 {weekday} 号提醒" in await message("把那个提醒改到下周五", chat)
    assert await records() == {weekday:{**first, "due_at":friday.timestamp()}}
    assert f"{weekday} 号 ·" not in await message("下周三的提醒", chat)
    date_key = await create(f"{saturday:%Y-%m-%d} 14:35提醒我签收日历测试件", chat)
    upper_key = await create(f"{outside.year:04d}年{outside.month:02d}月{outside.day:02d}日上午十点提醒我查下一周", chat)
    assert (await records())[date_key]["due_at"] == saturday.timestamp()
    assert (await records())[upper_key]["due_at"] == outside.timestamp()
    result = await message("下周有哪些提醒？", chat)
    assert all(f"{key} 号 ·" in result for key in (weekday, date_key)) and f"{upper_key} 号 ·" not in result
    result = await message(f"{saturday:%Y-%m-%d} 14:35到15:00的提醒有哪些？", chat)
    assert f"{date_key} 号 ·" in result and f"{weekday} 号 ·" not in result
    before = await records()
    yesterday = today - timedelta(days=1)
    assert "已经过去" in await message(f"{yesterday:%Y-%m-%d} 14:35提醒我检查错误日期", chat)
    assert "已经过去" in await message(f"把{weekday}号提醒改到{yesterday:%Y-%m-%d}", chat)
    assert "日期无效" in await message("2027年2月29日10:00提醒我检查错误闰日", chat)
    assert "两项提醒操作" in await message("下周三上午九点提醒我检查隔离状态，再帮我取消所有提醒", chat)
    assert await records() == before
    pid = server.pid
    await server.stop()
    await server.start()
    assert pid != server.pid and await records() == before
    assert f"{weekday} 号 ·" in await message("下周五的提醒有哪些？", chat)
    report["calendar_http"] = {"created":wednesday.isoformat(), "moved":friday.isoformat(),
                               "iso_date":saturday.isoformat(), "same_id":weekday,
                               "verified_restart":True, "past_and_compound_writes_unchanged":True}


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("reminder-queries-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "cases": [], "model_requests": [], "windows_notifications_enabled": False}
    test_clock = datetime.now()
    report["local_date"] = test_clock.astimezone().isoformat()
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0",
                      MOSS_PROVIDER_DIR=str(folder / "providers"), LOCALAPPDATA=str(folder / "local-profile"))
    for _, prefixes, _, _ in SPECS.values():
        for prefix in prefixes:
            os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"

    async def model_guard(request):
        report["model_requests"].append(request.path)
        return web.Response(status=503)

    guard = web.Application()
    guard.router.add_route("*", "/{path:.*}", model_guard)
    runner = web.AppRunner(guard)
    await runner.setup()
    model_site = web.TCPSite(runner, "127.0.0.1", 0)
    await model_site.start()
    _, model_port = runner.addresses[0]
    model_url = f"http://127.0.0.1:{model_port}/v1"
    server = BrowserServer(folder / "chat-scopes", 18968, model_url=model_url)

    async def message(text, chat=None):
        start = perf_counter()
        body = {"text":text, "chat_id":chat} if chat else {"text":text}
        status, state = await server.post("/api/message", body)
        assert status == 200
        turn = state["turns"][-1]
        row = {"text": text, "reply": turn.get("reply"), "source": turn.get("source"),
               "status":turn["status"], "chat_id":turn["chat_id"], "cloud_requests":turn["cloud_requests"],
               "elapsed_ms": round((perf_counter() - start) * 1000, 2)}
        report["cases"].append(row)
        assert row["source"] == "timer" and row["status"] == "replied", row
        assert row["cloud_requests"] == {"total":0, "by_provider":{}, "complete":True}
        assert not chat or chat == state["chat_id"] == turn["chat_id"]
        return row["reply"]

    async def create(text, chat=None):
        reply = await message(text, chat)
        number = re.search(r"已设置 (\d+) 号提醒", reply)
        assert number, reply
        return int(number[1])

    try:
        verify_legacy_reminder()
        report["legacy_schema_migration"] = True
        report["midnight_fixed_clock"] = verify_midnight_clock()
        report["calendar_fixed_clock"] = verify_calendar_clock()
        await server.start()
        await chat_scopes(server, message, create, report)
        server = BrowserServer(folder / "reschedule", 18968, model_url=model_url)
        await server.start()
        await reschedule_cases(server, message, create, report)
        await midnight_cases(server, message, create, report)
        await calendar_cases(server, message, create, report)
        await server.stop()
        server = BrowserServer(folder, 18968, model_url=model_url)
        await server.start()
        initial_pid = server.pid
        assert "目前没有待处理的提醒" in await message("提醒还有多久？")
        if test_clock.date() != (test_clock + timedelta(seconds=300)).date():
            raise RuntimeError("当前接近日界，下一轮需要使用跨日测试的独立日期样本。")
        due = await create("0.1秒后提醒我完成快速验收")
        nearest = await create("5分钟后提醒我检查本机索引")
        earliest_tomorrow = await create("明天00:10提醒我核对日界前后的提醒")
        nine = await create("明天09:00提醒我复查工程进度")
        noon = await create("明天12:00提醒我检查时段结束边界")
        midnight = await create("明天23:30提醒我复查跨日样本")
        following = await create("后天01:00提醒我检查跨日样本")
        cancelled = await create("后天23:30提醒我执行取消验收")
        assert f"已取消 {cancelled} 号提醒" in await message(f"取消提醒 {cancelled}")
        await server.stop()
        before = saved_rows(server.data / "session.sqlite3")
        await server.start()
        assert initial_pid != server.pid

        result = await message("今天还有哪些提醒？")
        assert "检查本机索引" in result and "完成快速验收" in result and "工程进度" not in result
        assert "已到时间" in result
        result = await message("我明天有什么提醒？")
        for key in (earliest_tomorrow, nine, noon, midnight):
            assert f"{key} 号 ·" in result
        assert f"{following} 号 ·" not in result
        result = await message("明天的提醒有哪些？")
        assert "明天有 4 个待处理的提醒" in result
        result = await message("查看明天上午的提醒")
        assert f"{nine} 号 ·" in result and all(f"{key} 号 ·" not in result for key in (earliest_tomorrow, noon, midnight))
        result = await message("明天09:00到12:00还有哪些提醒？")
        assert f"{nine} 号 ·" in result and all(f"{key} 号 ·" not in result for key in (earliest_tomorrow, noon, midnight))
        assert result == await message("明天上午九点到十二点有什么提醒？")
        result = await message("明天23:00到02:00有哪些提醒？")
        assert f"{midnight} 号 ·" in result and f"{following} 号 ·" in result and f"{earliest_tomorrow} 号 ·" not in result
        assert result == await message("明晚十一点至凌晨两点的提醒有哪些？")
        assert "两个不同的有效时刻" in await message("明天09:00到09:00还有哪些提醒？")
        assert f"{nearest} 号提醒「检查本机索引」还有" in await message("提醒还有多久？")
        assert f"{earliest_tomorrow} 号提醒「核对日界前后的提醒」还有" in await message(f"{earliest_tomorrow}号提醒还有多久？")
        assert "已到时间" in await message(f"{due}号提醒还有多久？")
        assert "已取消" in await message(f"{cancelled}号提醒还有多久？")
        assert f"{cancelled} 号提醒已取消" in await message("上一个提醒还剩多久？")
        assert "找不到" in await message("9999号提醒还有多久？")

        report["non_query_boundaries"] = {}
        for text in ("今天的提醒有哪些然后帮我取消它", "明天有什么提醒？另外查一下天气",
                     "提醒这两个字怎么解释", "介绍日历", "搜索音乐《明天的提醒》", "这个闹钟响了意味着什么"):
            assert parse_timer(text) is None, text
            report["non_query_boundaries"][text] = "未接管"
        await server.stop()
        assert saved_rows(server.data / "session.sqlite3") == before
        prior_pid = server.pid
        await server.start()
        assert prior_pid != server.pid
        result = await message("明天23:00到02:00有哪些提醒？")
        assert f"{midnight} 号 ·" in result and f"{following} 号 ·" in result
        assert "已取消" in await message(f"{cancelled}号提醒还有多久？")
        await server.stop()
        assert saved_rows(server.data / "session.sqlite3") == before
        assert not report["model_requests"]
        report.update(passed=True, reminder_rows_unchanged=True, restarts=2)
    finally:
        await server.stop()
        await runner.cleanup()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
