import asyncio
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import stat
import _winapi

from PIL import Image
import win32con
import win32file

from browser_lab import ROOT
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated
from verify_file_organization import focus_is, snapshot


NAME = "讲义.pdf"
SELECT = "按计划归类 " + NAME


def fingerprint(path):
    info = path.stat()
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def move_fixture(source, destination, fixtures):
    for path in (source, destination):
        assert path.resolve().is_relative_to(fixtures.resolve()) and path != fixtures, path
    assert source.is_file() and not destination.exists()
    source.rename(destination)


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("file-organization-apply-%Y%m%d-%H%M%S-%f")
    fixtures = folder / "验收文件"
    fixtures.mkdir(parents=True)
    preserved = fixtures / "保留原件"
    preserved.mkdir()
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "grey").save(buffer, format="PDF")
    original = buffer.getvalue() + b"\n% fixture-A\n"
    replacement = buffer.getvalue() + b"\n% fixture-B\n"
    catalog = folder / "空索引.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0")
    server = isolated(folder / "app")
    report = {"passed": False, "model_connections": 0, "app_pids": [], "cases": [],
              "turns": [], "recovery_setup": "隔离服务停机后记录批准中断边界，真实卷上布置三种磁盘状态。"}
    result = folder / "result.json"
    print(result, flush=True)

    def fixture(label, existing_category=False):
        root = fixtures / label
        root.mkdir()
        if existing_category:
            (root / "文档").mkdir()
        (root / NAME).write_bytes(original)
        (root / "未选择.txt").write_text("保留这一项", encoding="utf-8")
        return root

    def replace_original(root):
        source = root / NAME
        old = fingerprint(source)
        backup = preserved / (root.name + ".pdf")
        move_fixture(source, backup, fixtures)
        source.write_bytes(replacement)
        os.utime(source, ns=(source.stat().st_atime_ns, old["mtime_ns"]))
        new = fingerprint(source)
        assert (old["size"], old["mtime_ns"]) == (new["size"], new["mtime_ns"])
        assert old["inode"] != new["inode"] and old["sha256"] != new["sha256"]
        assert fingerprint(backup) == old
        return {"original": old, "replacement": new}

    async def guard_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def start():
        await server.start()
        report["app_pids"].append(server.pid)
        state = await server.state()
        assert state["model_id"] == "local" and state["memory_backend"] == "sqlite"
        return state["chat_id"]

    async def request(endpoint, body, *, modifies=False):
        before = snapshot(fixtures)
        code, state = await server.post("/api/" + endpoint, body)
        assert code == 200, (endpoint, code, state)
        turn = state["turns"][-1]
        turn_id = body.get("turn_id", turn["id"])
        async with asyncio.timeout(18):
            while turn["status"] in {"saving", "thinking"}:
                await asyncio.sleep(.04)
                turn = (await server.state())["turns"][-1]
        after = snapshot(fixtures)
        report["turns"].append({"endpoint": endpoint, "request": body, "id": turn["id"],
                                "chat_id": turn["chat_id"], "status": turn["status"], "source": turn.get("source"),
                                "reply": turn.get("reply"), "error": turn.get("error"), "before": before,
                                "after": after, "cloud_requests": turn["cloud_requests"],
                                "file_move_receipt": turn.get("file_move_receipt")})
        assert turn["id"] == turn_id and turn["chat_id"] == chat, turn
        assert turn["cloud_requests"]["total"] == report["model_connections"] == 0, turn
        if not modifies:
            assert before == after, report["turns"][-1]
        return turn

    async def say(text):
        turn = await request("message", {"text": text, "chat_id": chat})
        assert turn["source"] == "context", turn
        return turn

    async def plan(root):
        turn = await say(f"按类型整理 {root}，先给我看计划")
        focus_is(turn, root)
        assert turn["status"] == "replied" and turn["file_plan"]["status"] == "preview", turn
        assert "_file_plan_snapshots" not in turn and "snapshots" not in turn["file_plan"], turn
        assert NAME in turn["reply"], turn

    async def prepare(root):
        turn = await say(SELECT)
        assert turn["status"] == "pending" and not turn.get("file_move_receipt"), turn
        card = turn["desktop_confirmation"]
        assert card["action"] == "organize_file" and "snapshot" not in card, card
        lines = card["detail"].splitlines()
        assert any(str(root / NAME) in line for line in lines), card
        assert any(str(root / "文档" / NAME) in line for line in lines), card
        return turn

    def check_moved(turn, root, initial):
        source, destination = root / NAME, root / "文档" / NAME
        receipt = turn["file_move_receipt"]
        assert turn["status"] == "replied" and NAME in turn["reply"], turn
        assert receipt["status"] == "moved" and Path(receipt["source"]) == source, receipt
        assert Path(receipt["destination"]) == destination, receipt
        assert not source.exists() and fingerprint(destination) == initial, (initial, fingerprint(destination))
        assert list(root.rglob(NAME)) == [destination]
        assert (root / "未选择.txt").read_text(encoding="utf-8") == "保留这一项"

    def refused(turn):
        assert turn["status"] == "replied" and turn.get("reply") and not turn.get("desktop_confirmation"), turn
        assert not turn.get("file_move_receipt"), turn

    async def approve_error(pending):
        turn = await request("approve", {"turn_id": pending["id"]})
        assert turn["status"] == "error" and turn.get("error"), turn
        assert (turn.get("file_move_receipt") or {}).get("status") != "moved", turn

    def saving_boundary(turn_id):
        db = server.data / "session.sqlite3"
        assert db.is_file() and server.proc is None
        with sqlite3.connect(db) as connection:
            row = connection.execute("""UPDATE turns SET doc=json_set(doc,'$.status','saving',
                '$.approved_at',?,'$.tool_started_at',?) WHERE id=? AND json_extract(doc,'$.status')='pending'
                AND json_extract(doc,'$.desktop_confirmation.action')='organize_file'""",
                                     (datetime.now().isoformat(), datetime.now().isoformat(), turn_id))
            assert row.rowcount == 1

    try:
        async with await asyncio.start_server(guard_model, "127.0.0.1", 0) as guard:
            server.model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = server.model_url
            chat = await start()
            refused(await say(SELECT))
            report["cases"].append({"case": "空白会话不归类"})

            root = fixture("正常归类")
            initial = fingerprint(root / NAME)
            await plan(root)
            cancelled = await prepare(root)
            declined = await request("cancel", {"turn_id": cancelled["id"]})
            assert declined["status"] == "cancelled", declined
            before = snapshot(fixtures)
            code, _ = await server.post("/api/approve", {"turn_id": cancelled["id"]})
            assert code == 409 and before == snapshot(fixtures)
            report["cases"].append({"case": "取消后文件保留", "subsequent_approve_http": code})
            await plan(root)
            pending = await prepare(root)
            await server.stop()
            with sqlite3.connect(f"{(server.data / 'session.sqlite3').as_uri()}?mode=ro", uri=True) as connection:
                private_card = connection.execute(
                    "SELECT json_type(doc,'$.desktop_confirmation.snapshot') FROM turns WHERE id=?",
                    (pending["id"],)).fetchone()
                private_plan = connection.execute(
                    "SELECT count(*) FROM turns WHERE chat_id=? AND json_type(doc,'$._file_plan_snapshots')='object'",
                    (chat,)).fetchone()
                assert private_card == ("object",) and private_plan[0] > 0
            connection.close()
            assert await start() == chat
            restored = (await server.state())["turns"][-1]
            assert restored["id"] == pending["id"] and restored["status"] == "pending", restored
            assert restored["desktop_confirmation"] == pending["desktop_confirmation"], restored
            assert fingerprint(root / NAME) == initial and not (root / "文档").exists()
            moved = await request("approve", {"turn_id": pending["id"]}, modifies=True)
            check_moved(moved, root, initial)
            assert moved["file_move_receipt"]["recovered"] is False, moved
            duplicate = await request("approve", {"turn_id": pending["id"]})
            check_moved(duplicate, root, initial)
            assert duplicate["approved_at"] == moved["approved_at"] and duplicate["file_move_receipt"] == moved["file_move_receipt"]
            report["cases"].append({"case": "重启续办与重复确认", "detail": restored["desktop_confirmation"]["detail"],
                                    "private_snapshots_in_sqlite_only": True, "source_before": initial,
                                    "destination_after": fingerprint(root / "文档" / NAME)})

            for phase in ("预览后", "确认前"):
                root = fixture(phase + "替换原件")
                await plan(root)
                if phase == "确认前":
                    pending = await prepare(root)
                change = replace_original(root)
                if phase == "预览后":
                    refused(await say(SELECT))
                else:
                    await approve_error(pending)
                assert not (root / "文档").exists() and (root / NAME).read_bytes() == replacement
                report["cases"].append({"case": root.name, **change})

            root = fixture("新增同名目标", existing_category=True)
            await plan(root)
            pending = await prepare(root)
            (root / "文档" / NAME).write_bytes(replacement)
            await approve_error(pending)
            assert (root / NAME).read_bytes() == original and (root / "文档" / NAME).read_bytes() == replacement
            report["cases"].append({"case": root.name})

            root = fixture("分类目录被替换为联接")
            external = fixtures / "独立联接目标"
            external.mkdir()
            (external / "标记.txt").write_text("保留", encoding="utf-8")
            await plan(root)
            pending = await prepare(root)
            _winapi.CreateJunction(str(external), str(root / "文档"))
            assert (root / "文档").lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
            await approve_error(pending)
            assert not (external / NAME).exists() and (root / NAME).read_bytes() == original
            report["cases"].append({"case": root.name})

            root = fixture("源文件已有写句柄")
            await plan(root)
            pending = await prepare(root)
            sharing = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE
            handle = win32file.CreateFile(str(root / NAME), win32con.GENERIC_WRITE, sharing, None,
                                          win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
            try:
                await approve_error(pending)
            finally:
                handle.Close()
            assert (root / NAME).read_bytes() == original and not (root / "文档").exists()
            report["cases"].append({"case": root.name, "writer_shares_read_write_delete": True})

            for outcome in ("moved", "not_moved", "unknown", "category_changed"):
                root = fixture("恢复状态-" + outcome, existing_category=True)
                initial = fingerprint(root / NAME)
                await plan(root)
                pending = await prepare(root)
                await server.stop(hard=True)
                saving_boundary(pending["id"])
                if outcome == "moved":
                    move_fixture(root / NAME, root / "文档" / NAME, fixtures)
                elif outcome == "unknown":
                    (root / "文档" / NAME).write_bytes(replacement)
                elif outcome == "category_changed":
                    category = root / "文档"
                    parked = root / "原分类目录"
                    assert category.resolve().is_relative_to(fixtures) and parked.resolve().is_relative_to(fixtures)
                    assert category.is_dir() and not parked.exists()
                    category.rename(parked)
                    category.mkdir()
                    assert category.stat().st_ino != parked.stat().st_ino
                before = snapshot(fixtures)
                assert await start() == chat
                turn = (await server.state())["turns"][-1]
                assert turn["id"] == pending["id"] and turn["cloud_requests"]["total"] == 0, turn
                assert before == snapshot(fixtures)
                if outcome == "moved":
                    check_moved(turn, root, initial)
                    assert turn["file_move_receipt"]["recovered"] is True, turn
                    await request("approve", {"turn_id": pending["id"]})
                elif outcome == "not_moved":
                    assert turn["status"] == "pending" and not turn["approved_at"] and turn["error"], turn
                    assert fingerprint(root / NAME) == initial and not (root / "文档" / NAME).exists()
                    assert (await request("cancel", {"turn_id": turn["id"]}))["status"] == "cancelled"
                elif outcome == "unknown":
                    assert turn["status"] == "error" and turn["file_move_receipt"]["status"] == "unknown", turn
                    assert fingerprint(root / NAME) == initial and (root / "文档" / NAME).read_bytes() == replacement
                else:
                    receipt = turn["file_move_receipt"]
                    assert turn["status"] == "error" and receipt["status"] == "not_moved", turn
                    assert receipt["requires_new_preview"] is True and "预览" in turn["error"], turn
                    assert fingerprint(root / NAME) == initial and not (root / "文档" / NAME).exists()
                report["cases"].append({"case": root.name, "status": turn["status"], "receipt": turn.get("file_move_receipt"),
                                        "reply": turn.get("reply"), "error": turn.get("error"), "fixture_unchanged": before})
            assert report["model_connections"] == 0
            report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        try:
            await server.stop()
        finally:
            result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已通过 {len(report['cases'])} 项隔离文件操作验收", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
