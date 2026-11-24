"""Exercise local calendar changes, persisted receipts, and stale confirmation guards."""

import asyncio
from datetime import datetime
import json
import os
from unittest.mock import patch

from browser_lab import ROOT
from local_calendar import calendar_event_snapshot, parse_calendar, recover_event
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_local_calendar import CoreProfile


TITLE = "隔离日程演练"
CREATION = f"明天下午3点到4点安排日程：{TITLE}"
RESCHEDULE = "把刚才的日程改到后天上午10点到11点。"
CANCEL = "取消刚才的日程"


def ledger(db, operation):
    return db.execute("SELECT action, receipt FROM calendar_receipts WHERE operation_id=?", (operation,)).fetchone()


async def send(moss, text):
    operation = await moss.message(text)
    if moss.job and not moss.job.done():
        await asyncio.wait_for(moss.job, 8)
    turn = moss._get(operation)
    assert turn["source"] in {"calendar", "holo"}, turn
    public = next(row for row in moss.state()["turns"] if row["id"] == operation)
    assert public["cloud_requests"]["total"] == 0
    return turn


async def approve(moss, turn):
    assert await moss.approve(turn["id"], pending_calendar_only=True) == turn["id"]
    await asyncio.wait_for(moss.job, 8)
    return moss._get(turn["id"])


def pending(turn, action):
    assert turn["status"] == "pending" and turn["desktop_confirmation"]["action"] == action, turn
    assert not turn.get("calendar_receipt"), turn
    return turn["desktop_confirmation"]["snapshot"]


async def exercise(profile, report):
    moss = await profile.restart()
    home = moss.chat_id
    create = await send(moss, CREATION)
    create_plan = pending(create, "calendar_create")
    original = (await approve(moss, create))["calendar_receipt"]
    event_id = original["operation_id"]
    before = calendar_event_snapshot(moss.db, event_id)
    assert before["slot"] == original["slot"]

    # Convert this isolated event and a duplicate approval to the original schema.
    legacy_duplicate = "isolated-existing-duplicate"
    with moss.db:
        moss.db.execute("DELETE FROM calendar_receipts WHERE operation_id=?", (create["id"],))
        moss.db.executemany("INSERT INTO calendar_operations VALUES (?,?,?)",
                            [(operation, event_id, before["slot"]) for operation in (create["id"], legacy_duplicate)])
    assert recover_event(moss.db, create["id"], create_plan)["slot"] == before["slot"]

    move = await send(moss, RESCHEDULE)
    plan = pending(move, "calendar_reschedule")
    detail = move["desktop_confirmation"]["detail"]
    assert detail == f"{TITLE}\n原时间 {before['slot']}\n新时间 {plan['after']['slot']}"
    assert calendar_event_snapshot(moss.db, event_id) == before
    with patch.object(moss, "_start") as suppressed:
        await moss.approve(move["id"], pending_calendar_only=True)
        suppressed.assert_called_once()
    moss = await profile.restart()
    assert pending(moss._get(move["id"]), "calendar_reschedule") == plan
    assert calendar_event_snapshot(moss.db, event_id) == before

    launch = moss._start
    checkpoint = None

    def record_checkpoint(turn, **kwargs):
        nonlocal checkpoint
        checkpoint = moss.db.execute("SELECT doc, pending FROM turns WHERE id=?", (turn["id"],)).fetchone()
        launch(turn, **kwargs)

    with patch.object(moss, "_start", side_effect=record_checkpoint):
        moved = await approve(moss, move)
    assert moved["calendar_receipt"]["previous_slot"] == before["slot"]
    assert calendar_event_snapshot(moss.db, event_id)["version"] == move["id"]
    assert recover_event(moss.db, legacy_duplicate, create_plan)["slot"] == before["slot"]
    assert not moss.db.execute("SELECT * FROM calendar_operations WHERE event_id=?", (event_id,)).fetchall()
    assert json.loads(checkpoint[0])["status"] == "saving"
    with moss.db:
        moss.db.execute("UPDATE turns SET doc=?, pending=? WHERE id=?", (*checkpoint, move["id"]))
    moss = await profile.restart()
    recovered = moss._get(move["id"])["calendar_receipt"]
    assert recovered == {**moved["calendar_receipt"], "recovered": True}
    assert await moss.approve(move["id"]) == move["id"]
    report["cases"].append({"case": "preview and approval across restart", "move": recovered, "legacy_receipts_preserved":True})

    assert TITLE not in (await send(moss, "明天有哪些日程"))["reply"]
    read = await send(moss, "后天有哪些日程")
    assert read["_calendar_listing_target"]["version"] == move["id"] and TITLE in read["reply"]
    stale = await send(moss, CANCEL)
    expected_stale = pending(stale, "calendar_cancel")["before"]
    assert expected_stale == read["_calendar_listing_target"]

    await moss.new_chat()
    for after in ("明天下午3点到4点", "后天上午10点到11点"):
        shifted = await send(moss, f"把日程：{TITLE}改到{after}")
        pending(shifted, "calendar_reschedule")
        assert (await approve(moss, shifted))["calendar_receipt"]
    live = calendar_event_snapshot(moss.db, event_id)
    assert live["slot"] == expected_stale["slot"] and live["version"] != expected_stale["version"]
    try:
        await moss.approve(stale["id"])
    except ValueError:
        pass
    else:
        raise AssertionError("另一对话接受了日程预览")
    await moss.switch_chat(home)
    rejected = await approve(moss, stale)
    assert rejected["status"] == "error" and "预览后已变化" in rejected["error"], rejected
    assert calendar_event_snapshot(moss.db, event_id) == live and ledger(moss.db, stale["id"]) is None
    report["cases"].append({"case": "stale preview after same-slot round trip", "cross_chat_rejected":True})

    assert TITLE in (await send(moss, "后天有哪些日程"))["reply"]
    unrelated = await send(moss, "现在几点")
    assert unrelated.get("holo_tools") == ["get_time"]
    unfocused = await send(moss, CANCEL)
    assert unfocused["status"] == "replied" and not unfocused.get("desktop_confirmation")

    duplicate = await send(moss, f"后天下午3点到4点安排日程：{TITLE}")
    duplicated = (await approve(moss, duplicate))["calendar_receipt"]
    ambiguous = await send(moss, "后天有哪些日程")
    assert ambiguous["reply"].count(TITLE) == 2 and not ambiguous.get("_calendar_listing_target")
    assert not (await send(moss, CANCEL)).get("desktop_confirmation")
    assert "多条同名" in (await send(moss, f"取消日程：{TITLE}"))["reply"]

    conflict = await send(moss, f"把后天上午的{TITLE}日程改到后天下午3点到4点")
    pending(conflict, "calendar_reschedule")
    failed = await approve(moss, conflict)
    assert failed["status"] == "error" and "同名" in failed["error"]
    assert calendar_event_snapshot(moss.db, event_id) == live

    extension = await send(moss, f"后天上午10点到12点安排日程：{TITLE}")
    extended = (await approve(moss, extension))["calendar_receipt"]
    assert "多条同名" in (await send(moss, f"取消后天上午十点的{TITLE}日程"))["reply"]
    range_choice = await send(moss, f"取消后天上午十点到十二点的{TITLE}日程")
    assert pending(range_choice, "calendar_cancel")["before"]["operation_id"] == extended["operation_id"]
    assert (await approve(moss, range_choice))["calendar_receipt"]
    start_choice = await send(moss, f"取消后天上午十点的{TITLE}日程")
    assert pending(start_choice, "calendar_cancel")["before"]["operation_id"] == event_id
    await moss.cancel(start_choice["id"], pending_calendar_only=True)
    report["cases"].append({"case": "recent context scope and ambiguous names", "destination_kept":True,
                            "exact_start_and_range":True})

    removal = await send(moss, f"取消后天上午的{TITLE}日程")
    plan = pending(removal, "calendar_cancel")
    assert plan["before"] == live and calendar_event_snapshot(moss.db, event_id) == live
    await moss.cancel(removal["id"], pending_calendar_only=True)
    assert calendar_event_snapshot(moss.db, event_id) == live
    removal = await send(moss, f"取消后天上午的{TITLE}日程")
    removed = await approve(moss, removal)
    assert removed["calendar_receipt"]["action"] == "calendar_cancel"
    assert calendar_event_snapshot(moss.db, event_id) is None
    assert calendar_event_snapshot(moss.db, duplicated["operation_id"]) is not None
    assert recover_event(moss.db, create["id"], create_plan)["slot"] == before["slot"]
    assert recover_event(moss.db, move["id"], pending(move, "calendar_reschedule"))["slot"] == live["slot"]
    assert await moss.approve(removal["id"]) == removal["id"]
    assert TITLE not in (await send(moss, "后天上午有哪些日程"))["reply"]
    recreated = await send(moss, f"后天上午10点到11点安排日程：{TITLE}")
    recreated_receipt = (await approve(moss, recreated))["calendar_receipt"]
    assert recreated_receipt["operation_id"] != event_id and recreated_receipt["slot"] == live["slot"]
    assert not moss.db.in_transaction and not moss.db.execute("PRAGMA foreign_key_check").fetchall()
    assert parse_calendar("取消全部提醒") is None and parse_calendar("不要取消刚才的日程") is None
    report["cases"].append({"case": "confirmed cancellation and clean recreation", "removed": removed["calendar_receipt"],
                            "recreated":recreated_receipt})


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("calendar-mutations-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result, flush=True)
    report = {"passed": False, "model_connections": 0, "cases": []}
    catalog = folder / "empty.sqlite3"
    MediaLibrary(catalog)

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            profile = CoreProfile(folder / "isolated-profile", model_url)
            env = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_MEDIA_DB":str(catalog),
                   "MOSS_PROVIDER_DIR":str(folder / "providers"), "MOSS_MEM0_ENABLED":"0",
                   "MOSS_WINDOWS_REMINDERS":"0", "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_STANDBY":"0"}
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    env.update({prefix + "_BASE_URL":model_url, prefix + "_API_KEY":"isolated-qa"})
            with patch.dict(os.environ, env):
                try:
                    await exercise(profile, report)
                finally:
                    await profile.close()
            assert not report["model_connections"], report
            report["passed"] = True
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(run())
