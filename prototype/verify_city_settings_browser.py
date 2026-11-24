"""Verify city settings, ambient weather, and local weather chat from isolated Edge."""

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from browser_lab import ROOT, confirm_muted_edge
from city_catalog import selected_city
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port
from verify_weather_browser_location import sample_forecast
from weather_forecast import format_weather


STAGED = {
    "/": ("conversation.html", "text/html"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/location-weather.js": ("location-weather.js", "text/javascript"),
    "/static/agent-terminal.js": ("agent-terminal.js", "text/javascript"),
    "/conversation-assets/earth-view.js": ("earth-view.js", "text/javascript"),
}
INITIAL = "cn:110105"
FORM_CITY = "cn:310115"
MAP_CITY = "world:tokyo"
FIX = {"latitude":34.766293, "longitude":113.628463}
ROUNDED = {"latitude":34.77, "longitude":113.63}
NEARBY = "我当前位置的天气怎么样？"
NAMED = "北京市东城区现在天气怎么样？"
RESTORED = "这里明天的天气怎么样？"
PROBE = """
localStorage.setItem('moss.voice.enabled', 'off');
sessionStorage.setItem('moss.agent.opened', '1');
if (!sessionStorage.getItem('city-verifier-seeded')) {
  localStorage.setItem('moss.weather.city', JSON.stringify({id:'cn:110105'}));
  sessionStorage.setItem('city-verifier-seeded', '1');
}
window.cityProbe = {fixes:0, turns:[], spareAbort:false, lateStatus:null, lateAborted:false};
window.addEventListener('moss-agent-state', ({detail}) => {
  const turn = detail.turns.at(-1);
  if (turn) cityProbe.turns.push(turn);
});
const originalPosition = navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);
navigator.geolocation.getCurrentPosition = (...args) => {
  cityProbe.fixes++;
  return originalPosition(...args);
};
const originalCityFetch = window.fetch;
window.fetch = async function(input, options) {
  const endpoint = new URL(input instanceof Request ? input.url : input, location.href).pathname;
  if (endpoint !== '/api/weather/current' || !cityProbe.spareAbort)
    return originalCityFetch.apply(this, arguments);
  cityProbe.spareAbort = false;
  const {signal, ...request} = options;
  signal?.addEventListener('abort', () => { cityProbe.lateAborted = true; }, {once:true});
  const response = await originalCityFetch.call(this, input, request);
  cityProbe.lateStatus = response.status;
  return response;
};
"""


def ambient_reading(body, *, late=False):
    key = body.get("city_id")
    coordinates = selected_city(key) if key else body
    assert coordinates and set(body) == ({"city_id"} if key else set(ROUNDED)), body
    sample = {axis:round(coordinates[axis], 2) for axis in ROUNDED}
    return {**sample, **({"city_id":key} if key else {}),
            "sampled_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "time_zone":"Asia/Tokyo" if key == MAP_CITY else "Asia/Shanghai",
            "temperature":47 if late else {INITIAL:12.5, FORM_CITY:16.5, MAP_CITY:20.5}.get(key, 9.5),
            "apparent_temperature":18, "humidity":62, "wind_speed":12,
            "condition":"雷阵雨" if late else "晴", "code":95 if late else 0}


async def show_settings(page):
    preferences = page.locator("details.preferences")
    if not await preferences.evaluate("node => node.open"):
        await preferences.locator("summary").click()
    await preferences.locator("#city-configure").click()
    dialog = page.locator("#city-settings")
    await expect(dialog).to_be_visible()
    await expect(dialog.locator("#city-location-fields")).to_be_enabled()
    return dialog


async def settings_geometry(page, dialog, folder, label):
    box = await dialog.evaluate("""dialog => {
      const r = dialog.getBoundingClientRect(), map = dialog.querySelector('#city-world-map');
      const mapBox = map.getBoundingClientRect();
      return {width:innerWidth, height:innerHeight, pageWidth:document.documentElement.scrollWidth,
        dialog:r.toJSON(), dialogScrollWidth:dialog.scrollWidth, dialogClient:dialog.clientWidth,
        map:mapBox.toJSON(), markerCount:map.querySelectorAll('[data-city-id]').length};
    }""")
    rect = box["dialog"]
    assert 0 <= rect["left"] < rect["right"] <= box["width"] + 1, box
    assert 0 <= rect["top"] < rect["bottom"] <= box["height"] + 1, box
    assert box["pageWidth"] <= box["width"] + 1 and box["dialogScrollWidth"] <= box["dialogClient"] + 1, box
    assert box["markerCount"] >= 5 and box["map"]["width"] > 200, box
    image = folder / f"city-settings-{label}.png"
    await page.screenshot(path=image, animations="disabled")
    return {"geometry":box, "screenshot":str(image)}


async def ask(page, text, posts):
    before = len(posts)
    composer = page.get_by_test_id("composer")
    await composer.fill(text)
    await page.get_by_test_id("send").click()
    handle = await page.wait_for_function("""text => cityProbe.turns.find(turn =>
        turn.user === text && ['replied','error'].includes(turn.status))""", arg=text)
    turn = await handle.json_value()
    await handle.dispose()
    assert len(posts) == before + 1 and posts[-1]["text"] == text, posts
    assert turn["status"] == "replied" and turn["cloud_requests"]["total"] == 0, turn
    assert turn.get("weather"), turn
    dialog = page.get_by_test_id("weather-panel")
    await expect(dialog).to_be_visible()
    await expect(dialog.get_by_role("heading", level=2)).to_contain_text(turn["weather"]["city"])
    await page.keyboard.press("Escape")
    await expect(dialog).to_be_hidden()
    return turn["weather"], posts[-1]


async def expect_reading(page, place, temperature, *, configured=True):
    tile = page.locator("#location-weather")
    await expect(tile).to_have_attribute("data-phase", "ready")
    await expect(tile.locator("#location-weather-place")).to_have_text(place)
    await expect(tile.locator("#location-weather-value")).to_contain_text(f"{temperature} °C")
    assert (await tile.get_attribute("data-city-configured") is not None) == configured


async def exercise(browser, base, folder, report, width, assets):
    size = {"width":width, "height":900 if width == 1440 else 812}
    context = await browser.new_context(viewport=size, reduced_motion="reduce", service_workers="block",
                                        geolocation={**FIX, "accuracy":24}, has_touch=width == 375)
    await context.grant_permissions(["geolocation"], origin=base)
    case = {"viewport":size, "weather_posts":[], "chat_posts":[], "catalog_reads":[], "checks":{}}
    report["cases"][str(width)] = case
    ambient, posts, catalogue_reads = case["weather_posts"], case["chat_posts"], case["catalog_reads"]
    seen = set()
    hold_next = False
    late_started, release_late = asyncio.Event(), asyncio.Event()

    async def route_request(route):
        nonlocal hold_next
        request = route.request
        url = urlsplit(request.url)
        if url.scheme in {"http", "https"} and not request.url.startswith(base + "/"):
            report["outside"].append([url.hostname, url.path])
            await route.abort()
        elif url.path in assets:
            seen.add(url.path)
            data, mime = assets[url.path]
            await route.fulfill(body=data, content_type=f"{mime}; charset=utf-8")
        elif url.path == "/api/cities":
            catalogue_reads.append(parse_qs(url.query).get("id", [None])[0])
            await route.continue_()
        elif url.path == "/api/weather/current":
            assert request.method == "POST", request.method
            body = request.post_data_json
            ambient.append(body)
            late, hold_next = hold_next, False
            if late:
                late_started.set()
                await asyncio.wait_for(release_late.wait(), 15)
            await route.fulfill(json=ambient_reading(body, late=late))
        elif url.path == "/api/message" and request.method == "POST":
            posts.append(request.post_data_json)
            await route.continue_()
        elif url.path.startswith("/api/") and request.method not in {"GET", "HEAD"}:
            report["other_api_writes"].append([request.method, url.path])
            await route.abort()
        else:
            await route.continue_()

    try:
        await context.route("**/*", route_request)
        await context.add_init_script(PROBE)
        page = await context.new_page()
        page.set_default_timeout(9000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(state="attached")
        await page.bring_to_front()
        assert await page.evaluate("navigator.permissions.query({name:'geolocation'}).then(p=>p.state)") == "granted"
        await expect_reading(page, "朝阳区", 12.5)
        assert ambient == [{"city_id":INITIAL}] and catalogue_reads == [INITIAL], case
        assert await page.evaluate("cityProbe.fixes") == 0

        dialog = await show_settings(page)
        await expect(dialog.locator("#city-current-source")).to_have_text("北京市朝阳区 · 手动设定")
        await expect(dialog.locator("#city-province option")).to_have_count(35)
        await dialog.locator("#city-province").select_option("上海市")
        await dialog.locator("#city-prefecture").select_option("上海市")
        await dialog.locator("#city-district").select_option(FORM_CITY)
        await expect(dialog.locator("#city-selection")).to_have_text("待设定 上海市浦东新区")
        case["checks"]["domestic_form"] = await settings_geometry(page, dialog, folder, f"form-{width}")
        await dialog.locator("#city-save").click()
        await expect(dialog).to_be_hidden()
        await expect_reading(page, "浦东新区", 16.5)
        assert ambient[-1] == {"city_id":FORM_CITY}, ambient
        assert await page.evaluate("cityProbe.fixes") == 0

        await page.reload(wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(state="attached")
        await expect_reading(page, "浦东新区", 16.5)
        assert ambient[-1] == {"city_id":FORM_CITY} and catalogue_reads[-1] == FORM_CITY, case
        assert await page.evaluate("cityProbe.fixes") == 0
        case["checks"]["reload_without_device_fix"] = True

        if width == 1440:
            local, payload = await ask(page, NEARBY, posts)
            assert local["city"] == "上海市浦东新区" and local["location_kind"] == "configured", local
            assert payload["preferred_city_id"] == FORM_CITY and "browser_location" not in payload, payload
            named, payload = await ask(page, NAMED, posts)
            assert named["city"] == "北京市东城区" and named["location_kind"] == "district", named
            assert "preferred_city_id" not in payload and "browser_location" not in payload, payload
            assert await page.evaluate("cityProbe.fixes") == 0
            case["checks"]["chat_local_vs_named"] = [local["location_kind"], named["location_kind"]]

            hold_next = True
            await page.evaluate("cityProbe.spareAbort = true")
            await page.locator("#location-weather-control").click()
            await asyncio.wait_for(late_started.wait(), 6)

        dialog = await show_settings(page)
        await dialog.locator('[data-city-map="world"]').click()
        marker = dialog.locator(f'[data-city-id="{MAP_CITY}"]')
        await expect(marker).to_have_count(1)
        dot = await marker.evaluate("""point => {
          const center = new DOMPoint(point.cx.baseVal.value, point.cy.baseVal.value).matrixTransform(point.getScreenCTM());
          return {x:center.x, y:center.y};
        }""")
        if width == 375:
            await page.touchscreen.tap(dot["x"], dot["y"])
        else:
            await page.mouse.click(dot["x"], dot["y"])
        await expect(dialog.locator("#city-selection")).to_have_text("待设定 东京")
        await expect(dialog.locator("#city-international")).to_have_value(MAP_CITY)
        case["checks"]["world_map"] = await settings_geometry(page, dialog, folder, f"map-{width}")
        await dialog.locator("#city-save").click()
        await expect(dialog).to_be_hidden()
        await expect_reading(page, "东京", 20.5)
        assert ambient[-1] == {"city_id":MAP_CITY}, ambient
        assert await page.evaluate("cityProbe.fixes") == 0

        if width == 1440:
            release_late.set()
            await page.wait_for_function("cityProbe.lateStatus === 200")
            assert await page.evaluate("cityProbe.lateAborted"), "old ambient fetch was not aborted"
            await page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            await expect_reading(page, "东京", 20.5)
            case["checks"]["stale_city_response_discarded"] = True

        dialog = await show_settings(page)
        await dialog.locator("#city-clear").click()
        await expect(dialog).to_be_hidden()
        await expect_reading(page, "当前位置", 9.5, configured=False)
        await expect(page.locator("#location-weather-coordinate")).to_have_text("34.77°N 113.63°E")
        assert ambient[-1] == ROUNDED and await page.evaluate("cityProbe.fixes") == 1, case
        assert await page.evaluate("localStorage.getItem('moss.weather.city')") is None
        if width == 1440:
            restored, payload = await ask(page, RESTORED, posts)
            assert restored["location_kind"] == "browser" and "preferred_city_id" not in payload, (restored, payload)
            assert all(abs(payload["browser_location"][axis] - FIX[axis]) < .001 for axis in FIX), payload
        case["checks"]["browser_fix_restored"] = True
        assert seen == set(assets) - {"/conversation-assets/earth-view.js"}, seen
        case["asset_hits"] = sorted(seen)
    finally:
        release_late.set()
        await context.close()


async def verify(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("city-settings-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    assets = {url:((candidate / name).read_bytes(), mime) for url, (name, mime) in STAGED.items()
              if url != "/conversation-assets/earth-view.js" or (candidate / name).is_file()} if candidate else {}
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed":False, "cases":{}, "outside":[], "other_api_writes":[], "page_errors":[],
              "model_connections":0, "forecast_provider_points":[],
              "candidate_asset_sha256":{url:sha256(data).hexdigest() for url, (data, _) in assets.items()}}
    environment = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "MOSS_MEDIA_DB":str(catalog), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0", "MOSS_SPEECH_MIC_WARM":"0"}

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def forecast(city, period, place, *, part_of_day=None, question=""):
        assert place, city
        report["forecast_provider_points"].append({"place":place, "period":period})
        return format_weather(sample_forecast(), place, period, part_of_day, question)

    try:
        async with await asyncio.start_server(guard, "127.0.0.1", 0) as sink:
            model = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            profiles = {key:Profile(key, label, model, model_id, SecretStr(""))
                        for key, (label, _, _, model_id) in SPECS.items()}
            with patch.dict(os.environ, environment), patch("core.profiles", return_value=profiles), \
                    patch("assistant_brain.fetch_weather", forecast):
                port, data = free_port(), folder / "data"
                base = f"http://127.0.0.1:{port}"
                moss = Moss(data, model, "isolated-model-guard")
                runner = web.AppRunner(application(moss, lambda:"standby", data, port, "fixture", asyncio.Event()),
                                       access_log=None, shutdown_timeout=1)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", port).start()
                    async with async_playwright() as playwright:
                        edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                        browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                            args=["--mute-audio", "--disable-gpu", "--disable-renderer-backgrounding"])
                        try:
                            await confirm_muted_edge(browser)
                            report["browser"] = browser.version
                            for width in (1440, 375):
                                await exercise(browser, base, folder, report, width, assets)
                        finally:
                            await browser.close()
                    assert [row["place"]["kind"] for row in report["forecast_provider_points"]] == [
                        "configured", "district", "browser"], report
                    assert not report["model_connections"] and len(moss.state()["turns"]) == 3, report
                    assert not any(report[key] for key in ("outside", "other_api_writes", "page_errors")), report
                    report["passed"] = True
                finally:
                    await runner.cleanup()
                    await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, help="Overlay the staged city settings assets in Edge.")
    asyncio.run(verify(parser.parse_args().candidate))
