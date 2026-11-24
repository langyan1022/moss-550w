import argparse
import asyncio
from datetime import datetime
import json

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser


CAPTURE = """() => {
  const film = document.querySelector("#opening-film");
  const styled = getComputedStyle(film);
  const scale = new DOMMatrixReadOnly(styled.transform).a;
  return {seconds:film.currentTime, scale, sourceWidth:Math.round(film.videoWidth / scale), fit:styled.objectFit,
    light:styled.filter, video:[film.videoWidth,film.videoHeight], paused:film.paused,
    viewport:[innerWidth,innerHeight]};
}"""


async def at(page, seconds):
    await page.wait_for_function("""seconds => {
      const film = document.querySelector("#opening-film");
      return film.readyState >= 2 && !film.paused && film.currentTime >= seconds;
    }""", arg=seconds)
    return await page.evaluate(CAPTURE)


async def inspect(browser, url, folder, report):
    page = await browser.new_page(viewport={"width": 1536, "height": 864})
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.locator('main[data-opening="identity"]').wait_for(state="attached")
        samples = []
        for seconds, name in ((.14, "opening-early.png"), (.77, "opening-middle.png"), (1.74, "opening-frame.png")):
            samples.append(await at(page, seconds))
            await page.locator("#opening-film").evaluate("film => film.pause()")
            await page.screenshot(path=folder / name, timeout=10000)
            await page.locator("#opening-film").evaluate("film => film.play()")
        early, middle, late = samples
        report["first_run"] = [early, middle, late]
        assert early["video"] == [1536, 864]
        assert early["sourceWidth"] >= 940 and 1 < middle["scale"] < early["scale"] <= 1.62, report["first_run"]
        assert .99 <= late["scale"] <= 1.01 and late["light"] == "brightness(1)", late
        await page.locator("main[data-opening]").wait_for(state="detached")
        await page.locator(".preferences summary").click()
        await page.get_by_role("button", name="重播开场", exact=True).click()
        replay = await at(page, .17)
        assert 1.4 <= replay["scale"] <= 1.62 and replay["sourceWidth"] >= 940, replay
        await page.get_by_role("button", name="跳过开场").click()
        await page.locator("main[data-opening]").wait_for(state="detached")
        assert await page.locator("#opening-film").evaluate("film => film.paused && film.hidden")
        report["replay"] = replay
        report["skip_stopped_playback"] = True
    finally:
        await page.close()

    reduced = await browser.new_page(viewport={"width": 420, "height": 800}, reduced_motion="reduce")
    try:
        await reduced.goto(url + "/?scene=off", wait_until="domcontentloaded")
        await reduced.wait_for_function('document.querySelector("#connection-label").textContent.includes("本地服务已连接")',
                                        timeout=6000)
        assert await reduced.locator("main[data-opening]").count() == 0
        await reduced.locator(".preferences summary").click()
        await reduced.get_by_role("button", name="重播开场", exact=True).click()
        replay = await at(reduced, .17)
        assert .99 <= replay["scale"] <= 1.01 and replay["light"] == "brightness(1)" and replay["fit"] == "contain", replay
        report["reduced_motion"] = replay
    finally:
        await reduced.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    args = parser.parse_args()
    folder = ROOT / "runs" / datetime.now().strftime("opening-film-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder)
    server = BrowserServer(folder, 18967, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False}
    try:
        if not args.url:
            await server.start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                await inspect(browser, (args.url or server.url).rstrip("/"), folder, report)
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
