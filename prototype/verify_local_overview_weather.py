"""Exercise weather and real Windows readings through the normal message API."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import ClientConnectionError, ClientSession, web

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from local_overview import parse_local_overview, run_desktop
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_local_overview import check_live_values, independent_readings
from verify_local_overview_progressive import wait_turn
from verify_weather_browser_location import sample_forecast
from weather_forecast import format_weather


BATTERY_QUESTION = "明天北京的天气怎么样，顺便告诉我电脑还剩多少电？"
CLOCK_QUESTION = "现在几点？明天北京会下雨吗？"
NEARBY_QUESTION = "现在几点？这里天气怎么样？"
NEARBY_REMINDERS = "这里天气怎么样、我还有什么提醒、现在几点"
PREFERRED_CITY = "cn:110105"
BROWSER_FIX = {"latitude":31.2345, "longitude":121.4765}
PLACES = {
    "北京": {"name": "北京", "latitude": 39.9, "longitude": 116.4},
    "上海": {"name": "上海", "latitude": 31.2, "longitude": 121.5},
}
UNSUPPORTED = (
    "不用查北京天气、现在几点", "查询北京和上海的天气、系统音量多大？",
    "朝阳区明天天气、系统音量多大？", "天气、电量和音量都给我看一眼",
    "明天北京的天气怎么样，顺便把声音调到五十？", "查明天北京天气，取消全部提醒。",
    "今天北京天气、明天北京天气、电脑电量", "明天北京的天气怎么样？",
    "查明天北京天气，然后把这句话翻译成英语。",
)


async def check_edge(server, folder, started, release, hold_volume, volume_waiting, volume_release, candidate_app=None):
    from playwright.async_api import async_playwright

    page_errors = []
    captures = []
    messages = []
    candidate_hits = 0

    async def guard_page(route):
        nonlocal candidate_hits
        url = urlsplit(route.request.url)
        if (url.scheme in {"http", "https"} and (url.hostname, url.port) != ("127.0.0.1", server.port)
                or url.path.startswith(("/api/speak", "/api/prepare-speech", "/api/transcri"))):
            await route.abort()
        elif url.path == "/static/app.js" and candidate_app:
            candidate_hits += 1
            await route.fulfill(body=candidate_app.read_bytes(), content_type="text/javascript; charset=utf-8")
        elif url.path == "/api/weather/current":
            await route.fulfill(status=502, json={"error":"隔离测试停用常驻天气。"})
        elif url.path == "/api/message" and route.request.method == "POST":
            messages.append(route.request.post_data_json)
            await route.continue_()
        else:
            await route.continue_()

    async with async_playwright() as playwright:
        browser = await open_browser(playwright)
        try:
            await confirm_muted_edge(browser)
            page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            await page.route("**/*", guard_page)
            await page.add_init_script('localStorage.setItem("moss.voice.enabled", "off")')
            await page.add_init_script("""() => {
                window.locationReadCount = 0;
                const getPosition = navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);
                navigator.geolocation.getCurrentPosition = (...args) => {
                    window.locationReadCount++;
                    return getPosition(...args);
                };
            }""")
            await page.goto(server.url + "/?chat=1&scene=off")
            composer = page.get_by_test_id("composer")
            submit = page.get_by_test_id("send")
            local = page.get_by_test_id("local-status-panel")
            weather = page.get_by_test_id("weather-panel")

            async def ask(text):
                await composer.fill(text)
                await submit.click()

            async def capture(name):
                await page.screenshot(path=folder / name, animations="disabled")
                captures.append(name)

            await ask(CLOCK_QUESTION)
            async with asyncio.timeout(3):
                await started.wait()
            await local.locator('[data-reading-kind="clock"]').wait_for()
            assert await local.get_attribute("data-phase") == "reading"
            assert not await weather.is_visible() and not release.is_set()
            await capture("01-先显示本机时间.png")
            release.set()
            await local.locator('[data-reading-kind="weather"]').wait_for()
            assert await local.get_attribute("data-phase") == "complete"
            assert await local.locator("h2").text_content() == "本机状态和天气"
            assert await page.locator("dialog[open]").count() == 1 and not await weather.is_visible()
            weather_row = local.locator('[data-reading-kind="weather"]')
            assert await weather_row.get_by_text("天气", exact=True).is_visible()
            assert await weather_row.locator('[data-weather-icon="rain"]').is_visible()
            assert await weather_row.evaluate("row => row.clientWidth === row.parentElement.clientWidth")
            await capture("02-同一面板合并结果.png")
            for width, height in ((768, 1024), (375, 812)):
                await page.set_viewport_size({"width":width, "height":height})
                assert await local.evaluate("""panel => {
                    const rect = panel.getBoundingClientRect();
                    return rect.x >= 0 && rect.right <= innerWidth && panel.scrollWidth <= panel.clientWidth;
                }""")
                assert await weather_row.locator('[data-weather-icon="rain"]').is_visible()
                await capture(f"02-同一面板-{width}.png")
            await page.set_viewport_size({"width":1440, "height":900})
            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await page.get_by_test_id("open-weather").last.click()
            await weather.wait_for()
            assert "北京" in (await weather.inner_text())
            await capture("03-点击打开天气详情.png")
            await page.keyboard.press("Escape")
            await weather.wait_for(state="hidden")
            await ask("今天上海天气怎么样？")
            await weather.wait_for()
            assert "上海" in (await weather.inner_text()) and not await local.is_visible()
            await capture("04-单问天气弹出详情.png")
            await page.keyboard.press("Escape")
            await weather.wait_for(state="hidden")
            hold_volume.set()
            try:
                await ask("查一下明天北京的天气，再看看系统音量。")
                await asyncio.wait_for(volume_waiting.wait(), 2)
                await weather_row.wait_for(timeout=1500)
                assert await local.get_attribute("data-phase") == "reading"
                assert await weather_row.locator(".weather-icon").count() == 0
            finally:
                volume_release.set()
                hold_volume.clear()
            await weather_row.locator('[data-weather-icon="rain"]').wait_for()
            assert await local.get_attribute("data-phase") == "complete"
            volume_row = local.locator('[data-reading-kind="volume"]')
            assert await volume_row.evaluate("row => row.clientWidth === row.parentElement.clientWidth")
            await capture("05-天气先到后补图标.png")

            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await ask(NEARBY_QUESTION)
            await weather_row.wait_for()
            assert "定位" in await weather_row.inner_text() and await local.locator('[data-reading-kind="clock"]').is_visible()
            assert not ({"browser_location", "preferred_city_id"} & messages[-1].keys()), messages[-1]
            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await ask(NEARBY_REMINDERS)
            await weather_row.wait_for()
            assert "定位" in await weather_row.inner_text() and not await page.evaluate("window.locationReadCount")
            assert await local.locator('[data-reading-kind="reminders"]').is_visible()
            assert not ({"browser_location", "preferred_city_id"} & messages[-1].keys()), messages[-1]

            await page.context.set_geolocation({**BROWSER_FIX, "accuracy":30})
            await page.context.grant_permissions(["geolocation"])
            await page.evaluate("id => localStorage.setItem('moss.weather.city', JSON.stringify({id}))", PREFERRED_CITY)
            await page.reload(wait_until="domcontentloaded")
            await ask(NEARBY_QUESTION)
            await weather_row.wait_for()
            assert "北京市朝阳区" in await weather_row.inner_text(), await weather_row.inner_text()
            assert messages[-1].get("preferred_city_id") == PREFERRED_CITY and "browser_location" not in messages[-1]
            assert await local.locator('[data-reading-kind="clock"]').is_visible() and not await weather.is_visible()
            await capture("06-手选城市和本机时间.png")
            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await ask("查看我的全部提醒，这里天气怎么样？")
            await weather_row.wait_for()
            assert "北京市朝阳区" in await weather_row.inner_text(), await weather_row.inner_text()
            assert await local.locator('[data-reading-kind="reminders"]').is_visible()
            assert messages[-1].get("preferred_city_id") == PREFERRED_CITY and "browser_location" not in messages[-1]

            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await ask(CLOCK_QUESTION)
            await weather_row.wait_for()
            assert "北京明天" in await weather_row.inner_text(), await weather_row.inner_text()
            assert not ({"browser_location", "preferred_city_id"} & messages[-1].keys()), messages[-1]

            await page.evaluate("localStorage.removeItem('moss.weather.city')")
            await page.reload(wait_until="domcontentloaded")
            await ask("现在几点这里天气怎么样")
            await weather_row.wait_for()
            assert "当前位置附近" in await weather_row.inner_text(), await weather_row.inner_text()
            assert messages[-1].get("browser_location") == BROWSER_FIX and "preferred_city_id" not in messages[-1]
            await capture("07-恢复浏览器定位后合并结果.png")
            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await ask("扬声器是静音的么？这里天气怎么样？")
            await weather_row.wait_for()
            volume_row = local.locator('[data-reading-kind="volume"]')
            assert "静音" in await volume_row.inner_text(), await volume_row.inner_text()
            assert "当前位置附近" in await weather_row.inner_text(), await weather_row.inner_text()
            assert messages[-1].get("browser_location") == BROWSER_FIX

            for text in (NEARBY_REMINDERS, "明天上午还有哪些提醒，这里会下雨吗"):
                await page.keyboard.press("Escape")
                await local.wait_for(state="hidden")
                await ask(text)
                await weather_row.wait_for()
                assert "当前位置附近" in await weather_row.inner_text(), await weather_row.inner_text()
                assert await local.locator('[data-reading-kind="reminders"]').is_visible()
                assert messages[-1].get("browser_location") == BROWSER_FIX and "preferred_city_id" not in messages[-1]

            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            await ask("明天北京会下雨吗、我还有什么提醒")
            await weather_row.wait_for()
            assert "北京" in await weather_row.inner_text() and "当前位置" not in await weather_row.inner_text()
            assert not ({"browser_location", "preferred_city_id"} & messages[-1].keys()), messages[-1]

            await page.keyboard.press("Escape")
            await local.wait_for(state="hidden")
            reads_before_reminder = await page.evaluate("window.locationReadCount")
            reminder_text = "10分钟后提醒我查看这里天气"
            await ask(reminder_text)
            async with asyncio.timeout(3):
                while True:
                    turn = (await server.state())["turns"][-1]
                    if turn["user"] == reminder_text and turn["status"] == "replied":
                        break
                    await asyncio.sleep(.02)
            assert turn["source"] == "timer" and not turn.get("weather") and not turn["cloud_requests"]["total"], turn
            assert not ({"browser_location", "preferred_city_id"} & messages[-1].keys()), messages[-1]
            assert await page.evaluate("window.locationReadCount") == reads_before_reminder
        finally:
            await browser.close()
    assert not page_errors, page_errors
    assert not candidate_app or candidate_hits >= 3, candidate_hits
    return captures, messages


async def run(browser, candidate_app=None):
    folder = ROOT / "runs" / datetime.now().strftime("local-overview-weather-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result, flush=True)
    report = {"passed": False, "model_connections": 0, "forecast_calls": [], "cases": []}
    started, release, cancelled = (asyncio.Event() for _ in range(3))
    hold_volume, volume_waiting, volume_release = (asyncio.Event() for _ in range(3))
    mode = "slow"

    async def forecast(city, period, place, *, part_of_day=None, question=""):
        place = place or PLACES[city]
        report["forecast_calls"].append([place["name"], period, part_of_day, question])
        if mode == "slow":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if mode == "unavailable":
            raise ClientConnectionError("isolated forecast connection failure")
        return format_weather(sample_forecast(), place, period, part_of_day, question)

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def read_desktop(intent):
        if hold_volume.is_set():
            volume_waiting.set()
            await volume_release.wait()
        return await run_desktop(intent)

    async def unexpected_model(**request):
        raise AssertionError(f"Unexpected model call: {request['text']}")

    def verify_weather(turn, city, period, **extra):
        weather = turn["weather"]
        assert all(weather.get(key) == value for key, value in dict(city=city, period=period, **extra).items()), turn
        assert weather["date"] and weather["condition"] and weather["simple"], turn

    async def check_api(server, stream):
        async def begin(question, **location):
            start = (perf_counter(), datetime.now(), independent_readings())
            status, state = await server.post("/api/message", {"text": question, **location})
            assert status == 200, state
            return state["turns"][-1]["id"], start

        async def verify_final(turn_id, sample, kinds, early=None):
            elapsed_start, started_at, windows_before = sample
            turn = await wait_turn(stream, turn_id, "replied")
            finished_at, windows_after = datetime.now(), independent_readings()
            rows = turn["local_overview"]
            assert turn["source"] == "context" and not turn["cloud_requests"]["total"], turn
            assert [item["kind"] for item in rows] == kinds, turn
            assert turn["reply"] == "\n".join(item["reply"] for item in rows), turn
            assert not turn.get("partial_reply") and not turn.get("local_overview_partial"), turn
            assert not turn.get("desktop_confirmation") and not turn.get("receipt"), turn
            if early:
                assert all(item in rows for item in early), turn
            check_live_values(rows, started_at, finished_at, windows_before, windows_after)
            report["cases"].append({"text": turn["user"], "rows": rows, "weather": turn.get("weather"),
                                    "ms": round((perf_counter() - elapsed_start) * 1000, 1),
                                    "windows": {"before": windows_before, "after": windows_after}})
            return turn

        async def wait_one_reading(turn_id, kind):
            async with asyncio.timeout(3):
                await started.wait()
            turn = await wait_turn(stream, turn_id, "thinking", with_partial=True)
            rows = turn["local_overview_partial"]
            assert [item["kind"] for item in rows] == [kind] and not turn.get("weather"), turn
            assert rows[0]["reply"] == turn["partial_reply"] and not release.is_set(), turn
            return rows

        nonlocal mode
        turn_id, sample = await begin(BATTERY_QUESTION)
        early = await wait_one_reading(turn_id, "battery")
        report["partial_ms"] = round((perf_counter() - sample[0]) * 1000, 1)
        release.set()
        done = await verify_final(turn_id, sample, ["weather", "battery"], early)
        verify_weather(done, "北京", "tomorrow")

        mode = "ready"
        status, state = await server.post("/api/message", {"text": "那后天呢？"})
        assert status == 200, state
        followup = await wait_turn(stream, state["turns"][-1]["id"], "replied")
        verify_weather(followup, "北京", "day_after_tomorrow")

        for text, expected, city, period in (
            (CLOCK_QUESTION, ["clock", "weather"], "北京", "tomorrow"),
            ("查一下明天北京的天气，再看看系统音量。", ["weather", "volume"], "北京", "tomorrow"),
            ("查一下明天北京的天气，顺便告诉我还有什么提醒。", ["weather", "reminders"], "北京", "tomorrow"),
            ("查询上海天气、系统音量多大？", ["weather", "volume"], "上海", "current"),
        ):
            turn_id, reading = await begin(text)
            done = await verify_final(turn_id, reading, expected)
            verify_weather(done, city, period)
            if "reminders" in expected:
                assert "没有" in done["local_overview"][1]["reply"], done

        turn_id, reading = await begin("北京朝阳明天下午会下雨吗、电脑电量是多少")
        done = await verify_final(turn_id, reading, ["weather", "battery"])
        verify_weather(done, "北京市朝阳区", "tomorrow", part_of_day="下午", location_kind="district")
        assert report["forecast_calls"][-1] == ["北京市朝阳区", "tomorrow", "下午", "下雨"], report

        mode = "unavailable"
        turn_id, sample = await begin(CLOCK_QUESTION)
        done = await verify_final(turn_id, sample, ["clock", "weather"])
        assert "再查一次" in done["local_overview"][1]["reply"] and not done.get("weather"), done
        forecast_count = len(report["forecast_calls"])
        mode = "ready"
        code, state = await server.post("/api/message", {"text": "需要"})
        assert code == 200, state
        retried = await wait_turn(stream, state["turns"][-1]["id"], "replied")
        verify_weather(retried, "北京", "tomorrow")
        assert len(report["forecast_calls"]) == forecast_count + 1 and not retried.get("local_overview"), retried

        mode = "slow"
        started.clear()
        release.clear()
        turn_id, _ = await begin(CLOCK_QUESTION)
        await wait_one_reading(turn_id, "clock")
        cancel_at = perf_counter()
        code, _ = await server.post("/api/cancel", {"turn_id": turn_id})
        assert code == 200
        turned = await wait_turn(stream, turn_id, "cancelled")
        async with asyncio.timeout(2):
            await cancelled.wait()
        assert not turned.get("weather") and not turned.get("local_overview") and not release.is_set(), turned
        report["cancel_ms"] = round((perf_counter() - cancel_at) * 1000, 1)
        mode = "ready"
        release.set()
        turn_id, sample = await begin(CLOCK_QUESTION)
        await verify_final(turn_id, sample, ["clock", "weather"])

        turn_id, sample = await begin(NEARBY_QUESTION, preferred_city_id=PREFERRED_CITY, browser_location=BROWSER_FIX)
        done = await verify_final(turn_id, sample, ["clock", "weather"])
        verify_weather(done, "北京市朝阳区", "current", location_kind="configured")
        turn_id, sample = await begin(CLOCK_QUESTION, preferred_city_id=PREFERRED_CITY)
        done = await verify_final(turn_id, sample, ["clock", "weather"])
        verify_weather(done, "北京", "tomorrow")
        turn_id, sample = await begin("现在几点这里天气怎么样", browser_location=BROWSER_FIX)
        done = await verify_final(turn_id, sample, ["clock", "weather"])
        verify_weather(done, "当前位置附近", "current", location_kind="browser")
        assert "browser_location" not in done and not ({"latitude", "longitude"} & done["weather"].keys())
        turn_id, sample = await begin("当前位置天气、系统音量")
        done = await verify_final(turn_id, sample, ["weather", "volume"])
        assert not done.get("weather") and "定位" in done["local_overview"][0]["reply"]

        for text in UNSUPPORTED:
            assert parse_local_overview(text) is None, text
        report["excluded_compounds"] = list(UNSUPPORTED)

    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch("assistant_brain.fetch_weather", forecast))
            stack.enter_context(patch("assistant_brain.run_holo", unexpected_model))
            stack.enter_context(patch("local_overview.run_desktop", read_desktop))
            guard = await stack.enter_async_context(await asyncio.start_server(model_guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
                    os.environ[prefix + "_API_KEY"] = "isolated-qa"
            server = BrowserServer(folder, free_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "moss-overview-weather-fixture")
            stack.push_async_callback(moss.close)
            stopped = asyncio.Event()
            app = application(moss, lambda: "standby", server.data, server.port, "overview-weather-qa", stopped)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", server.port).start()
            try:
                async with ClientSession() as client, client.get(server.url + "/api/events") as events:
                    assert events.status == 200 and events.content_type == "text/event-stream"
                    await check_api(server, events.content)
                if browser:
                    release.clear()
                    started.clear()
                    mode = "slow"
                    report["screenshots"], report["browser_message_posts"] = await check_edge(
                        server, folder, started, release, hold_volume, volume_waiting, volume_release, candidate_app)
                    if candidate_app:
                        report["candidate_app_sha256"] = hashlib.sha256(candidate_app.read_bytes()).hexdigest()
                assert report["model_connections"] == 0, report
                report["passed"] = True
            finally:
                stopped.set()
                release.set()
                moss._notify()
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="Exercise the same route in muted Edge")
    parser.add_argument("--candidate-app", type=Path, help="Serve a staged app.js in muted Edge")
    args = parser.parse_args()
    asyncio.run(run(args.browser, args.candidate_app))
