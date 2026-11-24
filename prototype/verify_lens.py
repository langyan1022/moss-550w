import asyncio
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from time import perf_counter

from PIL import Image
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT


async def scene(worker):
    return await worker.evaluate("async()=> (await import(self.location.href)).inspectScene()")


async def inspect(browser, server, folder, original):
    context = await browser.new_context(viewport={"width":1280,"height":800}, reduced_motion="no-preference")
    if original:
        await context.route("**/conversation-assets/moss-render.js", lambda route: route.fulfill(
            path=str(original), content_type="text/javascript"))
    page = await context.new_page()
    page.set_default_timeout(90000)
    await page.add_init_script("""sessionStorage.setItem('moss.agent.opened','1');
        window.voiceEvents=[];
        for(const type of ['pending','start','progress','end','error'])
          window.addEventListener('moss-speech-'+type,e=>voiceEvents.push({type,...e.detail}));""")
    try:
        await page.goto(server.url, wait_until="domcontentloaded")
        await page.locator('#moss-model[data-ready="true"]').wait_for()
        await page.locator('label[data-code="OPTICS"]').click()
        worker = next(w for w in page.workers if "moss-render" in w.url)
        for _ in range(30):
            snap = await scene(worker)
            if snap["activePreset"] == "optics":
                break
            await asyncio.sleep(.15)
        assert snap["activePreset"] == "optics"
        await asyncio.sleep(1)
        canvas = await page.locator("#moss-model").bounding_box()
        snap = await scene(worker)
        x, y = snap["eyePixel"]
        side = 200
        clip = {"x":round(canvas["x"]+x-side/2),"y":round(canvas["y"]+y-side/2),"width":side,"height":side}
        frames, stats = [], []
        before = perf_counter()
        for _ in range(18):
            frame = Image.open(BytesIO(await page.screenshot(clip=clip))).convert("RGB")
            centre = frame.crop((side//2-35,side//2-35,side//2+35,side//2+35))
            values = sorted((r-max(g,b) for r,g,b in centre.get_flattened_data()),reverse=True)
            frames.append(frame)
            state = await scene(worker)
            stats.append({"sec":round(perf_counter()-before,3), "intensity":state["tof"],
                          "red":round(sum(values[:8])/8,2)})
            await asyncio.sleep(.13)
        label = "before" if original else "after"
        frames[0].save(folder / (label+".gif"),save_all=True,append_images=frames[1:],loop=0,
                       duration=[max(80,round((b["sec"]-a["sec"])*1000)) for a,b in zip(stats,stats[1:])]+[200])
        frames[max(range(len(stats)),key=lambda i:stats[i]["red"])].save(folder/(label+".png"))
        intensities = [s["intensity"] for s in stats]
        red = [s["red"] for s in stats]
        result = {"label":label,"samples":stats,"software":snap["software"],"red_range":[min(red),max(red)]}
        (folder/(label+".json")).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        if original:
            assert max(intensities)-min(intensities)<.001
        else:
            assert max(intensities)-min(intensities)>.6 and max(red)-min(red)>15, result
            await page.locator("#history-toggle").click()
            await page.locator("#message").fill("我想听刀郎的歌")
            await page.locator("#send").click()
            await page.wait_for_function("voiceEvents.some(e=>e.type==='start')")
            spoken = []
            for _ in range(40):
                state = await scene(worker)
                spoken.append({k:state[k] for k in ("phase","source","tof","level")})
                if await page.evaluate("voiceEvents.some(e=>e.type==='end')"):
                    break
                await asyncio.sleep(.08)
            await page.wait_for_function("voiceEvents.some(e=>e.type==='end')")
            await asyncio.sleep(1)
            result.update(spoken=spoken,events=await page.evaluate("voiceEvents"),resting=(await scene(worker))["phase"])
            vocal = [s["tof"] for s in spoken if s["source"]=="voice" and s["phase"]=="speak"]
            assert len(vocal)>=2 and max(vocal)-min(vocal)>.05 and result["resting"]=="standby", result
        return result
    finally:
        await context.close()


async def main():
    folder = ROOT/"runs"/datetime.now().strftime("lens-%Y%m%d-%H%M%S")
    server = BrowserServer(folder,18365)
    shutil.copytree(ROOT/"data/voice-announcements",server.data/"voice-announcements")
    await server.start()
    try:
        async with async_playwright() as pw:
            edge = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
            browser = await pw.chromium.launch(executable_path=str(edge),headless=True,args=["--mute-audio"])
            original = Path("F:/moss/tts/voice-trials/conversation-assets/moss-render.js")
            results = [await inspect(browser,server,folder,original),await inspect(browser,server,folder,None)]
            (folder/"result.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
            print(folder, [r["red_range"] for r in results])
            await browser.close()
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
