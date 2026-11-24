import asyncio
from datetime import datetime
import json
from math import isclose

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_lens import scene


async def inspect_view(browser, server, folder, width, height, touch):
    context = await browser.new_context(viewport={"width": width, "height": height},
                                        reduced_motion="reduce", has_touch=touch)
    name = "touch" if touch else "desktop"
    page = await context.new_page()
    page.set_default_timeout(90000)
    await page.add_init_script("sessionStorage.setItem('moss.agent.opened','1')")
    try:
        await page.goto(server.url, wait_until="domcontentloaded")
        canvas = page.locator("#moss-model")
        await page.locator('#moss-model[data-ready="true"]').wait_for()
        worker = next(w for w in page.workers if "moss-render" in w.url)
        rect = await canvas.bounding_box()

        async def scene_after(predicate):
            async with asyncio.timeout(12):
                while True:
                    snap = await scene(worker)
                    if predicate(snap):
                        return snap
                    await asyncio.sleep(.1)

        async def point(snap):
            position = await canvas.evaluate("""(canvas, grip) => {
                const r = canvas.getBoundingClientRect();
                const left=Math.max(0,grip.left), right=Math.min(1,grip.right);
                const top=Math.max(0,grip.top), bottom=Math.min(1,grip.bottom);
                for(const fy of [.5,.8,.2]) for(const fx of [.5,.8,.2]) {
                    const x=r.x+(left+(right-left)*fx)*r.width, y=r.y+(top+(bottom-top)*fy)*r.height;
                    if(document.elementFromPoint(x,y)===canvas) return [x,y];
                }
            }""", snap["railGrip"])
            assert position, (name, snap["railGrip"], rect)
            return position

        initial = await scene_after(lambda s: s["railGrip"] and s["activePreset"] == "interview")
        x, y = await point(initial)
        await page.mouse.move(x,y)
        await page.wait_for_function("document.querySelector('#moss-model').style.cursor==='ew-resize'")
        await page.screenshot(path=folder / f"{name}-before.png")
        await page.mouse.down()
        await page.mouse.move(x + .16 * rect["width"], y, steps=8)
        right = await scene_after(lambda s: s["railGrip"]["anchor"] > initial["railGrip"]["anchor"]+.12)
        assert right["camera"] == initial["camera"] and right["shoulderTurn"] == initial["shoulderTurn"]
        await page.screenshot(path=folder / f"{name}-right.png")
        await page.mouse.move(x - .14 * rect["width"], y, steps=8)
        left = await scene_after(lambda s: s["railGrip"]["anchor"] < initial["railGrip"]["anchor"]-.05)
        await page.mouse.move(rect["x"]-100, y, steps=8)
        edge = await scene_after(lambda s: s["railGrip"]["left"] < .045)
        assert edge["railGrip"]["left"] > -.02, edge["railGrip"]
        await page.mouse.up()
        await page.mouse.move(x,y)
        await page.mouse.wheel(0, 620)
        await page.mouse.wheel(0, -430)
        await asyncio.sleep(.35)
        wheeled = await scene(worker)
        assert wheeled["camera"] == edge["camera"] and wheeled["cameraRange"] == edge["cameraRange"]

        # 在真实指针截获期间移出和移入，并验证中断后不会继续控制滑车。
        hx, hy = await point(wheeled)
        await page.mouse.move(hx,hy)
        await page.mouse.down()
        await page.mouse.move(hx+.14*rect["width"],hy,steps=5)
        middle = await scene_after(lambda s: s["railGrip"]["anchor"] > edge["railGrip"]["anchor"]+.1)
        await canvas.evaluate("c=>{if(c.hasPointerCapture(1)) c.releasePointerCapture(1)}")
        await page.mouse.move(hx+.20*rect["width"], hy, steps=4)
        await page.mouse.up()
        await asyncio.sleep(.35)
        released = await scene(worker)
        assert isclose(released["railPosition"],middle["railPosition"],abs_tol=.015)

        await page.locator('label[data-code="OPTICS"]').click()
        optics = await scene_after(lambda s: s["activePreset"] == "optics")
        assert isclose(optics["railPosition"],0,abs_tol=.01)
        await page.locator('label[data-code="MAIN"]').click()
        reset = await scene_after(lambda s: s["activePreset"] == "interview" and s["railGrip"])
        x, y = await point(reset)
        outside_y=min(rect["y"]+rect["height"]*.65,rect["y"]+(reset["railGrip"]["bottom"]+.20)*rect["height"])
        await page.mouse.move(x,outside_y)
        await page.mouse.down()
        await page.mouse.move(x+34,outside_y+8,steps=6)
        await page.mouse.up()
        turned = await scene_after(lambda s: abs(s["shoulderTurn"])>.04)
        assert isclose(turned["railPosition"],reset["railPosition"],abs_tol=.01)
        if touch:
            cdp = await context.new_cdp_session(page)
            async def fingers(kind, spread):
                points = [] if spread is None else [
                    {"x": x-spread, "y": outside_y, "id": 1}, {"x": x+spread, "y": outside_y, "id": 2}]
                await cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})
            await fingers("touchStart",12)
            for spread in (16,22,30,39):
                await fingers("touchMove",spread)
            await fingers("touchEnd",None)
            await asyncio.sleep(.5)
            pinched = await scene(worker)
            assert isclose(pinched["cameraRange"],turned["cameraRange"],rel_tol=.00001)
            hx, hy = await point(pinched)
            for kind, x_ in [("touchStart",hx), ("touchMove",hx+42), ("touchCancel",None), ("touchStart",hx), ("touchEnd",None)]:
                points=[] if x_ is None else [{"x":x_, "y":hy, "id":4}]
                await cdp.send("Input.dispatchTouchEvent", {"type":kind,"touchPoints":points})
            touched = await scene_after(lambda s: s["railGrip"]["anchor"]>pinched["railGrip"]["anchor"]+.06)
            assert touched["shoulderTurn"] == pinched["shoulderTurn"]
            await cdp.detach()
        else:
            hx,hy = await point(turned)
            await page.mouse.move(hx,hy)
            await page.mouse.down()
            await page.mouse.move(width-1,hy,steps=8)
            await page.mouse.up()
            await scene_after(lambda s: s["railGrip"]["anchor"]>.8)
            await page.set_viewport_size({"width":390,"height":844})
            resized = await scene_after(lambda s: s["viewport"][0]<400 and s["railGrip"] and s["railGrip"]["right"]<1)
            await point(resized)
        return {"size":[width,height],"rendering":await canvas.get_attribute("data-rendering"),
                "grip_x":[round(s["railGrip"]["anchor"],4) for s in (initial,right,left,edge,released)],
                "camera_distance":right["cameraRange"],"preset_change":optics["activePreset"],
                "joint_rotation":turned["shoulderTurn"], "pinch_checked":touch}
    finally:
        await context.close()


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("rail-drag-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18582)
    await server.start()
    result = {"passed": False, "views": []}
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                for width,height,touch in [(1440,900,False),(390,844,True)]:
                    result["views"].append(await inspect_view(browser,server,folder,width,height,touch))
            finally:
                await browser.close()
        result["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, result, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
