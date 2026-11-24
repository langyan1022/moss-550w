import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter, time_ns
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_context_reply import free_port


ARMS = ("light", "earth", "model", "combined", "candidate")
ASSETS = ROOT / "static/conversation-assets"
PROBE = """
sessionStorage.setItem('moss.agent.opened', '1');
localStorage.setItem('moss.voice.enabled', 'off');
const epoch = () => performance.timeOrigin + performance.now();
const marks = window.sceneMemoryMarks = { navigation: performance.timeOrigin };
document.addEventListener('click', ({target}) => {
  if (target.closest?.('#scene-mode-link')) marks.trigger = epoch();
}, true);
const original = CanvasRenderingContext2D.prototype.clearRect;
CanvasRenderingContext2D.prototype.clearRect = function(...args) {
  const value = original.apply(this, args);
  if (!marks.earthReady && this.canvas.matches('#earth-view .earth-overlay')) marks.earthReady = epoch();
  return value;
};
const observer = new MutationObserver(() => {
  if (document.querySelector('#moss-model[data-ready="true"]')) {
    marks.modelReady = epoch();
    observer.disconnect();
  }
});
observer.observe(document, {attributes:true, childList:true, subtree:true, attributeFilter:['data-ready']});
"""
WINDOW_STATE = """() => {
  const main = document.querySelector('main');
  const earth = document.querySelector('#earth-view');
  const model = document.querySelector('#moss-model');
  const surface = earth.querySelector('.earth-sphere');
  return {
    innerWidth, innerHeight, devicePixelRatio, hidden:document.hidden,
    reducedMotion:matchMedia('(prefers-reduced-motion: reduce)').matches,
    opening:main.hasAttribute('data-opening'), light:main.hasAttribute('data-light-scene'),
    dialogueOpen:document.querySelector('#dialogue').hasAttribute('data-open'),
    earthVisible:earth.checkVisibility({visibilityProperty:true}), earthCovered:earth.hasAttribute('data-covered'),
    earthRenderer:surface.dataset.renderer ?? null,
    earthSurfaceWidth:surface.getBoundingClientRect().width,
    modelReady:model.dataset.ready === 'true', modelRenderer:model.dataset.rendering ?? null,
    modelSourceSha256:model.dataset.modelSourceSha256 ?? null,
    modelError:model.dataset.error ?? null
  };
}"""


def wall_ms():
    return time_ns() / 1_000_000


def sample(pid, *, uss=False):
    started = wall_ms()
    root = psutil.Process(pid)
    processes = []
    for process in (root, *root.children(recursive=True)):
        try:
            info = process.memory_full_info() if uss else process.memory_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        evidence = {"pid": process.pid, "rssBytes": info.rss, "privateBytes": info.private}
        if uss:
            evidence["ussBytes"] = info.uss
        processes.append(evidence)
    finished = wall_ms()
    return {"epochMs": (started + finished) / 2, "sampleMs": round(finished - started, 2),
            "rssBytes": sum(item["rssBytes"] for item in processes),
            "privateBytes": sum(item["privateBytes"] for item in processes),
            "availableBytes": psutil.virtual_memory().available, "processes": processes,
            **({"ussBytes": sum(item["ussBytes"] for item in processes)} if uss else {})}


def memory_summary(rows):
    if not rows:
        return {"n": 0}
    return {"n": len(rows), **{
        key.replace("Bytes", "MiB"): {"first": round(rows[0][key] / 2**20, 2),
                                      "median": round(median(row[key] for row in rows) / 2**20, 2),
                                      "peak": round(max(row[key] for row in rows) / 2**20, 2),
                                      "last": round(rows[-1][key] / 2**20, 2)}
        for key in ("rssBytes", "privateBytes")},
        "minAvailableMiB": round(min(row["availableBytes"] for row in rows) / 2**20, 2)}


async def sample_loop(pid, seconds, min_available_mib, rows, stream, stopped):
    while not stopped.is_set():
        started = perf_counter()
        row = await asyncio.to_thread(sample, pid)
        rows.append(row)
        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        stream.flush()
        if row["availableBytes"] / 2**20 < min_available_mib:
            raise RuntimeError(f"系统可用内存低于 {min_available_mib} MiB，已结束此对照。")
        try:
            await asyncio.wait_for(stopped.wait(), max(0, seconds - (perf_counter() - started)))
        except TimeoutError:
            pass


async def probe_gpu(browser):
    session = await browser.new_browser_cdp_session()
    try:
        gpu = (await session.send("SystemInfo.getInfo"))["gpu"]
        roles = (await session.send("SystemInfo.getProcessInfo"))["processInfo"]
    finally:
        await session.detach()
    attributes = gpu.get("auxAttributes", {})
    features = gpu.get("featureStatus", {})
    return {"devices": gpu.get("devices", []), "processInfo": roles,
            "attributes": {name: attributes[name] for name in ("glRenderer", "glVendor", "glVersion", "glImplementationParts")
                           if name in attributes},
            "features": {name: features[name] for name in ("webgl", "webgl2", "gpu_compositing", "rasterization")
                         if name in features}}


async def observe_arm(browser, server, name, args, arm, stream):
    expected_model = name in {"model", "combined", "candidate"}
    expected_earth = name in {"earth", "combined", "candidate"}
    expect_earth_draw = expected_earth and not args.chat_open
    expect_earth_init = expected_earth and not (args.chat_open and name == "candidate")
    module_for = {"model": "/conversation-assets/moss-view.js", "earth": "/conversation-assets/earth-view.js"}
    skipped = {module_for[kind] for kind, expected in (("model", expected_model), ("earth", expected_earth)) if not expected}
    name_of = {module_for["model"], module_for["earth"], "/conversation-assets/earth-renderer.js",
               "/conversation-assets/moss-render.js", "/conversation-assets/moss-exterior.glb",
               "/conversation-assets/moss-tof.glb"}
    actual = []
    requests = []
    errors = []
    outside = []
    rows = []
    await confirm_muted_edge(browser)
    gpu = await probe_gpu(browser)
    pid = next(proc["id"] for proc in gpu["processInfo"] if proc["type"] == "browser")
    arm.update(edgePid=pid, browserVersion=browser.version, gpu=gpu, requests=requests, responses=actual,
               outsideRequests=outside, pageErrors=errors)
    context = await browser.new_context(viewport={"width": 1360, "height": 820}, device_scale_factor=1,
                                        reduced_motion="reduce", service_workers="block")
    page = await context.new_page()

    async def serve_only(route):
        url = urlsplit(route.request.url)
        if url.scheme in {"http", "https"} and not route.request.url.startswith(server.url + "/"):
            outside.append({"host": url.hostname, "path": url.path})
            await route.abort()
        elif url.path in skipped:
            await route.fulfill(status=200, content_type="text/javascript", body=(
                "export function mountMossView(){return ()=>{};}\n"
                "export function mountEarthView(){}\nexport function unmountEarthView(){}\n"
            ))
        elif name == "candidate" and url.path == module_for["earth"]:
            await route.fulfill(status=200, content_type="text/javascript", path=args.candidate_earth)
        else:
            await route.continue_()

    def watch_request(request):
        path = urlsplit(request.url).path
        if path in name_of or path.startswith("/conversation-assets/encom-globe/"):
            requests.append({"path": path, "epochMs": wall_ms(), "stub": path in skipped})

    def watch_response(response):
        path = urlsplit(response.url).path
        if path in name_of or path.startswith("/conversation-assets/encom-globe/"):
            actual.append({"path": path, "status": response.status, "epochMs": wall_ms(), "stub": path in skipped})

    await context.route("**/*", serve_only)
    context.on("request", watch_request)
    context.on("response", watch_response)
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.add_init_script(PROBE)
    ready_dom = "Boolean((!expected.model || sceneMemoryMarks.modelReady) && (!expected.earth || sceneMemoryMarks.earthReady))"
    stopped = asyncio.Event()
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(sample_loop(pid, args.sample_seconds, args.min_available_mib, rows, stream, stopped))
            try:
                await page.goto(server.url + "/?scene=off", wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for(state="attached", timeout=15000)
                await page.locator("main[data-light-scene]").wait_for(timeout=15000)
                if not args.chat_open:
                    await page.locator("#close-chat").click()
                    await page.locator("#dialogue[data-open]").wait_for(state="detached", timeout=15000)
                await page.locator(".preferences summary").click()
                await asyncio.sleep(args.baseline_seconds)
                original_document = await page.evaluate("performance.timeOrigin")
                arm["windowBefore"] = await page.evaluate(WINDOW_STATE)
                if args.uss:
                    arm["ussBefore"] = await asyncio.to_thread(sample, pid, uss=True)
                if name == "light":
                    await page.locator(".preferences summary").click()
                    await page.evaluate("sceneMemoryMarks.trigger = performance.timeOrigin + performance.now()")
                else:
                    await page.locator("#scene-mode-link").click()
                    try:
                        await page.wait_for_function(f"expected => {ready_dom}",
                                                     arg={"model": expected_model, "earth": expect_earth_draw},
                                                     timeout=args.ready_timeout_seconds * 1000)
                        if expect_earth_init and not expect_earth_draw:
                            await page.locator(".earth-sphere[data-renderer]").wait_for(state="attached")
                    except Exception:
                        arm["windowAtFailure"] = await page.evaluate(WINDOW_STATE)
                        arm["marksEpochMs"] = await page.evaluate("sceneMemoryMarks")
                        raise
                marks = await page.evaluate("sceneMemoryMarks")
                arm["marksEpochMs"] = marks
                arm["triggerToReadyMs"] = {key: round(marks[key] - marks["trigger"], 2)
                                           for key in ("modelReady", "earthReady") if key in marks}
                arm["windowReady"] = await page.evaluate(WINDOW_STATE)
                await asyncio.sleep(args.steady_seconds)
                arm["windowAfter"] = await page.evaluate(WINDOW_STATE)
                arm["sameDocument"] = original_document == await page.evaluate("performance.timeOrigin")
            finally:
                stopped.set()
        if args.uss:
            arm["ussTail"] = await asyncio.to_thread(sample, pid, uss=True)
        arm["gpuAfter"] = await probe_gpu(browser)
        end = rows[-1]["epochMs"]
        ready = max(marks.get("modelReady", marks["trigger"]), marks.get("earthReady", marks["trigger"]))
        arm["memory"] = {
            "baseline": memory_summary([row for row in rows if marks["trigger"] - args.baseline_seconds * 1000 <= row["epochMs"] < marks["trigger"]]),
            "startup": memory_summary([row for row in rows if marks["trigger"] <= row["epochMs"] <= ready]),
            "firstFiveSecondsAfterReady": memory_summary([row for row in rows if ready < row["epochMs"] <= ready + 5000]),
            "settledTail": memory_summary([row for row in rows if max(ready, end - min(10, args.steady_seconds) * 1000) < row["epochMs"]]),
            "peakSinceTrigger": memory_summary([row for row in rows if row["epochMs"] >= marks["trigger"]]),
        }
        baseline = arm["memory"]["baseline"]
        assert baseline["n"] >= 1 and arm["memory"]["settledTail"]["n"] >= 1, arm["memory"]
        arm["peakPrivateMiBAboveBaseline"] = round(arm["memory"]["peakSinceTrigger"]["privateMiB"]["peak"] - baseline["privateMiB"]["median"], 2)
        arm["settledPrivateMiBAboveBaseline"] = round(arm["memory"]["settledTail"]["privateMiB"]["median"] - baseline["privateMiB"]["median"], 2)
        after = arm["windowAfter"]
        assert arm["sameDocument"] and not after["hidden"] and after["modelReady"] == expected_model, after
        assert after["light"] == (name == "light") and bool(after["earthRenderer"]) == expect_earth_init, after
        assert after["dialogueOpen"] == args.chat_open and after["earthVisible"] == expect_earth_draw, after
        if args.chat_open and name == "candidate":
            assert not any(item["path"].startswith("/conversation-assets/encom-globe/") for item in requests), requests
        assert not errors and not outside, (errors, outside)
        if expected_model:
            paths = {item["path"] for item in actual if item["status"] == 200 and not item["stub"]}
            required = {module_for["model"], "/conversation-assets/moss-render.js", "/conversation-assets/moss-exterior.glb", "/conversation-assets/moss-tof.glb"}
            assert required <= paths, (required, paths)
        arm["passed"] = True
    finally:
        await context.close()


async def run(args):
    folder = args.out
    folder.mkdir(parents=True, exist_ok=False)
    print(folder, flush=True)
    opened = []
    report = {"passed": False, "arms": [], "modelConnections": 0,
              "viewport": [1360, 820], "sampleSeconds": args.sample_seconds, "chatOpen": args.chat_open,
              "baselineSeconds": args.baseline_seconds, "steadySeconds": args.steady_seconds,
              "assetSha256": {name: sha256((ASSETS / name).read_bytes()).hexdigest()
                              for name in ("moss-view.js", "moss-render.js", "moss-exterior.glb",
                                           "moss-tof.glb", "earth-view.js", "earth-renderer.js")}}
    if args.candidate_earth:
        report["candidateEarthSha256"] = sha256(args.candidate_earth.read_bytes()).hexdigest()
    target = folder / "result.json"

    def checkpoint():
        temp = folder / "result.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)

    async def model_guard(_reader, writer):
        report["modelConnections"] += 1
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
        model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
        server = BrowserServer(folder / "server", free_port(), model_url=model_url)
        settings = {"MOSS_WINDOWS_REMINDERS": "0", "MOSS_MEDIA_DB": str(folder / "empty-catalog.sqlite3"),
                    "MOSS_PROVIDER_DIR": str(folder / "providers"), "MOSS_MEM0_ENABLED": "0",
                    "LOCALAPPDATA": str(folder / "profile"), "MOSS_SPEECH_STANDBY": "0", "MOSS_SPEECH_MIC_WARM": "0"}
        try:
            with patch.dict(os.environ, settings):
                await server.start()
            async with async_playwright() as playwright:
                for index, name in enumerate(args.arms, 1):
                    trace = f"{index:02}-{name}-samples.jsonl"
                    arm = {"name": name, "passed": False, "samples": trace}
                    report["arms"].append(arm)
                    checkpoint()
                    print(f"{index}/{len(args.arms)} {name}", flush=True)
                    browser = await open_browser(playwright)
                    try:
                        with (folder / trace).open("x", encoding="utf-8") as stream:
                            await observe_arm(browser, server, name, args, arm, stream)
                        assert arm["edgePid"] not in opened, opened
                        opened.append(arm["edgePid"])
                        print(name, arm["triggerToReadyMs"], arm["peakPrivateMiBAboveBaseline"],
                              arm["settledPrivateMiBAboveBaseline"], flush=True)
                    finally:
                        await browser.close()
                        checkpoint()
            assert not report["modelConnections"] and not (await server.state())["turns"], report
            report["passed"] = True
        except BaseException as error:
            report["error"] = repr(error)
            raise
        finally:
            checkpoint()
            await server.stop()
            print(target, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / datetime.now().strftime("550w-memory-%Y%m%d-%H%M%S-%f"))
    parser.add_argument("--arms", nargs="+", choices=ARMS)
    parser.add_argument("--candidate-earth", type=Path)
    parser.add_argument("--baseline-seconds", type=float, default=2.0)
    parser.add_argument("--steady-seconds", type=float, default=20.0)
    parser.add_argument("--sample-seconds", type=float, default=.2)
    parser.add_argument("--ready-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--min-available-mib", type=int, default=4608)
    parser.add_argument("--chat-open", action="store_true", help="保持对话栏打开，比较不可见的地球是否仍在加载")
    parser.add_argument("--uss", action="store_true", help="基线与末尾额外记录 Windows 私有驻留量")
    args = parser.parse_args()
    if args.arms is None:
        args.arms = ARMS if args.candidate_earth else ARMS[:-1]
    if "candidate" in args.arms and not (args.candidate_earth and args.candidate_earth.is_file()):
        parser.error("candidate 需要一个可读取的 --candidate-earth 文件")
    if args.chat_open and not set(args.arms) <= {"combined", "candidate"}:
        parser.error("--chat-open 比较完整原版与候选，请选择 combined 和 candidate")
    if not 0 < args.sample_seconds <= min(args.baseline_seconds, args.steady_seconds) or args.ready_timeout_seconds <= 0:
        parser.error("计时参数须大于零，采样间隔不能长于基线或稳定窗口")
    asyncio.run(run(args))
