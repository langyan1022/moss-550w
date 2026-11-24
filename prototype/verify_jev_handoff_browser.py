"""Exercise Jev's review dialog, HTTP receipt and native editor in isolation."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
import os
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientSession, web
from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss, now
import jev_handoff
from local_media.library import MediaLibrary
from serve import application
from verify_context_reply import free_port


REPLY = "收到，谢谢！\n第二行：明天 9 点见。🙂"
COMPOSER = "保留我正在输入的这句话"
ENDPOINT = "/api/context/jev-handoff"


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("jev-handoff-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result_path = folder / "result.json"
    print(result_path, flush=True)
    report = {"passed":False, "page_errors":[], "model_connections":0, "native_attempts":[],
              "outside_requests":[]}
    editors = {}
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    params = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_MEDIA_DB":str(catalog),
              "MOSS_PROVIDER_DIR":str(folder / "providers"), "MOSS_MEM0_ENABLED":"0",
              "MOSS_WINDOWS_REMINDERS":"0", "MOSS_KEV_ENABLED":"0", "MOSS_SPEECH_PREPARE":"0"}
    real_open = jev_handoff.open_draft_window
    real_uia = jev_handoff.uia_session

    async def offscreen(text, target, still_current):
        return await real_open(text, target, still_current, offscreen=True)

    @asynccontextmanager
    async def trace_native():
        async with real_uia() as send:
            async def command(message):
                if message["mode"] == "arm":
                    editors[message["pid"]] = (psutil.Process(message["pid"]).create_time(), message["target_id"])
                began = perf_counter()
                result = await send(message)
                report["native_attempts"].append({"mode":message["mode"], "status":result.get("status"),
                                                  "sha256":result.get("sha256"), "bytes":result.get("bytes"),
                                                  "ms":round((perf_counter()-began)*1000, 1)})
                return result
            yield command

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    @asynccontextmanager
    async def server(model_url):
        moss = Moss(folder / "data", model_url, "test-only", prefer_cloud=False)
        runner = web.AppRunner(application(moss, lambda: "standby", folder / "data", port, "jev-test", asyncio.Event()),
                               access_log=None, shutdown_timeout=1)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            yield moss
        finally:
            await runner.cleanup()
            await moss.close()

    async def post(body, *, origin=url):
        headers = {"Origin":origin} if origin else {}
        async with ClientSession() as client, client.post(url + ENDPOINT, json=body, headers=headers) as response:
            return response.status, await response.json() if response.content_type == "application/json" else {}

    try:
        with patch.dict(os.environ, params), patch.object(jev_handoff, "open_draft_window", offscreen), \
                patch.object(jev_handoff, "uia_session", trace_native):
            async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as sink:
                model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
                async with server(model_url) as moss:
                    chat = moss.chat_id
                    first, newest, other = (uuid4().hex for _ in range(3))
                    with moss.db:
                        for turn_id, reply, source in ((first, REPLY, "jev"), (newest, "另一轮已完成的拟稿。", "jev"),
                                                       (other, "普通回覆。", "smalltalk")):
                            moss._insert_turn({"chat_id":chat, "id":turn_id, "user":"隔离验证",
                                               "source":source, "status":"replied", "created_at":now(),
                                               "reply":reply, "jev":{"candidate_count":1} if source == "jev" else None})

                    async with async_playwright() as playwright:
                        browser = await open_browser(playwright)
                        try:
                            await confirm_muted_edge(browser)
                            page = await browser.new_page(viewport={"width":1280, "height":850}, reduced_motion="reduce")
                            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                            requests = []

                            async def local(route):
                                target = urlsplit(route.request.url)
                                if target.scheme in {"http", "https"} and target.netloc != f"127.0.0.1:{port}":
                                    report["outside_requests"].append(target.netloc)
                                    await route.abort()
                                    return
                                if target.path == ENDPOINT:
                                    requests.append(route.request.post_data_json)
                                await route.continue_()

                            await page.route("**/*", local)
                            await page.goto(url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                            button = page.locator(f'[data-turn-id="{first}"]').get_by_test_id("jev-handoff-open")
                            await expect(page.get_by_test_id("jev-handoff-open")).to_have_count(2)
                            await page.get_by_test_id("composer").fill(COMPOSER)
                            panel = page.get_by_test_id("jev-handoff-panel")
                            await button.click()
                            await expect(panel).to_be_visible()
                            assert await page.get_by_test_id("jev-handoff-preview").text_content() == REPLY
                            await page.keyboard.press("Escape")
                            await expect(panel).to_be_hidden()
                            await expect(button).to_be_focused()
                            assert not requests and not editors
                            await button.click()
                            await page.screenshot(path=folder / "jev-review.png", animations="disabled")
                            began = perf_counter()
                            await page.get_by_test_id("jev-handoff-confirm").click()
                            await expect(panel).to_have_attribute("data-phase", "filled", timeout=25000)
                            report["from_confirm_to_receipt_ms"] = round((perf_counter()-began)*1000, 1)
                            assert len(requests) == 1 and set(requests[0]) == {"operation_id", "chat_id", "turn_id"}
                            assert requests[0]["chat_id"] == chat and requests[0]["turn_id"] == first
                            await page.screenshot(path=folder / "jev-receipt.png", animations="disabled")
                            await page.get_by_test_id("jev-handoff-close").click()
                            await expect(button).to_be_focused()
                            await expect(page.get_by_test_id("composer")).to_have_value(COMPOSER)
                        finally:
                            await browser.close()

                    operation = requests[0]
                    expected = {"status":"filled", "bytes":len(REPLY.encode("utf-8"))}
                    assert await post(operation) == (200, expected)
                    assert await post(operation, origin="http://example.invalid") == (403, {})
                    assert await post(operation, origin=None) == (403, {})
                    assert (await post({**operation, "text":"不可信正文"}))[0] == 400
                    assert (await post({**operation, "turn_id":newest}))[0] == 400
                    assert (await post({**operation, "turn_id":other, "operation_id":str(uuid4())}))[0] == 400
                    unknown = {**operation, "operation_id":str(uuid4())}
                    with moss.db:
                        moss.db.execute("INSERT INTO jev_handoffs VALUES(?,?,?,NULL)", (unknown["operation_id"], chat, first))
                    assert len(moss.state()["turns"]) == 3
                async with server(model_url) as restarted:
                    assert await post(operation) == (200, expected)
                    assert (await post(unknown))[1]["status"] == "uncertain"
                    await restarted.new_chat()
                    assert (await post(operation))[0] == 400
                arm, fill = report["native_attempts"]
                assert len(editors) == 1 and arm["status"] == "armed" and fill["status"] == "filled"
                assert fill["sha256"] == hashlib.sha256(REPLY.encode("utf-8")).hexdigest()
                assert fill["bytes"] == expected["bytes"]
                assert not any(report[key] for key in ("outside_requests", "page_errors", "model_connections")), report
                report["passed"] = True
    finally:
        for pid, (created, target) in editors.items():
            try:
                process = psutil.Process(pid)
                args = process.cmdline()
                if (process.create_time() == created and "-Offscreen" in args and target in args and
                        str(jev_handoff.EDITOR) in args):
                    process.terminate()
                    process.wait(5)
            except psutil.NoSuchProcess:
                pass
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result_path, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
