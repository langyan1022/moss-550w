import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from time import perf_counter
import _winapi

from browser_lab import ROOT
from context_tools import parse_context
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated


REQUEST = "按类型整理这个目录，先给我看计划"


def snapshot(root):
    entries = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            info = entry.lstat()
            key = entry.relative_to(root).as_posix()
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                detail = ["链接", os.readlink(entry)]
            elif entry.is_dir():
                detail = ["目录"]
                pending.append(entry)
            else:
                detail = ["文件", info.st_size, hashlib.sha256(entry.read_bytes()).hexdigest()]
            entries[key] = [info.st_dev, info.st_ino, info.st_mtime_ns, *detail]
    raw = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_files(folder, names):
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(f"隔离文件分类样本 {name}\n".encode("utf-8"))
    return folder


def focus_is(turn, path):
    focus = turn["directory_focus"]
    actual = path.resolve(strict=True)
    info = actual.stat()
    assert Path(focus["path"]) == path, focus
    real, device, inode = focus["identity"]
    assert Path(real) == actual and (device, inode) == (info.st_dev, info.st_ino), focus


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("file-organization-%Y%m%d-%H%M%S-%f")
    fixtures = folder / "测试目录"
    fixtures.mkdir(parents=True)
    mixed = create_files(fixtures / "混合 文件", (
        "会议记录.txt", "封面.PNG", "试听.wav", "片头.mp4", "附件.zip", "待定.zzz", "Report.TXT"))
    create_files(mixed / "文档", ["report.txt"])
    create_files(mixed / "已归档", ["子目录专用文件.txt"])
    external = create_files(fixtures / "联接目标", ["联接内部标记.txt"])
    junction = mixed / "资料联接"
    _winapi.CreateJunction(str(external), str(junction))
    assert junction.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    assert junction.resolve(strict=True) == external
    blocked = create_files(fixtures / "分类目标不可用", ["海报.jpg", "片段.mkv", "视频"])
    _winapi.CreateJunction(str(external), str(blocked / "图片"))
    paged = create_files(fixtures / "分页目录", [f"资料-{n:02}.txt" for n in range(47)])
    empty = create_files(fixtures / "空目录", ())
    original_path = create_files(fixtures / "身份检查", ["最初标记.txt"])
    catalog = folder / "空索引.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0")
    server = isolated(folder / "app")
    report = {"passed":False, "scope":"本机 HTTP、合成文件名、Windows NTFS 目录联接、按扩展名分类预览",
              "app_pids":[], "model_connections":0, "cases":[]}
    result = folder / "result.json"
    print(result, flush=True)

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(chat_id, text, source="context"):
        before = snapshot(fixtures)
        tick = perf_counter()
        code, state = await server.post("/api/message", {"chat_id":chat_id, "text":text})
        assert code == 200, (code, state)
        turn = state["turns"][-1]
        turn_id = turn["id"]
        async with asyncio.timeout(15):
            while turn["status"] == "thinking":
                await asyncio.sleep(.04)
                turn = (await server.state())["turns"][-1]
        elapsed = round((perf_counter() - tick) * 1000, 1)
        after = snapshot(fixtures)
        row = {"text":text, "chat_id":chat_id, "turn_id":turn_id, "reply":turn.get("reply"),
               "source":turn.get("source"), "status":turn["status"], "error":turn.get("error"),
               "cloud_requests":turn["cloud_requests"], "holo_tools":turn.get("holo_tools"),
               "file_plan":turn.get("file_plan"),
               "directory_focus":turn.get("directory_focus"), "before":before, "after":after, "ms":elapsed}
        report["cases"].append(row)
        assert turn["id"] == turn_id and turn["chat_id"] == chat_id, row
        assert before == after, row
        assert turn["source"] == source and turn["status"] == "replied", row
        assert turn["cloud_requests"]["total"] == report["model_connections"] == 0, row
        return turn

    async def plan(chat_id, path, *, explicit=False):
        text = f"按类型整理 {path}，先给我看计划" if explicit else REQUEST
        turn = await ask(chat_id, text)
        focus_is(turn, path)
        assert isinstance(turn.get("file_plan"), dict) and turn["file_plan"], turn
        assert "子目录专用文件" not in turn["reply"] and "联接内部标记" not in turn["reply"], turn
        return turn

    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            server.model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = server.model_url
            await server.start()
            report["app_pids"].append(server.pid)
            state = await server.state()
            chat = state["chat_id"]
            assert state["model_id"] == "local" and state["memory_backend"] == "sqlite"
            capabilities = await ask(chat, "你现在有哪些工具？", "holo")
            assert "list_capabilities" in capabilities["holo_tools"] and "预览文件分类" in capabilities["reply"], capabilities
            first = await plan(chat, mixed, explicit=True)
            for label in ("文档", "图片", "音频", "视频", "压缩包", "冲突"):
                assert label in first["reply"], first["reply"]
            assert re.search(r"未分类|保留|未知|其他|未识别", first["reply"]), first["reply"]
            assert "report.txt" in first["reply"].casefold(), first["reply"]
            detail = first["file_plan"]
            assert (detail["scanned"], detail["suggested"], detail["conflicts"], detail["omitted"]) == (10, 5, 1, 0), detail
            assert detail["status"] == "preview" and not detail["limited"]
            assert detail["skipped"] == {"directory":2, "reparse":1, "unknown_type":1}, detail
            expected = {"会议记录.txt":"文档", "封面.PNG":"图片", "试听.wav":"音频", "片头.mp4":"视频", "附件.zip":"压缩包"}
            suggestions = {item["name"]:item for item in detail["items"] if item["status"] == "suggested"}
            assert {key:item["category"] for key, item in suggestions.items()} == expected, suggestions
            assert all(Path(suggestions[key]["destination"]) == Path(category) / key for key, category in expected.items())
            conflicts = [(item["name"], item["reason"]) for item in detail["items"] if item["status"] == "conflict"]
            assert conflicts == [("Report.TXT", "name_exists")], conflicts
            again = await plan(chat, mixed)
            assert first["file_plan"] == again["file_plan"], again
            await server.stop()
            await server.start()
            report["app_pids"].append(server.pid)
            assert len(set(report["app_pids"])) == 2 and (await server.state())["chat_id"] == chat
            assert (await plan(chat, mixed))["file_plan"] == first["file_plan"]
            (mixed / "新增记录.md").write_text("隔离回归新增记录", encoding="utf-8")
            updated = (await plan(chat, mixed))["file_plan"]
            assert updated["suggested"] == 6 and any(item["name"] == "新增记录.md" for item in updated["items"]), updated
            code, fresh = await server.post("/api/new-chat", {})
            assert code == 200 and fresh["chat_id"] != chat
            no_focus = await ask(fresh["chat_id"], REQUEST)
            assert not no_focus.get("file_plan") and mixed.name not in no_focus["reply"]
            assert re.search(r"列出|路径|先.*(?:文件夹|目录)", no_focus["reply"]), no_focus
            assert (await server.post("/api/switch-chat", {"id":chat}))[0] == 200
            listing = await ask(chat, f"列出目录 {paged}")
            assert listing["directory_page"]["more"] and listing["directory_page"]["identity"], listing
            assert (await plan(chat, paged))["file_plan"]["suggested"] == 47
            await ask(chat, f"列出目录 {paged}")
            last_page = await ask(chat, "下一页")
            assert not last_page["directory_page"]["more"] and "资料-46.txt" in last_page["reply"], last_page
            planned = await plan(chat, paged)
            detail = planned["file_plan"]
            assert detail["scanned"] == detail["suggested"] == 47 and "47" in planned["reply"], planned
            assert 0 < len(detail["items"]) < 47 and len(detail["items"]) + detail["omitted"] == 47, detail
            await ask(chat, f"列出目录 {empty}")
            empty_plan = await plan(chat, empty)
            assert re.search(r"空|没有|0", empty_plan["reply"]), empty_plan
            assert empty_plan["file_plan"]["scanned"] == 0 and not empty_plan["file_plan"]["items"]
            blocked_plan = (await plan(chat, blocked, explicit=True))["file_plan"]
            assert blocked_plan["conflicts"] == 2 and blocked_plan["suggested"] == 0, blocked_plan
            assert {row["name"]:row["reason"] for row in blocked_plan["items"]} == {
                "海报.jpg":"destination_reparse", "片段.mkv":"destination_not_directory"}, blocked_plan
            identity = (await plan(chat, original_path, explicit=True))["directory_focus"]["identity"]
            saved_path = fixtures / "身份检查原件"
            assert original_path.resolve().parent == saved_path.resolve().parent == fixtures.resolve()
            assert not saved_path.exists() and not original_path.is_symlink()
            original_path.rename(saved_path)
            create_files(original_path, ["替换目标专用文件.txt"])
            assert original_path.stat().st_ino != identity[2]
            rejected = await ask(chat, REQUEST)
            assert not rejected.get("file_plan") and "替换目标专用文件" not in rejected["reply"], rejected
            assert re.search(r"变化|改变|重新|更换|换过", rejected["reply"]), rejected
            report["unrelated"] = {}
            for text in ("按类型整理会议纪要中的行动项", "整理会议纪要，先给我看计划", "请把会议纪要按类型整理"):
                intent = parse_context(text)
                report["unrelated"][text] = intent
                assert intent is None or intent["action"] != "file_plan", (text, intent)
            assert report["model_connections"] == 0
            report["passed"] = True
    finally:
        try:
            await server.stop()
        finally:
            result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(result, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
