import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path

from playwright.async_api import async_playwright
from win32com.client import Dispatch

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser


async def last(server):
    return (await server.state())["turns"][-1]


async def wait_done(server, turn_id):
    async with asyncio.timeout(15):
        while True:
            turn = await last(server)
            assert turn["id"] == turn_id
            if turn["status"] not in ("saving", "thinking"):
                return turn
            await asyncio.sleep(.05)


def recycled_item(original, content):
    items = Dispatch("Shell.Application").NameSpace(10).Items()
    expected = hashlib.sha256(content).hexdigest()
    for entry in items:
        parent = entry.ExtendedProperty("System.Recycle.DeletedFrom")
        if not parent or Path(parent) != original.parent:
            continue
        if Path(entry.Path).is_file() and hashlib.sha256(Path(entry.Path).read_bytes()).hexdigest() == expected:
            return entry
    raise AssertionError("Windows 回收站没有刚才的测试文件")


async def verify(use_browser):
    folder = ROOT / "runs" / datetime.now().strftime("desktop-confirm-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "cases": []}
    server = BrowserServer(folder, 18659, model_url="http://127.0.0.1:9/v1")
    content = ("MOSS 回收站验收 " + folder.name).encode("utf-8")
    original = folder / "回收测试原件.txt"
    original.write_bytes(content)
    changed = folder / "确认期间更新的原件.txt"
    changed.write_text("原始内容", encoding="utf-8")

    async def prepare(text, action):
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200, state
        turn = await last(server)
        assert turn["status"] == "pending" and turn["desktop_confirmation"]["action"] == action, turn
        return turn["id"]

    async def cancel(turn_id):
        assert (await server.post("/api/cancel", {"turn_id": turn_id}))[0] == 200
        assert (await last(server))["status"] == "cancelled"
        assert (await server.post("/api/approve", {"turn_id": turn_id}))[0] == 409

    try:
        await server.start()
        lock = await prepare("锁屏", "lock")
        await cancel(lock)
        sleep = await prepare("让电脑进入睡眠", "sleep")
        await server.stop()
        await server.start()
        assert (await last(server))["id"] == sleep
        await cancel(sleep)
        report["cases"].append({"power_previews_cancelled": True, "sleep_preview_restored": True})
        original_message = f"把文件“{original}”移入回收站"
        declined = await prepare(original_message, "recycle")
        await cancel(declined)
        assert original.read_bytes() == content
        altered = await prepare(f"把文件“{changed}”移入回收站", "recycle")
        changed.write_text("修改后的内容", encoding="utf-8")
        assert (await server.post("/api/approve", {"turn_id": altered}))[0] == 200
        found = await wait_done(server, altered)
        assert found["status"] == "error" and "文件在确认前发生了变化" in found["error"]
        assert changed.read_text(encoding="utf-8") == "修改后的内容"
        report["cases"].append({"cancel_kept_file": True, "changed_source_rejected": True})

        accepted = await prepare(original_message, "recycle")
        await server.stop()
        await server.start()
        assert (await last(server))["id"] == accepted and original.read_bytes() == content
        if use_browser:
            async with async_playwright() as pw:
                browser = await open_browser(pw)
                try:
                    await confirm_muted_edge(browser)
                    page = await browser.new_page(reduced_motion="reduce")
                    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                    row = page.locator(f'[data-turn-id="{accepted}"]')
                    preview = row.get_by_test_id("preview")
                    assert await preview.locator("strong").inner_text() == "移入回收站"
                    assert await preview.locator(".card-body").inner_text() == str(original)
                    await row.get_by_role("button", name="确认执行").click()
                    await row.get_by_test_id("final").wait_for()
                    assert "回收站" in await row.get_by_test_id("final").inner_text()
                    report["browser"] = True
                finally:
                    await browser.close()
        else:
            assert (await server.post("/api/approve", {"turn_id": accepted}))[0] == 200
        receipt = await wait_done(server, accepted)
        assert receipt["status"] == "replied" and receipt["source"] == "desktop" and not original.exists(), receipt
        item = recycled_item(original, content)
        assert (await server.post("/api/approve", {"turn_id": accepted}))[0] == 200
        duplicate = await last(server)
        assert duplicate["approved_at"] == receipt["approved_at"]
        item.InvokeVerb("undelete")
        async with asyncio.timeout(7):
            while not original.exists():
                await asyncio.sleep(.1)
        assert original.read_bytes() == content
        report["cases"].append({"restart_preview": True, "duplicate_receipt": True,
                                "recycle_and_restore_sha256": hashlib.sha256(content).hexdigest()})

        command = "执行 PowerShell：Add-Content -LiteralPath '测试输出.txt' -Value '真实命令回执' -Encoding UTF8\nGet-Content -LiteralPath '测试输出.txt' -Encoding UTF8"
        output = server.data / "desktop-work" / "测试输出.txt"
        declined = await prepare(command, "powershell")
        await cancel(declined)
        assert not output.exists()
        terminal = await prepare(command, "powershell")
        assert not output.exists()
        assert (await server.post("/api/approve", {"turn_id": terminal}))[0] == 200
        receipt = await wait_done(server, terminal)
        assert receipt["status"] == "replied" and receipt["reply"] == "真实命令回执", receipt
        assert (await server.post("/api/approve", {"turn_id": terminal}))[0] == 200
        assert output.read_text(encoding="utf-8-sig").strip() == receipt["reply"]

        stoppable = await prepare("执行 PowerShell：Set-Content '已开始.txt' 'START'; Start-Sleep -Seconds 15; Set-Content '未完成.txt' 'DONE'", "powershell")
        assert (await server.post("/api/approve", {"turn_id": stoppable}))[0] == 200
        async with asyncio.timeout(6):
            while not (output.parent / "已开始.txt").exists():
                await asyncio.sleep(.05)
        assert (await server.post("/api/cancel", {"turn_id": stoppable}))[0] == 200
        assert (await last(server))["status"] == "cancelled"
        assert not (output.parent / "未完成.txt").exists()

        max_output = await prepare("执行 PowerShell：'汉字' * 4000", "powershell")
        assert (await server.post("/api/approve", {"turn_id": max_output}))[0] == 200
        capped = await wait_done(server, max_output)
        assert capped["status"] == "error" and "16 KiB" in capped["error"], capped
        report["cases"].append({"powershell_approval": True, "utf8": receipt["reply"], "output_cap": True,
                                "stopped_live_process": True, "duplicate_confirmation_executed_once": True})
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    asyncio.run(verify(parser.parse_args().browser))
