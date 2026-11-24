"""Verify the speech-space controls in muted Edge using known audio fixtures."""

import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port
from verify_staff_console_browser import open_preferences


STAGED = {
    "/": ("conversation.html", "text/html"),
    **{f"/static/{name}": (name, "text/javascript") for name in (
        "agent-terminal.js", "agent-speech.js", "app.js", "location-weather.js")},
}
PREVIEW = "这里是 MOSS。语音通道已就绪。"
STORAGE = "moss.speech.space.percent"
PROBE = """
window.spaceProbe = {starts:[], stops:0, ends:0, aborted:[]};
const start = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...args) {
  const result = start.apply(this, args);
  if (this.buffer) {
    const peak = this.buffer.getChannelData(0).reduce((peak, n) => Math.max(peak, Math.abs(n)), 0);
    spaceProbe.starts.push({seconds:this.buffer.duration, peak});
    this.addEventListener('ended', () => ++spaceProbe.ends, {once:true});
  }
  return result;
};
const stop = AudioBufferSourceNode.prototype.stop;
AudioBufferSourceNode.prototype.stop = function(...args) {
  ++spaceProbe.stops;
  return stop.apply(this, args);
};
const originalFetch = window.fetch;
window.fetch = function(input, options) {
  const url = new URL(input instanceof Request ? input.url : input, location.href);
  if (url.pathname === '/api/speak') {
    const body = JSON.parse(options.body);
    options.signal?.addEventListener('abort', () => spaceProbe.aborted.push(body), {once:true});
  }
  return originalFetch.apply(this, arguments);
};
"""


def repeat_wav(fixture):
    data = io.BytesIO()
    with wave.open(io.BytesIO(fixture), "rb") as source, wave.open(data, "wb") as target:
        target.setparams(source.getparams())
        frames = source.readframes(source.getnframes())
        while target.tell() < target.getframerate() * 8:
            target.writeframes(frames)
    return data.getvalue()


async def open_space(page):
    await open_preferences(page)
    await page.locator("#speech-space-configure").click()
    dialog = page.locator("#speech-space-settings")
    await expect(dialog).to_be_visible()
    return dialog


async def close_space(dialog):
    await dialog.get_by_role("button", name="关闭", exact=True).click()
    await expect(dialog).to_be_hidden()


async def set_amount(dialog, amount):
    slider = dialog.locator("#speech-space-amount")
    await slider.press("End")
    for _ in range((150 - amount) // 5):
        await slider.press("ArrowLeft")
    await expect(slider).to_have_value(str(amount))
    await expect(dialog.locator("#speech-space-value")).to_have_text(f"{amount}%")


async def exercise(browser, base, folder, report, assets, fixture):
    context = await browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce",
                                        service_workers="block")
    seen = set()
    held = asyncio.Event()
    release = asyncio.Event()
    long_fixture = repeat_wav(fixture)

    async def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme in {"http", "https"} and not request.url.startswith(base + "/"):
            report["outside"].append([url.hostname, url.path])
            await route.abort()
        elif url.path in assets:
            seen.add(url.path)
            body, mime = assets[url.path]
            await route.fulfill(body=body, content_type=mime + "; charset=utf-8")
        elif url.path == "/api/speak" and request.method == "POST":
            body = request.post_data_json
            report["speak_calls"].append(body)
            if body == {"text": PREVIEW, "category": "reply", "room_amount": 115, "prefer_cached": False}:
                held.set()
                await release.wait()
                await route.abort()
                return
            audio = long_fixture if body.get("room_amount") == 150 else fixture
            await route.fulfill(body=audio, content_type="audio/wav")
        elif url.path.startswith("/api/") and request.method not in {"GET", "HEAD"} and url.path != "/api/message":
            report["other_api_writes"].append([request.method, url.path])
            await route.abort()
        else:
            await route.continue_()

    def preview_body(amount):
        body = {"text": PREVIEW, "category": "reply", "prefer_cached": False}
        if amount != 100:
            body["room_amount"] = amount
        return body

    try:
        await context.route("**/*", route_request)
        await context.add_init_script(PROBE)
        page = await context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        await page.goto(base + "/?scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(state="attached")
        await page.bring_to_front()
        dialog = await open_space(page)
        slider = dialog.locator("#speech-space-amount")
        for property_name, value in (("min", "0"), ("max", "150"), ("step", "5")):
            await expect(slider).to_have_attribute(property_name, value)
        await expect(slider).to_have_value("100")
        preview = dialog.locator("#speech-space-preview")
        status = dialog.locator("#speech-space-status")

        await dialog.locator('[data-amount="0"]').click()
        await expect(slider).to_have_value("0")
        await preview.click()
        await expect(status).to_have_text("试听完成。")
        assert report["speak_calls"] == [preview_body(0)], report["speak_calls"]
        await page.wait_for_function("spaceProbe.starts.length === 1 && spaceProbe.ends === 1")

        await dialog.locator('[data-amount="150"]').click()
        await expect(slider).to_have_value("150")
        await preview.click()
        await page.wait_for_function("spaceProbe.starts.length === 2 && spaceProbe.starts[1].seconds >= 8")
        await expect(preview).to_have_text("停止试听")
        await preview.click()
        await expect(status).to_have_text("试听已停止。")
        await page.wait_for_function("spaceProbe.stops >= 1 && spaceProbe.ends === 2")

        await set_amount(dialog, 115)
        await dialog.screenshot(path=folder / "speech-space-115.png", animations="disabled")
        await preview.click()
        await asyncio.wait_for(held.wait(), timeout=8)
        await close_space(dialog)
        await page.wait_for_function("spaceProbe.aborted.some(body => body.room_amount === 115)")
        assert await page.evaluate("spaceProbe.starts.length") == 2
        report["before_reload"] = await page.evaluate("spaceProbe")
        release.set()

        await page.reload(wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(state="attached")
        dialog = await open_space(page)
        await expect(dialog.locator("#speech-space-amount")).to_have_value("115")
        assert await page.evaluate("key => localStorage.getItem(key)", STORAGE) == "115"
        await dialog.locator("#speech-space-reset").click()
        await expect(dialog.locator("#speech-space-amount")).to_have_value("100")
        assert await page.evaluate("key => localStorage.getItem(key)", STORAGE) == "100"
        await dialog.locator("#speech-space-preview").click()
        await expect(dialog.locator("#speech-space-status")).to_have_text("试听完成。")
        assert report["speak_calls"] == [preview_body(level) for level in (0, 150, 115, 100)], report["speak_calls"]

        await set_amount(dialog, 115)
        await close_space(dialog)
        await page.get_by_test_id("composer").fill("你好")
        async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/message") as seen_message:
            await page.get_by_test_id("send").click()
        assert (await seen_message.value).ok
        await page.wait_for_function("spaceProbe.starts.length === 2 && spaceProbe.ends === 2")
        assert report["speak_calls"][-1] == {"text": "你好。", "category": "reply", "room_amount": 115,
                                              "prefer_cached": False}, report["speak_calls"]
        report["after_reload"] = await page.evaluate("spaceProbe")
        assert all(row["peak"] > 0 for result in (report["before_reload"], report["after_reload"])
                   for row in result["starts"]), report
        assert seen == set(assets), seen
        report["asset_hits"] = sorted(seen)
    finally:
        release.set()
        await context.close()


async def verify(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("speech-space-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    assets = {url: ((candidate / name).read_bytes(), mime) for url, (name, mime) in STAGED.items()} if candidate else {}
    fixture = (ROOT / "voice" / "lines" / "done.wav").read_bytes()
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed": False, "speak_calls": [], "outside": [], "other_api_writes": [], "page_errors": [],
              "model_connections": 0, "candidate_asset_sha256": {url: sha256(data).hexdigest()
              for url, (data, _) in assets.items()}, "fixture_wave_sha256": sha256(fixture).hexdigest()}
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0", "MOSS_SPEECH_MIC_WARM": "0"}

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with await asyncio.start_server(guard, "127.0.0.1", 0) as sink:
            model = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            profiles = {key: Profile(key, label, model, model_id, SecretStr(""))
                        for key, (label, _, __, model_id) in SPECS.items()}
            with patch.dict(os.environ, environment), patch("core.profiles", return_value=profiles):
                port, data = free_port(), folder / "data"
                base = f"http://127.0.0.1:{port}"
                moss = Moss(data, model, "isolated-model-guard")
                runner = web.AppRunner(application(moss, lambda: "standby", data, port, "fixture", asyncio.Event()),
                                       access_log=None, shutdown_timeout=1)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", port).start()
                    async with async_playwright() as playwright:
                        edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                        browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                            args=["--mute-audio", "--disable-gpu", "--disable-renderer-backgrounding",
                                  "--autoplay-policy=document-user-activation-required"])
                        try:
                            await confirm_muted_edge(browser)
                            report["browser"] = browser.version
                            await exercise(browser, base, folder, report, assets, fixture)
                        finally:
                            await browser.close()
                    turn, = moss.state()["turns"]
                    assert turn["user"] == "你好" and turn["reply"] == "你好。" and turn["source"] == "smalltalk", turn
                    assert not any(report[key] for key in ("outside", "other_api_writes", "page_errors", "model_connections")), report
                    report["passed"] = True
                finally:
                    await runner.cleanup()
                    await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, help="Overlay a directory of staged browser files.")
    asyncio.run(verify(parser.parse_args().candidate))
