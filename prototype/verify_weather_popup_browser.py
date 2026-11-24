"""Verify typed weather requests, permission handling, and popup ownership in muted Edge."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
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
from verify_local_overview import QUESTION
from weather_forecast import format_weather


POSITION = {"latitude": 31.218763, "longitude": 121.492174}
FIXTURE_DATE = datetime(2031, 8, 17)
NAMED = "北京市明天的天气怎么样？"
HERE = "我当前位置的天气怎么样？"
HOME = "当前位置附近"
RETRY_CITY = "郑州市"
OPTIONS = {"北京市": (39.9042, 116.4074, 0, 17), "杭州市": (30.2741, 120.1551, 2, 19),
           HOME: (POSITION["latitude"], POSITION["longitude"], 61, 21),
           RETRY_CITY: (34.76, 113.65, 0, 20)}
PROBE = """
localStorage.setItem('moss.voice.enabled', 'off');
window.weatherQA = {opens:[], states:[], prompts:[], stopped:[]};
window.addEventListener('moss-speech-stop', event => weatherQA.stopped.push(event.detail.id));
window.addEventListener('moss-agent-state', event => {
  const turn = event.detail.turns.at(-1);
  if (turn) weatherQA.states.push(turn);
});
const show = HTMLDialogElement.prototype.showModal;
HTMLDialogElement.prototype.showModal = function (...args) {
  if (this.id === 'weather-panel') weatherQA.opens.push(this.querySelector('h2').textContent);
  return show.apply(this,args);
};
const geolocate = navigator.geolocation?.getCurrentPosition.bind(navigator.geolocation);
if (geolocate) navigator.geolocation.getCurrentPosition = function (...args) {
  weatherQA.prompts.push(performance.now());
  return geolocate(...args);
};
"""


def sample(city, period, place, *, part_of_day, question):
    name = place["name"] if place else city
    lat, lon, code, low = OPTIONS[name]
    probability, rain = (65, .8) if name == HOME else (15, 0)
    days = [FIXTURE_DATE + timedelta(days=index) for index in range(3)]
    hours = [(day.replace(hour=hour)).isoformat(timespec="minutes") for day in days for hour in range(24)]
    readings = {"latitude": round(lat, 1), "longitude": round(lon, 1),
                "current": {"time": FIXTURE_DATE.replace(hour=14).isoformat(timespec="minutes"),
                            "temperature_2m": low + 4, "weather_code": code, "precipitation": rain, "wind_speed_10m": 9.4},
                "daily": {"time": [day.date().isoformat() for day in days], "weather_code": [code] * 3,
                          "temperature_2m_min": [low + index for index in range(3)],
                          "temperature_2m_max": [low + index + 8 for index in range(3)],
                          "precipitation_probability_max": [probability] * 3, "wind_speed_10m_max": [12] * 3,
                          "rain_sum": [rain] * 3, "showers_sum": [0] * 3},
                "hourly": {"time": hours, "temperature_2m": [low + 4] * len(hours), "weather_code": [code] * len(hours),
                           "precipitation_probability": [probability] * len(hours), "wind_speed_10m": [9.4] * len(hours),
                           "rain": [rain] * len(hours), "showers": [0] * len(hours)}}
    point = place or {"name": name, "latitude": lat, "longitude": lon, "kind": "city"}
    return format_weather(readings, point, period, part_of_day, question)


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("weather-popup-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    report = {"passed": False, "model_connections": 0, "external_requests": [], "speech_requests": [],
              "page_errors": [], "provider_calls": [], "cases": []}
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    forecast_waiting = asyncio.Event()
    deliver_forecast = asyncio.Event()
    retry_forecasts = []

    async def fetch(city, period, place, *, part_of_day=None, question=""):
        report["provider_calls"].append({"city": city, "period": period, "place": place, "part_of_day": part_of_day})
        if city == "杭州市" and period == "day_after_tomorrow" and part_of_day == "下午":
            forecast_waiting.set()
            await asyncio.wait_for(deliver_forecast.wait(), 5)
        requested_city = place["name"] if place else city
        if requested_city.removesuffix("市") == RETRY_CITY.removesuffix("市"):
            retry_forecasts.append({"city": RETRY_CITY, "period": period, "part_of_day": part_of_day,
                                    "question": question})
            if len(retry_forecasts) == 1:
                raise TimeoutError("isolated weather provider timeout")
            city = RETRY_CITY
            if place:
                place = {**place, "name": RETRY_CITY}
        return sample(city, period, place, part_of_day=part_of_day, question=question)

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(page, question, posts, *, spoken=False):
        before = len(posts)
        if spoken:
            await page.evaluate("""async text => {
              const script = [...document.scripts].find(tag => tag.src.includes('/static/agent-terminal.js'));
              await (await import(script.src)).submitRecognizedText(text);
            }""", question)
        else:
            composer = page.get_by_test_id("composer")
            await composer.fill(question)
            await page.get_by_test_id("send").click()
        row = await page.wait_for_function("""question => weatherQA.states.find(turn =>
            turn.user === question && ['replied','error'].includes(turn.status))""", arg=question)
        try:
            turn = await row.json_value()
        finally:
            await row.dispose()
        assert len(posts) == before + 1 and posts[-1]["text"] == question, posts
        assert turn["status"] == "replied" and turn["cloud_requests"]["total"] == 0, turn
        return turn, posts[-1]

    async def check_panel(page, turn, *, city, period):
        dialog = page.get_by_test_id("weather-panel")
        await expect(dialog).to_be_visible()
        assert await dialog.evaluate("node => node.open && node.matches(':modal') && node.getAttribute('aria-labelledby') === 'weather-title'")
        await expect(dialog.get_by_role("heading", level=2)).to_contain_text(city)
        await expect(dialog.locator(".weather-temperature")).to_contain_text("°C")
        await expect(dialog.locator(".weather-condition")).to_contain_text(turn["weather"]["condition"])
        icon = dialog.locator(".weather-condition .weather-icon")
        expected_icon = {"北京市":"sun", "杭州市":"partly", HOME:"rain", RETRY_CITY:"sun"}[city]
        await expect(icon).to_have_attribute("data-weather-icon",expected_icon)
        await expect(icon).to_have_attribute("aria-hidden","true")
        assert await dialog.locator(".weather-temperature").evaluate("node=>parseFloat(getComputedStyle(node).fontSize)>=25")
        gauge = dialog.locator(".weather-probability-gauge")
        if turn["weather"].get("precipitation_probability") is not None:
            expected = int(turn["weather"]["precipitation_probability"] / 5) * 5
            assert await gauge.evaluate("node=>node.style.getPropertyValue('--weather-fill')") == f"{expected}%"
        await expect(dialog.get_by_role("link", name="天气数据由 Open-Meteo 提供")).to_have_attribute("href", "https://open-meteo.com/")
        assert turn["weather"]["city"] == city and turn["weather"]["period"] == period, turn
        assert turn["weather"]["date"] in await dialog.locator("#weather-content").inner_text()
        size = await dialog.evaluate("""node => {
          const r=node.getBoundingClientRect(), content=node.querySelector('#weather-content');
          return {bounds:[r.left,r.top,r.right,r.bottom], size:[innerWidth,innerHeight],
            page:[document.documentElement.clientWidth,document.documentElement.scrollWidth],
            content:[content.clientWidth,content.scrollWidth]};
        }""")
        x, y, right, bottom = size["bounds"]
        width, height = size["size"]
        assert 0 <= x < right <= width + 1 and 0 <= y < bottom <= height + 1, size
        assert size["page"][1] <= size["page"][0] + 1 and size["content"][1] <= size["content"][0] + 1, size
        return dialog, size

    async def close_panel(page, dialog, focus):
        await page.keyboard.press("Escape")
        await expect(dialog).to_be_hidden()
        await expect(focus).to_be_focused()

    async def check_sibling(page, turn):
        await page.wait_for_function("id => weatherQA.states.some(turn => turn.id===id && turn.status==='replied')", arg=turn["id"])
        await expect(page.get_by_test_id("weather-panel")).to_be_hidden()
        assert await page.evaluate("weatherQA.opens.length") == 0

    async def open_tabs(browser, server, stack, *, width, granted):
        context = await browser.new_context(viewport={"width": width, "height": 900 if width > 375 else 812},
                                            geolocation={**POSITION, "accuracy": 24}, reduced_motion="reduce",
                                            has_touch=width == 375, service_workers="block")
        stack.push_async_callback(context.close)
        if granted:
            await context.grant_permissions(["geolocation"], origin=server.url)
        else:
            await context.grant_permissions([], origin=server.url)
        await context.add_init_script(PROBE)

        async def route_request(route):
            url = urlsplit(route.request.url)
            if url.scheme in {"http", "https"} and (url.hostname != "127.0.0.1" or url.port != server.port):
                report["external_requests"].append(route.request.url)
                await route.abort()
            elif url.path in {"/api/speak", "/api/speak-batch", "/api/prepare-speech"}:
                report["speech_requests"].append(url.path)
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_request)
        page, sibling = await context.new_page(), await context.new_page()
        posts = []
        page.on("request", lambda request: posts.append(request.post_data_json)
                if request.method == "POST" and urlsplit(request.url).path == "/api/message" else None)
        for tab in (page, sibling):
            tab.set_default_timeout(6500)
            tab.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await tab.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await tab.locator(".connection.online").wait_for()
            state = await tab.evaluate("navigator.permissions.query({name:'geolocation'}).then(p=>p.state)")
            assert state == ("granted" if granted else "denied"), state
        await page.bring_to_front()
        return page, sibling, posts

    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch("assistant_brain.fetch_weather", fetch))
            sink = await stack.enter_async_context(await asyncio.start_server(guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
                    os.environ[prefix + "_API_KEY"] = "isolated-qa"
            server = BrowserServer(folder, available_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "unused")
            stack.push_async_callback(moss.close)
            stop = asyncio.Event()
            app = application(moss, lambda: "standby", server.data, server.port, "weather-qa", stop)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", server.port).start()
            playwright = await stack.enter_async_context(async_playwright())
            browser = await open_browser(playwright)
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)

            async with AsyncExitStack() as desktop:
                page, sibling, posts = await open_tabs(browser, server, desktop, width=1440, granted=True)
                composer = page.get_by_test_id("composer")
                first, payload = await ask(page, NAMED, posts)
                assert "browser_location" not in payload
                panel, bounds = await check_panel(page, first, city="北京市", period="tomorrow")
                shot = folder / "weather-1440x900.png"
                await page.screenshot(path=shot, animations="disabled")
                await check_sibling(sibling, first)
                await close_panel(page, panel, composer)
                report["cases"].append({"case": "explicit forecast opens only on origin", "turn": first["id"], "bounds": bounds,
                                        "screenshot": str(shot)})

                moved, payload = await ask(page, "杭州市呢？", posts)
                assert "browser_location" not in payload
                panel, _ = await check_panel(page, moved, city="杭州市", period="tomorrow")
                await close_panel(page, panel, composer)
                report["cases"].append({"case": "named follow-up keeps the date and supplies no device coordinates"})

                local, payload = await ask(page, HERE, posts)
                assert payload["browser_location"] == POSITION and local["weather"]["location_kind"] == "browser", (payload, local)
                panel, _ = await check_panel(page, local, city=HOME, period="current")
                await check_sibling(sibling, local)
                shown = await page.evaluate("weatherQA.opens.length")
                await panel.get_by_role("link", name="天气数据由 Open-Meteo 提供").focus()
                next_day, payload = await ask(page, "明天呢？", posts, spoken=True)
                assert payload["browser_location"] == POSITION and next_day["weather"]["location_kind"] == "browser", (payload, next_day)
                panel, _ = await check_panel(page, next_day, city=HOME, period="tomorrow")
                await expect(panel.get_by_role("button", name="关闭")).to_be_focused()
                assert await page.evaluate("weatherQA.opens.length") == shown
                await check_sibling(sibling, next_day)
                await close_panel(page, panel, composer)
                umbrella, payload = await ask(page, "明天下午要不要带伞？", posts)
                assert payload["browser_location"] == POSITION and umbrella["weather"]["part_of_day"] == "下午"
                assert "建议带伞" in umbrella["reply"], umbrella
                panel, _ = await check_panel(page, umbrella, city=HOME, period="tomorrow")
                await expect(panel.locator(".weather-table tbody tr")).to_have_count(6)
                await close_panel(page, panel, composer)
                public = json.dumps(await server.state(), ensure_ascii=False)
                assert all(str(value) not in public for value in POSITION.values())
                report["cases"].append({"case": "granted location, tomorrow and umbrella advice are grounded without publishing exact coordinates"})

                count = len(report["provider_calls"])
                history = page.locator(f'[data-turn-id="{next_day["id"]}"]').get_by_test_id("open-weather")
                await history.focus()
                await page.keyboard.press("Enter")
                await check_panel(page, next_day, city=HOME, period="tomorrow")
                await close_panel(page, panel, history)
                assert await page.evaluate("weatherQA.opens.length") == 5
                assert len(posts) == 5 and len(report["provider_calls"]) == count
                report["cases"].append({"case": "keyboard reopen uses verified history without another request"})

                await ask(page, "北京市今天的天气怎么样？", posts)
                await expect(panel).to_be_visible()
                start = perf_counter()
                overview, _ = await ask(page, QUESTION, posts, spoken=True)
                status_panel = page.get_by_test_id("local-status-panel")
                await expect(status_panel).to_be_visible(timeout=3000)
                ms = round((perf_counter()-start)*1000, 1)
                await expect(panel).to_be_hidden()
                assert await page.locator("dialog[open]").count() == 1
                assert await status_panel.locator("[data-reading-kind]").evaluate_all(
                    "rows=>rows.map(row=>row.dataset.readingKind)"
                ) == [item["kind"] for item in overview["local_overview"]] == ["clock", "battery", "volume"]
                await sibling.wait_for_function("id=>weatherQA.states.some(turn=>turn.id===id)", arg=overview["id"])
                await expect(sibling.get_by_test_id("local-status-panel")).to_be_hidden()
                local_shot = folder / "voice-followup-local.png"
                await page.screenshot(path=local_shot, animations="disabled")
                report["cases"].append({"case":"spoken Windows readings replace automatic weather only on the originating tab",
                                        "elapsed_ms":ms, "screenshot":str(local_shot)})

                again, _ = await ask(page, "查一下电脑电量和系统音量。", posts, spoken=True)
                assert await status_panel.locator("[data-reading-kind]").evaluate_all(
                    "rows=>rows.map(row=>row.dataset.readingKind)"
                ) == [item["kind"] for item in again["local_overview"]] == ["battery", "volume"]
                reverse, _ = await ask(page, "杭州市今天的天气怎么样？", posts, spoken=True)
                await expect(status_panel).to_be_hidden()
                await check_panel(page, reverse, city="杭州市", period="today")
                await check_sibling(sibling, reverse)
                await close_panel(page, panel, composer)
                report["cases"].append({"case":"spoken follow-ups refresh the local panel and transfer back to weather"})

                await history.click()
                await check_panel(page, next_day, city=HOME, period="tomorrow")
                queued, _ = await ask(page, "系统音量多大、现在几点？", posts, spoken=True)
                await expect(status_panel).to_be_hidden()
                await check_panel(page, next_day, city=HOME, period="tomorrow")
                await close_panel(page, panel, history)
                reading_link = page.locator(f'[data-turn-id="{queued["id"]}"]').get_by_test_id("open-local-status")
                await reading_link.click()
                await expect(status_panel).to_be_visible()
                await close_panel(page, status_panel, reading_link)
                report["cases"].append({"case":"manually opened history retains focus until dismissal and new result is still usable"})

                await ask(page, "查询系统音量、电脑电量。", posts)
                await expect(status_panel).to_be_visible()
                pending = asyncio.create_task(ask(page, "杭州市后天下午天气怎么样？", posts, spoken=True))
                try:
                    await asyncio.wait_for(forecast_waiting.wait(), 4)
                    await close_panel(page, status_panel, composer)
                finally:
                    deliver_forecast.set()
                dismissed, _ = await pending
                await expect(panel).to_be_hidden()
                await expect(page.locator(f'[data-turn-id="{dismissed["id"]}"]').get_by_test_id("open-weather")).to_be_visible()
                report["cases"].append({"case":"Escape during a spoken follow-up leaves its result in chat without reopening a modal"})

                await page.get_by_role("button", name="便签", exact=True).click()
                notes = page.locator("#notes-panel")
                await expect(notes).to_be_visible()
                await notes.locator("#notes-body").fill("保留正在写的便签")
                await ask(page, "现在几点、系统音量多大？", posts, spoken=True)
                await expect(status_panel).to_be_hidden()
                await expect(notes).to_be_visible()
                await expect(notes.locator("#notes-body")).to_have_value("保留正在写的便签")
                await page.keyboard.press("Escape")
                await expect(notes).to_be_hidden()
                await page.reload(wait_until="domcontentloaded")
                await expect(page.locator(f'[data-turn-id="{dismissed["id"]}"]').get_by_test_id("open-weather")).to_be_visible()
                await expect(page.locator("dialog[open]")).to_have_count(0)
                report["cases"].append({"case":"existing modal and unsaved note survive a voice result, reload does not claim a popup"})

                await page.get_by_role("button", name="便签", exact=True).click()
                editing = notes.locator("#notes-body")
                await editing.fill("写到一半的便签")
                await editing.focus()
                status, state = await server.post("/api/message", {"text":"1秒后提醒我核对隔离通知"})
                assert status == 200
                timer, = state["timers"]
                notice = notes.get_by_test_id("timer-notice")
                await expect(notice).to_be_visible(timeout=4000)
                await expect(editing).to_be_focused()
                await expect(editing).to_have_value("写到一半的便签")
                assert await page.get_by_test_id("timer-notice").count() == 1
                original_due = notice.get_by_test_id("timer-due-time")
                await expect(original_due).to_contain_text(f"原定 {datetime.fromtimestamp(timer['due_at']).year}年")
                due_ms = await original_due.evaluate("node => Date.parse(node.dateTime)")
                assert abs(due_ms - timer["due_at"] * 1000) < 2
                await expect(notice.get_by_test_id("timer-source")).to_be_hidden()
                await notice.evaluate("node => { window.qaReminder = node; }")
                await page.set_viewport_size({"width":375, "height":812})
                assert await notice.evaluate("""node => {
                    const area = node.getBoundingClientRect();
                    return area.left >= 0 && area.right <= innerWidth &&
                        node.scrollWidth <= node.clientWidth;
                }""")
                shot = folder / "timer-in-note-375x812.png"
                await page.screenshot(path=shot, animations="disabled")
                await page.set_viewport_size({"width":1440, "height":900})
                await page.keyboard.press("Escape")
                await expect(notes).to_be_hidden()
                aside = page.get_by_test_id("timer-notice")
                assert await aside.evaluate("node => node === qaReminder && node.parentElement === document.body")
                await expect(aside.get_by_test_id("timer-source")).to_be_visible()

                old_weather = page.locator(f'[data-turn-id="{dismissed["id"]}"]').get_by_test_id("open-weather")
                await old_weather.click()
                await expect(panel).to_be_visible()
                inline_notice = panel.get_by_test_id("timer-notice")
                await expect(inline_notice).to_be_visible()
                assert await inline_notice.evaluate("node => node === qaReminder")
                await inline_notice.get_by_test_id("timer-dismiss").focus()
                await page.keyboard.press("Enter")
                await expect(aside).to_be_hidden()
                await expect(panel).to_be_visible()
                await expect(panel.get_by_role("button", name="关闭")).to_be_focused()
                assert not (await server.state())["timers"]
                await close_panel(page, panel, old_weather)
                report["cases"].append({"case":"a due reminder stays actionable inside any current modal, then restores focus",
                                        "screenshot":str(shot), "due_at_ms":due_ms})

                reminder_now = FIXTURE_DATE.replace(hour=14)

                class ReminderClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return reminder_now if tz is None else reminder_now.astimezone(tz)

                with patch("assistant_timers.datetime", ReminderClock), patch(
                        "assistant_timers.time", side_effect=lambda: reminder_now.timestamp()):
                    for text in ("2秒后提醒我复核已改期待办", "1秒后提醒我到期复查"):
                        status, state = await server.post("/api/message", {"text":text})
                        assert status == 200
                    other, linked = state["timers"]
                    assert (other["label"], linked["label"]) == ("到期复查", "复核已改期待办")
                    reminder_now += timedelta(seconds=3)
                    await page.clock.set_fixed_time(reminder_now)
                    # A full navigation initializes the same fragment Windows sends on toast activation.
                    url = server.url + "/?chat=1&scene=off"
                    deep_link = f"{url}#reminder={linked['id']}"
                    await page.goto(deep_link, wait_until="commit")
                    await page.reload(wait_until="domcontentloaded")
                    await expect(aside.locator("p")).to_have_text(linked["label"])
                    await expect(page).to_have_url(deep_link)
                    await page.get_by_role("button", name="便签", exact=True).click()
                    await editing.fill("改期期间保留草稿")
                    await editing.focus()
                    await expect(notice.locator("p")).to_have_text(linked["label"])

                    status, state = await server.post("/api/message", {
                        "text":f"把{linked['id']}号提醒改到一分钟后"})
                    assert status == 200 and state["timers"][0] == other
                    assert state["timers"][1]["due_at"] > reminder_now.timestamp()
                    await expect(notice.locator("p")).to_have_text(other["label"])
                    await expect(page).to_have_url(url)
                    await expect(editing).to_be_focused()
                    await expect(editing).to_have_value("改期期间保留草稿")
                    await close_panel(page, notes, page.get_by_role("button", name="便签", exact=True))

                    status, state = await server.post("/api/message", {"text":f"取消提醒 {linked['id']}"})
                    assert status == 200 and state["timers"] == [other]
                    await page.goto(deep_link, wait_until="commit")
                    await page.reload(wait_until="domcontentloaded")
                    await expect(aside.locator("p")).to_have_text(other["label"])
                    await expect(page).to_have_url(url)
                    await old_weather.click()
                    await expect(inline_notice.locator("p")).to_have_text(other["label"])
                    await expect(panel.get_by_role("button", name="关闭")).to_be_focused()
                    await inline_notice.get_by_test_id("timer-dismiss").click()
                    await expect(aside).to_be_hidden()
                    await expect(panel).to_be_visible()
                    assert not (await server.state())["timers"]
                    await close_panel(page, panel, old_weather)
                report["cases"].append({"case":"a rescheduled or cancelled Windows reminder link yields to other due timers",
                                        "uses_fixed_clock":True, "preserves_note_focus":True,
                                        "fallback_remains_actionable_inside_weather":True})

            code, _ = await server.post("/api/new-chat", {})
            assert code == 200
            async with AsyncExitStack() as mobile:
                page, sibling, posts = await open_tabs(browser, server, mobile, width=375, granted=False)
                before = len(report["provider_calls"])
                denied, payload = await ask(page, HERE, posts)
                assert "browser_location" not in payload and not denied.get("weather"), denied
                assert "城市" in denied["reply"] and len(report["provider_calls"]) == before
                await expect(page.get_by_test_id("weather-panel")).to_be_hidden()
                manual, payload = await ask(page, "北京市明天下午的天气怎么样？", posts)
                assert "browser_location" not in payload
                panel, bounds = await check_panel(page, manual, city="北京市", period="tomorrow")
                await expect(panel.locator(".weather-table tbody tr")).to_have_count(6)
                await expect(panel.locator(".weather-table [data-weather-icon='sun']")).to_have_count(6)
                await check_sibling(sibling, manual)
                shot = folder / "weather-375x812.png"
                await page.screenshot(path=shot, animations="disabled")
                assert await page.evaluate("weatherQA.prompts.length") == 0
                report["cases"].append({"case": "denied permission asks for location and named weather remains usable", "bounds": bounds,
                                        "screenshot": str(shot)})
                voice_label = panel.locator("#weather-voice-status")
                stop_label = panel.get_by_role("button", name="停止播报")
                await expect(voice_label).to_be_hidden()
                await expect(stop_label).to_be_hidden()
                await close_panel(page, panel, page.get_by_test_id("composer"))
                await page.locator(".preferences summary").click()
                await page.get_by_role("checkbox", name="MOSS 语音播报").check()
                await page.locator(".preferences summary").click()
                speech_ready = asyncio.Event()
                wav = (ROOT / "voice" / "lines" / "hello.wav").read_bytes()

                async def play_test_voice(route):
                    await asyncio.wait_for(speech_ready.wait(), 5)
                    await route.fulfill(status=200, content_type="audio/wav", body=wav)

                await page.route("**/api/speak", play_test_voice)
                try:
                    forecast, _ = await ask(page, "北京市后天的天气怎么样？", posts)
                    await check_panel(page, forecast, city="北京市", period="day_after_tomorrow")
                    await expect(voice_label).to_have_text("正在准备播报")
                    await expect(voice_label).to_be_visible()
                    await expect(stop_label).to_be_visible()
                    await expect(panel.get_by_role("button", name="关闭")).to_be_focused()
                    shot = folder / "weather-voice-375x812.png"
                    await panel.screenshot(path=shot, animations="disabled")
                finally:
                    speech_ready.set()
                await expect(voice_label).to_have_text("正在播报")
                await expect(voice_label).to_be_hidden()
                await close_panel(page, panel, page.get_by_test_id("composer"))
                history_link = page.locator(f'[data-turn-id="{forecast["id"]}"]').get_by_test_id("open-weather")
                await history_link.click()
                await expect(voice_label).to_be_hidden()
                await expect(stop_label).to_be_hidden()
                await close_panel(page, panel, history_link)
                report["cases"].append({"case":"forecast modal reports preparation and playback without changing focus",
                                        "screenshot":str(shot)})

                speech_ready.clear()
                pending_intercepted = asyncio.Event()

                async def pending_test_voice(route):
                    pending_intercepted.set()
                    await asyncio.wait_for(speech_ready.wait(), 5)
                    await route.abort()

                await page.route("**/api/speak", pending_test_voice)
                try:
                    pending, _ = await ask(page, NAMED, posts)
                    await check_panel(page, pending, city="北京市", period="tomorrow")
                    await asyncio.wait_for(pending_intercepted.wait(), 3)
                    await expect(voice_label).to_have_text("正在准备播报")
                    await expect(stop_label).to_be_visible()
                    hit_area = await stop_label.bounding_box()
                    assert hit_area and min(hit_area["width"], hit_area["height"]) >= 44, hit_area
                    await stop_label.press("Enter")
                    await expect(stop_label).to_be_hidden()
                    await expect(voice_label).to_be_hidden()
                    await expect(panel.get_by_role("button", name="关闭")).to_be_focused()
                    await check_panel(page, pending, city="北京市", period="tomorrow")
                    assert await page.evaluate("weatherQA.stopped") == [pending["id"]]
                finally:
                    speech_ready.set()
                    await page.unroute("**/api/speak", pending_test_voice)
                await close_panel(page, panel, page.get_by_test_id("composer"))

                status, _ = await ask(page, QUESTION, posts)
                status_panel = page.get_by_test_id("local-status-panel")
                status_label = status_panel.locator("#local-status-progress")
                stop_status = status_panel.get_by_role("button", name="停止播报")
                await expect(status_label).to_have_text("正在播报")
                await stop_status.click()
                await expect(stop_status).to_be_hidden()
                await expect(status_label).to_have_text("读取完成")
                await expect(status_panel.get_by_role("button", name="关闭")).to_be_focused()
                assert await status_panel.evaluate("node => node.matches(':modal')")
                assert await status_panel.locator("[data-reading-kind]").evaluate_all(
                    "rows=>rows.map(row=>row.dataset.readingKind)"
                ) == [item["kind"] for item in status["local_overview"]] == ["clock", "battery", "volume"]
                assert await page.evaluate("weatherQA.stopped") == [pending["id"], status["id"]]
                await close_panel(page, status_panel, page.get_by_test_id("composer"))
                report["cases"].append({"case":"keyboard stops pending forecast speech and pointer stops playing device speech",
                                        "weather_turn":pending["id"], "device_turn":status["id"],
                                        "preserves_results_and_returns_focus":True})

            code, _ = await server.post("/api/new-chat", {})
            assert code == 200
            async with AsyncExitStack() as retry:
                page, sibling, posts = await open_tabs(browser, server, retry, width=375, granted=False)
                clarification, initial = await ask(page, "明天下午的天气怎么样？", posts)
                assert not clarification.get("weather") and not retry_forecasts
                await expect(page.get_by_test_id("weather-panel")).to_be_hidden()

                failed, location_answer = await ask(page, "郑州", posts)
                assert not failed.get("weather") and len(retry_forecasts) == 1, failed
                assert (retry_forecasts[0]["period"], retry_forecasts[0]["part_of_day"]) == ("tomorrow", "下午")
                failed_row = page.locator(f'[data-turn-id="{failed["id"]}"]')
                await expect(failed_row.get_by_test_id("final")).to_contain_text(re.compile("重试|再试|再查"))
                await expect(failed_row.get_by_test_id("open-weather")).to_have_count(0)
                await expect(page.locator("dialog[open]")).to_have_count(0)

                restored, acceptance = await ask(page, "需要", posts)
                assert retry_forecasts == [retry_forecasts[0]] * 2, retry_forecasts
                assert all("browser_location" not in entry for entry in (initial, location_answer, acceptance)), posts
                assert await page.evaluate("weatherQA.prompts.length") == 0
                panel, bounds = await check_panel(page, restored, city=RETRY_CITY, period="tomorrow")
                assert restored["weather"]["part_of_day"] == "下午", restored
                await expect(panel.locator(".weather-table tbody tr")).to_have_count(6)
                await expect(page.locator("dialog[open]")).to_have_count(1)
                assert await page.evaluate("weatherQA.opens.length") == 1
                await check_sibling(sibling, restored)
                shot = folder / "weather-retry-375x812.png"
                await page.screenshot(path=shot, animations="disabled")
                await close_panel(page, panel, page.get_by_test_id("composer"))
                report["cases"].append({"case":"explicit acceptance retries the same city forecast after provider timeout",
                                        "provider_calls":retry_forecasts, "bounds":bounds, "screenshot":str(shot)})

            assert report["model_connections"] == 0
            assert not any(report[key] for key in ("external_requests", "speech_requests", "page_errors")), report
            stop.set()
            moss._notify()
            report["passed"] = True
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
