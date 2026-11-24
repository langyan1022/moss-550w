import argparse
import asyncio
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_550w_model import file_hash, wait_scene
from verify_lens import scene


def measured_finish(images, folder, report):
    regions = {"P04": (778, 339, 850, 360), "M-01": (772, 251, 853, 289)}
    proof = Image.new("RGB", (620, 508))
    labels = ImageDraw.Draw(proof)
    rows = {name: [] for name in regions}
    for index, frame in enumerate(images):
        closeup = frame.crop((725, 158, 880, 402)).resize((310, 488))
        proof.paste(closeup, (index * 310, 20))
        labels.text((index * 310 + 10, 4), ("CURRENT", "ALTERNATE")[index], fill="white")
        for name, box in regions.items():
            gray = frame.crop(box).convert("L")
            values = sorted(ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(2))).get_flattened_data())
            rows[name].append(values[int(.95 * len(values))])
    proof.save(folder / "joint-closeup.png")
    report["luma_residual_p95"] = rows
    assert rows["P04"][0] >= 12 and rows["P04"][1] <= .7 * rows["P04"][0], rows


async def capture(browser, server, folder, label, asset, renderer, turn_direction, probe=None):
    context = await browser.new_context(viewport={"width": 1460, "height": 980}, reduced_motion="reduce")
    counts = {"asset": 0, "renderer": 0}

    async def load_asset(route):
        counts["asset"] += 1
        await route.fulfill(path=str(asset), content_type="model/gltf-binary")

    async def load_renderer(route):
        counts["renderer"] += 1
        await route.fulfill(path=str(renderer), content_type="text/javascript")

    if asset:
        await context.route("**/conversation-assets/moss-exterior.glb*", load_asset)
    if renderer:
        await context.route("**/conversation-assets/moss-render.js*", load_renderer)
    page = await context.new_page()
    page.set_default_timeout(90000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        await page.goto(server.url, wait_until="domcontentloaded")
        canvas = page.locator('#moss-model[data-ready="true"]')
        await canvas.wait_for()
        worker = next(worker for worker in page.workers if "moss-render" in worker.url)
        before = await scene(worker)
        bounds = await canvas.bounding_box()
        x = bounds["x"] + bounds["width"] * .50
        y = bounds["y"] + bounds["height"] * .44
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + bounds["width"] * .15 * turn_direction, y + 7, steps=6)
        await page.mouse.up()
        after = await wait_scene(worker, lambda snapshot: abs(snapshot["shoulderTurn"]) > .20)
        await wait_scene(worker, lambda snapshot: snapshot["frames"] >= after["frames"] + 3)
        await page.mouse.move(8, 75)
        frame = await page.screenshot(path=folder / (label + ".png"))
        assert not errors and counts["asset"] == bool(asset) and counts["renderer"] == bool(renderer), (errors, counts)
        info = {
            "sha256": file_hash(asset) if asset else before["modelRelease"]["glbSha256"],
            "meshes": before["modelRelease"]["visibleMeshes"], "triangles": before["modelRelease"]["visibleTriangles"],
            "camera": after["camera"], "turn": after["shoulderTurn"], "fixture_requests": counts,
        }
        if probe == "material":
            info["materialProbe"] = {}
            for mode in ("no-metal", "no-mr", "no-color", "flat"):
                rendered = (await scene(worker))["frames"]
                count = await worker.evaluate("mode => self.exteriorMaterialProbe(mode)", mode)
                await wait_scene(worker, lambda snapshot: snapshot["frames"] > rendered)
                info["materialProbe"][mode] = count
                await page.screenshot(path=folder / (mode + ".png"))
        elif probe:
            points = json.loads(probe)
            coords = [[(px - bounds["x"]) / bounds["width"], (py - bounds["y"]) / bounds["height"]]
                      for px, py in points]
            hits = await worker.evaluate("points => self.exteriorPixelProbe(points)", coords)
            info["pixelProbe"] = list(zip(points, hits))
        return Image.open(BytesIO(frame)).convert("RGB"), info
    finally:
        await context.close()


async def main(args):
    folder = ROOT / "runs" / datetime.now().strftime("joint-finish-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    server = BrowserServer(folder, 18973, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False}
    try:
        await server.start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                if args.probe:
                    _, report["probe"] = await capture(browser, server, folder, "probe", args.alternate,
                                                       args.renderer, -1 if args.left else 1, args.probe)
                    report["passed"] = True
                    return
                frames = []
                for label, asset, renderer in (("current", None, None), ("alternate", args.alternate, args.renderer)):
                    image, info = await capture(browser, server, folder, label, asset, renderer,
                                                -1 if args.left else 1)
                    frames.append(image)
                    report[label] = info
                side_by_side = Image.new("RGB", (1460, 510))
                labels = ImageDraw.Draw(side_by_side)
                for index, frame in enumerate(frames):
                    side_by_side.paste(frame.resize((730, 490)), (730 * index, 20))
                    labels.text((730 * index + 10, 4), ("CURRENT", "ALTERNATE")[index], fill="white")
                side_by_side.save(folder / "comparison.png")
                assert report["current"]["triangles"] == report["alternate"]["triangles"]
                assert abs(report["current"]["turn"] - report["alternate"]["turn"]) < .04
                if args.require_finish:
                    measured_finish(frames, folder, report)
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--alternate", type=Path)
    parser.add_argument("--renderer", type=Path)
    parser.add_argument("--left", action="store_true")
    parser.add_argument("--probe")
    parser.add_argument("--require-finish", action="store_true")
    args = parser.parse_args()
    if not (args.alternate or args.renderer):
        parser.error("请选择用于对照的机身或渲染器")
    if args.require_finish and not args.left:
        parser.error("关节像素核验需要固定的左侧机位")
    asyncio.run(main(args))
