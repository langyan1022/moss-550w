import asyncio
from datetime import datetime
import json
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send


SPECTRUM = """() => {
    const canvas = document.querySelector('#tool-player .player-spectrum');
    if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return 0;
    const bytes = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let visible = 0;
    for (let i = 3; i < bytes.length; i += 4) if (bytes[i]) visible++;
    return visible;
}"""


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("legacy-music-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18469, model_url="http://127.0.0.1:18278/v1")
    report = {"passed": False, "errors": [], "cases": {}}
    await server.start()
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
                page.set_default_timeout(40000)
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url + "?chat=1", wait_until="domcontentloaded")
                await page.locator("#moss-model[data-ready=true]").wait_for(timeout=110000)
                panel = page.locator("#tools-panel")
                start = perf_counter()
                prompt = "我想听刘德华的歌"
                await send(page, server, prompt)
                first = (await server.state())["turns"][-1]["media"]
                items = first["items"]
                assert first["online_calls"] == 0 and len(items) >= 2

                async def selection(index):
                    selected = items[index]
                    await page.wait_for_function("""id => {
                        const audios = Array.from(document.querySelectorAll('.media-audio')).filter(a => !a.paused);
                        return audios.length === 1 && audios[0].dataset.mediaId === id &&
                               audios[0].currentTime > .2 && audios[0].duration > 20;
                    }""", arg=selected["media_id"])
                    assert await panel.locator(".player-heading strong").inner_text() == selected["title"]
                    assert await panel.locator(".player-artist").inner_text() == selected["artist"]
                    assert not await panel.locator(".projection-player audio").count()
                    assert await panel.locator(".player-heading .music-availability").is_visible()
                    assert await panel.get_by_role("button", name="上一首", exact=True).is_disabled() is (index == 0)
                    state = (await server.state())["turns"][-1]
                    assert state["media"]["selected"]["media_id"] == selected["media_id"]

                await selection(0)
                report["time_to_playing_s"] = round(perf_counter() - start, 3)
                for name, width, height in [("desktop", 1440, 900), ("narrow", 390, 844)]:
                    await page.set_viewport_size({"width": width, "height": height})
                    await page.wait_for_function(SPECTRUM)
                    level = await page.evaluate(SPECTRUM)
                    await page.wait_for_function("(" + SPECTRUM + ")() !== " + str(level) + " && (" + SPECTRUM + ")() > 0")
                    rect = await panel.locator(".player-controls").bounding_box()
                    if not (rect and rect["x"] >= -1 and rect["x"] + rect["width"] <= width + 1):
                        raise AssertionError(f"{name} 控件超出视口 {rect}")
                    await page.screenshot(path=str(folder / (name + ".png")))
                    report["cases"][name] = {"spectrum_pixels": level, "controls": rect}
                await panel.get_by_role("button", name="下一首", exact=True).click()
                await selection(1)
                await panel.get_by_role("button", name="上一首", exact=True).click()
                await selection(0)
                assert len((await server.state())["turns"]) == 3
                report["switch_order"] = [items[0]["title"], items[1]["title"], items[0]["title"]]
                await panel.get_by_role("button", name="暂停", exact=True).click()
                await page.wait_for_function("() => Array.from(document.querySelectorAll('.media-audio')).every(a=>a.paused)")
                await page.wait_for_function("(" + SPECTRUM + ")() === 0")
                await panel.get_by_role("button", name="播放", exact=True).click()
                await selection(0)
                await page.wait_for_function(SPECTRUM)
                assert not report["errors"], report["errors"]
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
