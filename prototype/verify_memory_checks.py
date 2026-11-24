import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys

from aiohttp import web

from browser_lab import BrowserServer, ROOT
from core import Moss
from local_media.library import MediaLibrary
from mcp_configured import MCP_DATA
from memory_store import memory_command
from model_catalog import SPECS
from serve import application


RED = "我的工作饮品偏好是红茶"
WHITE = "我的工作饮品偏好是白茶"
BEDTIME = "我的睡前饮品偏好是温水"
SWEET = "我不喜欢甜食"
SOUR = "我喜欢酸味"
BOOK = "我的阅读清单里有《你好吗？》"
WINDOW = "我坐高铁喜欢靠窗"
AISLE = "我坐高铁喜欢靠过道"
NAME = "我希望你以后叫我小青"
EMPLOYEE = "我的工号是QA-271"
FIRST_BOX = "我的工具箱编号是QA-16"
SECOND_BOX = "我的工具箱编号是QA-17"


def memory_rows(db):
    rows = db.execute("SELECT * FROM memories ORDER BY rowid")
    columns = [column[0] for column in rows.description]
    return [dict(zip(columns, row)) for row in rows]


def digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


async def verify(folder, resume):
    result_file = folder / "result.json"
    report = json.loads(result_file.read_text(encoding="utf-8")) if resume else {
        "passed":False, "application_pids":[], "cases":[], "blocked_connections":[], "keys":{}}
    data = folder / "data"
    catalog = folder / "empty-catalog.sqlite3"
    os.environ.update(LOCALAPPDATA=str(folder / "local-profile"), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0")
    for _, prefixes, _, _ in SPECS.values():
        for prefix in prefixes:
            os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"
    MediaLibrary(catalog)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def guard(event, args):
        if event == "socket.connect":
            host, destination, *_ = args[1]
        elif event == "socket.getaddrinfo":
            host, destination = args[:2]
        else:
            return
        if host == "127.0.0.1" and destination == port:
            return
        report["blocked_connections"].append({"event":event, "host":host, "port":destination})
        raise PermissionError("验收仅允许连接本轮 HTTP 端口。")

    sys.addaudithook(guard)
    keys = report["keys"]
    try:
        async with AsyncExitStack() as stack:
            moss = Moss(data, "http://127.0.0.1:9/v1", "qa-local")
            stack.push_async_callback(moss.close)
            stack.callback(MCP_DATA.reset, MCP_DATA.set(data))
            endpoint = BrowserServer(folder, port)
            app = application(moss, lambda:"external", data, port, secrets.token_urlsafe(24), asyncio.Event())
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", port).start()
            state = await endpoint.state()
            assert state["model_id"] == "local" and state["memory_backend"] == "sqlite"
            assert not any(profile.public()["available"] for profile in moss.cloud.values())
            assert os.getpid() not in report["application_pids"]
            report["application_pids"].append(os.getpid())
            report["sqlite_columns"] = [r[1] for r in moss.db.execute("PRAGMA table_info(memories)")]

            async def send(text, *, read_only=False):
                before = memory_rows(moss.db)
                code, state = await endpoint.post("/api/message", {"chat_id":moss.chat_id, "text":text})
                assert code == 200, (code, state)
                turn = state["turns"][-1]
                after = memory_rows(moss.db)
                case = {"text":text, "reply":turn.get("reply"), "source":turn.get("source"), "status":turn["status"],
                        "memory_ids":turn.get("memory_ids"), "cloud_requests":turn["cloud_requests"],
                        "read_only":read_only, "before_sha256":digest(before), "after_sha256":digest(after),
                        "before_count":len(before), "after_count":len(after), "pid":os.getpid()}
                report["cases"].append(case)
                assert turn["user"] == text and turn["source"] == "memory" and turn["status"] == "replied", case
                assert turn["cloud_requests"] == {"total":0, "by_provider":{}, "complete":True}, case
                stored, = moss.db.execute("SELECT cloud_requests FROM turns WHERE id=?", (turn["id"],)).fetchone()
                assert json.loads(stored) == {"by_provider":{}, "complete":True}
                if read_only:
                    assert before == after and not turn.get("memory_id"), case
                return turn

            async def check(text, expected=(), *, present=(), absent=(), missing=False):
                turn = await send(text, read_only=True)
                assert (turn.get("memory_ids") or []) == list(expected), turn
                assert all(piece in turn["reply"] for piece in present), turn["reply"]
                assert all(piece not in turn["reply"] for piece in absent), turn["reply"]
                if missing:
                    assert re.search(r"没|未|找不到|查不到|无(?:相关|此|匹配)", turn["reply"]), turn["reply"]

            async def save(name, fact):
                turn = await send("记住：" + fact)
                key = turn["memory_id"]
                row = next(row for row in memory_rows(moss.db) if row["id"] == key)
                assert row["text"] == fact and row["source_turn"] == turn["id"]
                keys[name] = key

            if not resume:
                await save("drink", RED)
                await save("bedtime", BEDTIME)
                await save("sweet", SWEET)
                await save("sour", SOUR)
                await save("book", BOOK)
                await save("rail", WINDOW)
                await save("name", NAME)
                await save("id", EMPLOYEE)
                await save("box_one", FIRST_BOX)
                await save("box_two", SECOND_BOX)
                corrected = await send("以后改成靠过道")
                assert corrected["memory_id"] == keys["rail"]
                await check("记住我坐高铁喜欢靠窗了吗？", absent=(AISLE,), missing=True)
                await check("记住我坐高铁喜欢靠过道了吗？", [keys["rail"]], present=(AISLE,))
                rail = next(row for row in memory_rows(moss.db) if row["id"] == keys["rail"])
                assert rail["text"] == AISLE and rail["source_turn"] == corrected["id"]
                report["natural_correction"] = {"id":rail["id"], "before":WINDOW, "after":rail["text"],
                                                "correction_turn":corrected["id"]}
                for text in ("记住我的工作饮品偏好了吗？", "记住我的工作饮品偏好了吗？谢谢",
                             "记住我的工作饮品偏好？", "你记住了我的工作饮品偏好吗？"):
                    await check(text, [keys["drink"]], present=(RED,), absent=("温水",))
                await check("记住我的工作饮品偏好是绿茶了吗？", absent=("红茶", "温水"), missing=True)
                await check("记住我的工作饮品偏好是红茶了吗？", [keys["drink"]], present=(RED,), absent=("温水",))
                await check("我的工作饮品偏好是什么？", [keys["drink"]], present=(RED,), absent=("温水",))
                await check("我的睡前饮品偏好是啥来着？", [keys["bedtime"]], present=(BEDTIME,), absent=("红茶",))
                await check("我的阅读清单里有什么？", [keys["book"]], present=(BOOK,))
                await check("你之前记的阅读清单里有哪些？", [keys["book"]], present=(BOOK,))
                await check("我的工号是多少？", [keys["id"]], present=(EMPLOYEE,))
                await check("我的工号是多少。", [keys["id"]], present=(EMPLOYEE,))
                await check("请问，我之前让你记下的工号是多少？", [keys["id"]], present=(EMPLOYEE,))
                await check("我之前让你记下的工具箱编号是多少来着？", present=("多条",), absent=("QA-16", "QA-17"))
                await check("你之前记录的访客代号叫什么？", missing=True, absent=("QA-271",))
                await check("我之前让你叫我什么来着？", [keys["name"]], present=(NAME,))
                for plain_question in ("按我的工作饮品偏好推荐一杯茶。",
                                       "我的工作饮品偏好是什么，顺便介绍这种茶的做法。",
                                       "我的电脑型号是什么？", "成都明天会下雨吗？", "你想叫我什么？"):
                    assert memory_command(plain_question) is None, plain_question
                await check("记住我叫小顾了吗", absent=("红茶", "甜食", BOOK), missing=True)
                await check("记住我不喜欢甜食这件事了吗？", [keys["sweet"]], present=(SWEET,))
                await check("记住我喜欢甜食了吗？", absent=(SWEET,), missing=True)
                await check("记住我不喜欢酸味了吗？", absent=(SOUR,), missing=True)
                for text in ("记住了吗", "你记住了吗"):
                    await check(text, absent=("红茶", "温水", "甜食", "酸味", "你好吗", *keys.values()))
                await check("查记忆：甜食", [keys["sweet"]], present=(SWEET,))
                edited = await send(f"修改记忆：{RED} → {WHITE}")
                assert edited["memory_id"] == keys["drink"]
                await check("记住我的工作饮品偏好是红茶了吗？", absent=("白茶",), missing=True)
                await check("记住我的工作饮品偏好是白茶了吗？", [keys["drink"]], present=(WHITE,))
                previous_chat = moss.chat_id
                code, state = await endpoint.post("/api/new-chat", {})
                assert code == 200 and state["chat_id"] != previous_chat and not state["turns"]
                report["natural_recall_chat"] = state["chat_id"]
                await check("我的工作饮品偏好是什么？", [keys["drink"]], present=(WHITE,), absent=("红茶",))
                await check("你还记得我让你叫我什么吗？", [keys["name"]], present=(NAME,))
                report["before_restart"] = memory_rows(moss.db)
            else:
                assert memory_rows(moss.db) == report["before_restart"]
                assert moss.chat_id == report["natural_recall_chat"]
                await check("记住我坐高铁喜欢靠过道了吗？", [keys["rail"]], present=(AISLE,))
                await check("记住我的工作饮品偏好了吗？", [keys["drink"]], present=(WHITE,), absent=("红茶",))
                await check("我的工作饮品偏好是什么？", [keys["drink"]], present=(WHITE,), absent=("红茶",))
                await check("记住我不喜欢甜食这件事了吗？", [keys["sweet"]], present=(SWEET,))
                await send("撤回记忆：" + keys["drink"])
                await check("记住我的工作饮品偏好是白茶了吗？", absent=("温水",), missing=True)
                await check("我的工作饮品偏好是什么？", absent=("白茶", "温水"), missing=True)
                await check("查记忆：阅读清单", [keys["book"]], present=(BOOK,))
                remaining = memory_rows(moss.db)
                assert remaining == [row for row in report["before_restart"] if row["id"] != keys["drink"]]
                report["after_withdraw"] = remaining
            assert not report["blocked_connections"], report["blocked_connections"]
            report["passed"] = resume
    finally:
        result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    folder = args.resume or ROOT / "runs" / datetime.now().strftime("memory-checks-%Y%m%d-%H%M%S-%f")
    assert folder.resolve().parent == (ROOT / "runs").resolve() and folder.name.startswith("memory-checks-")
    if not args.resume:
        folder.mkdir()
    asyncio.run(verify(folder, bool(args.resume)))
    if not args.resume:
        subprocess.run([sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--resume", str(folder)],
                       check=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
        result = folder / "result.json"
        assert json.loads(result.read_text(encoding="utf-8"))["passed"]
        print(result, "通过", flush=True)
