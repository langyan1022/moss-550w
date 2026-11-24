"""Check progressive local status in muted Edge with the real app and Windows readings."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
import os
import re
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_local_overview_progressive import available_port


SIZES = ((1440, 900), (768, 1024), (375, 812))
FAST = ["clock", "battery"]
ALL = [*FAST, "volume"]
VOLUME = "系统音量为 37%。"
READY = "本机连接已就绪"
READING = "正在处理消息"
REPLIED = "本轮完成"
CANCELLED = "本轮已取消"
OBSERVE = """
localStorage.setItem('moss.voice.enabled', 'off');
window.statusQA = {states:[], openings:[], runtime:[]};
window.addEventListener('moss-agent-state', event => {
  const turn = event.detail.turns.at(-1);
  if (turn) statusQA.states.push({...turn, qaRevision:event.detail.revision});
});
const runtimeQA = new EventSource('/conversation/runtime-log');
runtimeQA.onmessage = event => statusQA.runtime.push(JSON.parse(event.data));
const nativeShowModal = HTMLDialogElement.prototype.showModal;
HTMLDialogElement.prototype.showModal = function (...args) {
  if (this.id === 'local-status-panel') statusQA.openings.push({phase:this.dataset.phase, ms:performance.now()});
  return nativeShowModal.apply(this, args);
};
"""

SURFACE = """panel => {
  function rgba(css) {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = css;
    ctx.fillRect(0, 0, 1, 1);
    return Array.from(ctx.getImageData(0, 0, 1, 1).data);
  }
  function luminance(rgb) {
    return rgb.slice(0, 3).map(value => {
      const channel = value / 255;
      return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
    }).reduce((v, x, i) => v + x * [.2126, .7152, .0722][i], 0);
  }
  function background(element) {
    for (let current = element; current; current = current.parentElement) {
      const rgb = rgba(getComputedStyle(current).backgroundColor);
      if (rgb[3] === 255) return rgb;
    }
    return rgba('#010305');
  }
  function contrast(left, right) {
    const a = luminance(left), b = luminance(right);
    return (Math.max(a,b) + .05) / (Math.min(a,b) + .05);
  }
  const textNodes = [...panel.querySelectorAll('h2, button, dt, dd, footer span, time, #local-status-progress')]
    .filter(node => node.getClientRects().length && node.textContent.trim());
  const panelStyle = getComputedStyle(panel), box = panel.getBoundingClientRect();
  return {
    viewport:[innerWidth,innerHeight],
    page:[document.documentElement.clientWidth,document.documentElement.scrollWidth],
    panel:{x:box.x, y:box.y, right:box.right, bottom:box.bottom, client:panel.clientWidth, scroll:panel.scrollWidth,
      radius:parseFloat(panelStyle.borderTopLeftRadius), shadow:panelStyle.boxShadow, blur:panelStyle.backdropFilter,
      cssFilter:panelStyle.filter, background:panelStyle.backgroundColor},
    text:textNodes.map(node => {
      const style = getComputedStyle(node), rect = node.getBoundingClientRect();
      return {text:node.textContent.trim(), px:parseFloat(style.fontSize), color:style.color, font:style.fontFamily,
        contrast:contrast(rgba(style.color),background(node)), x:rect.x, right:rect.right};
    }),
    animations:panel.getAnimations({subtree:true}).filter(animation=>animation.playState==='running').length,
    audio:['.stage-audio-lane','.stage-signal','#stage-audio'].map(selector => {
      const node = document.querySelector(selector);
      const style = getComputedStyle(node);
      return {selector, alpha:rgba(style.backgroundColor)[3], image:style.backgroundImage, shadow:style.boxShadow,
        borders:['Top','Right','Bottom','Left'].map(side=>parseFloat(style['border'+side+'Width']))};
    })
  };
}"""


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("local-status-progressive-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    report = {"passed": False, "cases": [], "model_connections": 0, "speech_requests": [], "outside_requests": [], "page_errors": []}
    print(result, flush=True)
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    gates = [(asyncio.Event(), asyncio.Event(), asyncio.Event()) for _ in range(len(SIZES) + 2)]
    reads = 0

    async def read_volume(intent):
        nonlocal reads
        assert intent["action"] == "get_volume", intent
        started, release, cancelled = gates[reads]
        reads += 1
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return VOLUME

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
           "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0", "MOSS_KEV_ENABLED": "0",
           "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}

    async def seen(page, turn_id, status, *, count=0, revision=0):
        handle = await page.wait_for_function("""({id,status,count,revision}) => statusQA.states.find(row => row.id === id &&
          row.status === status && row.qaRevision >= revision && (!count || row.local_overview_partial?.length === count))""",
                                              arg={"id": turn_id, "status": status, "count": count, "revision": revision})
        try:
            return await handle.json_value()
        finally:
            await handle.dispose()

    async def readings(panel):
        return await panel.locator("[data-reading-kind]").evaluate_all("rows => rows.map(row => row.dataset.readingKind)")

    async def runtime_sequence(page, sibling, messages):
        async def read(tab):
            await tab.wait_for_function("""({count,last}) => statusQA.runtime.length >= count &&
              statusQA.runtime.at(-1)?.text === last""", arg={"count": len(messages), "last": messages[-1]})
            entries = await tab.evaluate("statusQA.runtime")
            assert [entry["text"] for entry in entries] == messages, entries
            assert all(isinstance(entry["time"], int) and entry["time"] > 0 for entry in entries), entries
            return entries

        return await asyncio.gather(read(page), read(sibling))

    async def mutate_during_read(server, page, sibling, key, index):
        old = await server.state()
        assert old["turns"][-1]["id"] == key and old["turns"][-1]["status"] == "thinking", old
        code, changed = await server.post("/api/remember", {"text": f"运行日志隔离验收记录 {index}。"})
        revision = changed["revision"]
        assert code == 200 and revision > old["revision"] and changed["turns"][-1]["status"] == "thinking", changed
        await asyncio.gather(*(seen(tab, key, "thinking", count=len(FAST), revision=revision) for tab in (page, sibling)))
        assert not gates[index][1].is_set() and not gates[index][2].is_set()
        return revision

    async def begin(server, page, sibling, index):
        started, release, cancelled = gates[index]
        await page.bring_to_front()
        quick = page.locator("#quick-local-status")
        await expect(quick).to_be_enabled()
        tick = perf_counter()
        await quick.click()
        async with asyncio.timeout(3):
            await started.wait()
        key = (await server.state())["turns"][-1]["id"]
        early = await seen(page, key, "thinking", count=len(FAST))
        panel = page.get_by_test_id("local-status-panel")
        await expect(panel).to_be_visible()
        await expect(panel).to_have_attribute("data-phase", "reading")
        await expect(panel.locator("#local-status-progress")).to_have_text("读取中")
        await expect(panel.locator("#local-status-footer")).to_be_hidden()
        assert await panel.evaluate("el => el.matches(':modal') && el.open && Boolean(el.getAttribute('aria-labelledby'))")
        assert await readings(panel) == FAST and [row["kind"] for row in early["local_overview_partial"]] == FAST
        clock = re.search(r"\d\d:\d\d:\d\d", early["local_overview_partial"][0]["reply"])[0]
        await expect(panel.locator('[data-reading-kind="clock"] .status-reading-value')).to_have_text(clock)
        assert early["local_overview_partial"][1]["reply"] in early["partial_reply"]
        await seen(sibling, key, "thinking", count=len(FAST))
        await expect(sibling.get_by_test_id("local-status-panel")).to_be_hidden()
        assert not release.is_set() and not cancelled.is_set(), "The real dialog must show readings while volume is still gated"
        return key, panel, round((perf_counter() - tick) * 1000, 1)

    async def complete(server, page, key, panel, index):
        gates[index][1].set()
        done = await seen(page, key, "replied")
        assert done["cloud_requests"]["total"] == 0 and [row["kind"] for row in done["local_overview"]] == ALL, done
        assert done["local_overview"][-1]["reply"] == VOLUME and not done.get("local_overview_partial"), done
        await expect(panel).to_have_attribute("data-phase", "complete")
        await expect(panel.locator("#local-status-progress")).to_have_text("读取完成")
        await expect(panel.locator("#local-status-cancel")).to_be_hidden()
        await expect(panel.locator('#local-status-time')).to_have_attribute("datetime", done["replied_at"])
        assert await readings(panel) == ALL and (await server.state())["turns"][-1]["id"] == key
        return done

    async def visuals(panel, size):
        values = await panel.evaluate(SURFACE)
        width, height = size
        box = values["panel"]
        assert values["viewport"] == [width, height] and values["page"][1] <= values["page"][0] + 1, values
        assert 0 <= box["x"] < box["right"] <= width + 1 and 0 <= box["y"] < box["bottom"] <= height + 1, values
        assert box["scroll"] <= box["client"] + 1 and values["animations"] == 0, values
        assert box["radius"] <= 2 and box["shadow"] == box["blur"] == box["cssFilter"] == "none", values
        assert values["text"] and all(row["px"] >= 12 and row["contrast"] >= 4.5 for row in values["text"]), values
        assert all(box["x"] - 1 <= row["x"] <= row["right"] <= box["right"] + 1 for row in values["text"]), values
        assert all(item["alpha"] == 0 and item["image"] == item["shadow"] == "none" and not any(item["borders"])
                   for item in values["audio"]), values["audio"]
        return values

    async def tabs(browser, server, size, stack):
        width, height = size
        context = await browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce",
                                            has_touch=width == 375, service_workers="block")
        stack.push_async_callback(context.close)

        async def route_request(route):
            url = urlsplit(route.request.url)
            if url.scheme in {"http", "https"} and (url.hostname != "127.0.0.1" or url.port != server.port):
                report["outside_requests"].append(route.request.url)
                await route.abort()
            elif url.path in {"/api/speak", "/api/speak-batch", "/api/prepare-speech"}:
                report["speech_requests"].append(url.path)
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_request)
        await context.add_init_script(OBSERVE)
        primary, sibling = await context.new_page(), await context.new_page()
        for page in (primary, sibling):
            page.set_default_timeout(6000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
        return primary, sibling

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch("local_overview.run_desktop", read_volume))
            sink = await stack.enter_async_context(await asyncio.start_server(guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
                    os.environ[prefix + "_API_KEY"] = "isolated-qa"
            server = BrowserServer(folder, available_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "unused")
            stack.push_async_callback(moss.close)
            for _, release, _ in gates:
                stack.callback(release.set)
            stopped = asyncio.Event()
            app = application(moss, lambda: "standby", server.data, server.port, "local-status-qa", stopped)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", server.port).start()
            playwright = await stack.enter_async_context(async_playwright())
            browser = await open_browser(playwright)
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)

            for index, size in enumerate(SIZES):
                async with AsyncExitStack() as viewport:
                    page, sibling = await tabs(browser, server, size, viewport)
                    initial = READY if index == 0 else REPLIED
                    await runtime_sequence(page, sibling, [initial])
                    key, panel, early_ms = await begin(server, page, sibling, index)
                    mutation = None
                    if index == 0:
                        await runtime_sequence(page, sibling, [initial, READING])
                        mutation = await mutate_during_read(server, page, sibling, key, index)
                    close = panel.get_by_role("button", name="关闭", exact=True)
                    await expect(close).to_be_focused()
                    await panel.evaluate("""node => {statusQA.dialog = node; statusQA.close = document.activeElement;
                      statusQA.readings = [...node.querySelectorAll('[data-reading-kind]')];}""")
                    done = await complete(server, page, key, panel, index)
                    assert await panel.evaluate("""node => node === statusQA.dialog && document.activeElement === statusQA.close &&
                      statusQA.readings.every((row,index)=>node.querySelectorAll('[data-reading-kind]')[index] === row)""")
                    await seen(sibling, key, "replied")
                    runtime = await runtime_sequence(page, sibling, [initial, READING, REPLIED])
                    await expect(sibling.get_by_test_id("local-status-panel")).to_be_hidden()
                    assert await sibling.evaluate("statusQA.openings.length") == 0
                    assert await page.evaluate("statusQA.openings.map(row=>row.phase)") == ["reading"]
                    values = await visuals(panel, size)
                    assert (size[0] > 375) or (await close.bounding_box())["height"] >= 44
                    screenshot = folder / f"status-{size[0]}x{size[1]}.png"
                    await page.screenshot(path=screenshot, animations="disabled")
                    await page.keyboard.press("Escape")
                    await expect(panel).to_be_hidden()
                    history = page.locator(f'[data-turn-id="{key}"]').get_by_test_id("open-local-status")
                    await history.focus()
                    await page.keyboard.press("Enter")
                    await expect(panel).to_be_visible()
                    await expect(close).to_be_focused()
                    assert await close.evaluate("node => { const s=getComputedStyle(node); return node.matches(':focus-visible') && s.outlineStyle!=='none' && parseFloat(s.outlineWidth)>=2; }")
                    await page.keyboard.press("Escape")
                    await expect(panel).to_be_hidden()
                    await expect(history).to_be_focused()
                    report["cases"].append({"case": "responsive progressive modal", "size": size, "early_ms": early_ms,
                                            "readings": done["local_overview"], "appearance": values, "screenshot": str(screenshot),
                                            "runtime": runtime, "same_turn_mutation_revision": mutation})

            async with AsyncExitStack() as interaction:
                page, sibling = await tabs(browser, server, SIZES[0], interaction)
                await runtime_sequence(page, sibling, [REPLIED])
                index = len(SIZES)
                key, panel, _ = await begin(server, page, sibling, index)
                await runtime_sequence(page, sibling, [REPLIED, READING])
                mutation = await mutate_during_read(server, page, sibling, key, index)
                await page.keyboard.press("Escape")
                await expect(panel).to_be_hidden()
                gates[index][1].set()
                await seen(page, key, "replied")
                await expect(page.locator(f'[data-turn-id="{key}"]').get_by_test_id("open-local-status")).to_be_visible()
                await expect(panel).to_be_hidden()
                assert await page.evaluate("statusQA.openings.length") == 1
                runtime = await runtime_sequence(page, sibling, [REPLIED, READING, REPLIED])
                report["cases"].append({"case": "dismissed partial stays closed after final", "runtime": runtime,
                                        "same_turn_mutation_revision": mutation})

                key, panel, _ = await begin(server, page, sibling, index + 1)
                cancel = panel.locator("#local-status-cancel")
                await expect(cancel).to_be_visible()
                async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/cancel") as response:
                    await cancel.click()
                assert (await response.value).ok
                await seen(page, key, "cancelled")
                await seen(sibling, key, "cancelled")
                runtime = await runtime_sequence(page, sibling, [REPLIED, READING, REPLIED, READING, CANCELLED])
                await expect(panel).to_be_hidden()
                async with asyncio.timeout(2):
                    await gates[index + 1][2].wait()
                gates[index + 1][1].set()
                stopped_turn = (await server.state())["turns"][-1]
                assert stopped_turn["id"] == key and stopped_turn["status"] == "cancelled" and not stopped_turn.get("local_overview")
                assert await page.locator(f'[data-turn-id="{key}"]').get_by_test_id("open-local-status").count() == 0
                assert await page.evaluate("statusQA.openings.length") == 2
                assert await sibling.evaluate("statusQA.openings.length") == 0
                report["cases"].append({"case": "cancel in modal closes it for the owner and ignores late reading", "runtime": runtime})

            assert reads == len(gates) and report["model_connections"] == 0
            assert not any(report[key] for key in ("speech_requests", "outside_requests", "page_errors")), report
            stopped.set()
            moss._notify()
            report["passed"] = True
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
