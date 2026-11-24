"""Verify passive Windows media status in local overview responses and the FUI."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from local_media.library import MediaLibrary
from local_overview import parse_local_overview
from model_catalog import SPECS
from system_media import query_system_media, system_media_intent, system_media_text
from verify_context_reply import free_port
from verify_reminder_ordinals import serve_profile


CLOCK_FIRST = "现在几点，系统播放器正在播放什么？"
PLAYER_FIRST = "系统播放器正在播放什么，顺便告诉我现在几点？"
PLAYBACK = "system_media"
PLAYING_REPLY = "系统播放器正在播放，当前曲目是验收歌手的《安静的航站楼》。"
POSITIVE = (
    (CLOCK_FIRST, ["clock", PLAYBACK]),
    (PLAYER_FIRST, [PLAYBACK, "clock"]),
    ("现在几点系统播放器正在播放什么", ["clock", PLAYBACK]),
    ("外部播放器在放哪一首现在几点", [PLAYBACK, "clock"]),
    ("外部播放器的播放状态和电脑还有多少电", [PLAYBACK, "battery"]),
    ("系统音量多大、现在 Windows 播放器的当前曲目", ["volume", PLAYBACK]),
)
PROTECTED = (
    "系统播放器、现在几点",
    "现在几点、现有外部播放器",
    "不要查系统播放器正在播放什么、现在几点",
    "翻译“系统播放器正在播放什么”，然后告诉我现在几点",
    "暂停系统播放器、现在几点",
    "现在几点、让外部播放器继续播放",
    "Windows 播放器下一首、电脑还有多少电",
    "系统播放器正在播放什么、现在几点、锁屏",
    "系统播放器正在播放什么、取消全部提醒",
)


async def terminal_turn(server, text):
    code, state = await server.post("/api/message", {"text": text})
    assert code == 200, state
    turn = state["turns"][-1]
    ident = turn["id"]
    async with asyncio.timeout(10):
        while turn["status"] == "thinking":
            await asyncio.sleep(0.025)
            turn = next(row for row in (await server.state())["turns"] if row["id"] == ident)
    assert turn["status"] == "replied" and not turn["cloud_requests"]["total"], turn
    return turn


async def check_browser(server, folder, candidate, started, release):
    calls = []
    errors = []
    async with async_playwright() as playwright:
        edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
        browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                                                    args=["--mute-audio", "--disable-gpu"])
        await confirm_muted_edge(browser)
        try:
            page = await browser.new_page(viewport={"width":1440,"height":900}, reduced_motion="reduce")
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script('localStorage.setItem("moss.voice.enabled","off")')

            async def route_request(route):
                url = urlsplit(route.request.url)
                if ((url.hostname, url.port) != ("127.0.0.1", server.port)
                        or url.path.startswith(("/api/speak", "/api/prepare-speech", "/api/transcri"))):
                    await route.abort()
                elif url.path == "/static/app.js":
                    calls.append("candidate")
                    await route.fulfill(body=candidate.read_bytes(), content_type="text/javascript; charset=utf-8")
                elif url.path == "/api/weather/current":
                    await route.fulfill(status=502, json={"error":"隔离测试停用常驻天气。"})
                else:
                    await route.continue_()

            await page.route("**/*", route_request)
            await page.goto(server.url + "/?chat=1&scene=off")
            await page.get_by_test_id("composer").fill(PLAYER_FIRST)
            await page.get_by_test_id("send").click()
            await asyncio.wait_for(started.wait(), 5)
            panel = page.get_by_test_id("local-status-panel")
            player = panel.locator('[data-reading-kind="system_media"]')
            clock = panel.locator('[data-reading-kind="clock"]')
            await expect(clock).to_be_visible()
            await expect(player).to_have_count(0)
            await expect(panel).to_have_attribute("data-phase", "reading")
            release.set()
            await expect(player.locator("dt")).to_have_text("系统播放器")
            await expect(player.locator("dd")).to_have_text(PLAYING_REPLY)
            await expect(panel).to_have_attribute("data-phase", "complete")
            assert await panel.locator(".status-reading").evaluate_all("(rows) => rows.map(row => row.dataset.readingKind)") == [
                PLAYBACK, "clock"]
            assert await page.locator("dialog[open]").count() == 1
            await page.screenshot(path=folder / "播放器和时间-1440.png", animations="disabled")
            await page.set_viewport_size({"width":375,"height":812})
            assert await panel.evaluate("""node => {
                const rect = node.getBoundingClientRect();
                return rect.left >= 0 && rect.right <= innerWidth && node.scrollWidth <= node.clientWidth;
            }""")
            await page.screenshot(path=folder / "播放器和时间-375.png", animations="disabled")
            await page.keyboard.press("Escape")
            await expect(panel).to_be_hidden()
        finally:
            release.set()
            await browser.close()
    assert calls and not errors, (calls, errors)


async def run(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("system-playback-overview-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result)
    report = {"passed":False, "model_connections":0, "reads":[], "cases":[], "protected":list(PROTECTED)}
    mode = "actual"
    started, release = asyncio.Event(), asyncio.Event()

    async def model_guard(_, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def status_reader(action, **options):
        assert action == "status" and not options, (action, options)
        if mode == "actual":
            row = await query_system_media(action)
            if row["code"] == "status":
                row = {"code":"status", "before":{"state":(row.get("before") or {}).get("state")}}
        else:
            if mode == "held":
                started.set()
                await release.wait()
            row = {"code":"status", "before":{"state":"Playing", "title":"安静的航站楼", "artist":"验收歌手"}}
        report["reads"].append({"mode":mode,"result":row["code"],"reply":system_media_text(action,row)})
        return row

    assert all(parse_local_overview(text) is None for text in PROTECTED)
    assert system_media_intent("系统播放器") == "status"
    assert system_media_intent("暂停系统播放器") == "pause"
    assert system_media_intent("继续刚才暂停的外部播放器") == "resume_paused"
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    try:
        async with AsyncExitStack() as stack:
            guard = await stack.enter_async_context(await asyncio.start_server(model_guard,"127.0.0.1",0))
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            env = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "MOSS_MEDIA_DB":str(catalog), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0"}
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    env[prefix+"_BASE_URL"] = model_url
                    env[prefix+"_API_KEY"] = "isolated-no-real-provider"
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch("model_catalog.allowed", side_effect=lambda _, url: url == model_url))
            stack.enter_context(patch("system_media.query_system_media", status_reader))
            server = BrowserServer(folder, free_port(), model_url=model_url)
            async with serve_profile(server, model_url) as moss:
                code, _ = await server.post("/api/select-model", {"id":"deepseek"})
                assert code == 200 and moss.model_id == "deepseek"
                separate = await terminal_turn(server, "系统播放器正在播放什么")
                assert separate["source"] == "context" and not separate.get("local_overview"), separate
                assert separate["reply"] == report["reads"][-1]["reply"]
                for pass_kind in ("actual","fixture"):
                    mode = pass_kind
                    for text, expected in POSITIVE if mode == "fixture" else POSITIVE[:2]:
                        before = len(report["reads"])
                        turn = await terminal_turn(server, text)
                        readings = turn["local_overview"]
                        assert turn["source"] == "context" and [row["kind"] for row in readings] == expected, turn
                        assert [row["reply"] for row in readings] == turn["reply"].splitlines(), turn
                        assert len(report["reads"]) == before + 1 and not turn.get("media"), turn
                        observed = next(row["reply"] for row in readings if row["kind"] == PLAYBACK)
                        assert observed == report["reads"][-1]["reply"], turn
                        report["cases"].append({"text":text,"mode":mode,"readings":readings})
                if candidate:
                    mode = "held"
                    await check_browser(server, folder, candidate, started, release)
                    report["candidate_app_sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
                assert report["model_connections"] == 0, report
            report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-app", type=Path, help="Render a staged app.js in isolated muted Edge")
    args = parser.parse_args()
    asyncio.run(run(args.candidate_app))
