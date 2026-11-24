import argparse
import asyncio
from contextlib import AsyncExitStack, closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from urllib.parse import urlsplit

import win32api
import win32con
import win32file
import win32security

from browser_lab import BrowserServer, ROOT
from serve import engine_process


@contextmanager
def deny_file_creation(drafts, run):
    if not drafts.resolve().is_relative_to(run.resolve()) or drafts.name != "drafts":
        raise ValueError("权限测试仅可使用本次新建的隔离草稿目录")
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()
    def change(*options):
        subprocess.run(["icacls.exe", str(drafts), *options], check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    change("/deny", f"*{sid}:(WD)")
    try:
        yield
    finally:
        change("/remove:d", f"*{sid}")


async def wait_until(server, predicate):
    async with asyncio.timeout(150):
        while True:
            rows = (await server.state())["turns"]
            if rows and predicate(rows[-1]):
                return rows[-1]
            await asyncio.sleep(.08)


async def retry_during_verification(server, key):
    code, _ = await server.post("/api/approve", {"turn_id": key})
    assert code == 200
    return await wait_until(server, lambda t: t["id"] == key and t["status"] != "saving")


def check_directory_denial(staging, point):
    probe = staging.with_suffix(".probe")
    try:
        if point == "create":
            probe.touch(exist_ok=False)
        else:
            os.link(staging, probe)
    except PermissionError as error:
        return {"errno": error.errno, "winerror": error.winerror}
    else:
        probe.unlink()
        raise AssertionError("隔离目录的拒绝创建权限未生效")


async def check_published_recovery(server, key, call, target, checkpoint, report):
    # 在隔离库中重放本轮真实 saving 快照，模拟发布成功、回执提交前进程退出。
    before = target.stat()
    original = target.read_bytes()
    await server.stop()
    with closing(sqlite3.connect(server.data / "session.sqlite3")) as db, db:
        db.execute("UPDATE turns SET doc=?,pending=? WHERE id=?", (*checkpoint, key))
    evidence = {"point": "published_recovery", "turn": key, "call": call, "file_id": before.st_ino,
                "mtime_ns": before.st_mtime_ns, "fault": "saving_checkpoint_replay_with_exclusive_win32_handle"}
    report["cases"].append(evidence)
    handle = win32file.CreateFile(str(target), win32con.GENERIC_READ, 0, None,
                                  win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
    try:
        await server.start()
        restored = (await server.state())["turns"][-1]
        assert restored["status"] == "pending" and restored["verify_only"] and not restored["receipt"], restored
        locked = await retry_during_verification(server, key)
        evidence.update(locked_status=locked["status"], locked_error=locked["error"])
        assert locked["status"] == "pending" and locked["verify_only"] and not locked["receipt"]
    finally:
        handle.Close()
    moved = target.with_suffix(".held")
    target.rename(moved)
    try:
        for _ in range(2):
            missing = await retry_during_verification(server, key)
            evidence.update(missing_status=missing["status"], missing_verify_only=missing["verify_only"],
                            recreated=target.exists())
            assert missing["status"] == "pending" and missing["verify_only"] and not missing["receipt"]
            assert not target.exists()
    finally:
        if not target.exists():
            moved.rename(target)
    saved = await retry_during_verification(server, key)
    after = target.stat()
    evidence.update(final_status=saved["status"], same_file_id=before.st_ino == after.st_ino,
                    same_mtime=before.st_mtime_ns == after.st_mtime_ns, same_call=saved["call_id"] == call,
                    hash_verified=saved["receipt"]["sha256"] == hashlib.sha256(original).hexdigest())
    assert saved["status"] in {"replied", "saved"} and not target.with_suffix(".part").exists()
    assert all(evidence[field] for field in ("same_file_id", "same_mtime", "same_call", "hash_verified"))


async def pending_fixture(args, folder):
    if args.pending_db:
        source = args.pending_db.resolve()
        if source.name != "session.sqlite3" or not source.is_relative_to((ROOT / "runs").resolve()):
            raise ValueError("仅接受隔离测试目录中的已生成待确认记录")
        return source
    async with AsyncExitStack() as stack:
        if args.model_url:
            if urlsplit(args.model_url).hostname != "127.0.0.1":
                raise ValueError("测试仅允许本机模型")
            model_url = args.model_url
        else:
            await stack.enter_async_context(engine_process(args.engine_port, folder, False))
            model_url = f"http://127.0.0.1:{args.engine_port}/v1"
        server = BrowserServer(folder / "prepare", args.port, model_url=model_url)
        await server.start()
        stack.push_async_callback(server.stop)
        code, _ = await server.post("/api/message", {"text": "帮我保存一份本地草稿，内容逐字写：周五核对清单"})
        assert code == 200
        pending = await wait_until(server, lambda t: t["status"] != "thinking")
        assert pending["status"] == "pending" and pending["preview"] == "周五核对清单", pending
        return server.data / "session.sqlite3"


async def main(args):
    folder = ROOT / "runs" / datetime.now().strftime("draft-write-failure-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    report = {"passed": False, "cases": []}
    try:
        fixture = await pending_fixture(args, folder)
        report["pending_fixture"] = str(fixture)
        async with AsyncExitStack() as stack:
            for point in ("create", "publish"):
                server = BrowserServer(folder / point, args.port, write_delay=1.2, model_url="http://127.0.0.1:9/v1")
                server.data.mkdir(parents=True)
                with (closing(sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True)) as source,
                      closing(sqlite3.connect(server.data / "session.sqlite3")) as destination):
                    source.backup(destination)
                await server.start()
                stack.push_async_callback(server.stop)
                state = await server.state()
                pending = state["turns"][-1]
                assert state["model_id"] == "local" and pending["status"] == "pending" and pending["preview"], pending
                expected = pending["preview"]
                key = pending["id"]
                call = pending["call_id"]
                draft_dir = server.data / "drafts"
                staging = draft_dir / (key + ".part")
                target = staging.with_suffix(".txt")
                if point == "publish":
                    # 预先创建空临时文件，允许覆写；仅阻止创建发布文件的目录项。
                    staging.touch(exist_ok=False)
                with deny_file_creation(draft_dir, folder):
                    denial = check_directory_denial(staging, point)
                    code, _ = await server.post("/api/approve", {"turn_id": key})
                    assert code == 200
                    failed = await wait_until(server, lambda t: t["id"] == key and t["status"] != "saving")
                    evidence = {"point": point, "turn": key, "call": call, "ntfs_denial": denial, "failure_status": failed["status"],
                                "error": failed["error"], "tool_started": failed["tool_started_at"],
                                "failure_receipt": failed["receipt"], "published_on_failure": target.exists(),
                                "part_on_failure": staging.exists()}
                    report["cases"].append(evidence)
                    assert failed["call_id"] == call and failed["tool_started_at"]
                    assert not target.exists() and not failed["receipt"] and not staging.exists(), evidence
                await server.stop()
                await server.start()
                restored = (await server.state())["turns"][-1]
                code, body = await server.post("/api/approve", {"turn_id": key})
                evidence.update(restarted_status=restored["status"], retry_status=code)
                if code != 200:
                    evidence["retry_error"] = body.get("error")
                    await server.stop()
                    continue
                if point == "publish":
                    await server.stop(hard=True)
                    with closing(sqlite3.connect(server.data / "session.sqlite3")) as db:
                        checkpoint = db.execute("SELECT doc,pending FROM turns WHERE id=?", (key,)).fetchone()
                    assert json.loads(checkpoint[0])["status"] == "saving" and checkpoint[1]
                    await server.start()
                    restored = (await server.state())["turns"][-1]
                    assert restored["status"] == "pending" and restored["call_id"] == call
                    code, _ = await server.post("/api/approve", {"turn_id": key})
                    assert code == 200
                saved = await wait_until(server, lambda t: t["id"] == key and t["status"] != "saving")
                actual = target.read_bytes()
                digest = hashlib.sha256(actual).hexdigest()
                evidence.update(final_status=saved["status"], hash_verified=digest == saved["receipt"]["sha256"],
                                same_call=call == saved["call_id"], files=len(list(draft_dir.glob(key + ".txt"))))
                assert actual == expected.encode("utf-8") and evidence["same_call"] and evidence["hash_verified"]
                assert evidence["files"] == 1 and not staging.exists()
                if point == "publish":
                    await check_published_recovery(server, key, call, target, checkpoint, report)
                await server.stop()
            cases = report["cases"]
            report["passed"] = len(cases) == 3 and all(
                c["failure_status"] == "pending" and c["restarted_status"] == "pending"
                and c["retry_status"] == 200 and c.get("hash_verified") for c in cases[:2]
            ) and cases[-1].get("hash_verified")
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)
    assert report["passed"], report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-url")
    parser.add_argument("--pending-db", type=Path, help="复用隔离环境已生成的真实待确认 SQLite 数据库")
    parser.add_argument("--port", type=int, default=18773)
    parser.add_argument("--engine-port", type=int, default=18774)
    asyncio.run(main(parser.parse_args()))
