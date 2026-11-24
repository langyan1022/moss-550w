"""Check deferred Earth startup in isolated Edge with the real Earth renderer."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_context_reply import free_port


EARLY_POINT = {"latitude": 31.2, "longitude": 121.5, "title": "早到的天气定位", "kind": "weather"}
MODEL_STUB = """
export function mountMossView(canvas) {
  window.finishModel = outcome => {
    canvas.dataset[outcome] = outcome === 'ready' ? 'true' : 'isolated model fault';
  };
  return () => { delete window.finishModel; };
}
if (new URL(import.meta.url).searchParams.get('lifecycle') !== '1')
  mountMossView(document.getElementById('moss-model'));
"""
INIT = """
sessionStorage.setItem('moss.agent.opened','1');
localStorage.setItem('moss.voice.enabled','off');
window.earthLocateCalls = 0;
const originalQuery = navigator.permissions.query.bind(navigator.permissions);
const prompt = new EventTarget();
prompt.state = 'prompt';
navigator.permissions.query = item => item.name === 'geolocation' ? Promise.resolve(prompt) : originalQuery(item);
navigator.geolocation.getCurrentPosition = done => {
  window.earthLocateCalls++;
  queueMicrotask(() => done({coords:{latitude:38.12,longitude:115.21,accuracy:50},timestamp:Date.now()}));
};
"""


async def exercise(browser, server, folder, case, candidate, after_model, scene_candidate, scene_lifecycle):
    narrow = case == "narrow_model_error"
    context = await browser.new_context(
        viewport={"width": 375, "height": 812} if narrow else {"width": 1440, "height": 900},
        reduced_motion="reduce", service_workers="block",
    )
    page = await context.new_page()
    errors, outside, requests = [], [], []
    allow_tiles = asyncio.Event()
    page.on("pageerror", lambda exc: errors.append(getattr(exc, "stack", None) or str(exc)))

    async def route_request(route):
        url = urlsplit(route.request.url)
        if url.scheme in {"http", "https"} and not route.request.url.startswith(server.url + "/"):
            outside.append(route.request.url)
            await route.abort()
        elif url.path.endswith("/moss-view.js"):
            await route.fulfill(content_type="application/javascript", body=MODEL_STUB)
        elif candidate is not None and url.path.endswith("/earth-view.js"):
            await route.fulfill(content_type="application/javascript; charset=utf-8", body=candidate)
        elif scene_candidate and url.path in {"/static/agent-terminal.js", "/static/assistant-gestures.js",
                                             "/conversation-assets/earth-view.js", "/conversation-assets/earth-renderer.js"}:
            await route.fulfill(content_type="application/javascript; charset=utf-8", path=scene_candidate / Path(url.path).name)
        else:
            if "/encom-globe/" in url.path:
                requests.append(url.path.rsplit("/", 1)[-1])
                if url.path.endswith("/tiles.json"):
                    await allow_tiles.wait()
            await route.continue_()

    async def stays_deferred(phase):
        # Allow callbacks, layout observers and eager resource requests to run.
        await page.wait_for_timeout(350)
        assert not requests, (case, phase, requests)
        assert await page.locator(".earth-sphere[data-renderer]").count() == 0, (case, phase)

    try:
        await context.route("**/*", route_request)
        await page.add_init_script(INIT)
        initially_covered = case == "covered" and not after_model
        await page.goto(server.url + ("/?chat=1" if initially_covered else "/"), wait_until="domcontentloaded")
        await page.wait_for_function("typeof window.finishModel === 'function'")
        await page.locator("#earth-view:not([hidden])").wait_for(state="attached")
        globe = page.locator("#earth-view")
        coords = globe.locator("small")
        label = globe.locator("figcaption span").first
        assert await globe.evaluate("node=>node.checkVisibility({visibilityProperty:true})") is (not (narrow or initially_covered))

        await page.evaluate("""point => window.dispatchEvent(new CustomEvent('moss-geo', {detail:point}))""", EARLY_POINT)
        await expect(label).to_have_text(EARLY_POINT["title"])
        if after_model or narrow or initially_covered:
            await stays_deferred("before_initialization")
        if initially_covered:
            await page.locator("#close-chat").click()
            await page.locator("#dialogue[data-open]").wait_for(state="detached")
        elif case == "covered":
            await page.locator("#history-toggle").click()
            drawer = page.locator("#dialogue")
            await drawer.evaluate("""node=>{
                node.style.cssText='position:absolute!important;inset:0!important;width:100vw!important;'
                    +'height:100vh!important;max-width:none!important;max-height:none!important';
            }""")
            await page.locator("#earth-view[data-covered]").wait_for(state="attached")
            await page.evaluate("finishModel('ready')")
            await stays_deferred("drawer_covers_globe")
            await drawer.evaluate("node=>{node.removeAttribute('style');node.querySelector('#close-chat').click()}")
            await page.locator("#earth-view:not([data-covered])").wait_for(state="attached")
        elif narrow:
            await page.evaluate("finishModel('error')")
            await stays_deferred("narrow_hidden_with_model_error")
            await page.set_viewport_size({"width": 1440, "height": 900})
        elif after_model:
            await page.evaluate("finishModel('ready')")

        allow_tiles.set()
        surface = page.locator(".earth-sphere[data-renderer][data-location-visible]")
        await surface.wait_for(timeout=20000)
        await expect(label).to_have_text(EARLY_POINT["title"])
        await expect(coords).to_contain_text("31.2°N")
        await expect(coords).to_contain_text("121.5°E")
        await globe.screenshot(path=folder / (case + ".png"))
        renderer = await surface.get_attribute("data-renderer")
        assert renderer in {"encom-webgl", "encom-canvas2d"}
        assert sorted(requests) == ["encom-globe.min.js", "tiles.json"], (case, requests)
        visible_before_model = not after_model and not narrow
        if visible_before_model:
            assert await page.locator('#moss-model[data-ready]').count() == 0
            await page.evaluate("finishModel('ready')")

        if case == "visible":
            previous = await coords.text_content()
            rotation = globe.locator("#earth-motion")
            await rotation.click()
            await expect(rotation).to_have_attribute("aria-pressed", "false")
            await expect(coords).not_to_have_text(previous)
            await rotation.click()
            await expect(rotation).to_have_attribute("aria-pressed", "true")
            assert await page.evaluate("earthLocateCalls") == 0
            await globe.locator("#earth-locate").click()
            await expect(label).to_have_text("浏览器定位")
            await expect(coords).to_contain_text("38.1°N")
            await expect(coords).to_contain_text("115.2°E")
            assert await page.evaluate("earthLocateCalls") == 1
            if scene_lifecycle:
                original = await page.evaluate("""() => {
                  window.previousEarthCanvas = document.querySelector('.earth-overlay');
                  window.previousEarthTerrain = document.querySelector('.earth-terrain');
                  return performance.timeOrigin;
                }""")

                async def switch_scene():
                    await page.locator(".preferences summary").click()
                    await page.locator("#scene-mode-link").click()

                await switch_scene()
                await expect(globe).to_be_hidden()
                assert not await surface.count()
                assert await page.evaluate("previousEarthCanvas.width === 0 && previousEarthCanvas.height === 0 && !previousEarthTerrain?.isConnected")
                await switch_scene()
                await surface.wait_for(timeout=15000)
                await expect(label).to_have_text("浏览器定位")
                await expect(coords).to_contain_text("115.2°E")
                assert await page.evaluate("performance.timeOrigin") == original
                assert await page.evaluate("previousEarthCanvas.width > 0 && previousEarthCanvas.height > 0")
                assert sorted(requests) == ["encom-globe.min.js", "tiles.json", "tiles.json"], requests
        assert not errors and not outside, (errors, outside)
        return {"renderer": renderer, "requests": requests, "early_location_drawn": True,
                "model": "error" if narrow else "ready", "buttons_verified": case == "visible",
                "visible_before_model":visible_before_model, "released_and_remounted":case == "visible" and scene_lifecycle}
    finally:
        allow_tiles.set()
        await context.close()


async def run(candidate_path, after_model, scene_candidate, scene_lifecycle):
    folder = ROOT / "runs" / datetime.now().strftime("earth-deferred-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    candidate = candidate_path.read_bytes() if candidate_path is not None else None
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
           "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0"}
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:1/v1")
    report = {"passed": False, "candidate_earth": str(candidate_path) if candidate_path else None, "after_model": after_model,
              "uses_fixture_model_readiness": True, "scene_lifecycle":scene_lifecycle, "cases": {}}
    try:
        with patch.dict(os.environ, env):
            await server.start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                for case in ("visible", "covered", "narrow_model_error"):
                    report["cases"][case] = await exercise(browser, server, folder, case, candidate, after_model,
                                                          scene_candidate, scene_lifecycle)
            finally:
                await browser.close()
        assert not (await server.state())["turns"]
        report["passed"] = True
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        await server.stop()
        path = folder / "result.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(path, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-earth", type=Path, help="在隔离浏览器加载候选地球入口")
    parser.add_argument("--after-model", action="store_true", help="复验等待机身就绪的实验版本")
    parser.add_argument("--scene-candidate", type=Path, help="加载候选场景和地球入口")
    parser.add_argument("--scene-lifecycle", action="store_true", help="验证地球同页卸载和重新出现")
    args = parser.parse_args()
    asyncio.run(run(args.candidate_earth, args.after_model, args.scene_candidate, args.scene_lifecycle))
