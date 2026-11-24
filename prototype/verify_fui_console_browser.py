import asyncio
from datetime import datetime
import json
import os
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_550w_model import wait_scene
from verify_configured_mcp import isolated
from verify_local_overview import QUESTION
from verify_local_overview_browser import inspect_panel
from verify_stage_spectrum import HOME_SNAPSHOT


CONTROL_GEOMETRY = """el => {
  const rect = el.getBoundingClientRect();
  const old = el.style.pointerEvents;
  try {
    // 状态标签通常允许鼠标穿透。只在同步取样时将它加入命中测试。
    el.style.pointerEvents = "auto";
    const hits = [];
    for (const x of [.14, .5, .86]) for (const y of [.25, .5, .75]) {
      const node = document.elementFromPoint(rect.left + x*rect.width, rect.top + y*rect.height);
      hits.push(node === el || el.contains(node));
    }
    return {box:[rect.left,rect.top,rect.right,rect.bottom], width:innerWidth, height:innerHeight,
            visible:el.checkVisibility({visibilityProperty:true, opacityProperty:true}), hits};
  } finally {el.style.pointerEvents = old;}
}"""

DEVICE_STRIP_GEOMETRY = """strip => {
  const bounds = el => el?.getBoundingClientRect().toJSON() ?? null;
  const shown = el => el?.checkVisibility({visibilityProperty:true, opacityProperty:true});
  const values = Object.fromEntries([...strip.querySelectorAll('[data-main-metric] dd')].map(el => [
    el.closest('[data-main-metric]').dataset.mainMetric,
    {value:el.textContent.trim(), bounds:bounds(el), visible:shown(el)}
  ]));
  const box = selector => bounds(document.querySelector(selector));
  const drawer = document.querySelector('#dialogue');
  return {phase:strip.dataset.phase, state:strip.querySelector('#device-strip-state').textContent.trim(),
    visible:shown(strip), strip:bounds(strip), width:innerWidth, height:innerHeight, values,
    header:box('.top'), shortcut:box('#quick-local-status'), model:box('.model-viewport'),
    signal:box('.stage-signal'), drawer:shown(drawer) ? bounds(drawer) : null};
}"""


def overlaps(left, right):
    return (min(left["right"], right["right"]) - max(left["left"], right["left"]) > 1 and
            min(left["bottom"], right["bottom"]) - max(left["top"], right["top"]) > 1)


async def check_control(page, selector):
    element = page.locator(selector)
    await element.wait_for()
    data = await element.evaluate(CONTROL_GEOMETRY)
    left, top, right, bottom = data["box"]
    assert data["visible"] and all(data["hits"]), (selector, data)
    assert 0 <= left < right <= data["width"] + 1 and 0 <= top < bottom <= data["height"] + 1, (selector, data)
    return data


async def check_device_strip(page):
    strip = page.locator(".top #device-strip")
    await strip.wait_for()
    await page.wait_for_function("""() => {
      const panel = document.querySelector('.top #device-strip');
      const cpu = panel?.querySelector('[data-main-metric="cpu"] dd')?.textContent.trim();
      return panel?.dataset.phase === 'connected' && /\\d{1,3}\\s*%/.test(cpu ?? '');
    }""")
    data = await strip.evaluate(DEVICE_STRIP_GEOMETRY)
    box, header, shortcut = (data[name] for name in ("strip", "header", "shortcut"))
    assert data["visible"] and data["phase"] == "connected", data
    assert 0 <= box["left"] < box["right"] <= data["width"] + 1, data
    assert 0 <= box["top"] < box["bottom"] <= data["height"] + 1, data
    assert box["top"] >= header["top"] - 1 and box["bottom"] <= header["bottom"] + 1, data
    if data["width"] >= 1101:
        assert box["top"] < shortcut["bottom"] and box["bottom"] > shortcut["top"], data
    else:
        assert box["top"] >= shortcut["bottom"] - 1, data
    assert not overlaps(box, shortcut), data
    for key in ("model", "signal", "drawer"):
        assert not data[key] or not overlaps(box, data[key]), (key, data)
    values = data["values"]
    assert set(values) == {"clock", "cpu", "memory", "battery", "disk"}, data
    assert all(item["visible"] and item["value"] for item in values.values()), data
    for name, item in values.items():
        rect = item["bounds"]
        assert (box["left"] - 1 <= rect["left"] < rect["right"] <= box["right"] + 1 and
                box["top"] - 1 <= rect["top"] < rect["bottom"] <= box["bottom"] + 1), (name, data)
    assert re.search(r"\d{1,2}:\d{2}", values["clock"]["value"]), data
    for name in ("memory", "disk"):
        assert re.search(r"[0-9]", values[name]["value"]), (name, data)
    assert len(page.context.pages) == 1 and not await page.locator("dialog[open]").count()
    assert not await page.evaluate("Boolean(window.documentPictureInPicture?.window)")
    return data


async def confirm_idle(page):
    await page.wait_for_function("document.querySelector('#console-phase-label')?.textContent.trim() === '待机'")
    audio_surfaces = await page.locator(".stage-audio-lane, .stage-audio-lane > .caption, .stage-signal, .stage-signal figcaption, #stage-audio").evaluate_all("""nodes => nodes.map(node => {
      const css = getComputedStyle(node);
      return {component:node.id || node.className || node.tagName, background:css.backgroundColor,
              backgroundImage:css.backgroundImage, borderWidth:css.borderWidth};
    })""")
    assert all(item["background"] == "rgba(0, 0, 0, 0)" and item["backgroundImage"] == "none" and
               item["borderWidth"] == "0px" for item in audio_surfaces), audio_surfaces
    controls = {}
    for selector in ("#console-phase", "#quick-local-status", "#history-toggle"):
        controls[selector] = await check_control(page, selector)
    hud = await page.wait_for_function("""() => {
        const s = window.fuiSpectrum();
        return s.source === '待机' && s.signal === 'idle' && s.visible && s.canvasHits === 9 &&
               s.pixels === 0 && s.modeHits === 9 && s.sourceHits === 9 && s;
    }""")
    spectrum = await hud.json_value()
    await hud.dispose()
    left, top, right, bottom = spectrum["chart"]
    assert 0 <= left < right <= spectrum["width"] + 1 and 0 <= top < bottom <= spectrum["height"] + 1, spectrum
    assert (top + bottom) / 2 > spectrum["height"] / 2 and spectrum["mode"] == "实时频谱", spectrum
    wave = page.locator(".standby-wave")
    assert await wave.is_visible()
    assert await wave.evaluate("el => el.getAnimations({subtree:true}).length === 0")
    return {"controls": controls, "spectrum": spectrum, "audio_surfaces": audio_surfaces}


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("fui-console-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    result = folder / "result.json"
    print(result, flush=True)
    report = {"passed": False, "views": {}, "model_connections": 0, "blocked_requests": [],
              "page_errors": [], "screenshots": [], "device_status_reads": []}
    catalog = folder / "空媒体索引.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0")
    server = isolated(folder / "app")
    release_exterior = asyncio.Event()

    async def guard_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def guard_page(route):
        url = urlsplit(route.request.url)
        local = url.hostname == "127.0.0.1" and url.port == server.port
        if local and url.path == "/api/device-status":
            report["device_status_reads"].append(route.request.method)
        voice = url.path.startswith(("/api/speak", "/api/prepare-speech", "/api/transcri"))
        if url.scheme in {"http", "https"} and (not local or voice):
            report["blocked_requests"].append([url.hostname, url.path])
            await route.abort()
        else:
            if url.path == "/conversation-assets/moss-exterior.glb":
                await release_exterior.wait()
            await route.continue_()

    async def capture(page, name):
        await page.screenshot(path=folder / name, animations="disabled")
        report["screenshots"].append(name)

    async def check_view(page, label):
        data = report["views"][label] = {}
        drawer = page.locator("#dialogue")
        toggle = page.locator("#history-toggle")
        shortcut = page.locator("#quick-local-status")
        panel = page.get_by_test_id("local-status-panel")
        assert await toggle.get_attribute("aria-expanded") == "false" and not await drawer.is_visible()
        areas = await page.evaluate("""() => {
          const box = sel => document.querySelector(sel).getBoundingClientRect().toJSON();
          return {viewport:box('.model-viewport'), rail:box('.camera-rail'), readings:box('.telemetry'),
            reticle:box('.optic-target'), title:box('#aperture-title'), width:innerWidth, height:innerHeight};
        }""")
        assert areas["viewport"]["height"] > areas["height"] * .6 and areas["title"]["width"] >= 100, areas
        if areas["width"] >= 1101:
            assert areas["rail"]["right"] + 8 <= areas["viewport"]["left"] and areas["readings"]["left"] >= areas["viewport"]["right"] + 8, areas
        else:
            assert areas["rail"]["bottom"] <= areas["viewport"]["top"], areas
        data["composition"] = areas
        data["home"] = await confirm_idle(page)
        inline_before = await server.state()
        data["device_strip"] = await check_device_strip(page)
        inline_after = await server.state()
        assert inline_after["chat_id"] == inline_before["chat_id"] and inline_after["turns"] == inline_before["turns"]
        data["device_strip_no_dialogue_or_popup"] = True
        caption = page.locator("#caption")
        assert not await caption.is_visible(), f"{label} 主控台显示了历史字幕"
        await capture(page, label + "-主控台.png")

        for opened in (False, True):
            if opened:
                await toggle.click()
                await drawer.wait_for()
                await page.wait_for_function("document.querySelector('#history-toggle').getAttribute('aria-expanded') === 'true'")
                await check_control(page, "#close-chat")
                await page.get_by_test_id("composer").fill("测试输入区")
                assert await page.get_by_test_id("send").is_enabled()
                await page.get_by_test_id("composer").fill("")
                data["drawer"] = await confirm_idle(page)
                inline_before = await server.state()
                data["device_strip_with_drawer"] = await check_device_strip(page)
                assert (await server.state())["turns"] == inline_before["turns"]
                await capture(page, label + "-文字对话.png")
            before = await server.state()
            await shortcut.click()
            await page.locator('#local-status-panel[data-phase="complete"]').wait_for()
            after = await server.state()
            assert len(after["turns"]) == len(before["turns"]) + 1 and after["chat_id"] == before["chat_id"]
            turn = after["turns"][-1]
            assert turn["user"] == QUESTION and turn["source"] == "context" and turn["status"] == "replied", turn
            assert turn["cloud_requests"]["total"] == report["model_connections"] == 0, turn
            await page.locator(f'[data-turn-id="{turn["id"]}"][data-status="replied"]').wait_for(state="attached")
            readings = await inspect_panel(page, turn)
            assert await toggle.get_attribute("aria-expanded") == str(opened).lower()
            await check_control(page, "#local-status-panel button[type=submit]")
            if not opened:
                await capture(page, label + "-本机状态.png")
            await page.keyboard.press("Escape")
            await panel.wait_for(state="hidden")
            await page.wait_for_function("document.activeElement === document.querySelector('#quick-local-status')")
            assert await drawer.is_visible() == opened and await toggle.get_attribute("aria-expanded") == str(opened).lower()
            assert not await caption.is_visible(), f"{label} 关闭本机状态后显示了字幕"
            data["quick_from_" + ("open" if opened else "closed")] = {"turn_id": turn["id"], "items": readings}
        await page.locator("#close-chat").click()
        await drawer.wait_for(state="hidden")
        assert await toggle.get_attribute("aria-expanded") == "false"
        assert not await caption.is_visible(), f"{label} 收起对话后显示了本机状态的历史字幕"
        data["horizontal_overflow"] = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
        assert data["horizontal_overflow"] <= 1, data["horizontal_overflow"]

    async def check_caption_on_reload(page):
        # 在纯主控台上等待新回复，可区分刷新前可见的新字幕和刷新后载入的历史回复。
        status, state = await server.post("/api/message", {"text": "在吗"})
        assert status == 200, (status, state)
        turn_id = state["turns"][-1]["id"]
        reply = page.locator(f'[data-turn-id="{turn_id}"][data-status="replied"]')
        await reply.wait_for(state="attached")
        caption = page.locator("#caption")
        await caption.wait_for()
        assert await caption.inner_text() == "我在。"

        await page.reload(wait_until="domcontentloaded")
        await reply.wait_for(state="attached")
        await page.locator('.connection.online').wait_for(state="attached")
        assert not await page.locator("#dialogue").is_visible()
        assert not await caption.is_visible(), "刷新后显示了已完成回复的字幕"
        report["views"]["窄屏375"]["caption_reload"] = await confirm_idle(page)
        await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=115000)
        await capture(page, "窄屏375-刷新回到待机.png")

    try:
        async with await asyncio.start_server(guard_model, "127.0.0.1", 0) as guard:
            server.model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = server.model_url
            await server.start()
            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                try:
                    await confirm_muted_edge(browser)
                    context = await browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce",
                                                        service_workers="block")
                    await context.route("**/*", guard_page)
                    await context.add_init_script("""
                      sessionStorage.setItem('moss.agent.opened','1');
                      localStorage.setItem('moss.voice.enabled','off');
                      window.cameraCommands = [];
                      addEventListener('DOMContentLoaded', () => {
                        const canvas = document.getElementById('moss-model');
                        canvas.addEventListener('moss-view-preset', event => {
                          cameraCommands.push([event.detail, canvas.dataset.ready === 'true']);
                        });
                      });
                    """ + "window.fuiSpectrum = " + HOME_SNAPSHOT)
                    page = await context.new_page()
                    page.set_default_timeout(15000)
                    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                    await page.goto(server.url + "/", wait_until="domcontentloaded")
                    await page.locator(".connection.online").wait_for(state="attached")
                    await page.locator('label[data-code="FRONT"]').click()
                    await page.locator("#reset-view").click()
                    assert await page.locator('.camera-options input[value="interview"]').is_checked()
                    await page.locator('label[data-code="ARRAY"]').click()
                    await page.locator('label[data-code="OPTICS"]').click()
                    await page.locator("#history-toggle").click()
                    await page.get_by_test_id("composer").fill("模型载入期间可以打字")
                    assert await page.get_by_test_id("send").is_enabled()
                    await page.get_by_test_id("composer").fill("")
                    await page.locator("#close-chat").click()
                    assert await page.evaluate("cameraCommands.length === 0")
                    assert await page.locator("#moss-model").get_attribute("data-ready") is None
                    release_exterior.set()
                    canvas = page.locator('#moss-model[data-ready="true"]')
                    await canvas.wait_for(timeout=115000)
                    worker = next(worker for worker in page.workers if "moss-render" in worker.url)
                    optical = await wait_scene(worker, lambda value: value["ready"] and value["activePreset"] == "optics")
                    assert await page.evaluate("cameraCommands") == [["optics", True]]
                    await page.locator('label[data-code="MAIN"]').click()
                    scene = await wait_scene(worker, lambda value: value["activePreset"] == "interview" and value["railGrip"])
                    report["camera_startup"] = {"queued": optical["activePreset"], "after_ready": scene["activePreset"],
                                                "commands": await page.evaluate("cameraCommands")}
                    release = scene["modelRelease"]
                    assert release["visibleMeshes"] > 0 and release["visibleTriangles"] > 0, release
                    assert await canvas.get_attribute("data-model-release") == release["sourceFile"]
                    assets = await worker.evaluate("performance.getEntriesByType('resource').map(e=>[e.name,e.encodedBodySize])")
                    assert any(urlsplit(url).path.endswith("/moss-exterior.glb") and size > 0 for url, size in assets), assets
                    report["model"] = release
                    report["worker_renderer"] = "software" if scene["software"] else "hardware"
                    report["browser"] = browser.version
                    assert await page.evaluate("localStorage.getItem('moss.voice.enabled')") == "off"
                    await check_view(page, "桌面1440")
                    for width, height, label in ((768, 1024, "平板768"), (375, 812, "窄屏375")):
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.evaluate("new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)))")
                        await check_view(page, label)
                    await check_caption_on_reload(page)
                    assert report["device_status_reads"] and set(report["device_status_reads"]) == {"GET"}, report
                    assert not report["page_errors"] and not report["blocked_requests"] and not report["model_connections"], report
                    report["passed"] = True
                finally:
                    release_exterior.set()
                    await browser.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        try:
            await server.stop()
        finally:
            result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(verify())
