import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
from time import perf_counter
import wave

from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_context_reply import free_port


OBSERVE = """
window.micAudit = {calls:0, tracks:[]};
const openMic = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async options => {
  micAudit.calls++;
  const stream = await openMic(options);
  micAudit.tracks.push(...stream.getAudioTracks());
  return stream;
};
"""
OWNED_BY_OTHER = "另一个 MOSS 标签正在使用麦克风。关闭那页的录音或唤醒后可重试。"


async def records(page):
    return await page.evaluate("({calls:micAudit.calls, tracks:micAudit.tracks.map(track=>track.readyState)})")


async def verify(browser, server, report):
    context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce")
    await context.add_init_script(OBSERVE + "localStorage.setItem('moss.voice.enabled','off');")
    first, second = await context.new_page(), await context.new_page()
    posted = []
    errors = []
    for page in (first, second):
        page.on("request", lambda request: posted.append(request.url) if request.method == "POST" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        await page.locator(".preferences summary").click()
        await expect(page.locator("#wake-listen")).not_to_be_checked()
        assert await records(page) == {"calls": 0, "tracks": []}
    try:
        await first.locator("#wake-listen").check()
        await expect(first.locator("#wake-status")).to_have_text("等待唤醒")
        assert await records(first) == {"calls": 1, "tracks": ["live"]}

        start = perf_counter()
        await second.locator(".preferences summary").click()
        await second.locator("#mic").click()
        await expect(second.locator("#status")).to_have_text(OWNED_BY_OTHER)
        report["manual_conflict_ms"] = round((perf_counter() - start) * 1000, 1)
        assert await records(second) == {"calls": 0, "tracks": []}
        await second.locator(".preferences summary").click()
        await second.locator("#wake-listen").check()
        await expect(second.locator("#wake-status")).to_have_text(OWNED_BY_OTHER)
        await expect(second.locator("#wake-listen")).not_to_be_checked()
        assert await records(second) == {"calls": 0, "tracks": []}

        await first.locator(".preferences summary").click()
        await first.locator("#mic").click()
        await first.wait_for_function("micAudit.tracks.length === 2 && micAudit.tracks[0].readyState === 'ended'")
        report["single_tab_wake_to_manual"] = await records(first)
        assert report["single_tab_wake_to_manual"] == {"calls": 2, "tracks": ["ended", "live"]}
        await first.close()
        start = perf_counter()
        await second.locator("#wake-listen").check()
        await expect(second.locator("#wake-status")).to_have_text("等待唤醒")
        report["transfer_after_close_ms"] = round((perf_counter() - start) * 1000, 1)
        assert await records(second) == {"calls": 1, "tracks": ["live"]}

        await second.locator("#wake-listen").uncheck()
        await second.wait_for_function("micAudit.tracks.every(track => track.readyState === 'ended')")
        await second.wait_for_function("async () => !(await navigator.locks.query()).held.some(lock=>lock.name === 'moss-microphone')")
        assert all(url == server.url + "/api/prepare-voice" for url in posted), posted
        assert not errors and not (await server.state())["turns"], errors
        report.update(second_tab_final=await records(second), request_paths=[url.removeprefix(server.url) for url in posted],
                      user_recording=False, input="浏览器文件麦克风，16 kHz 静音 WAV")
    finally:
        await context.close()


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("voice-tab-lock-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    fixture = folder / "input.wav"
    with wave.open(str(fixture), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(bytes(2 * 16000 * 12))
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    report = {"passed": False}
    print(folder, flush=True)
    try:
        await server.start()
        exe = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=str(exe), headless=True, args=[
                "--mute-audio", "--disable-gpu", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--use-file-for-fake-audio-capture=" + str(fixture), "--disable-background-networking"])
            try:
                await confirm_muted_edge(browser)
                await verify(browser, server, report)
            finally:
                await browser.close()
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
