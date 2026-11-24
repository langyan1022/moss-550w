import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import aiohttp
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_lens import scene


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def downloaded_hash(client, url):
    sha = hashlib.sha256()
    size = 0
    async with client.get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(262144):
            size += len(chunk)
            sha.update(chunk)
    if not size:
        raise ValueError(f"资源为空 {url}")
    return sha.hexdigest(), size


async def wait_scene(worker, predicate):
    async with asyncio.timeout(15):
        while True:
            snapshot = await scene(worker)
            if predicate(snapshot):
                return snapshot
            await asyncio.sleep(.1)


async def inspect(browser, server, folder, report):
    page = await browser.new_page(viewport={"width": 1360, "height": 820}, reduced_motion="reduce")
    await page.add_init_script("sessionStorage.setItem('moss.agent.opened','1')")
    await page.add_init_script("""window.modelStages = [];
        const watch = new MutationObserver(() => {
            const stage=document.querySelector('#model-load-stage')?.textContent;
            if(stage && window.modelStages.at(-1)?.stage!==stage)
                window.modelStages.push({stage,ms:Math.round(performance.now())});
        });
        watch.observe(document,{childList:true,subtree:true});""")
    page.set_default_timeout(90000)
    failed = []
    page.on("pageerror", lambda err: failed.append(str(err)))
    try:
        start = perf_counter()
        await page.goto(server.url, wait_until="domcontentloaded")
        chat_button = page.locator("#history-toggle")
        await chat_button.click()
        await page.locator("#message").fill("测试输入")
        report["chatInputUsableMs"] = round((perf_counter() - start) * 1000)
        await page.locator("#message").fill("")
        await chat_button.click()
        canvas = page.locator('#moss-model[data-ready="true"]')
        await canvas.wait_for()
        report["firstReadyMs"] = round((perf_counter() - start) * 1000)
        report["loadingStages"] = await page.evaluate("window.modelStages")
        worker = next(w for w in page.workers if "moss-render" in w.url)
        initial = await wait_scene(worker, lambda s: s["ready"] and s["railGrip"])
        release = initial["modelRelease"]
        report.update(modelRelease=release, softwareRenderer=initial["software"])
        assert release["sourceSha256"].lower() == report["sourceSha256"]
        assert release["sourceFile"] == report["sourceFile"]
        assert release["visibleMeshes"] > 0 and release["visibleTriangles"] > 0 and release["groups"] > 0
        if release["sourceFile"] == "550W-MOSS-外壳轻磨损.blend":
            assert release["h50PanelTriangles"] > 0
        decals = initial.get("decalAudit") or []
        assert len(decals) >= 18, f"Expected >= 18 decals, got {len(decals)}"
        inverted = [d for d in decals if d.get("duRight", 0) <= 0 or d.get("imageUpSign", 0) <= 0]
        assert not inverted, f"Inverted/mirrored decals detected: {inverted}"
        by_node = {d["nodeName"]: d for d in decals}
        for req_flipped in (
            "CAD-独立制作标识_·_opticalProcessor",
            "CAD-独立制作标识_·_serviceWarning",
            "CAD-独立制作标识_·_serviceWarning004",
            "CAD-独立制作标识_·_serviceWarning005",
            "CAD-独立制作标识_·_warningRed001",
            "CAD-独立制作标识_·_exhaust",
        ):
            assert by_node.get(req_flipped, {}).get("vCorrected") is True, f"Missing V-flip fix on {req_flipped}"
        for bc_node, expect_red in (("CAD-独立制作标识_·_h50001", True), ("CAD-独立制作标识_·_h50003", False)):
            bc = by_node.get(bc_node, {}).get("barcode") or {}
            assert bc.get("upgraded") is True and bc.get("transitions", 0) >= 40 and bc.get("midAlpha", 99) == 0 and bc.get("tabAlpha", 0) >= 245 and bc.get("tabRed") is expect_red, f"Bad barcode on {bc_node}: {bc}"
        assert "(crisp vector barcode)" not in (by_node.get("CAD-独立制作标识_·_h50004", {}).get("mapName") or ""), "I/O SERVICE decal must stay untouched"
        report["decalAuditSummary"] = {"count": len(decals), "allUpright": True, "barcodesUpgraded": 2}
        assert await canvas.get_attribute("data-model-release") == report["sourceFile"]
        assert report["sourceFile"].removesuffix(".blend") in await canvas.get_attribute("title")
        await page.screenshot(path=folder / "550W-首页.png")
        entries = await worker.evaluate("performance.getEntriesByType('resource').map(e=>e.name)")
        glb = next(url for url in entries if urlsplit(url).path.endswith("/moss-exterior.glb"))
        entry = await page.evaluate("""() => performance.getEntriesByType('resource').map(e=>e.name)
            .find(url=>new URL(url).pathname==='/conversation-assets/moss-view.js')""")
        assert entry
        report["assets"] = {"renderer": worker.url, "glb": glb, "entry": entry}
        point = await canvas.evaluate("""(c,g)=>{
            const r=c.getBoundingClientRect();
            for(const y of [.5,.25,.75]) for(const x of [.5,.25,.75]) {
                const a=r.x+r.width*(g.left+x*(g.right-g.left));
                const b=r.y+r.height*(g.top+y*(g.bottom-g.top));
                if(document.elementFromPoint(a,b)===c) return [a,b,r.width];
            }
        }""", initial["railGrip"])
        assert point, "页面上找不到吊装座的拖动区域"
        x, y, width = point
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + .13 * width, y, steps=6)
        shifted = await wait_scene(worker, lambda s: s["railGrip"]["anchor"] > initial["railGrip"]["anchor"] + .06)
        await page.mouse.up()
        await page.mouse.wheel(0, 430)
        await asyncio.sleep(.4)
        assert (await scene(worker))["camera"] == initial["camera"] == shifted["camera"]
        report["gripBeforeAfter"] = [initial["railGrip"]["anchor"], shifted["railGrip"]["anchor"]]
        report["cameraViews"] = []
        for label in ("ASSEMBLY", "FRONT", "ARRAY", "PANEL", "MAIN", "OPTICS"):
            control = page.locator(f'label[data-code="{label}"]')
            preset = await control.locator("input").get_attribute("value")
            previous_frame = (await scene(worker))["frames"]
            await control.click()
            await wait_scene(worker, lambda s: s["activePreset"] == preset and s["frames"] >= previous_frame + 3)
            report["cameraViews"].append(preset)
            if label == "PANEL":
                await page.screenshot(path=folder / "550W-面板.png")
            if label == "MAIN":
                bounds = await canvas.bounding_box()
                mid_x = bounds["x"] + .50 * bounds["width"]
                mid_y = bounds["y"] + .44 * bounds["height"]
                await page.mouse.move(mid_x, mid_y)
                await page.mouse.down()
                await page.mouse.move(mid_x + .20 * bounds["width"], mid_y + 8, steps=10)
                await page.mouse.up()
                rotated = await wait_scene(worker, lambda s: abs(s["shoulderTurn"]) > .2)
                report["bodyTurnRadians"] = rotated["shoulderTurn"]
                await page.screenshot(path=folder / "550W-机身侧面.png")
        await page.emulate_media(reduced_motion="no-preference")
        levels, rings = [], []
        for _ in range(16):
            frame = await scene(worker)
            levels.append(frame["tof"])
            rings.append(frame["dualTof"]["frame"])
            await asyncio.sleep(.22)
        report["lensSignalRange"] = [min(levels), max(levels)]
        report["movingRingFrames"] = len(set(rings))
        assert max(levels) - min(levels) > .6 and len(set(rings)) > 3
        await page.screenshot(path=folder / "550W-镜头.png")
        if not await page.locator("#message").is_visible():
            await page.locator("#history-toggle").click()
        await page.locator("#message").fill("算了")
        async with page.expect_response(lambda r: r.url.endswith("/api/speak")) as played:
            await page.locator("#send").click()
        voice = await played.value
        assert hashlib.sha256(await voice.body()).hexdigest() == file_hash(ROOT / "voice/lines/done.wav")
        spoken = await wait_scene(worker, lambda s: s["source"] == "voice" and s["level"] > .005)
        report["recordedVoice"] = {"level": spoken["level"], "light": spoken["tof"], "status": voice.status}
        assert not failed, failed
    finally:
        await page.close()


async def main(blend, live):
    folder = ROOT / "runs" / datetime.now().strftime("550w-release-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18963, model_url="http://127.0.0.1:18964/v1")
    report = {"passed": False, "sourceFile": blend.name, "sourceSha256": file_hash(blend)}
    await server.start()
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                await inspect(browser, server, folder, report)
            finally:
                await browser.close()
        async with aiohttp.ClientSession(trust_env=False) as client:
            for name, url in report["assets"].items():
                relative_url = url.removeprefix(server.url)
                assert relative_url.startswith("/conversation-assets/")
                sha, size = await downloaded_hash(client, url)
                assert (sha, size) == await downloaded_hash(client, live.rstrip("/") + relative_url)
                asset = ROOT / "static" / "conversation-assets" / Path(urlsplit(url).path).name
                assert sha == file_hash(asset)
                report[name + "Sha256"] = sha
                report[name + "Bytes"] = size
            entry = live.rstrip("/") + report["assets"]["entry"].removeprefix(server.url)
            async with client.head(entry) as fresh:
                fresh.raise_for_status()
                assert "no-cache" in fresh.headers.get("Cache-Control", "")
                etag = fresh.headers["ETag"]
            async with client.head(entry, headers={"If-None-Match": etag}) as cached:
                assert cached.status == 304
                report["unchangedEntryHttpStatus"] = cached.status
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("blend", type=Path)
    parser.add_argument("--live", default="http://127.0.0.1:18221")
    args = parser.parse_args()
    asyncio.run(main(args.blend, args.live))
