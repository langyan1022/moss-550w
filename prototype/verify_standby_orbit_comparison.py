import asyncio
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_context_reply import free_port


SKILL_ASSET = Path.home() / ".codex/skills/wandering-earth-fui-design/assets/fui-audio.js"
ADD_ORBIT = """async () => {
  const {createOrbit,drawOrbit} = await import('/comparison/fui-audio.js');
  const svg = document.querySelector('.standby-wave');
  const canvas = document.createElement('canvas');
  canvas.className = 'standby-wave';
  canvas.style.height = 'clamp(52px,7.5dvh,68px)';
  svg.style.display = 'none';
  svg.after(canvas);
  const orbit = createOrbit(13);
  const bands = new Float32Array(24);
  for (let index=0; index<20; index++) drawOrbit(canvas,orbit,{bands,layout:'line',waves:2,glow:true,dt:.07});
  const {width,height} = canvas;
  const pixels = canvas.getContext('2d').getImageData(0,0,width,height).data;
  let lit = 0, clear = 0;
  for (let pos=3; pos<pixels.length; pos+=4) {
    if (!pixels[pos]) clear++;
    if (pixels[pos]>80) lit++;
  }
  return {width,height,lit,clear};
}"""


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("standby-orbit-review-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "viewports": [], "outside": []}
    try:
        await server.start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(viewport={"width":1440,"height":900}, reduced_motion="no-preference")
                await page.add_init_script("sessionStorage.setItem('moss.agent.opened','1');")

                async def local(route):
                    url = urlsplit(route.request.url)
                    if url.netloc == f"127.0.0.1:{server.port}" and url.path == "/comparison/fui-audio.js":
                        await route.fulfill(path=SKILL_ASSET, content_type="text/javascript")
                    elif url.scheme in ("http","https") and url.netloc != f"127.0.0.1:{server.port}":
                        report["outside"].append(url.netloc)
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", local)
                await page.goto(server.url, wait_until="domcontentloaded")
                await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=90000)
                for width,height in ((1440,900),(375,812)):
                    await page.set_viewport_size({"width":width,"height":height})
                    await page.locator("svg.standby-wave").wait_for()
                    lane = page.locator(".stage-audio-lane")
                    await lane.screenshot(path=folder/f"{width}-当前波形.png")
                    row = await page.evaluate(ADD_ORBIT)
                    assert row["lit"] > 50 and row["clear"] > row["width"] * row["height"] * .7, row
                    await lane.screenshot(path=folder/f"{width}-FUI组件.png")
                    report["viewports"].append({"width":width,"canvas":row})
                    await page.evaluate("""() => {
                      document.querySelector('canvas.standby-wave').remove();
                      document.querySelector('svg.standby-wave').style.removeProperty('display');
                    }""")
                assert not report["outside"] and not (await server.state())["turns"], report
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
