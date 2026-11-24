"""Read-only comparison to the daily instance. Restored personal content stays in a temporary directory."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from unittest.mock import patch

import aiohttp

from backup_profile import LAUNCH, PLAYER, SESSION, backup, checked_db, restore, sha
from browser_lab import BrowserServer, ROOT


def fingerprint(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def projection(state):
    return {key: state[key] for key in ("chat_id", "chats", "turns", "memories", "model_id", "timers")}


async def daily_snapshot(client):
    base = "http://127.0.0.1:18221"
    result = []
    for path in ("/api/health", "/api/state"):
        async with client.get(base+path, timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            result.append(await response.json())
    assert result[0]["app"] == "moss-local-chat"
    return result


def add_qa_preview(destination, fixture):
    with closing(checked_db(fixture)) as source, closing(sqlite3.connect(destination / SESSION)) as dest, dest:
        turn = source.execute("SELECT id,doc,pending,chat_id FROM turns WHERE pending IS NOT NULL "
                              "AND json_extract(doc,'$.status')='pending' LIMIT 1").fetchone()
        assert turn
        chat = source.execute("SELECT id,title,created_at FROM chats WHERE id=?", (turn[3],)).fetchone()
        dest.execute("INSERT INTO chats(id,title,created_at) VALUES(?,?,?)", chat)
        dest.execute("INSERT INTO turns(id,doc,pending,chat_id) VALUES(?,?,?,?)", turn)
        dest.execute("UPDATE state SET active_chat=?,revision=revision+1", (turn[3],))
        return turn[0], json.loads(turn[1])["call_id"]


def expected_failure(source, target):
    try:
        restore(source, target)
    except ValueError:
        assert not target.exists()
        return
    raise AssertionError("An incomplete backup was accepted")


async def run(args):
    if args.port == 18221:
        raise ValueError("请选择独立端口")
    report_dir = ROOT / "runs" / datetime.now().strftime("daily-restore-%Y%m%d-%H%M%S-%f")
    report_dir.mkdir()
    report = {"passed":False, "snapshot":args.snapshot.name, "backup_sha256":sha(args.snapshot)}
    with closing(checked_db(args.snapshot)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        report["snapshot_counts"] = {t:db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                                     ("chats", "turns", "memories", "assistant_timers")}
        report["original_receipts"] = db.execute("SELECT COUNT(*) FROM turns WHERE json_type(doc,'$.receipt')='object'").fetchone()[0]
        active_id, = db.execute("SELECT active_chat FROM state WHERE id=1").fetchone()
        rows = [json.loads(row[0]) for row in db.execute("SELECT doc FROM turns WHERE chat_id=? ORDER BY rowid", (active_id,))]
        def saved_turns(items):
            return fingerprint([[r.get(k) for k in ("id","user","status","reply","call_id","receipt")] for r in items])
        archived = saved_turns(rows)
    report["existing_attachment_scope"] = {
        "legacy_database_only":True, "daily_draft_files":sum(1 for _ in (ROOT/"data/drafts").glob("*.txt")),
        "daily_player_ledger_exists":(ROOT/"data"/PLAYER).is_file(),
        "daily_reminder_launch_file_exists":(ROOT/"data"/LAUNCH).is_file()}
    connections = 0
    async def reject(reader, writer):
        nonlocal connections
        connections += 1
        writer.close()
        await writer.wait_closed()
    sink = await asyncio.start_server(reject, "127.0.0.1", 0)
    address = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}"
    try:
        async with sink, aiohttp.ClientSession(trust_env=False) as client:
            daily_before, live = await daily_snapshot(client)
            with tempfile.TemporaryDirectory(prefix=".isolated-personal-", dir=report_dir) as tmp:
                work = Path(tmp)
                server = BrowserServer(work/"restored-legacy", args.port, model_url=address+"/v1")
                full = BrowserServer(work/"restored-full", args.port, model_url=address+"/v1")
                settings = {"MOSS_WINDOWS_REMINDERS":"0", "MOSS_MEDIA_DB":str(work/"empty-media.sqlite3"),
                            "HTTPS_PROXY":address,"HTTP_PROXY":address,"ALL_PROXY":address, "NO_PROXY":"localhost,127.0.0.1"}
                with patch.dict(os.environ, settings):
                    report["restore_legacy"] = restore(args.snapshot, server.data)
                    assert sha(server.data/SESSION) == report["backup_sha256"]
                    try:
                        await server.start()
                        first_pid = server.pid
                        state = await server.state()
                        assert state["chat_id"] == active_id and saved_turns(state["turns"]) == archived
                        report["archived_turn_projection_sha256"] = archived
                        old = {s["id"]:fingerprint(s) for s in state["turns"]}
                        current = {s["id"]:fingerprint(s) for s in live["turns"]}
                        report["live_comparison"] = {
                            "exact_visible_state":projection(live)==projection(state),
                            "all_archived_turns_match_current":all(current.get(k)==v for k,v in old.items()),
                            "daily_turns_observed":len(current), "restored_turns":len(old),
                            "ordered_ids_sha256":fingerprint(list(old)),
                            "daily_visible_state_sha256":fingerprint(projection(live)),
                            "restored_visible_state_sha256":fingerprint(projection(state))}
                        await server.stop()
                        await server.start()
                        assert server.pid != first_pid and projection(await server.state()) == projection(state)
                        report["legacy_restart"] = {"pids":[first_pid,server.pid], "state_identical":True}
                    finally:
                        await server.stop()
                    # Only the scratch clone receives a previously observed, real deferred tool call.
                    key, call = add_qa_preview(server.data, args.pending)
                    try:
                        await server.start()
                        code, _ = await server.post("/api/approve", {"turn_id":key})
                        assert code == 200
                        async with asyncio.timeout(10):
                            while True:
                                row = (await server.state())["turns"][-1]
                                if row["status"] != "saving":
                                    break
                                await asyncio.sleep(.05)
                        receipt = row["receipt"]
                        assert row["call_id"] == call and receipt
                        link = "/drafts/" + receipt["filename"]
                        actual = sha(server.data/"drafts"/receipt["filename"])
                        assert actual == receipt["sha256"]
                        try:
                            backup(server.data, work/"while-running")
                        except ValueError:
                            assert not (work/"while-running").exists()
                        else:
                            raise AssertionError("Live exclusive profile backup should not start")
                    finally:
                        await server.stop()
                    # Independent QA playback data and launch options exercise the two optional files.
                    shutil.copyfile(args.player, server.data/PLAYER)
                    (server.data/LAUNCH).write_text(json.dumps({"model_url":address+"/v1","port":args.port}), encoding="utf-8")
                    package = work/"profile-backup"
                    report["full_backup"] = backup(server.data, package)
                    report["full_restore"] = restore(package, full.data)
                    assert sha(full.data/LAUNCH) == sha(server.data/LAUNCH)
                    assert sha(full.data/PLAYER) == sha(package/PLAYER)
                    try:
                        await full.start()
                        assert (await full.state())["turns"][-1]["receipt"]["sha256"] == actual
                        async with client.get(full.url+link) as response:
                            assert response.status == 200 and hashlib.sha256(await response.read()).hexdigest() == actual
                        code, _ = await full.post("/api/approve", {"turn_id":key})
                        assert code == 200 and len(list((full.data/"drafts").glob("*.txt"))) == 1
                        code, state2 = await full.post("/api/switch-chat", {"id":state["chat_id"]})
                        assert code == 200 and {t["id"]:fingerprint(t) for t in state2["turns"]} == old
                        report["isolated_qa_draft"] = {"existing_qa_tool_call_replayed":True, "receipt_sha256":actual,
                            "utf8_bytes":(full.data/"drafts"/receipt["filename"]).stat().st_size,
                            "verified_via_http":True, "files_after_repeat_approval":1, "original_turns_identical":True}
                    finally:
                        await full.stop()
                    expected_failure(package/SESSION, work/"missing-draft")
                    (package/"drafts"/receipt["filename"]).write_bytes(b"mismatched")
                    expected_failure(package, work/"corrupt-draft")
                    report["guards"] = ["active profile", "legacy receipt without draft body", "corrupted full backup"]
            report["private_scratch_removed"] = not Path(tmp).exists()
            daily_after, final = await daily_snapshot(client)
            report.update(daily_pid_unchanged=daily_before["pid"]==daily_after["pid"],
                          daily_state_unchanged_during_drill=projection(live)==projection(final),
                          original_backup_unchanged=sha(args.snapshot)==report["backup_sha256"],
                          connections_to_model_or_proxy_sink=connections)
            assert report["daily_pid_unchanged"] and report["private_scratch_removed"] and not connections and report["original_backup_unchanged"]
            report["passed"] = True
    finally:
        (report_dir/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_dir/"result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="已停服时创建的日常独立 SQLite 备份")
    parser.add_argument("--pending", type=Path, required=True, help="隔离 QA 中已提交的真实 pending 工具调用库")
    parser.add_argument("--player", type=Path, required=True, help="隔离 QA 媒体播放记录数据库")
    parser.add_argument("--port", type=int, default=18995)
    asyncio.run(run(parser.parse_args()))
