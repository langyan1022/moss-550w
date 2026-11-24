import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_context_reply import free_port


async def verify_fixture(browser, folder, source, findings):
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    server.data.mkdir(parents=True)
    uri = (source / "session.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as reader:
        with closing(sqlite3.connect(server.data / "session.sqlite3")) as writer:
            reader.backup(writer)
    await server.start()
    page = await browser.new_page(reduced_motion="reduce", viewport={"width": 1150, "height": 800})
    requests = []
    errors = []
    page.on("request", lambda item: requests.append(item.url))
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        await page.add_init_script("localStorage.setItem('moss.voice.enabled','off');")
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        state = await server.state()
        option = page.get_by_role("checkbox", name="显示云模型请求次数")
        settings = page.locator("details.preferences > summary")
        await settings.click()
        await expect(option).not_to_be_checked()
        await expect(page.get_by_test_id("cloud-requests")).to_have_count(0)
        await option.check()
        await expect(page.get_by_text("含发送失败和重试。费用以供应商账单为准。", exact=True)).to_be_visible()
        await settings.click()
        for chat in state["chats"]:
            status, response = await server.post("/api/switch-chat", {"id": chat["id"]})
            assert status == 200, status
            for turn in response["turns"]:
                value = turn["cloud_requests"]
                row = page.locator(f'[data-turn-id="{turn["id"]}"]')
                count = row.get_by_test_id("cloud-requests")
                if value is None:
                    await expect(count).to_have_text("云模型请求未记录")
                    findings.add("unrecorded")
                    continue
                prefix = "" if value["complete"] else "启用计数后 · "
                await expect(count).to_contain_text(prefix + f"云模型请求 {value['total']} 次")
                for vendor, label in (("deepseek", "DeepSeek"), ("aliyun", "百炼"), ("kimi", "Kimi")):
                    if vendor in value["by_provider"]:
                        await expect(count).to_contain_text(label)
                findings.add(str(value["total"]) if value["complete"] else "partial-history")
        await page.locator("#dialogue").screenshot(path=str(folder / "对话请求记录.png"), animations="disabled")
        await page.reload(wait_until="domcontentloaded")
        await settings.click()
        await expect(option).to_be_checked()
        if (await server.state())["turns"]:
            await expect(page.get_by_test_id("cloud-requests").first).to_be_visible()
        await option.uncheck()
        for count in await page.get_by_test_id("cloud-requests").all():
            await expect(count).to_be_hidden()
        assert not errors, errors
        assert all(urlsplit(url).netloc == f"127.0.0.1:{server.port}" for url in requests if url.startswith("http"))
    finally:
        try:
            await page.close()
        finally:
            await server.stop()


async def main(sources):
    folder = ROOT / "runs" / datetime.now().strftime("cloud-requests-ui-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    findings = set()
    report = {"passed": False, "fixture_count": len(sources)}
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                for index, source in enumerate(sources, 1):
                    await verify_fixture(browser, folder / str(index), source, findings)
            finally:
                await browser.close()
        assert {"0", "1", "2", "unrecorded", "partial-history"} <= findings, findings
        report["passed"] = True
    finally:
        report["displayed"] = sorted(findings)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", type=Path, nargs="+")
    asyncio.run(main(parser.parse_args().sources))
