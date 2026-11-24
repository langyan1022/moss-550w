import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from time import perf_counter

from browser_lab import BrowserServer, ROOT


async def verify_directory(server, folder, ask, report):
    import _winapi
    from stat import IO_REPARSE_TAG_MOUNT_POINT
    from context_tools import DIRECTORY_LIMIT, DIRECTORY_PAGE, list_directory, parse_context

    sample = folder / "目录样本"
    sample.mkdir()
    expected = []
    for number in reversed(range(47)):
        is_dir = number % 7 == 0
        name = ["文档", "Alpha", "beta"][number % 3] + f"-{number:02d}"
        if is_dir:
            (sample / name).mkdir()
        else:
            (sample / name).touch()
        expected.append((is_dir, name))
    expected.sort(key=lambda row: (not row[0], row[1].casefold(), row[1]))
    expected_names = [name + ("\\" if is_dir else "") for is_dir, name in expected]
    state = await server.state()
    assert state["model_id"] == "local"
    original = state["chat_id"]
    spoken_path = f"{sample.drive[0]} 盘的 {sample.relative_to(sample.anchor)}"
    assert parse_context("列出目录 G 盘") == parse_context("列出目录 G盘") == {"action":"directory", "path":"G:\\"}
    first = await ask(f"列出目录 {spoken_path}")
    assert first["reply"].splitlines()[1:] == expected_names[:DIRECTORY_PAGE]
    assert first["source"] == "context" and "下一页" in first["reply"]
    cursor_bytes = len(json.dumps(first["directory_page"], ensure_ascii=False).encode("utf-8"))
    assert cursor_bytes < 1024 and all(name not in str(first["directory_page"]) for name in expected_names)
    status, new = await server.post("/api/new-chat", {})
    assert status == 200 and new["chat_id"] != original
    assert "先列出" in (await ask("下一页"))["reply"]
    rejected, _ = await server.post("/api/message", {"chat_id":original, "text":"下一页"})
    assert rejected == 409
    status, restored = await server.post("/api/switch-chat", {"id":original})
    assert status == 200 and restored["chat_id"] == original
    before = server.pid
    await server.stop()
    await server.start()
    assert before != server.pid and (await server.state())["chat_id"] == original
    second = await ask("还有哪些？")
    assert second["source"] == "context" and second["reply"].splitlines()[1:] == expected_names[DIRECTORY_PAGE:]
    assert first["reply"].splitlines()[1:] + second["reply"].splitlines()[1:] == expected_names
    assert "已列完" in (await ask("下一页"))["reply"]
    await ask(f"列出目录 {sample}")
    assert (await ask("谢谢你！"))["reply"] == "不客气。"
    assert (await ask("多谢。"))["reply"] == "不客气。"
    after_thanks = await ask("还有哪些文件")
    assert after_thanks["source"] == "context" and after_thanks["reply"].splitlines()[1:] == expected_names[DIRECTORY_PAGE:]
    await ask(f"查看目录 {sample}")
    (sample / "刚添加.txt").touch()
    changed = await ask("看看下一页")
    assert "已变化" in changed["reply"] and not changed["directory_page"]
    await ask("谢谢")
    assert "先列出" in (await ask("下一页"))["reply"]
    await ask(f"列出目录 {sample}")
    await ask("在吗")
    assert "先列出" in (await ask("目录下一页"))["reply"]
    assert parse_context("还有哪些", {"source":"media","status":"replied"}) is None
    assert parse_context("还有哪些电影", first) is None
    empty = folder / "空目录"
    empty.mkdir()
    await ask(f"列出目录 {sample}")
    assert "空目录" in (await ask(f"列出目录 {empty}"))["reply"]
    assert "上次查看的目录为空" in (await ask("下一页"))["reply"]
    missing = await ask(f"列出目录 {folder / '没有这个目录'}")
    assert "找不到" in missing["reply"] and not missing["directory_page"]
    assert "先列出" in (await ask("下一页"))["reply"]

    junction = folder / "切换目标"
    _winapi.CreateJunction(str(sample), str(junction))
    await ask(f"列出目录 {junction}")
    assert junction.lstat().st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT
    assert junction.parent == folder and junction.resolve(strict=True) == sample
    junction.rmdir()
    (empty / "第二个目标的名字.txt").touch()
    _winapi.CreateJunction(str(empty), str(junction))
    moved = await ask("下一页")
    assert "位置已变化" in moved["reply"] and "第二个目标" not in moved["reply"]

    large = folder / "超过上限"
    large.mkdir()
    for number in range(DIRECTORY_LIMIT + 19):
        (large / f"测试-{number:04d}").touch()
    wide = await ask(f"列出目录 {large}")
    assert wide["directory_page"]["limited"] and f"先读取的 {DIRECTORY_LIMIT} 项" in wide["reply"]
    names = wide["reply"].splitlines()[1:]
    while wide["directory_page"]["more"]:
        wide = await ask("下一页")
        names.extend(wide["reply"].splitlines()[1:])
    assert names == sorted(set(names)) and len(names) == DIRECTORY_LIMIT
    assert f"仅列入先读取的 {DIRECTORY_LIMIT} 项" in (await ask("下一页"))["reply"]
    clipboard = await ask("读一下剪贴板")
    assert clipboard["source"] == "context" and clipboard["context_action"] == "clipboard"
    assert "点击读取" in clipboard["reply"] and "directory_page" not in clipboard
    durations = {}
    for target in (sample, large):
        readings = []
        for _ in range(5):
            started = perf_counter()
            list_directory({"action":"directory", "path":str(target)})
            readings.append(round((perf_counter() - started) * 1000, 2))
        durations[target.name] = readings
    report.update(page_size=DIRECTORY_PAGE, scan_limit=DIRECTORY_LIMIT, verified_entries=len(expected),
                  large_actual=DIRECTORY_LIMIT + 19, large_listed=len(names), cursor_bytes=cursor_bytes,
                  direct_ntfs_ms=durations, pids=[before, server.pid], stale_chat_http=rejected)


async def main(directory_only=False):
    folder = ROOT / "runs" / datetime.now().strftime("holo-daily-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18562)
    report = {"passed": False, "cases": []}
    gate = None
    if directory_only:
        from local_media.library import MediaLibrary
        from verify_configured_mcp import isolated

        server = isolated(folder)
        folder.mkdir()
        catalog = folder / "空媒体索引.sqlite3"
        MediaLibrary(catalog)
        os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0",
                          MOSS_PROVIDER_DIR=str(folder / "隔离模型配置"))
        report.update(scope="本机 NTFS，真实 HTTP，独立进程重启，隔离文件与 NTFS 目录连接。", model_connections=0)

        async def audit_model(reader, writer):
            report["model_connections"] += 1
            writer.close()
            await writer.wait_closed()

        gate = await asyncio.start_server(audit_model, "127.0.0.1", 0)
        server.model_url = f"http://127.0.0.1:{gate.sockets[0].getsockname()[1]}/v1"

    async def ask(text):
        start = perf_counter()
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200, state.get("error")
        turn = state["turns"][-1]
        async with asyncio.timeout(150):
            while turn["status"] == "thinking":
                await asyncio.sleep(.015 if directory_only else .08)
                turn = (await server.state())["turns"][-1]
        report["cases"].append({"ask": text, "answer": turn.get("reply"), "source": turn.get("source"),
                                "tools": turn.get("holo_tools"), "ms": round((perf_counter()-start)*1000)})
        assert turn["status"] == "replied", turn.get("error")
        return turn

    try:
        await server.start()
        if directory_only:
            await verify_directory(server, folder, ask, report)
            assert report["model_connections"] == 0
            report["passed"] = True
            return
        status, state = await server.post("/api/select-model", {"id": "deepseek"})
        assert status == 200 and state["agent_mode"] == "holojarvis"
        capabilities = await ask("你现在有哪些工具？")
        assert "list_capabilities" in capabilities["holo_tools"] and "回复" in capabilities["reply"]
        clock = await ask("读取本机时钟，告诉我现在的日期和时间。")
        assert "get_time" in clock["holo_tools"] and str(datetime.now().year) in clock["reply"]
        weather = await ask("帮我实际查一下上海明天的天气，大概多少度？")
        assert "get_weather" in weather["holo_tools"] and "上海" in weather["reply"]
        tomorrow = datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=1)
        date_in_reply = (tomorrow.isoformat(), f"{tomorrow.month}月{tomorrow.day}日")
        assert any(label in weather["reply"] for label in date_in_reply), weather["reply"]
        assert re.search(r"-?\d+(?:\.\d+)?\s*(?:°\s*C?|℃|摄氏度|度)", weather["reply"]), weather["reply"]
        plan = await ask("我有三十分钟，想散步和练琴，给我一个时间安排。")
        assert plan["source"] == "holo" and "散步" in plan["reply"] and "练琴" in plan["reply"]
        follow = await ask("把刚才的安排压成一句话，保留顺序和时长。")
        assert "散步" in follow["reply"] and "练琴" in follow["reply"]
        await server.stop()
        await server.start()
        assert (await server.state())["model_id"] == "deepseek"
        volume = await ask("系统音量是多少")
        assert volume["source"] == "desktop" and "%" in volume["reply"]
        directory = await ask(f"列出目录 {folder}")
        assert directory["source"] == "context" and "data" in directory["reply"]
        report["passed"] = True
    finally:
        await server.stop()
        if gate:
            gate.close()
            await gate.wait_closed()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"])


if __name__ == "__main__":
    asyncio.run(main("--directory-only" in sys.argv))
