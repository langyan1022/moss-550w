import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

import aiohttp
from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from desktop_bootstrap import has_package
from verify_context_reply import free_port


MARKER = "现在几点了？"
DRAFT = "未发送的测试草稿"


@asynccontextmanager
async def edge_with_native_focus(playwright, folder):
    executable = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    data = folder / "edge-profile"
    launcher = await asyncio.create_subprocess_exec(str(executable), "--headless=new", "--mute-audio", "--disable-gpu",
        "--disable-background-networking", "--no-first-run", "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0", f"--user-data-dir={data}", "about:blank", creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        active_port = data / "DevToolsActivePort"
        async with asyncio.timeout(12):
            while not active_port.is_file():
                if launcher.returncode is not None:
                    raise RuntimeError("隔离 Edge 启动失败。")
                await asyncio.sleep(.1)
        port, path = active_port.read_text(encoding="ascii").splitlines()
        browser = await playwright.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{path}", no_defaults=True)
        await confirm_muted_edge(browser)
        yield browser
    finally:
        if browser is not None:
            session = await browser.new_browser_cdp_session()
            await session.send("Browser.close")
            await browser.close()
        if launcher.returncode is None:
            try:
                await asyncio.wait_for(launcher.wait(), 5)
            except TimeoutError:
                launcher.terminate()
                await launcher.wait()


async def cli(server, option="--probe"):
    process = await asyncio.create_subprocess_exec(sys.executable, "-X", "utf8", str(ROOT / "wake_activation.py"),
        "--profile", str(server.data), option, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    output, error = await asyncio.wait_for(process.communicate(), 35)
    if process.returncode not in {0, 2} or error:
        raise RuntimeError(error.decode("utf-8"))
    return process.returncode, json.loads(output)


async def rejected(client, server, action, body, expected, *, origin=None):
    headers = {"Origin": origin} if origin else {}
    async with client.post(f"{server.url}/api/wake/{action}", json=body, headers=headers) as response:
        assert response.status == expected, await response.text()
        return response.status


async def browser_cases(browser, server, report):
    context = browser.contexts[0]
    await context.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
    pages = [context.pages[0], await context.new_page()]
    posted, errors = [], []
    for index, page in enumerate(pages):
        await page.bring_to_front()
        await page.emulate_media(reduced_motion="reduce")
        def record(request, index=index):
            if request.method == "POST":
                posted.append((index, request.url.removeprefix(server.url), request.post_data_json))

        page.on("request", record)
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        await page.locator("#message").fill(DRAFT)
        await page.locator("#close-chat").click(timeout=8000)
        await expect(page.locator("#dialogue")).to_be_hidden()

    primary = pages[1]
    await primary.bring_to_front()
    report["focus"] = [await item.evaluate("({visible:!document.hidden, focused:document.hasFocus()})") for item in pages]
    assert report["focus"] == [{"visible": False, "focused": False}, {"visible": True, "focused": True}]
    assert await cli(server, "--browser-ready") == (0, {"page_connected": True})

    try:
        code, receipt = await cli(server)
        assert code == 0 and receipt["result"] == "reused" and receipt["pid"] == server.pid, receipt
        report["existing_page"] = receipt
        opened = [index for index, page in enumerate(pages) if await page.locator("#dialogue").is_visible()]
        assert opened == [1] and len(context.pages) == 2
        displayed = pages[opened[0]]
        await expect(displayed.locator("#message")).to_have_value(DRAFT)
        await expect(displayed.get_by_text(MARKER, exact=True).first).to_be_visible()
        await expect(displayed.locator("#wake-listen")).not_to_be_checked()
        displayed_paths = [path for index, path, _ in posted if index == opened[0]]
        assert displayed_paths == ["/api/wake/claim", "/api/wake/shown"], posted
        assert sum(path == "/api/wake/shown" for _, path, _ in posted) == 1
        body = next(payload for _, path, payload in posted if path == "/api/wake/shown")

        owner = json.loads((server.data / "instance.json").read_text(encoding="utf-8"))
        async with aiohttp.ClientSession() as client:
            report["denied"] = [
                await rejected(client, server, "activate", {"token": "invalid"}, 403),
                await rejected(client, server, "status", {"token": "invalid"}, 403),
                await rejected(client, server, "claim", body, 403),
                await rejected(client, server, "claim", body, 403, origin="http://example.invalid"),
                await rejected(client, server, "shown", body, 410, origin=server.url),
            ]
            assert await rejected(client, server, "status", {"token": owner["token"]}, 200) == 200

        for page in pages:
            await page.bring_to_front()
            if not await page.locator("#dialogue").is_visible():
                await page.locator("#history-toggle").click()
            await page.get_by_test_id("history").click()
            await expect(page.locator("#history-panel")).to_be_visible()
        baseline = len(posted)
        attempts = await asyncio.gather(cli(server), cli(server))
        assert all(code == 0 for code, _ in attempts), attempts
        outcomes = sorted(outcome["result"] for _, outcome in attempts)
        assert outcomes == ["default_browser_required", "in_progress"], attempts
        fallback = next(outcome for _, outcome in attempts if outcome["result"] == "default_browser_required")
        assert fallback["receipt"] == "no_receipt" and len(posted) == baseline and len(context.pages) == 2
        report["modal_visible"] = fallback
        report["concurrent"] = outcomes
        assert all(path in {"/api/wake/claim", "/api/wake/shown"} for _, path, _ in posted) and not errors, (posted, errors)
    finally:
        await asyncio.gather(*(page.close() for page in pages))


async def main():
    os.environ["MOSS_WINDOWS_REMINDERS"] = "1"
    folder = ROOT / "runs" / datetime.now().strftime("wake-activation-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "system_shell_invoked": False, "physical_microphone": False}
    print(folder, flush=True)
    cold_pid = None
    try:
        await server.start()
        assert await cli(server, "--browser-ready") == (2, {"page_connected": False})
        assert (await server.post("/api/message", {"text": MARKER}))[0] == 200
        snapshot = await server.state()
        assert len(snapshot["turns"]) == 1 and snapshot["turns"][0]["reply"]
        report["original_turns"] = len(snapshot["turns"])
        async with async_playwright() as playwright:
            async with edge_with_native_focus(playwright, folder) as browser:
                await browser_cases(browser, server, report)
        _, report["closed_pages"] = await cli(server)
        assert report["closed_pages"]["receipt"] == "no_page"
        original_pid = server.pid
        await server.stop()
        _, report["closed_service"] = await cli(server)
        cold_pid = report["closed_service"]["pid"]
        assert cold_pid != original_pid and report["closed_service"]["port"] == server.port
        assert report["closed_service"]["receipt"] == "no_page"
        report["cold_service_packaged"] = has_package(cold_pid)
        assert not report["cold_service_packaged"]
        resumed = await server.state()
        assert resumed["chat_id"] == snapshot["chat_id"] and resumed["turns"] == snapshot["turns"]
        assert await cli(server, "--browser-ready") == (2, {"page_connected": False})
        report["passed"] = True
    finally:
        if server.proc is not None:
            await server.stop()
        else:
            instance = server.data / "instance.json"
            if instance.is_file():
                owner = json.loads(instance.read_text(encoding="utf-8"))
                assert owner["port"] == server.port and (cold_pid is None or owner["pid"] == cold_pid)
                candidate = psutil.Process(owner["pid"])
                assert (await server.post("/api/shutdown", {"token": owner["token"]}))[0] == 200
                await asyncio.to_thread(candidate.wait, 20)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
