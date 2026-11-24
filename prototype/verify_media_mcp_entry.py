import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
import os
import socket
import sqlite3
import sys
from time import perf_counter

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import DEFAULT_DB, MediaLibrary
from verify_memory import send


def sidecars(index):
    workers = []
    for proc in psutil.process_iter():
        try:
            args = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "local_media.mcp_search" in args and str(index) in args:
            workers.append(proc.pid)
    return workers


async def verify_sdk(index):
    params = StdioServerParameters(command=sys.executable, args=["-m", "local_media.mcp_search", str(index)], cwd=ROOT)
    async with asyncio.timeout(10), stdio_client(params) as channels:
        async with ClientSession(*channels) as client:
            ready = await client.initialize()
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            assert ready.serverInfo.name == "moss_media_readonly"
            assert names == ["list_sources", "search_media"]
            reply = await client.call_tool("search_media", {"query":"西海情歌", "kind":"audio"})
            assert not reply.isError and isinstance(reply.structuredContent, dict)
            rows = reply.structuredContent["items"]
            assert rows and all(set(row) == {"title", "artist", "kind", "source_kind"} for row in rows)
            assert any(row["title"] == "西海情歌" and row["source_kind"] == "local" for row in rows)
            assert (await client.call_tool("play_media", {})).isError
            assert (await client.call_tool("search_media", {"query":"！", "kind":"audio"})).isError
            workers = sidecars(index)
            assert workers
    assert not sidecars(index)
    assert all(not psutil.pid_exists(pid) for pid in workers)
    return {"sdk_version":version("mcp"), "server":ready.serverInfo.name,
            "tools":names, "structured":reply.structuredContent, "play_denied":True, "invalid_denied":True,
            "process_exited":True}


async def verify(browser_enabled):
    folder = ROOT / "runs" / datetime.now().strftime("media-mcp-entry-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    catalog = folder / "catalog.sqlite3"
    with closing(sqlite3.connect(DEFAULT_DB.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(catalog)) as copy:
            source.backup(copy)
    indexed = MediaLibrary(catalog, read_only=True)
    assert any(item["kind"] == "local" and item["path"].upper().startswith("G:") for item in indexed.sources())
    digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    with closing(indexed.connect()) as db:
        try:
            db.execute("DELETE FROM media WHERE 1=0")
        except sqlite3.OperationalError as error:
            assert "readonly" in str(error)
        else:
            raise AssertionError("MCP 索引连接允许写入")
    os.environ["MOSS_MEDIA_DB"] = str(catalog)
    os.environ["MOSS_WINDOWS_REMINDERS"] = "0"
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    server = BrowserServer(folder, port, model_url="http://127.0.0.1:9/v1")
    report = {"passed":False, "browser":browser_enabled, "api":[], "integration":"每次通过标准 stdio MCP 打开只读媒体索引",
              "no_cloud_or_gpu":True, "index_readonly":True}

    async def api(text):
        start = perf_counter()
        status, state = await server.post("/api/message", {"text":text})
        assert status == 200
        turn = state["turns"][-1]
        async with asyncio.timeout(12):
            while turn["status"] == "thinking":
                await asyncio.sleep(.06)
                turn = (await server.state())["turns"][-1]
        assert turn["status"] == "replied" and turn["source"] == "context", turn
        assert not any(turn.get(key) for key in ("media", "holo_tools", "receipt", "partial_reply"))
        report["api"].append({"message":text, "reply":turn["reply"], "wall_ms":round((perf_counter()-start)*1000, 1)})
        return turn["reply"]

    try:
        report["stdio"] = await verify_sdk(catalog)
        await server.start()
        assert not (await server.state())["turns"] and not (server.data / "media-playback.sqlite3").exists()
        assert not sidecars(catalog)
        if browser_enabled:
            async with async_playwright() as pw:
                browser = await open_browser(pw)
                try:
                    await confirm_muted_edge(browser)
                    page = await browser.new_page(reduced_motion="reduce")
                    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                    page_errors = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                    reply = await send(page, server, "用 MCP 查本地音乐 西海情歌")
                    assert "本机媒体 MCP · 找到" in reply and "西海情歌" in reply and not page_errors
                    assert not await page.locator(".media-audio, .media-video").count()
                    await page.screenshot(path=folder/"context-result.png", timeout=8000)
                    report["visible"] = reply
                finally:
                    await browser.close()
        else:
            listed = await api("列出 MCP 工具")
            assert "本机媒体 MCP 已连接" in listed and "索引来源" in listed
            reply = await api("用 MCP 查本地音乐 西海情歌")
            assert "西海情歌" in reply and "本机媒体 MCP · 找到" in reply
            changed = await api("用 MCP 查本地音乐 冲动的惩罚")
            assert "冲动的惩罚" in changed and "西海情歌" not in changed
            missing = await api("通过 MCP 查本地音乐 QA绝不会存在的空曲名")
            assert "没有找到匹配的可读媒体" in missing
            sources = await api("通过 MCP 查看本地媒体来源")
            assert "本机" in sources and "已索引" in sources
            assert "请输入" in await api("用 MCP 查本地音乐")
        assert not sidecars(catalog)
        ordinary = await server.post("/api/message", {"text":"在吗"})
        assert ordinary[0] == 200 and ordinary[1]["turns"][-1]["source"] == "smalltalk"
        assert not sidecars(catalog)
        assert not (server.data / "media-playback.sqlite3").exists()
        await server.stop()
        assert hashlib.sha256(catalog.read_bytes()).hexdigest() == digest
        report.update(passed=True, same_index_sha256=digest, playback_db_created=False, sidecar_idle=False)
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    asyncio.run(verify(parser.parse_args().browser))
