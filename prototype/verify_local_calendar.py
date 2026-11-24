import asyncio
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
import sqlite3
from unittest.mock import patch

from aiohttp import web

from backup_profile import backup, restore
from browser_lab import BrowserServer, ROOT
from core import Moss
from local_calendar import create_event, init_calendar, parse_calendar, recover_event
from local_overview import parse_local_overview
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_context_reply import free_port


def calendar_rows(db):
    return db.execute("SELECT * FROM calendar_events ORDER BY 1").fetchall()


def request_text(title):
    return f"明天上午10点到11点安排日程：{title}"


def assert_preview(turn, title):
    proposal = turn.get("desktop_confirmation") or {}
    assert turn["status"] == "pending" and proposal.get("action") == "calendar_create", turn
    assert title in proposal["detail"] and not turn.get("calendar_receipt"), turn


def assert_readback(turn, title):
    receipt = turn.get("calendar_receipt")
    assert turn["status"] == "replied" and receipt and title in turn["reply"], turn
    return receipt


def assert_receipt_row(receipt, row, *, recovered=False):
    assert tuple(receipt[field] for field in ("operation_id", "title", "start", "end", "slot")) == row, receipt
    assert receipt["recovered"] is recovered, receipt
    first = datetime.fromtimestamp(receipt["start"])
    last = datetime.fromtimestamp(receipt["end"])
    assert (first.hour, first.minute, last.hour, last.minute, first.date()) == (10, 0, 11, 0, last.date()), receipt


def assert_created(db, old_rows, operation, title):
    rows = calendar_rows(db)
    added = [row for row in rows if row not in old_rows]
    assert len(rows) == len(old_rows) + 1 and len(added) == 1, (operation, rows)
    assert operation in added[0] and title in added[0], (operation, added)
    assert all(row in rows for row in old_rows), (operation, old_rows, rows)
    return added[0]


def verify_expiry(report):
    current = datetime.now()
    expired = parse_calendar("今天上午10点到11点安排日程：过期预览",
                             current=(current - timedelta(days=2)).replace(hour=8))
    assert expired["action"] == "calendar_create"
    with closing(sqlite3.connect(":memory:")) as db:
        init_calendar(db)
        try:
            create_event(db, "expired", expired)
        except ValueError as exc:
            assert "已过去" in str(exc), exc
        else:
            raise AssertionError("过期预览写入了日历")
        assert not calendar_rows(db) and recover_event(db, "expired", expired) is None
    report["expired_preview_rejected"] = True


def verify_compound_routing(report):
    spoken = "现在几点、电脑还有多少电、音量多大，明天有什么日程？"
    overview = parse_local_overview(spoken)
    assert [query["kind"] for query in overview["queries"]] == ["clock", "battery", "volume", "calendar"]
    excluded = (
        "明天上午10点到11点安排日程：产品评审，顺便看看现在几点",
        "查看明天的日程，顺便把音量调到50%",
        "看看明天的日程，顺便帮我写封邮件",
        "不要查明天的日程，告诉我现在几点",
        "把‘查看明天的日程’翻译成英文，再说下现在几点",
    )
    assert all(parse_local_overview(text) is None for text in excluded)
    report["compound_routing"] = {"split_kinds": [query["kind"] for query in overview["queries"]],
                                   "not_intercepted": list(excluded)}


async def reject_approval(moss, operation):
    try:
        await moss.approve(operation)
    except ValueError:
        return
    raise AssertionError(f"未拒绝操作 {operation}")


class CoreProfile:
    def __init__(self, path, model_url):
        self.path = path
        self.model_url = model_url
        self.moss = None

    async def restart(self):
        await self.close()
        self.moss = Moss(self.path, self.model_url, "isolated-calendar-test")
        return self.moss

    async def close(self):
        if self.moss is not None:
            moss, self.moss = self.moss, None
            await moss.close()


async def run_fault_matrix(folder, model_url, report):
    profile = CoreProfile(folder / "fault-matrix", model_url)
    stages = ("duplicate_approval", "before_write", "after_write", "cancel_before_write", "wrong_chat",
              "historic_approval")
    topics = ("产品评审", "看牙复诊", "字幕验收", "接人", "项目交接")
    try:
        moss = await profile.restart()
        for topic in topics:
            for stage in stages:
                chat = await moss.new_chat()
                title = f"{topic} · {stage}"
                before = calendar_rows(moss.db)
                operation = await moss.message(request_text(title), chat_id=chat)
                pending = moss._get(operation)
                assert_preview(pending, title)
                assert calendar_rows(moss.db) == before, stage
                if stage == "before_write":
                    # Keep the real approval commit and suppress the task launch immediately after it.
                    with patch.object(moss, "_start") as launch:
                        await moss.approve(operation)
                        launch.assert_called_once()
                    assert moss._get(operation)["status"] == "saving" and calendar_rows(moss.db) == before
                    moss = await profile.restart()
                    restored = moss._get(operation)
                    assert_preview(restored, title)
                    assert restored["desktop_confirmation"] == pending["desktop_confirmation"]
                    assert calendar_rows(moss.db) == before
                elif stage == "wrong_chat":
                    other = await moss.new_chat()
                    await reject_approval(moss, operation)
                    assert moss.chat_id == other and calendar_rows(moss.db) == before
                    moss = await profile.restart()
                    assert moss.chat_id == other
                    await reject_approval(moss, operation)
                    await moss.switch_chat(chat)
                    assert_preview(moss._get(operation), title)
                elif stage == "cancel_before_write":
                    await moss.cancel(operation)
                    moss = await profile.restart()
                    assert moss._get(operation)["status"] == "cancelled"
                    await reject_approval(moss, operation)
                    assert calendar_rows(moss.db) == before
                    report["fault_cases"].append({"point": stage, "title": title, "operation": operation,
                                                  "status": "cancelled", "rows_added": 0})
                    continue

                saving = None
                launch_action = moss._start

                def record_approval(turn, **kwargs):
                    nonlocal saving
                    saving = moss.db.execute("SELECT doc, pending FROM turns WHERE id=?", (operation,)).fetchone()
                    launch_action(turn, **kwargs)

                with patch.object(moss, "_start", side_effect=record_approval) as launch:
                    assert await moss.approve(operation) == operation
                    if stage == "duplicate_approval":
                        assert moss._get(operation)["status"] == "saving"
                        for _ in range(3):
                            assert await moss.approve(operation) == operation
                    launch.assert_called_once()
                await asyncio.wait_for(moss.job, 8)
                receipt = assert_readback(moss._get(operation), title)
                row = assert_created(moss.db, before, operation, title)
                assert_receipt_row(receipt, row)

                if stage == "after_write":
                    # Replay the persisted checkpoint left by approval, keeping the real calendar row.
                    assert saving and json.loads(saving[0])["status"] == "saving"
                    with moss.db:
                        moss.db.execute("UPDATE turns SET doc=?, pending=? WHERE id=?", (*saving, operation))
                    moss = await profile.restart()
                    receipt = assert_readback(moss._get(operation), title)
                    assert assert_created(moss.db, before, operation, title) == row
                    assert_receipt_row(receipt, row, recovered=True)
                elif stage == "historic_approval":
                    newer = await moss.message(request_text(title), chat_id=chat)
                    assert newer != operation
                    assert_preview(moss._get(newer), title)
                    assert await moss.approve(operation) == operation
                    assert_preview(moss._get(newer), title)
                    assert assert_created(moss.db, before, operation, title) == row
                    assert await moss.approve(newer) == newer
                    await asyncio.wait_for(moss.job, 8)
                    duplicate = assert_readback(moss._get(newer), title)
                    assert_receipt_row(duplicate, row)

                assert await moss.approve(operation) == operation
                assert moss._get(operation)["calendar_receipt"] == receipt
                assert assert_created(moss.db, before, operation, title) == row
                report["fault_cases"].append({"point": stage, "title": title, "operation": operation,
                                              "status": "replied", "rows_added": 1})
        assert len(report["fault_cases"]) == 30
        assert len(calendar_rows(moss.db)) == 25

        await moss.new_chat()
        title = "操作编号已被占用"
        operation = await moss.message(request_text(title))
        proposal = moss._get(operation)["desktop_confirmation"]["snapshot"]
        with moss.db:
            moss.db.execute("INSERT INTO calendar_events (operation_id, title, start, end, slot) VALUES (?, ?, ?, ?, ?)",
                            (operation, "原有日程", proposal["start"], proposal["end"], proposal["slot"]))
        before = calendar_rows(moss.db)
        await moss.approve(operation)
        await asyncio.wait_for(moss.job, 8)
        turn = moss._get(operation)
        assert turn["status"] == "error" and turn["error"] and not turn.get("calendar_receipt"), turn
        moss = await profile.restart()
        assert calendar_rows(moss.db) == before
        assert moss._get(operation)["status"] == "error" and not moss._get(operation).get("calendar_receipt")
        report["existing_id_collision"] = {"operation": operation, "row_preserved": True, "receipt": False}
    finally:
        await profile.close()


async def post(server, path, body, expected=200):
    status, response = await server.post("/api/" + path, body)
    assert status == expected, (path, status, response)
    return response


async def wait_turn(server, operation):
    async with asyncio.timeout(10):
        while True:
            current = next(row for row in (await server.state())["turns"] if row["id"] == operation)
            if current["status"] not in {"saving", "thinking"}:
                return current
            await asyncio.sleep(.04)


async def run_http(folder, model_url, report):
    title = "本机验收 · 产品评审"
    server = BrowserServer(folder / "http", free_port(), model_url=model_url)
    try:
        await server.start()
        home = (await server.state())["chat_id"]
        initial = await post(server, "message", {"text": request_text(title), "chat_id": home})
        proposal = initial["turns"][-1]
        assert_preview(proposal, title)
        operation = proposal["id"]
        await post(server, "approve", {"turn_id": operation, "pending_draft_only": True}, 409)
        await post(server, "new-chat", {})
        await post(server, "approve", {"turn_id": operation}, 409)
        await post(server, "switch-chat", {"id": home})
        await post(server, "approve", {"turn_id": operation})
        assert_readback(await wait_turn(server, operation), title)
        saved = (await server.state())["turns"][-1]
        before = server.pid
        await server.stop()
        database = (server.data / "session.sqlite3").resolve()
        assert database.is_relative_to(folder.resolve())
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            row = assert_created(db, [], operation, title)
        assert_receipt_row(saved["calendar_receipt"], row)
        await server.start()
        assert before != server.pid and (await server.state())["chat_id"] == home
        await post(server, "approve", {"turn_id": operation})
        fetched = await post(server, "message", {"text": "查看明天的日程", "chat_id": home})
        history, listing = fetched["turns"][-2:]
        assert history["calendar_receipt"] == saved["calendar_receipt"]
        assert listing["status"] == "replied" and title in listing["reply"], listing
        for turn in (proposal, history, listing):
            assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, turn
        compound = []
        for prompt, kinds in (
            ("现在几点，明天有哪些日程？", ["clock", "calendar"]),
            ("列出明天的提醒，顺便看看明天的日程", ["reminders", "calendar"]),
            ("现在几点明天有什么日程", ["clock", "calendar"]),
        ):
            submission = await post(server, "message", {"text": prompt, "chat_id": home})
            checked = await wait_turn(server, submission["turns"][-1]["id"])
            assert checked["status"] == "replied" and checked["source"] == "context", checked
            readings = checked["local_overview"]
            assert [item["kind"] for item in readings] == kinds, checked
            assert readings[-1]["reply"] == listing["reply"], checked
            assert checked["reply"] == "\n".join(item["reply"] for item in readings), checked
            assert all(item["reply"].strip() for item in readings)
            assert checked["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, checked
            compound.append({"question": prompt, "readings": readings})
        await server.stop()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            assert calendar_rows(db) == [row]
        package = folder / "http-backup"
        restored = BrowserServer(folder / "http-restored", free_port(), model_url=model_url)
        backup(server.data, package)
        restore(package, restored.data)
        try:
            await restored.start()
            listing_restored = (await post(restored, "message", {"text": "查看明天的日程", "chat_id": home}))["turns"][-1]
            assert listing_restored["status"] == "replied" and listing_restored["reply"] == listing["reply"], listing_restored
        finally:
            await restored.stop()
        report["http"] = {"operation": operation, "preview_status": proposal["status"],
                          "receipt": saved["calendar_receipt"], "list_reply": listing["reply"],
                          "restart_readback_equal": True, "backup_restore_readback_equal": True,
                          "database_rows": 1, "compound_queries": compound}
    finally:
        await server.stop()


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("local-calendar-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "isolated": True, "fault_cases": [], "model_requests": []}
    print(folder, flush=True)
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)

    async def unexpected_model(request):
        report["model_requests"].append(request.path)
        return web.Response(status=503)

    guard = web.Application()
    guard.router.add_route("*", "/{path:.*}", unexpected_model)
    runner = web.AppRunner(guard)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        model_url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1"
        settings = {"MOSS_WINDOWS_REMINDERS": "0", "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0",
                    "MOSS_PROVIDER_DIR": str(folder / "providers"), "LOCALAPPDATA": str(folder / "profile"),
                    "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_STANDBY": "0", "MOSS_SPEECH_MIC_WARM": "0"}
        for _, prefixes, _, _ in SPECS.values():
            for prefix in prefixes:
                settings[prefix + "_BASE_URL"] = model_url
                settings[prefix + "_API_KEY"] = "isolated-qa"
        with patch.dict(os.environ, settings):
            verify_expiry(report)
            verify_compound_routing(report)
            await run_fault_matrix(folder, model_url, report)
            await run_http(folder, model_url, report)
        assert not report["model_requests"], report["model_requests"]
        report["passed"] = True
    finally:
        await runner.cleanup()
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
