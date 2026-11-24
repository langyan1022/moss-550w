import argparse
import asyncio
from array import array
from datetime import datetime
import json
from math import pi, sin
import os
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_550w_memory import memory_summary, sample
from verify_context_reply import free_port
from verify_memory import send


DRAFT = "场景换挡时还没发送的词。"
TRACK_COMMAND = "频谱测试员的双音阶梯"
CANDIDATE_PATHS = {
    "/static/agent-terminal.js": "agent-terminal.js",
    "/static/assistant-gestures.js": "assistant-gestures.js",
    "/conversation-assets/moss-view.js": "moss-view.js",
    "/conversation-assets/earth-view.js": "earth-view.js",
    "/conversation-assets/earth-renderer.js": "earth-renderer.js",
}

PROBE = """
sessionStorage.setItem('moss.agent.opened', '1');
localStorage.setItem('moss.voice.enabled', 'off');
const state = window.sceneLifecycle = {
  origin:performance.timeOrigin, workers:[], streams:[], interruptions:[]
};
const NativeWorker = window.Worker;
window.Worker = class extends NativeWorker {
  constructor(url, options) {
    super(url, options);
    if (new URL(url, location.href).pathname !== '/conversation-assets/moss-render.js') return;
    const entry = {startedAt:performance.now(), readyAt:null, stoppedAt:null};
    state.workers.push(entry);
    this.sceneEntry = entry;
    this.addEventListener('message', ({data}) => {
      if (data.type === 'ready') entry.readyAt = performance.now();
    });
  }
  terminate() {
    if (this.sceneEntry && this.sceneEntry.stoppedAt === null) this.sceneEntry.stoppedAt = performance.now();
    return super.terminate();
  }
};
const NativeEventSource = window.EventSource;
window.EventSource = class extends NativeEventSource {
  constructor(url, options) {
    super(url, options);
    if (new URL(url, location.href).pathname !== '/api/events') return;
    const record = {source:this, opens:0, revision:-1};
    state.streams.push(record);
    this.addEventListener('open', () => record.opens++);
    this.addEventListener('state', ({data}) => { record.revision = JSON.parse(data).revision; });
  }
};
window.sceneSnapshot = () => {
  const audio = state.audio;
  const canvas = document.getElementById('moss-model');
  const originalStream = state.streams[0];
  const playing = [...document.querySelectorAll('.media-audio')].filter(node => !node.paused && !node.ended);
  return {
    origin:state.origin, now:performance.now(), sameAudio:!!audio && audio.isConnected && playing[0] === audio,
    audioSrc:audio?.currentSrc, audioTime:audio?.currentTime, paused:audio?.paused, mediaError:audio?.error?.code ?? null,
    ended:audio?.ended, playing:playing.length,
    draft:document.getElementById('message').value,
    streams:state.streams.length, opens:originalStream?.opens, revision:originalStream?.revision,
    streamOpen:originalStream?.source.readyState === NativeEventSource.OPEN,
    workers:state.workers, currentCanvasReady:canvas?.dataset.ready === 'true',
    previousCanvasDetached:state.lastCanvas ? !state.lastCanvas.isConnected : null,
    currentCanvasNew:state.lastCanvas ? state.lastCanvas !== canvas : null,
    interruptions:state.interruptions
  };
};
"""


def create_catalog(folder):
    music = folder / "catalog" / "频谱测试员"
    music.mkdir(parents=True)
    rate = 16000
    samples = array("h", (round(10000 * sin(2 * pi * 230 * i / rate)) for i in range(rate))).tobytes()
    with wave.open(str(music / "双音阶梯.wav"), "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", ""))
        for _ in range(300):
            audio.writeframesraw(samples)
    index = folder / "catalog.sqlite3"
    library = MediaLibrary(index)
    library.configure([{"id": "lifecycle-qa", "kind": "local", "path": str(music.parent)}])
    assert library.scan()[0]["count"] == 1
    return index


async def click_scene(page):
    preferences = page.locator(".preferences")
    if await preferences.get_attribute("open") is None:
        await preferences.locator("summary").click()
    await page.locator("#scene-mode-link").click()


async def run_page(page, server, result, checkpoint, baseline, edge_pid=None):
    async def record_memory(name):
        if edge_pid is None:
            return
        await asyncio.sleep(3)
        rows = []
        for index in range(3):
            if index:
                await asyncio.sleep(.4)
            rows.append(await asyncio.to_thread(sample, edge_pid))
        result["memory"][name] = {"samples": rows, **memory_summary(rows)}
        checkpoint()

    async def verify_in_place(name, previous, expected_workers, *, light, advance=0.06):
        await page.wait_for_function(
            "time => window.sceneLifecycle.audio?.currentTime > time", arg=previous["audioTime"] + advance, timeout=10000
        )
        current = await page.evaluate("sceneSnapshot()")
        current["url"] = page.url
        result["stages"][name] = current
        checkpoint()
        assert current["origin"] == start["origin"] and current["draft"] == DRAFT, current
        assert current["sameAudio"] and current["audioSrc"] == start["audioSrc"], current
        assert current["playing"] == 1 and not current["paused"] and not current["ended"] and not current["mediaError"], current
        assert not current["interruptions"], current["interruptions"]
        assert current["streams"] == start["streams"] == 1 and current["opens"] == start["opens"] == 1, current
        assert current["streamOpen"], current
        assert len(current["workers"]) == expected_workers, current
        active = [worker for worker in current["workers"] if worker["stoppedAt"] is None]
        assert len(active) == (0 if light else 1), current
        assert (parse_qs(urlsplit(page.url).query).get("scene") == ["off"]) == light, page.url
        media_elapsed = current["audioTime"] - previous["audioTime"]
        clock_elapsed = (current["now"] - previous["now"]) / 1000
        assert abs(media_elapsed - clock_elapsed) < 1.5, (media_elapsed, clock_elapsed, current)
        return current

    async def prove_sse(name):
        status, payload = await server.post("/api/remember", {"text": f"这条隔离验收由同页事件流收到。阶段{name}"})
        assert status == 200, payload
        revision = (await server.state())["revision"]
        await page.wait_for_function("r => sceneLifecycle.streams[0].revision >= r", arg=revision, timeout=10000)
        result["sseRevisions"][name] = revision

    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.locator(".connection.online").wait_for(timeout=15000)
    assert "双音阶梯" in await send(page, server, TRACK_COMMAND)
    await page.wait_for_function("""() => [...document.querySelectorAll('.media-audio')].some(audio =>
      !audio.paused && !audio.error && audio.currentTime > .25)""", timeout=20000)
    await page.evaluate("""() => {
      const state = window.sceneLifecycle;
      state.audio = [...document.querySelectorAll('.media-audio')].find(audio => !audio.paused);
      for (const name of ['pause', 'seeking', 'ended', 'emptied']) {
        state.audio.addEventListener(name, () => state.interruptions.push(name));
      }
    }""")
    await page.get_by_test_id("composer").fill(DRAFT)
    start = await page.evaluate("sceneSnapshot()")
    assert start["sameAudio"] and start["streamOpen"] and start["workers"] == [], start
    result["stages"]["initial_light"] = start
    checkpoint()
    await record_memory("initial_light")

    previous = start
    for count in range(1, (1 if baseline else 2) + 1):
        launched = perf_counter()
        await click_scene(page)
        await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=75000)
        full = await verify_in_place(f"full_{count}", previous, count, light=False)
        full["readyMs"] = round((perf_counter() - launched) * 1000, 2)
        await record_memory(f"full_{count}")
        await page.evaluate("sceneLifecycle.lastCanvas = document.getElementById('moss-model'); void 0")
        await click_scene(page)
        await page.locator("main[data-light-scene]").wait_for(timeout=15000)
        if baseline:
            await page.wait_for_function("origin => performance.timeOrigin !== origin", arg=start["origin"], timeout=15000)
            await page.locator(".connection.online").wait_for(timeout=15000)
            navigated = await page.evaluate("sceneSnapshot()")
            navigated["url"] = page.url
            result["stages"]["baseline_light_after_navigation"] = navigated
            assert not navigated["sameAudio"]
            result["reproduced"] = True
            return
        await page.wait_for_function("n => sceneLifecycle.workers.slice(0,n).every(worker => worker.stoppedAt !== null)", arg=count)
        light = await verify_in_place(f"light_{count}", full, count, light=True)
        assert light["previousCanvasDetached"] and light["currentCanvasNew"] and not light["currentCanvasReady"], light
        await prove_sse(str(count))
        await record_memory(f"light_{count}")
        previous = light

    await click_scene(page)
    await page.wait_for_function("sceneLifecycle.workers.length === 3", timeout=10000)
    await page.evaluate("sceneLifecycle.lastCanvas = document.getElementById('moss-model'); void 0")
    assert not await page.locator('#moss-model[data-ready="true"]').count(), "取消用例需要在首帧前触发。"
    await click_scene(page)
    await page.locator("main[data-light-scene]").wait_for(timeout=15000)
    await page.wait_for_function("sceneLifecycle.workers[2].stoppedAt !== null", timeout=10000)
    cancelled = await verify_in_place("cancelled_startup", previous, 3, light=True)
    assert cancelled["workers"][2]["readyAt"] is None and cancelled["previousCanvasDetached"], cancelled
    await prove_sse("取消")
    assert len((await server.state())["turns"]) == 1
    await page.wait_for_timeout(300)
    assert len((await page.evaluate("sceneSnapshot()"))["workers"]) == 3


async def run(args):
    folder = ROOT / "runs" / datetime.now().strftime("scene-lifecycle-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    result = {
        "passed": False, "baseline": args.baseline, "stages": {}, "sseRevisions": {}, "pageErrors": [],
        "outsideRequests": [], "candidate": str(args.candidate) if args.candidate else None, "candidateResponses": [],
    }

    def checkpoint():
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint()
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    settings = {
        "LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
        "MOSS_MEDIA_DB": str(create_catalog(folder)), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0", "MOSS_SPEECH_MIC_WARM": "0",
    }
    try:
        with patch.dict(os.environ, settings):
            await server.start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                result["edge"] = browser.version
                edge_pid = None
                if not args.baseline:
                    session = await browser.new_browser_cdp_session()
                    try:
                        processes = (await session.send("SystemInfo.getProcessInfo"))["processInfo"]
                        edge_pid = next(process["id"] for process in processes if process["type"] == "browser")
                    finally:
                        await session.detach()
                    result.update(edgePid=edge_pid, memory={})
                context = await browser.new_context(viewport={"width": 1360, "height": 820}, reduced_motion="reduce",
                                                    service_workers="block")
                page = await context.new_page()
                page.set_default_timeout(20000)
                page.on("pageerror", lambda error: result["pageErrors"].append(str(error)))

                async def route_request(route):
                    url = urlsplit(route.request.url)
                    if url.scheme in {"http", "https"} and not route.request.url.startswith(server.url + "/"):
                        result["outsideRequests"].append({"host": url.hostname, "path": url.path})
                        await route.abort()
                        return
                    name = CANDIDATE_PATHS.get(url.path)
                    if name and args.candidate:
                        candidate = args.candidate / name
                        if candidate.is_file():
                            result["candidateResponses"].append(url.path)
                            await route.fulfill(path=candidate, content_type="text/javascript")
                            return
                    await route.continue_()

                await context.route("**/*", route_request)
                await page.add_init_script("(() => {\n" + PROBE + "\n})();")
                await run_page(page, server, result, checkpoint, args.baseline, edge_pid)
                assert not result["pageErrors"] and not result["outsideRequests"], result
                if args.candidate:
                    assert {"/static/agent-terminal.js", "/conversation-assets/moss-view.js"} <= set(result["candidateResponses"])
            finally:
                await browser.close()
        result["passed"] = True
    except Exception as error:
        result["error"] = repr(error)
        raise
    finally:
        await server.stop()
        checkpoint()
        print(folder / "result.json", result["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true", help="复现旧版从三维返回轻量时导航并失去播放元素")
    parser.add_argument("--candidate", type=Path, help="只在隔离 Edge 中提供候选界面模块")
    args = parser.parse_args()
    if args.baseline and args.candidate:
        parser.error("--baseline 使用现有发布资源")
    asyncio.run(run(args))
