import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright
import psutil

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from serve import application
from verify_550w_model import downloaded_hash, file_hash, inspect as inspect_model
from verify_550w_memory import memory_summary, probe_gpu, sample_loop
from verify_context_reply import free_port


ASSETS = ROOT / "static/conversation-assets"
RELEASE_ASSETS = json.loads((ROOT / "model-release.json").read_text(encoding="utf-8"))["assets"]
ASSET_SHA256 = {
    "moss-render.js": RELEASE_ASSETS["moss-render.js"],
    "moss-exterior.glb": RELEASE_ASSETS["moss-exterior.glb"],
    "moss-tof.glb": "0a64963f61eac1a9b5e8c44a4eb17aa8f460bd89349891f510a20f19f943df26",
    "moss-view.js": RELEASE_ASSETS["moss-view.js"],
}

PAGE_PROBE = """
sessionStorage.setItem("moss.agent.opened", "1");
const metrics = window.modelTiming = {nav:performance.timeOrigin};
const timestamp = () => performance.timeOrigin + performance.now();
const NativeWorker = window.Worker;
window.Worker = class extends NativeWorker {
  constructor(url, options) {
    const entry = new URL(url, location.href);
    if (entry.origin !== location.origin || entry.pathname !== "/conversation-assets/moss-render.js") {
      super(url, options);
      return;
    }
    metrics.workerConstructed = timestamp();
    metrics.originalEntry = entry.href;
    const probe = new URL("/qa-550w/worker", location.origin);
    probe.searchParams.set("entry", entry.href);
    super(probe, options);
    this.addEventListener("message", ({data}) => {
      if (data.type === "ready") metrics.pageReceivedReady = timestamp();
      if (data.type === "model-perf-diagnostic") metrics.worker = data.value;
    });
  }
};
const observer = new MutationObserver(() => {
  if (document.querySelector('#moss-model[data-ready="true"]') && !metrics.domReady) {
    metrics.domReady = timestamp();
    observer.disconnect();
  }
});
observer.observe(document, {attributes:true, childList:true, subtree:true, attributeFilter:["data-ready"]});
"""

ALLOCATION_PROBE = PAGE_PROBE.replace(
    'probe.searchParams.set("entry", entry.href);',
    'probe.searchParams.set("entry", entry.href); probe.searchParams.set("allocations", "1");',
).replace('super(probe, options);', 'super(probe, options); window.allocationWorker = this;') + """
let nextAllocationId = 0;
window.allocationSnapshot = phase => new Promise((resolve, reject) => {
  const worker = window.allocationWorker;
  const id = ++nextAllocationId;
  const timeout = setTimeout(() => {
    worker.removeEventListener('message', response);
    reject(new Error('550W 资源快照超时'));
  }, 5000);
  function response({data}) {
    if (data.type !== 'qa-allocation-sample' || data.id !== id) return;
    clearTimeout(timeout);
    worker.removeEventListener('message', response);
    resolve(data.value);
  }
  worker.addEventListener('message', response);
  worker.postMessage({type:'qa-allocation-sample', id, phase});
});
"""


def hashes():
    return {name: file_hash(ASSETS / name) for name in ASSET_SHA256}


async def inspect(browser, base, expected, capture):
    session = await browser.new_browser_cdp_session()
    try:
        info = (await session.send("SystemInfo.getInfo"))["gpu"]
    finally:
        await session.detach()
    aux = info.get("auxAttributes", {})
    features = info.get("featureStatus", {})
    gpu = {
        "devices": info["devices"],
        "attributes": {key: aux[key] for key in
                       ("glRenderer", "glVendor", "glVersion", "glImplementationParts", "passthroughCmdDecoder")
                       if key in aux},
        "features": {key: features[key] for key in ("webgl", "webgl2", "rasterization", "gpu_compositing")
                     if key in features},
    }
    page = await browser.new_page(viewport={"width": 1360, "height": 820}, reduced_motion="reduce")
    await page.add_init_script(PAGE_PROBE)
    responses = []
    errors = []

    def observe(response):
        path = urlsplit(response.url).path
        if path.startswith("/conversation-assets/") and path.rsplit("/", 1)[-1] in ASSET_SHA256:
            responses.append({"path": path, "url": response.url, "status": response.status})

    page.on("response", observe)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_default_timeout(90000)
    try:
        await page.goto(base, wait_until="domcontentloaded")
        button = page.locator("#history-toggle")
        await button.click()
        composer = page.locator("#message")
        await composer.fill("测试输入")
        await page.evaluate("modelTiming.chatUsable = performance.timeOrigin + performance.now()")
        await composer.fill("")
        await button.click()
        await page.locator('#moss-model[data-ready="true"]').wait_for()
        await page.wait_for_function("modelTiming.worker && modelTiming.domReady")
        result = await page.evaluate("modelTiming")
        result["devicePixelRatio"] = await page.evaluate("devicePixelRatio")
        if capture:
            await page.locator("#moss-model").screenshot(path=str(capture), animations="disabled")
        assert not errors, errors
    finally:
        await page.close()
    assert {r["path"].rsplit("/", 1)[-1] for r in responses if r["status"] == 200} == set(ASSET_SHA256), responses
    assert result["originalEntry"].endswith(expected["moss-render.js"])
    assert result["worker"]["release"]["glbSha256"] == expected["moss-exterior.glb"]
    return {"gpu": gpu, "browserVersion": browser.version, "responses": responses, "metrics": result}


def relative_ms(report, exterior_bytes):
    metrics = report["metrics"]
    origin = metrics["nav"]
    worker = metrics["worker"]
    timing = [dict(item, timeMs=round(item["time"] - origin, 2)) for item in worker["timeline"]]
    stages = {item["name"]: item["time"] for item in timing}
    texture = worker["bitmapDecode"]
    exterior = next(entry for entry in worker["resources"] if "/moss-exterior.glb" in entry["name"])
    required = {"graphics", "lighting", "geometry", "optics-ready", "assembly", "frame", "worker_ready"}
    assert required <= stages.keys(), stages
    assert exterior["encodedBytes"] == exterior_bytes, exterior
    assert exterior["transferBytes"] >= exterior["encodedBytes"], exterior
    assert metrics["workerConstructed"] <= stages["graphics"] <= stages["worker_ready"] <= metrics["pageReceivedReady"] <= metrics["domReady"]
    return {
        "chatUsable": round(metrics["chatUsable"] - origin, 2),
        "domReady": round(metrics["domReady"] - origin, 2),
        "workerSetupAfterConstruction": round(stages["graphics"] - metrics["workerConstructed"], 2),
        "webglInitialization": round(stages["lighting"] - stages["graphics"], 2),
        "environmentLighting": round(stages["geometry"] - stages["lighting"], 2),
        "exteriorResponseEnd": round(exterior["responseEnd"] - origin, 2),
        "exteriorTransfer": round(exterior["responseEnd"] - exterior["responseStart"], 2),
        "exteriorResponseEndToAssembly": round(stages["assembly"] - exterior["responseEnd"], 2),
        "opticsReady": round(stages["optics-ready"] - origin, 2),
        "bitmapDecodeLast": round(texture["last"] - origin, 2) if texture["last"] is not None else None,
        "assembly": round(stages["frame"] - stages["assembly"], 2),
        "shaderWarmup": round(stages["frame"] - stages["shaders"], 2) if "shaders" in stages else None,
        "frameStarted": round(stages["frame"] - origin, 2),
        "frameToFirstGLDraw": round(stages["first_gl_draw"] - stages["frame"], 2) if "first_gl_draw" in stages else None,
        "frameToWorkerReady": round(stages["worker_ready"] - stages["frame"], 2),
        "workerReadyToPageEvent": round(metrics["pageReceivedReady"] - stages["worker_ready"], 2),
        "pageEventToDomReady": round(metrics["domReady"] - metrics["pageReceivedReady"], 2),
    }


def memory_by_role(rows, roles):
    grouped = {}
    for index, row in enumerate(rows):
        sizes = {}
        for process in row["processes"]:
            role = roles.get(process["pid"], "other")
            sizes[role] = sizes.get(role, 0) + process["privateBytes"]
        for role in sizes.keys() - grouped.keys():
            grouped[role] = [0] * index
        for role, values in grouped.items():
            values.append(sizes.get(role, 0))
    return {role: {"median": round(median(values) / 2**20, 2), "peak": round(max(values) / 2**20, 2)}
            for role, values in grouped.items()}


async def inspect_allocations(browser, base, expected, folder, capture, fixed_viewport):
    gpu = await probe_gpu(browser)
    pid = next(process["id"] for process in gpu["processInfo"] if process["type"] == "browser")
    page = await browser.new_page(viewport={"width": 1360, "height": 820}, reduced_motion="reduce")
    errors = []
    outside = []

    async def local_model_only(route):
        if not route.request.url.startswith(base + "/"):
            outside.append(urlsplit(route.request.url).hostname)
            await route.abort()
        elif urlsplit(route.request.url).path == "/conversation-assets/earth-view.js":
            await route.fulfill(content_type="text/javascript", body="export {};")
        else:
            await route.continue_()

    await page.route("**/*", local_model_only)
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.add_init_script(ALLOCATION_PROBE)
    rows = []
    snapshots = []
    windows = {}
    stopped = asyncio.Event()
    epoch = "performance.timeOrigin + performance.now()"

    async def sample_rendering(name):
        start = await page.evaluate(epoch)
        await asyncio.sleep(5)
        measured = await page.evaluate("phase => allocationSnapshot(phase)", name)
        snapshots.append(measured)
        windows[name] = (start, measured["time"])
        return measured["drawingBuffer"]

    async def wait_size(original, *, same):
        async with asyncio.timeout(15):
            while True:
                measured = await page.evaluate("allocationSnapshot('resize')")
                if (measured["drawingBuffer"] == original) == same:
                    return
                await asyncio.sleep(.18)

    try:
        with (folder / "edge-allocation-samples.jsonl").open("x", encoding="utf-8") as stream:
            async with asyncio.TaskGroup() as group:
                group.create_task(sample_loop(pid, .25, 4608, rows, stream, stopped))
                try:
                    await page.goto(base + "/?scene=off", wait_until="domcontentloaded")
                    await page.locator(".connection.online").wait_for(state="attached", timeout=15000)
                    await page.locator("#close-chat").click()
                    await page.locator(".preferences summary").click()
                    baseline = await page.evaluate(epoch)
                    await asyncio.sleep(3)
                    trigger = await page.evaluate(epoch)
                    windows["light"] = (baseline, trigger)
                    await page.locator("#scene-mode-link").click()
                    await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=90000)
                    await page.wait_for_function("modelTiming.worker?.allocationStages.length && modelTiming.domReady")
                    metrics = await page.evaluate("modelTiming")
                    timeline = {stage["name"]: stage["time"] for stage in metrics["worker"]["timeline"]}
                    frame = timeline["frame"]
                    shaders = timeline.get("shaders", frame)
                    windows.update(model_setup=(trigger, shaders), shader_preparation=(shaders, frame),
                                   first_frame=(frame, metrics["domReady"]))
                    original = await sample_rendering("desktop")
                    during_gpu = await probe_gpu(browser)
                    if fixed_viewport:
                        assert await sample_rendering("desktop_control_2") == original
                        assert await sample_rendering("desktop_control_3") == original
                        if capture:
                            await page.locator("#moss-model").screenshot(path=str(folder / "desktop-frame.png"), animations="disabled")
                        await page.locator(".preferences summary").click()
                        await page.locator("#scene-mode-link").click()
                        await page.locator("main[data-light-scene]").wait_for()
                        await page.locator(".connection.online").wait_for(state="attached", timeout=15000)
                        returned = await page.evaluate(epoch)
                        await asyncio.sleep(5)
                        assert not page.workers and not await page.locator('#moss-model[data-ready]').count()
                        windows["returned_to_light"] = (returned, await page.evaluate(epoch))
                    else:
                        await page.set_viewport_size({"width": 1024, "height": 640})
                        await wait_size(original, same=False)
                        narrow = await sample_rendering("compact")
                        await page.set_viewport_size({"width": 1360, "height": 820})
                        await wait_size(original, same=True)
                        assert await sample_rendering("desktop_restored") == original
                        assert narrow[0] * narrow[1] < original[0] * original[1], (narrow, original)
                finally:
                    stopped.set()
        if capture and not fixed_viewport:
            await page.locator("#moss-model").screenshot(path=str(folder / "restored-frame.png"), animations="disabled")
        assert not errors and not outside, (errors, outside)
        assert metrics["originalEntry"].endswith(expected["moss-render.js"])
        assert metrics["worker"]["release"]["glbSha256"] == expected["moss-exterior.glb"]
        after_gpu = await probe_gpu(browser)
        roles = {p["id"]: p["type"] for group in (gpu, during_gpu, after_gpu) for p in group["processInfo"]}
        phases = {}
        for name, (start, end) in windows.items():
            selected = [row for row in rows if start <= row["epochMs"] < end]
            phases[name] = {**memory_summary(selected), "durationMs": round(end-start, 1),
                            "privateMiBByProcessRole": memory_by_role(selected, roles)}
        return {"gpu": after_gpu, "browserVersion": browser.version, "metrics": metrics,
                "memoryPhases": phases, "settledAllocationSnapshots": snapshots,
                "rawSamples": "edge-allocation-samples.jsonl", "earthModuleStubbed": True,
                "fixedViewport": fixed_viewport}
    finally:
        await page.close()


async def run(folder, candidate=None, capture=False, model=None, verify_controls=False, allocations=False, fixed_viewport=False):
    folder.mkdir(parents=True, exist_ok=True)
    before = hashes()
    assert before == ASSET_SHA256, before
    port = free_port()
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0",
                      MOSS_MEDIA_DB=str(folder / "catalog.sqlite3"), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0")
    data = folder / "data"
    moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
    app = application(moss, lambda: "standby", data, port, "isolated", asyncio.Event())
    expected = dict(before)
    exterior_file = ASSETS / "moss-exterior.glb"
    if candidate:
        bundles = {name: candidate / name for name in ("moss-render.js", "moss-view.js")}
        if model:
            exterior_file = candidate / "moss-exterior.glb"
            bundles.update({"moss-exterior.glb": exterior_file, "moss-exterior.json": candidate / "moss-exterior.json"})
        expected.update({name: file_hash(path) for name, path in bundles.items()})
        staged_pages = {
            f"/{url_folder}/{name}": candidate / name
            for url_folder, names in (("static", ("agent-terminal.js", "assistant-gestures.js")),
                                      ("conversation-assets", ("earth-view.js", "earth-renderer.js")))
            for name in names if (candidate / name).is_file()
        }

        @web.middleware
        async def candidate_assets(request, handler):
            prefix = "/conversation-assets/"
            if file := staged_pages.get(request.path):
                return web.FileResponse(file, headers={"Cache-Control": "no-store"})
            if request.path.startswith(prefix) and (file := bundles.get(request.path.removeprefix(prefix))):
                return web.FileResponse(file, headers={"Cache-Control": "no-store"})
            return await handler(request)

        app.middlewares.append(candidate_assets)

    async def worker(_request):
        return web.FileResponse(ROOT / "diagnostic_550w_worker.js", headers={"Content-Type": "text/javascript"})

    app.router.add_get("/qa-550w/worker", worker)
    if allocations:
        async def allocation_module(_request):
            return web.FileResponse(ROOT / "diagnostic_550w_allocations.js", headers={"Content-Type": "text/javascript"})

        app.router.add_get("/qa-550w/allocations", allocation_module)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    memory = psutil.virtual_memory().available / 2**30
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                if allocations:
                    report = await inspect_allocations(browser, base, expected, folder, capture, fixed_viewport)
                else:
                    report = await inspect(browser, base, expected,
                                           folder / "first-frame.png" if capture else None)
                if model or verify_controls:
                    if model:
                        source, source_hash = model.name, file_hash(model)
                    else:
                        registered = json.loads((ROOT / "model-release.json").read_text(encoding="utf-8"))
                        source, source_hash = registered["source"], registered["sourceSha256"]
                    proof = {"passed": False, "sourceFile": source, "sourceSha256": source_hash}
                    await inspect_model(browser, SimpleNamespace(url=base), folder, proof)
                    async with aiohttp.ClientSession(trust_env=False) as client:
                        for name, url in proof["assets"].items():
                            sha, size = await downloaded_hash(client, url)
                            assert sha == expected[Path(urlsplit(url).path).name]
                            proof[name + "Sha256"] = sha
                            proof[name + "Bytes"] = size
                    proof["passed"] = True
                    (folder / "model-qa.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
    assert hashes() == before
    report.update(passed=True, assetsSha256=expected, availableGiBBeforeEdge=round(memory, 2))
    if not allocations:
        report["timingsMs"] = relative_ms(report, exterior_file.stat().st_size)
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(folder, report.get("timingsMs", {}), report["metrics"]["worker"]["adapter"], flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / datetime.now().strftime("550w-timing-%Y%m%d-%H%M%S-%f"))
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--model-proof", type=Path)
    parser.add_argument("--verify-controls", action="store_true", help="复验已登记模型的机位、镜头和导轨")
    parser.add_argument("--allocations", action="store_true", help="记录机身 WebGL 分配和调整视口前后的 Edge 私有提交量")
    parser.add_argument("--fixed-viewport", action="store_true", help="保持原视口完成分配对照，随后测量返回轻量页")
    args = parser.parse_args()
    if args.model_proof and not args.candidate:
        parser.error("--model-proof 需要隔离候选资源")
    if args.fixed_viewport and not args.allocations:
        parser.error("--fixed-viewport 需要 --allocations")
    asyncio.run(run(args.out.resolve(), args.candidate.resolve() if args.candidate else None,
                    args.capture, args.model_proof, args.verify_controls, args.allocations, args.fixed_viewport))
