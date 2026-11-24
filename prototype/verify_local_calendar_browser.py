import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
import os
from time import perf_counter
from unittest.mock import patch

from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_context_reply import free_port


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("local-calendar-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "model_connections": 0, "cases": {}}
    title = "产品评审"
    create = f"莫斯，明天上午十点到十一点安排个{title}会议"
    query = "我明天有啥安排？"

    async def unexpected_model(_reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with AsyncExitStack() as stack:
            sink = await stack.enter_async_context(await asyncio.start_server(unexpected_model, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            catalog = folder / "media.sqlite3"
            MediaLibrary(catalog)
            env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0", "MOSS_SPEECH_MIC_WARM": "0"}
            server = BrowserServer(folder, free_port(), model_url=model_url)
            stack.push_async_callback(server.stop)
            with patch.dict(os.environ, env):
                await server.start()
            playwright = await stack.enter_async_context(async_playwright())
            browser = await open_browser(playwright)
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)
            page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await expect(page.locator(".connection")).to_have_class("connection online")
            composer = page.get_by_test_id("composer")

            async def send(text):
                await composer.fill(text)
                await page.get_by_test_id("send").click()
                row = page.get_by_test_id("turn").last
                await expect(row.locator(".user-message")).to_have_text(text)
                return row

            row = await send(create)
            preview = row.get_by_test_id("preview")
            await expect(preview.get_by_text("新建本机日程")).to_be_visible()
            details = preview.locator(".card-body")
            await expect(details).to_contain_text(title)
            await expect(details).to_contain_text("10:00")
            await expect(details).to_contain_text("11:00")
            pending = (await server.state())["turns"][-1]
            assert pending["status"] == "pending" and pending.get("calendar_receipt") is None
            await preview.screenshot(path=str(folder / "desktop-preview.png"))
            await server.stop()
            with patch.dict(os.environ, env):
                await server.start()
            await expect(row.get_by_role("button", name="确认创建")).to_be_enabled(timeout=15000)
            after = (await server.state())["turns"][-1]
            assert after["id"] == pending["id"] and after["desktop_confirmation"] == pending["desktop_confirmation"]

            stamp = perf_counter()
            await row.get_by_role("button", name="确认创建").click()
            await expect(row.get_by_test_id("final")).to_contain_text(title)
            confirmed_ms = round(1000 * (perf_counter() - stamp), 1)
            confirmed = (await server.state())["turns"][-1]
            assert confirmed["calendar_receipt"]["operation_id"] == pending["id"]
            report["cases"]["confirm_after_restart"] = {"confirmation_to_reply_ms": confirmed_ms,
                                                          "receipt": confirmed["calendar_receipt"]}

            await page.set_viewport_size({"width": 375, "height": 812})
            row = await send(query)
            result = row.get_by_test_id("final")
            await expect(result).to_contain_text(title)
            assert (await result.inner_text()).count(title) == 1
            report["cases"]["query_narrow"] = await result.inner_text()
            await result.screenshot(path=str(folder / "narrow-query.png"))

            row = await send("安排日程：取消测试，明天下午两点到三点")
            await expect(row.get_by_role("button", name="取消操作")).to_be_visible()
            await composer.fill("取消日程")
            await composer.press("Enter")
            await expect(row).to_have_attribute("data-status", "cancelled")
            row = await send(query)
            await expect(row.get_by_test_id("final")).to_contain_text(title)
            assert "取消测试" not in await row.get_by_test_id("final").inner_text()
            row = await send("现在几点、明天有什么日程？")
            panel = page.get_by_test_id("local-status-panel")
            await expect(panel.get_by_role("heading", name="本机状态和日程")).to_be_visible()
            await expect(panel.locator(".status-reading dt")).to_have_text(["时间", "日程"])
            schedule = panel.locator('[data-reading-kind="calendar"] dd')
            await expect(schedule).to_contain_text(title)
            await expect(schedule).to_have_css("white-space", "pre-line")
            report["cases"]["compound_popup"] = await schedule.inner_text()
            await panel.screenshot(path=str(folder / "narrow-calendar-popup.png"))
            state = await server.state()
            assert all(turn["cloud_requests"]["total"] == 0 for turn in state["turns"])
            assert report["model_connections"] == 0 and not errors, errors
            report["cases"]["typed_cancel"] = True
        report["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
