"""Exercise the ambient weather view with isolated Edge and loopback fixtures."""

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port


POSITION = {"latitude":31.218763, "longitude":121.492174}
COARSE = {"latitude":31.22, "longitude":121.49}
MOVED_POSITION = {"latitude":31.238919, "longitude":121.546721}
MOVED_COARSE = {"latitude":31.24, "longitude":121.55}
PATH = "/api/weather/current"
STATIC_ASSETS = {
    "/": ("conversation.html", "text/html"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/location-weather.js": ("location-weather.js", "text/javascript"),
    "/conversation-assets/inspection-console.css": ("inspection-console.css", "text/css"),
}
WEATHER = "#location-weather"
CONTROL = "#location-weather-control"
COORDINATE = "#location-weather-coordinate"
PLACE = "#location-weather-place"
DETAILS_TOGGLE = "#location-weather-details-toggle"
DETAILS = "#location-weather-details"
CONDITION = "雨夹雪"
LATE_CONDITION = "雷阵雨"
TEMPERATURE = re.compile(r"[−-]\s*3(?:[.,]0+)?(?:\s*(?:°|℃)|(?=\s|$))")
RECOVERED_TEMPERATURE = re.compile(r"\b6(?:[.,]0+)?\s*(?:°|℃)")
PROBE = """
localStorage.setItem('moss.voice.enabled', 'off');
sessionStorage.setItem('moss.agent.opened', '1');
window.weatherProbe = {locates:[], failNextLocation:false, surviveNextAbort:false, lateAborted:false, lateStatus:null};
const originalWeatherLocate = navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);
navigator.geolocation.getCurrentPosition = function(...args) {
  weatherProbe.locates.push({active:navigator.userActivation.isActive, at:performance.now()});
  if (weatherProbe.failNextLocation) {
    weatherProbe.failNextLocation = false;
    queueMicrotask(() => args[1]({code:2}));
    return;
  }
  return originalWeatherLocate(...args);
};
const originalAmbientFetch = window.fetch;
window.fetch = async function(input, options) {
  const path = new URL(input instanceof Request ? input.url : input, location.href).pathname;
  if (path !== '/api/weather/current' || !weatherProbe.surviveNextAbort)
    return originalAmbientFetch.apply(this, arguments);
  weatherProbe.surviveNextAbort = false;
  const {signal, ...request} = options;
  signal?.addEventListener('abort', () => { weatherProbe.lateAborted = true; }, {once:true});
  const response = await originalAmbientFetch.call(this, input, request);
  weatherProbe.lateStatus = response.status;
  return response;
};
"""
PROMPT_FIXTURE = """
const originalPermissionQuery = navigator.permissions.query.bind(navigator.permissions);
let geoState = 'prompt', answerPosition;
const geoPermission = new EventTarget();
Object.defineProperty(geoPermission, 'state', {get:()=>geoState});
geoPermission.onchange = null;
navigator.permissions.query = request => request.name === 'geolocation'
  ? Promise.resolve(geoPermission) : originalPermissionQuery(request);
navigator.geolocation.getCurrentPosition = function(success) {
  weatherProbe.locates.push({active:navigator.userActivation.isActive, at:performance.now()});
  answerPosition = success;
};
window.weatherProbe.allow = coords => {
  geoState = 'granted';
  answerPosition({coords:{...coords, accuracy:25}, timestamp:Date.now()});
  const event = new Event('change');
  geoPermission.dispatchEvent(event);
  geoPermission.onchange?.(event);
};
"""
GEOMETRY = """weather => {
  const bounds = node => node?.getBoundingClientRect().toJSON() ?? null;
  const selectorBounds = selector => {
    const node = document.querySelector(selector);
    return node?.checkVisibility({opacityProperty:true, visibilityProperty:true}) ? bounds(node) : null;
  };
  const control = weather.querySelector('#location-weather-control');
  const hit = control.getBoundingClientRect();
  const top = document.elementFromPoint(hit.left + hit.width/2, hit.top + hit.height/2);
  return {width:innerWidth,height:innerHeight,pageWidth:document.documentElement.scrollWidth,
    weather:bounds(weather),control:bounds(control),hit:top === control || control.contains(top),
    model:selectorBounds('.model-viewport'),spectrum:selectorBounds('.stage-signal'),
    drawer:selectorBounds('#dialogue'),device:selectorBounds('#device-strip > dl'),
    deviceState:selectorBounds('#device-strip-state'),
    shortcut:selectorBounds('#quick-local-status'),chatToggle:selectorBounds('#history-toggle')};
}"""


def weather_sample(point, *, late=False):
    return {**point, "condition":LATE_CONDITION if late else CONDITION, "code":95 if late else 68,
            "temperature":47 if late else -3, "apparent_temperature":44 if late else -5,
            "humidity":83, "wind_speed":12.4, "time_zone":"Asia/Shanghai",
            "sampled_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}


def overlap(a, b):
    return (min(a["right"], b["right"]) - max(a["left"], b["left"]) > 1 and
            min(a["bottom"], b["bottom"]) - max(a["top"], b["top"]) > 1)


async def check_geometry(page, folder, label):
    snapshot = await page.locator(WEATHER).evaluate(GEOMETRY)
    box, control = snapshot["weather"], snapshot["control"]
    assert snapshot["pageWidth"] <= snapshot["width"] + 1 and snapshot["hit"], snapshot
    for item in (box, control):
        assert 0 <= item["left"] < item["right"] <= snapshot["width"] + 1, snapshot
        assert 0 <= item["top"] < item["bottom"] < snapshot["height"] / 2, snapshot
    for name in ("model", "spectrum", "drawer", "device", "deviceState", "shortcut", "chatToggle"):
        boundary = snapshot[name]
        assert boundary is None or not overlap(box, boundary), (name, snapshot)
    shot = folder / f"ambient-weather-{label}.png"
    await page.screenshot(path=shot, animations="disabled")
    return {"geometry":snapshot, "screenshot":str(shot)}


async def check_main_position(root, point):
    coordinate = root.locator(COORDINATE)
    await expect(coordinate).to_be_visible()
    text = await coordinate.inner_text()
    assert re.findall(r"\d+\.\d+", text) == [f"{point[key]:.2f}" for key in ("latitude", "longitude")], text
    assert re.search(r"北纬|\bN\b", text) and re.search(r"东经|\bE\b", text), text
    tile, reading = await root.bounding_box(), await coordinate.bounding_box()
    assert tile and reading and reading["x"] >= tile["x"] - 1 and reading["y"] >= tile["y"] - 1, (tile, reading)
    for axis, length in (("x", "width"), ("y", "height")):
        assert reading[axis] + reading[length] <= tile[axis] + tile[length] + 1, (tile, reading)
    return text


async def check_weather_unavailable(root):
    await expect(root).to_have_attribute("data-phase", "error")
    value = root.locator("#location-weather-value")
    for superseded in (TEMPERATURE, RECOVERED_TEMPERATURE, CONDITION, "旧读数"):
        await expect(value).not_to_contain_text(superseded)
    await expect(root.locator("#location-weather-stamp")).to_be_hidden()
    await expect(root.locator(DETAILS_TOGGLE)).to_be_hidden()


async def check_details(page, folder, label, *, stale=False):
    toggle = page.locator(DETAILS_TOGGLE)
    details = page.locator(DETAILS)
    await expect(toggle).to_have_accessible_name("天气详情")
    await expect(details).to_be_hidden()
    await toggle.press("Enter")
    await expect(details).to_be_visible()
    freshness = details.locator("#location-weather-freshness")
    if stale:
        await expect(freshness).to_have_text("旧读数")
        await expect(freshness).to_be_visible()
    else:
        await expect(freshness).to_be_hidden()
    for name in ("定位点", "体感", "湿度", "风速", "数据时间", "数据来源"):
        await expect(details.get_by_text(name, exact=True)).to_be_visible()
    for selector, value in (
            ("#location-weather-point", re.compile(r"31\.22.*121\.49")),
            ("#location-weather-feels-like", re.compile(r"[−-]\s*5(?:[.,]0+)?\s*(?:°|℃)")),
            ("#location-weather-humidity", re.compile(r"83\s*%")),
            ("#location-weather-wind", re.compile(r"12\.4\s*km/h"))):
        reading = details.locator(selector)
        await expect(reading).to_be_visible()
        await expect(reading).to_contain_text(value)
    point = await details.locator("#location-weather-point").inner_text()
    assert re.search(r"北纬|\bN\b", point) and re.search(r"东经|\bE\b", point), point
    assert "31.218763" not in point and "121.492174" not in point, point
    stamp = await page.locator("#location-weather-stamp").inner_text()
    await expect(details.get_by_text(stamp, exact=False).first).to_be_visible()
    await expect(details.get_by_text("Open-Meteo", exact=False).first).to_be_visible()
    rect = await details.bounding_box()
    tile = await page.locator(WEATHER).bounding_box()
    viewport = page.viewport_size
    assert rect and tile and 0 <= rect["x"] < rect["x"] + rect["width"] <= viewport["width"] + 1, rect
    assert tile["y"] < rect["y"] < rect["y"] + rect["height"] <= viewport["height"] + 1, rect
    shot = folder / f"ambient-weather-{label}-details.png"
    await page.screenshot(path=shot, animations="disabled")
    await page.keyboard.press("Escape")
    await expect(details).to_be_hidden()
    return {"point":point, "stamp":stamp, "bounds":rect, "screenshot":str(shot), "escape_closed":True}


async def check_weather(browser, base, folder, report, mode, width, assets):
    height = 900 if width == 1440 else 812
    context = await browser.new_context(viewport={"width":width,"height":height}, reduced_motion="reduce",
                                        geolocation={**POSITION,"accuracy":25}, service_workers="block")
    calls = []
    case = report["cases"][mode] = {"size":[width,height], "weather_posts":calls}
    hold_next = recover_next = False
    fail_next = mode == "granted"
    expected_point = COARSE
    late, release_late = asyncio.Event(), asyncio.Event()
    asset_hits = set()

    async def route_request(route):
        nonlocal hold_next, fail_next, recover_next
        request = route.request
        url = urlsplit(request.url)
        if url.scheme in {"http", "https"} and not request.url.startswith(base + "/"):
            report["outside"].append([url.hostname, url.path])
            await route.abort()
        elif url.path in assets:
            data, mime = assets[url.path]
            asset_hits.add(url.path)
            await route.fulfill(status=200, body=data, content_type=f"{mime}; charset=utf-8")
        elif url.path == PATH:
            calls.append({"method":request.method,"body":request.post_data_json})
            assert request.method == "POST" and request.post_data_json == expected_point, calls[-1]
            if fail_next:
                fail_next = False
                await route.fulfill(status=502, json={"error":"天气接口暂时不可用"})
                return
            held, hold_next = hold_next, False
            if held:
                late.set()
                await asyncio.wait_for(release_late.wait(), 10)
            reading = weather_sample(expected_point, late=held)
            if recover_next:
                recover_next = False
                reading["temperature"] = 6
            await route.fulfill(status=200, json=reading)
        elif url.path.startswith("/api/") and request.method not in {"GET", "HEAD"}:
            report["other_api_writes"].append([request.method,url.path])
            await route.abort()
        else:
            await route.continue_()

    try:
        if mode != "prompt":
            await context.grant_permissions(["geolocation"] if mode == "granted" else [], origin=base)
        await context.route("**/*", route_request)
        await context.add_init_script(PROBE + (PROMPT_FIXTURE if mode == "prompt" else ""))
        page = await context.new_page()
        page.set_default_timeout(9000)
        if mode == "granted":
            await page.clock.install()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        await page.goto(base + "/?scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        await page.bring_to_front()
        root, control = page.locator(WEATHER), page.locator(CONTROL)
        await expect(root).to_be_visible()
        assert await page.evaluate("navigator.permissions.query({name:'geolocation'}).then(result=>result.state)") == mode

        if mode == "prompt":
            if await page.locator("#dialogue").is_visible():
                await page.locator("#close-chat").click()
                await expect(page.locator("#dialogue")).to_be_hidden()
            await expect(root).to_have_attribute("data-phase", "idle")
            await expect(root.locator(COORDINATE)).to_be_hidden()
            await page.wait_for_timeout(500)
            assert not calls and not await page.evaluate("weatherProbe.locates.length")
            await control.press("Enter")
            await page.wait_for_function("weatherProbe.locates.length === 1")
            assert await page.evaluate("weatherProbe.locates[0].active") and not calls
            await page.evaluate("coords => weatherProbe.allow(coords)", POSITION)
        if mode == "granted":
            await check_weather_unavailable(root)
            await expect(root.locator(PLACE)).to_have_text("当前位置")
            case["first_weather_failure_point"] = await check_main_position(root, COARSE)
            assert len(calls) == 1 and await page.evaluate("weatherProbe.locates.length") == 1, calls
            await page.clock.fast_forward(60 * 1000 + 1000)
        if mode in {"granted", "prompt"}:
            await expect(root).to_have_attribute("data-phase", "ready")
            await expect(root.locator("#location-weather-value")).to_contain_text(TEMPERATURE)
            await expect(root.locator(PLACE)).to_be_visible()
            await expect(root.locator(PLACE)).to_have_text("当前位置")
            await expect(page.locator(DETAILS)).to_be_hidden()
            case["main_point"] = await check_main_position(root, COARSE)
            await expect(root.locator("#location-weather-stamp")).not_to_be_empty()
            assert await root.locator("#location-weather-stamp").evaluate("""stamp =>
                stamp.textContent === new Intl.DateTimeFormat('zh-CN', {
                    timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hourCycle:'h23'
                }).format(new Date(stamp.dateTime))
            """)
            exposed = await root.evaluate("""node => [node.textContent,
                ...[node, ...node.querySelectorAll('[title],[aria-label]')].flatMap(el=>[el.title,el.ariaLabel])].join(' ')""")
            assert CONDITION in exposed, exposed
            assert len(calls) == (2 if mode == "granted" else 1), calls
            case["ready"] = await check_geometry(page, folder, f"{mode}-{width}")
            case["details"] = await check_details(page, folder, f"{mode}-{width}")

            if mode == "prompt":
                await page.locator("#history-toggle").click()
                await expect(page.locator("#dialogue")).to_be_visible()
                case["with_chat"] = await check_geometry(page, folder, f"{width}-chat-open")
            else:
                fail_next = True
                await page.clock.fast_forward(10 * 60 * 1000 + 1000)
                await expect(root).to_have_attribute("data-phase", "stale")
                await expect(root.locator("#location-weather-value")).to_contain_text(TEMPERATURE)
                assert "旧读数" in await root.inner_text()
                await expect(root.locator(PLACE)).to_have_text("当前位置")
                case["stale_main_point"] = await check_main_position(root, COARSE)
                assert len(calls) == 3, calls
                case["stale_details"] = await check_details(page, folder, "stale-1440", stale=True)
                fail_next = True
                expected_point = MOVED_COARSE
                await context.set_geolocation({**MOVED_POSITION, "accuracy":25})
                await page.clock.fast_forward(60 * 1000 + 1000)
                await check_weather_unavailable(root)
                await expect(root.locator(PLACE)).to_have_text("当前位置")
                case["moved_weather_failure_point"] = await check_main_position(root, MOVED_COARSE)
                assert len(calls) == 4, calls
                recover_next = True
                await page.clock.fast_forward(60 * 1000 + 1000)
                await expect(root).to_have_attribute("data-phase", "ready")
                await expect(root.locator("#location-weather-value")).to_contain_text(RECOVERED_TEMPERATURE)
                case["moved_main_point"] = await check_main_position(root, MOVED_COARSE)
                assert len(calls) == 5, calls
                case["automatic_refresh"] = {"failure_preserved_reading":True, "retry_recovered":True,
                                             "moved_point_followed":True, "previous_point_weather_cleared":True}
                await page.evaluate("weatherProbe.failNextLocation = true")
                await control.click()
                await expect(root.locator(PLACE)).to_have_text("上次定位")
                case["last_known_point"] = await check_main_position(root, MOVED_COARSE)
                assert len(calls) == 5, calls
                hold_next = True
                await page.evaluate("weatherProbe.surviveNextAbort = true")
                await control.click()
                await asyncio.wait_for(late.wait(), 8)
                await page.locator(DETAILS_TOGGLE).click()
                await expect(page.locator(DETAILS)).to_be_visible()
                await context.clear_permissions()
                await context.grant_permissions([], origin=base)
                await expect(root).to_have_attribute("data-phase", "denied")
                await expect(root.locator("#location-weather-value")).not_to_contain_text(RECOVERED_TEMPERATURE)
                await expect(root.locator("#location-weather-stamp")).to_be_hidden()
                await expect(root.locator(COORDINATE)).to_be_hidden()
                await expect(root.locator(COORDINATE)).to_be_empty()
                await expect(page.locator(DETAILS)).to_be_hidden()
                await expect(page.locator(DETAILS_TOGGLE)).to_be_hidden()
                await expect(page.locator("#location-weather-point")).to_have_text("—")
                release_late.set()
                await page.wait_for_function("weatherProbe.lateStatus === 200")
                await page.wait_for_timeout(350)
                await expect(root).to_have_attribute("data-phase", "denied")
                await expect(root.locator("#location-weather-value")).not_to_contain_text("47")
                await expect(root.locator(COORDINATE)).to_be_hidden()
                await expect(root.locator(COORDINATE)).to_be_empty()
                assert LATE_CONDITION not in await root.inner_text() and len(calls) == 6
                assert await page.evaluate("weatherProbe.lateAborted")
                case["revoked"] = {"late_200_discarded":True,"old_reading_cleared":True,
                                   "abort_requested":True,"details_closed":True,"main_point_cleared":True}
        else:
            await expect(root).to_have_attribute("data-phase", "denied")
            await expect(root.locator(COORDINATE)).to_be_hidden()
            await expect(page.locator(DETAILS_TOGGLE)).to_be_hidden()
            await expect(page.locator(DETAILS)).to_be_hidden()
            await page.wait_for_timeout(500)
            assert not calls and not await page.evaluate("weatherProbe.locates.length")
        assert len(context.pages) == 1 and not await page.locator("dialog[open]").count()
        assert not await page.evaluate("Boolean(window.documentPictureInPicture?.window)")
        assert asset_hits == assets.keys(), (asset_hits, assets.keys())
        case["no_new_window"] = True
    finally:
        release_late.set()
        await context.close()


async def verify(candidate):
    assets = {url:((candidate / name).read_bytes(), mime) for url, (name, mime) in STATIC_ASSETS.items()} if candidate else {}
    folder = ROOT / "runs" / datetime.now().strftime("ambient-weather-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    report = {"passed":False,"cases":{},"outside":[],"other_api_writes":[],"page_errors":[],"model_connections":0}
    report["candidate_asset_sha256"] = {url:sha256(data).hexdigest() for url, (data, _) in assets.items()}
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    server = BrowserServer(folder, free_port())
    environment = {"LOCALAPPDATA":str(folder / "profile"),"MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "MOSS_MEDIA_DB":str(catalog),"MOSS_MEM0_ENABLED":"0","MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_SPEECH_PREPARE":"0","MOSS_SPEECH_STANDBY":"0","MOSS_SPEECH_MIC_WARM":"0"}

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with await asyncio.start_server(guard, "127.0.0.1", 0) as sink:
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            profiles = {key:Profile(key, label, model_url, model, SecretStr(""))
                        for key, (label, _, __, model) in SPECS.items()}
            with patch.dict(os.environ, environment), patch("core.profiles", return_value=profiles):
                moss = Moss(server.data, model_url, "isolated-model-guard")
                runner = web.AppRunner(application(moss, lambda:"standby", server.data, server.port,
                                                   "fixture", asyncio.Event()), access_log=None, shutdown_timeout=1)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", server.port).start()
                    async with async_playwright() as playwright:
                        browser = await open_browser(playwright)
                        try:
                            await confirm_muted_edge(browser)
                            for mode, width in (("granted", 1440), ("prompt", 375), ("denied", 375)):
                                await check_weather(browser, server.url, folder, report, mode, width, assets)
                        finally:
                            await browser.close()
                    assert not moss.state()["turns"] and report["model_connections"] == 0, report
                    assert not any(report[key] for key in ("outside","other_api_writes","page_errors")), report
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
    parser.add_argument("--candidate", type=Path, help="Directory containing the four staged main-window assets.")
    asyncio.run(verify(parser.parse_args().candidate))
