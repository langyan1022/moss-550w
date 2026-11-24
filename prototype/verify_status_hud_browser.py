"""Verify the detached device HUD with isolated Edge and the real local status endpoint."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from PIL import ImageGrab
from playwright.async_api import Error, async_playwright, expect
from pydantic import SecretStr
import pywintypes
import win32con
import win32gui
import win32process

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port


LAUNCHER = "#open-status-window"
CLOSE = "#status-hud-close"
REFRESH = "#status-hud-refresh"
STATUS_API = "/api/device-status"
METRICS = {"cpu", "memory", "battery", "disk"}
HUD = """win => {
  const doc = win.document, hud = doc.querySelector('#status-hud');
  if (!hud) return null;
  return {
    mode:hud.dataset.windowMode,
    pip:win.matchMedia('(display-mode: picture-in-picture)').matches,
    metrics:Object.fromEntries([...doc.querySelectorAll('[data-metric]')]
      .map(el => [el.dataset.metric, el.querySelector('.hud-value')?.textContent.trim()])),
    connection:doc.querySelector('#status-hud-connection')?.textContent.trim(),
    clock:doc.querySelector('#status-hud-clock')?.textContent.trim(),
    sampled:doc.querySelector('#status-hud-sampled')?.dateTime,
    sampleClock:doc.querySelector('#status-hud-sampled')?.textContent.trim(),
    size:[win.innerWidth, win.innerHeight],
    content:[doc.documentElement.clientWidth, doc.documentElement.scrollWidth],
  };
}"""
MAIN_STRIP = """() => {
  const node = document.querySelector('#device-strip');
  const state = node?.querySelector('#device-strip-state');
  return node && {phase:node.dataset.phase, sampleTitle:state?.title,
    values:Object.fromEntries([...node.querySelectorAll('[data-main-metric] dd')].map(el => [
      el.closest('[data-main-metric]').dataset.mainMetric, el.textContent.trim()]))};
}"""
PIP_PROBE = """() => {
  const api = window.documentPictureInPicture;
  window.qaPiP = {enters:0, hides:0, resizes:[], secure:isSecureContext,
                  errors:[], supported:typeof api?.requestWindow === 'function'};
  api?.addEventListener('enter', ({window:win}) => {
    qaPiP.enters++;
    qaPiP.child = win;
    win.addEventListener('error', event => qaPiP.errors.push(String(event.message).slice(0, 350)));
    win.addEventListener('unhandledrejection', event => qaPiP.errors.push(String(event.reason).slice(0, 350)));
    win.addEventListener('pagehide', () => qaPiP.hides++, {once:true});
    win.addEventListener('resize', () => qaPiP.resizes.push([win.innerWidth,win.innerHeight]));
  });
}"""


async def wait_for(predicate, *, seconds=12):
    async with asyncio.timeout(seconds):
        while not (value := await predicate()):
            await asyncio.sleep(.08)
    return value


async def launch(playwright, *, fallback):
    binary = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    args = ["--mute-audio", "--disable-gpu", "--disable-background-networking",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding"]
    if fallback:
        args.append("--disable-blink-features=DocumentPictureInPictureAPI")
    return await playwright.chromium.launch(executable_path=str(binary), headless=fallback, args=args,
                                             ignore_default_args=["--disable-popup-blocking"])


async def browser_pid(browser):
    session = await browser.new_browser_cdp_session()
    try:
        response = await session.send("SystemInfo.getProcessInfo")
        return next(row["id"] for row in response["processInfo"] if row["type"] == "browser")
    finally:
        await session.detach()


def owned_windows(pid):
    windows = set()

    def collect(hwnd, _):
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid and win32gui.IsWindowVisible(hwnd):
                windows.add(hwnd)
        except pywintypes.error:
            pass

    win32gui.EnumWindows(collect, None)
    return windows


async def check_cache(url):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
        async def read():
            async with client.get(url + STATUS_API) as response:
                assert response.status == 200, response.status
                return await response.json()

        first = await read()
        second = await read()
        assert first == second, (first, second)
        assert {"sampled_at", "cpu_percent", "memory", "battery", "disk"} <= first.keys(), first
        datetime.fromisoformat(first["sampled_at"])
        value = first["cpu_percent"]
        assert value is None or type(value) is int and 0 <= value <= 100, first
        for key, available in (("memory", "available"), ("disk", "free")):
            metric = first[key]
            assert metric is None or 0 <= metric[available] <= metric["total"], (key, metric)
        battery = first["battery"]
        assert battery is None or battery["percent"] is None or 0 <= battery["percent"] <= 100, battery
        await asyncio.sleep(2.15)
        third = await read()
        assert third["sampled_at"] != first["sampled_at"], (first, third)
        assert type(third["cpu_percent"]) is int and 0 <= third["cpu_percent"] <= 100, third
        return {"cache_hit":second["sampled_at"], "refreshed":third["sampled_at"]}


async def exercise(page, context, url, folder, traffic, evidence, *, fallback, pid):
    calls, outside, api_posts, page_errors = [], [], [], []
    traffic.update(calls=calls, posts=api_posts, offline=False)

    def observe_errors(view):
        view.on("pageerror", lambda error: page_errors.append(str(error)[:350]))

    observe_errors(page)
    context.on("page", observe_errors)

    async def observe(route):
        request = route.request
        if urlsplit(request.url).scheme in {"http", "https"} and not request.url.startswith(url + "/"):
            outside.append(request.url)
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", observe)
    await page.goto(url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.locator(".connection.online").wait_for(timeout=15000)
    await page.evaluate(PIP_PROBE)
    supported = await page.evaluate("qaPiP.supported")
    if not fallback and not supported:
        return {"supported":False, "reason":"Document Picture-in-Picture API unavailable", "secure":
                await page.evaluate("qaPiP.secure")}
    assert supported is not fallback and await page.evaluate("qaPiP.secure")

    async def read_main():
        return await page.evaluate(MAIN_STRIP)

    async def main_connected():
        main = await read_main()
        return main if main and main["phase"] == "connected" else None

    async def check_shared_values():
        paired = await page.evaluate(f"""() => ({{
            main: ({MAIN_STRIP})(),
            pip: ({HUD})(documentPictureInPicture.window)
        }})""")
        main, hud = paired["main"], paired["pip"]
        matching = {name: hud["metrics"][name] for name in METRICS}
        for key in ("memory", "disk"):
            if " / " in matching[key]:
                matching[key] = matching[key].split(" / ", 1)[0] + "\u00a0GiB"
        assert main["values"] == {**matching, "clock":hud["clock"]}, paired
        assert main["sampleTitle"] == "采样于 " + hud["sampleClock"], paired
        return paired

    evidence["main_before_open"] = await wait_for(main_connected)
    opener = page.locator(LAUNCHER)
    pages_before = len(context.pages)
    windows_before = owned_windows(pid) if not fallback else set()
    lane = "popup" if fallback else "main"
    traffic["fault_lane"] = lane
    opened_after = len(calls)
    opened_at = perf_counter()
    if fallback:
        async with page.expect_popup() as opened:
            await opener.press("Enter")
        popup = await opened.value
        await popup.wait_for_url(url + "/status")

        async def read():
            return await popup.evaluate(f"() => ({HUD})(window)")

        async def activate(selector):
            await popup.locator(selector).press("Enter")
    else:
        await opener.press("Enter")
        await page.wait_for_function("qaPiP.enters === 1 && !!documentPictureInPicture.window")

        async def read():
            return await page.evaluate(f"() => documentPictureInPicture.window && ({HUD})(documentPictureInPicture.window)")

        async def activate(selector):
            await page.evaluate("selector=>documentPictureInPicture.window.document.querySelector(selector).click()", selector)

    async def connected():
        hud = await read()
        return hud if hud and "已连接" in (hud["connection"] or "") else None

    async def interrupted():
        hud = await read()
        return hud if hud and "连接中断" in (hud["connection"] or "") else None

    try:
        initial = await wait_for(connected)
    except TimeoutError:
        evidence.update(timeout="initial_connected", api_present=supported, request_log=calls,
                        page_errors=page_errors, outside_requests=outside, api_posts=api_posts)
        try:
            evidence["hud_at_timeout"] = await read()
            evidence["opener_and_child"] = await page.evaluate("""() => {
              const win = window.documentPictureInPicture?.window ?? qaPiP.child;
              const notice = document.getElementById('status-window-notice');
              const safeUrl = doc => { const url = new URL(doc.URL); return url.origin === 'null' ? url.protocol + url.pathname : url.origin + url.pathname; };
              let child = null;
              if (win && !win.closed) child = {visibility:win.document.visibilityState,
                url:safeUrl(win.document), origin:win.location.origin, effectiveOrigin:win.origin,
                ready:win.document.readyState, focused:win.document.hasFocus()};
              return {child, openerVisibility:document.visibilityState, openerOrigin:location.origin,
                notice:notice?.textContent.trim(), noticeHidden:notice?.hidden,
                enters:qaPiP.enters, hides:qaPiP.hides, errors:qaPiP.errors};
            }""")
            if fallback:
                evidence["popup_at_timeout"] = await popup.evaluate("""() => ({visibility:document.visibilityState,
                  url:location.origin+location.pathname, origin:location.origin, ready:document.readyState})""")
        except Exception as error:
            evidence["diagnostic_error"] = str(error)[:350]
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
        raise
    first_count = len(calls)
    assert first_count >= 1 and set(initial["metrics"]) == METRICS, (calls, initial)
    assert initial["mode"] == ("popup" if fallback else "pip") and initial["pip"] is not fallback, initial
    assert all(initial["metrics"].values()) and initial["clock"], initial
    assert initial["content"][1] <= initial["content"][0] + 1, initial
    evidence.update(initial=initial, api_present=supported, request_log=calls)

    try:
        def lane_calls(start=0, source=lane):
            return [item for item in calls[start:] if item["lane"] == source]

        async def intervals(start=opened_after, source=lane, count=2):
            entries = lane_calls(start, source)
            if len(entries) >= count and all("status" in entry for entry in entries[:count]):
                sample = entries[:count]
                return [right["at"] - left["at"] for left, right in zip(sample, sample[1:])], sample

        async def sampled_again():
            hud = await connected()
            return hud if hud and hud["sampled"] != initial["sampled"] else None

        gaps, entries = await wait_for(lambda: intervals(count=2 if fallback else 3), seconds=16)
        assert entries[0]["at"] - opened_at < 3.5, calls
        assert all(3.5 <= gap <= 9 for gap in gaps) and all(entry["status"] == 200 for entry in entries), calls
        steady = await wait_for(sampled_again)
        assert re.fullmatch(r"[0-9]{1,3}%", steady["metrics"]["cpu"]) and steady["clock"] != initial["clock"], steady
        evidence.update(poll_seconds=round(gaps[0], 3), stable_poll_gaps=[round(gap, 3) for gap in gaps], sampled=steady)

        if fallback:
            await expect(popup.locator("#status-hud")).to_be_visible()
            await opener.press("Enter")
            assert len(context.pages) == pages_before + 1, len(context.pages)
            await popup.screenshot(path=folder / "status-popup.png", animations="disabled")
            evidence["screenshot"] = str(folder / "status-popup.png")
        else:
            evidence["same_sample_in_main_and_pip"] = await check_shared_values()
            assert await page.evaluate("qaPiP.child === documentPictureInPicture.window")
            await opener.press("Enter")
            assert await page.evaluate("qaPiP.enters === 1 && qaPiP.child === documentPictureInPicture.window")
            added = owned_windows(pid) - windows_before
            if len(added) == 1:
                hwnd, = added
                styles = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                evidence["native_topmost"] = bool(styles & win32con.WS_EX_TOPMOST)
                evidence["native_bounds"] = win32gui.GetWindowRect(hwnd)
                assert evidence["native_topmost"], evidence
                try:
                    shot = folder / "status-pip.png"
                    ImageGrab.grab(window=hwnd).save(shot)
                    evidence["screenshot"] = str(shot)
                except (TypeError, OSError) as error:
                    evidence["native_screenshot_unavailable"] = str(error)
            else:
                evidence["native_topmost"] = None
                evidence["native_reason"] = f"Expected one new test-owned visible window; found {len(added)}"

            # The trusted opener click is propagated to the same-origin PiP window.
            await page.evaluate("""() => {
              const control = document.createElement('button');
              control.textContent = '验收：调整浮窗尺寸';
              control.id = 'qa-resize-hud';
              Object.assign(control.style, {position:'fixed',left:'8px',top:'8px',zIndex:100000});
              control.onclick = () => {
                const win = documentPictureInPicture.window;
                win.resizeBy(64,32);
              };
              document.body.append(control);
            }""")
            await page.locator("#qa-resize-hud").click()
            await page.wait_for_function("qaPiP.resizes.some(size => size[0] > " + str(initial["size"][0]) + ")")
            resized = await read()
            assert resized["size"][0] > initial["size"][0] and resized["content"][1] <= resized["content"][0] + 1, resized
            evidence["resized"] = resized
            owners = [handle for handle in windows_before if "550W" in win32gui.GetWindowText(handle)]
            assert len(owners) == 1, owners
            owner, = owners
            win32gui.ShowWindow(owner, win32con.SW_MINIMIZE)
            try:
                assert win32gui.IsIconic(owner)
                evidence["minimized_document_visibility"] = await page.evaluate("document.visibilityState")

                async def refreshed_in_background():
                    hud = await connected()
                    return hud if hud and hud["sampled"] != resized["sampled"] else None

                evidence["refreshed_with_opener_minimized"] = await wait_for(refreshed_in_background)
                assert win32gui.IsIconic(owner)
                if len(added) == 1:
                    assert win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd)
            finally:
                win32gui.ShowWindow(owner, win32con.SW_RESTORE)
                await page.bring_to_front()

        steady = await wait_for(connected)
        traffic["offline"] = True
        await activate(REFRESH)
        failed = await wait_for(interrupted)
        assert failed["metrics"] == steady["metrics"] and any(row["offline"] for row in calls), (steady, failed)
        traffic["offline"] = False
        await activate(REFRESH)
        evidence["recovered"] = await wait_for(connected)
        if not fallback:
            evidence["same_sample_after_recovery"] = await check_shared_values()
        closed_after = len(calls)
        closed_at = perf_counter()
        detached_count = len(lane_calls(source="popup"))
        if fallback:
            async with popup.expect_event("close"):
                try:
                    await activate(CLOSE)
                except Error:
                    if not popup.is_closed():
                        raise
        else:
            await activate(CLOSE)
            await page.wait_for_function("qaPiP.hides === 1 && !documentPictureInPicture.window")
        await page.bring_to_front()
        if fallback:
            await asyncio.sleep(5.6)
            assert len(lane_calls(source="popup")) == detached_count and lane_calls(closed_after, "main"), calls
            evidence["popup_requests_stopped"] = True
        else:
            gaps, resumed = await wait_for(lambda: intervals(closed_after, "main"))
            assert resumed[0]["at"] - closed_at < 3.5, calls
            assert 3.5 <= gaps[0] <= 9 and resumed[0]["status"] == resumed[1]["status"] == 200, calls
            evidence["main_poll_seconds_after_pip_close"] = round(gaps[0], 3)
        assert not outside and not api_posts, (calls, outside, api_posts)
        evidence["main_after_close"] = await wait_for(main_connected)
        assert all(row["method"] == "GET" for row in calls)
        if fallback:
            await page.set_viewport_size({"width":320, "height":720})
            bounds = await opener.bounding_box()
            assert bounds and 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= 320, bounds
            evidence["launcher_at_320px"] = bounds
        evidence.update(closed=True, outside_requests=outside, api_posts=api_posts)
        evidence["request_log"] = [{**row, "at":round(row["at"] - calls[0]["at"], 3)} for row in calls]
        return evidence
    finally:
        traffic.update(calls=None, offline=False)
        if fallback and not popup.is_closed():
            await popup.close()
        if not fallback:
            await page.evaluate("documentPictureInPicture.window?.close()")


async def verify(require_pip):
    folder = ROOT / "runs" / datetime.now().strftime("status-hud-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    server = BrowserServer(folder, free_port())
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed":False, "model_connections":0, "cases":{}}
    traffic = {"calls":None, "posts":[], "offline":False, "fault_lane":None}
    settings = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_MEDIA_DB":str(catalog),
                "MOSS_PROVIDER_DIR":str(folder / "providers"), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0", "MOSS_SPEECH_MIC_WARM":"0"}

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    @web.middleware
    async def observe_api(request, handler):
        calls = traffic["calls"]
        if calls is not None and request.path == STATUS_API:
            lane = "popup" if urlsplit(request.headers.get("Referer", "")).path == "/status" else "main"
            offline = traffic["offline"] and traffic["fault_lane"] == lane
            entry = {"at":perf_counter(), "method":request.method, "offline":offline, "lane":lane,
                     "fetch_site":request.headers.get("Sec-Fetch-Site"), "origin":request.headers.get("Origin")}
            calls.append(entry)
            try:
                response = (web.json_response({"error":"隔离验收暂时离线"}, status=503) if offline
                            else await handler(request))
            except web.HTTPException as error:
                entry["status"] = error.status
                raise
            entry["status"] = response.status
            return response
        if calls is not None and request.path.startswith("/api/") and request.method != "GET":
            traffic["posts"].append({"path":request.path, "method":request.method})
            return web.json_response({"error":"隔离验收禁止此写入"}, status=409)
        return await handler(request)

    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            profiles = {key:Profile(key, label, model_url, model, SecretStr(""))
                        for key, (label, _, __, model) in SPECS.items()}
            with patch.dict(os.environ, settings), patch("core.profiles", return_value=profiles):
                moss = Moss(server.data, model_url, "fixture-local")
                app = application(moss, lambda: "standby", server.data, server.port, "isolated", asyncio.Event())
                app.middlewares.insert(0, observe_api)
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", server.port).start()
                    report["endpoint_cache"] = await check_cache(server.url)
                    async with async_playwright() as playwright:
                        for fallback in (False, True):
                            browser = await launch(playwright, fallback=fallback)
                            try:
                                await confirm_muted_edge(browser)
                                report["edge"] = browser.version
                                context = await browser.new_context(no_viewport=True, reduced_motion="reduce",
                                                                    service_workers="block")
                                page = await context.new_page()
                                case = "popup" if fallback else "pip"
                                evidence = report["cases"].setdefault(case, {})
                                result = await exercise(page, context, server.url, folder, traffic, evidence, fallback=fallback,
                                                        pid=await browser_pid(browser))
                                report["cases"][case] = result
                            finally:
                                await browser.close()
                    if require_pip:
                        assert report["cases"]["pip"].get("closed") and report["cases"]["pip"].get("native_topmost"), report["cases"]["pip"]
                    assert report["model_connections"] == 0 and not moss.state()["turns"], report
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-pip", action="store_true", help="要求本机 Edge 实际打开原生画中画窗口")
    args = parser.parse_args()
    asyncio.run(verify(args.require_pip))
