"""Exercise local memory through the actual memory panel and chat composer."""

import asyncio
from datetime import datetime
import json
import os
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_memory import new_chat, send
from verify_memory_checks import RED, WHITE
from verify_memory_history_provenance import free_port


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("memory-browser-local-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(LOCALAPPDATA=str(folder / "profile"), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0")
    result = {"passed": False, "model_connections":0, "page_errors":[], "outside_requests":[], "replies":[]}

    async def guard(reader, writer):
        result["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(guard, "127.0.0.1", 0) as trap:
        server = BrowserServer(folder, free_port(), model_url=f"http://127.0.0.1:{trap.sockets[0].getsockname()[1]}/v1")

        async def local_only(route):
            target = urlsplit(route.request.url)
            if target.scheme in {"http", "https"} and target.netloc != urlsplit(server.url).netloc:
                result["outside_requests"].append(target.hostname)
                await route.abort()
            else:
                await route.continue_()

        async def recall(page):
            reply = await send(page, server, "我的工作饮品偏好是什么？")
            turn = (await server.state())["turns"][-1]
            assert turn["source"] == "memory" and turn["cloud_requests"]["total"] == 0
            result["replies"].append(reply)
            return reply

        try:
            await server.start()
            async with async_playwright() as playwright:
                edge = await open_browser(playwright)
                try:
                    await confirm_muted_edge(edge)
                    page = await edge.new_page(reduced_motion="reduce")
                    await page.route("**/*", local_only)
                    page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
                    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off');")
                    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                    await page.locator(".connection.online").wait_for()
                    await page.get_by_test_id("memories").click()
                    await page.get_by_test_id("memory-input").fill(RED)
                    await page.get_by_test_id("memory-add").click()
                    await page.get_by_test_id("memory-item").wait_for()
                    saved, = (await server.state())["memories"]
                    key = saved["id"]
                    await page.get_by_test_id("memory-close").click()
                    first_chat = await new_chat(page, server)
                    assert RED in await recall(page)
                    await send(page, server, f"修改记忆：{RED} → {WHITE}")
                    second_chat = await new_chat(page, server)
                    assert second_chat != first_chat and WHITE in await recall(page)
                    await page.get_by_test_id("memories").click()
                    remembered = page.locator(f'[data-memory-id="{key}"]')
                    assert WHITE in await remembered.inner_text()
                    await remembered.get_by_test_id("memory-remove").click()
                    await remembered.wait_for(state="detached")
                    await page.get_by_test_id("memory-close").click()
                    missing = await recall(page)
                    assert "没有保存" in missing and "白茶" not in missing
                    assert not (await server.state())["memories"]
                finally:
                    await edge.close()
            assert not result["model_connections"] and not result["outside_requests"] and not result["page_errors"], result
            result["passed"] = True
        finally:
            await server.stop()
            path = folder / "result.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(path, result["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
