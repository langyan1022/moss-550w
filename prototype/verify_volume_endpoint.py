"""Compare Windows default-volume reads through the actual local HTTP and SSE routes."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from unittest.mock import Mock, patch

from aiohttp import ClientSession, web

from browser_lab import ROOT, confirm_muted_edge, open_browser
import desktop_actions as desktop
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_local_overview import QUESTION
from verify_local_overview_progressive import available_port


AUDIO = {"action": "get_volume"}
SINGLE = "扬声器音量多大？"


def legacy_read():
    device = desktop.AudioUtilities.GetSpeakers()
    return round(device.volume_percent), bool(device.EndpointVolume.GetMute())


def check_endpoints():
    first, second = Mock(), Mock()
    for device, level, muted in ((first, .367, 0), (second, .82, 1)):
        volume = device.Activate.return_value.QueryInterface.return_value
        volume.GetMasterVolumeLevelScalar.return_value = level
        volume.GetMute.return_value = muted
    api = Mock()
    api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.side_effect = [first, second, None]
    with patch.object(desktop, "AudioUtilities", api):
        assert desktop._run(AUDIO) == "系统音量为 37%。"
        assert desktop._run(AUDIO) == "系统音量为 82%，当前已静音。"
        try:
            desktop._run(AUDIO)
        except ValueError as exc:
            assert "没有默认" in str(exc)
        else:
            raise AssertionError("Missing audio endpoint was not reported")
        second.Activate.side_effect = desktop.COMError(-2147467259, "test activation failure", None)
        api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.side_effect = None
        api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.return_value = second
        try:
            desktop._run(AUDIO)
        except ValueError as exc:
            assert "未能完成" in str(exc)
        else:
            raise AssertionError("Endpoint activation failure was not reported")
    assert api.GetDeviceEnumerator.call_count == 4
    assert all(call.args == (desktop.EDataFlow.eRender.value, desktop.ERole.eMultimedia.value)
               for call in api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.call_args_list)
    first.Activate.assert_called_once_with(desktop.IAudioEndpointVolume._iid_, desktop.CLSCTX_ALL, None)
    api.GetSpeakers.assert_not_called()
    for device in (first, second):
        volume = device.Activate.return_value.QueryInterface.return_value
        volume.SetMasterVolumeLevelScalar.assert_not_called()
        volume.SetMute.assert_not_called()


def check_mute_parser():
    positive = {
        "系统静音": True, "把系统声音静音": True, "请让这台电脑静音吧！": True,
        "静音本机": True, "把电脑的音量设置为静音": True,
        "取消系统静音": False, "解除电脑的静音": False, "给本机声音取消静音": False,
        "帮我把系统音频解除静音。": False,
    }
    for phrase, muted in positive.items():
        assert desktop.parse_desktop(phrase) == {"action":"set_mute", "muted":muted}, phrase
    negative = (
        "静音", "取消静音", "给视频静音", "取消播放器静音", "暂停语音播报",
        "不要把系统静音", "他说‘系统静音’", "叫‘系统静音’的便签", "系统静音了吗？",
        "系统静音，然后暂停播放", "把微信静音", "关闭声音", "系统静音和电脑睡眠",
    )
    for phrase in negative:
        assert (desktop.parse_desktop(phrase) or {}).get("action") != "set_mute", phrase
    assert desktop.parse_desktop("音量多大？") == AUDIO
    assert desktop.parse_desktop("把系统音量调到百分之十五") == {"action":"set_volume", "level":15}
    outside_relative = ("再大一点", "把这个视频的音量调小一点", "不要把电脑音量调小一点",
                        "说一句‘把电脑音量调小一点’", "把电脑音量调小一点然后暂停音乐",
                        "电脑静音了吗？把音量调大一点", "电脑声音太小了", "怎么把电脑音量调小一点？")
    for phrase in outside_relative:
        action = (desktop.parse_desktop(phrase) or {}).get("action")
        assert action not in {"set_volume", "set_mute", "adjust_volume"}, phrase
    return {"matched":len(positive), "outside_scope":len(negative), "no_relative_write":len(outside_relative)}


def fake_volume_endpoint():
    endpoint, api = Mock(), Mock()
    api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.return_value = endpoint
    volume = endpoint.Activate.return_value.QueryInterface.return_value
    state = {"muted": False, "refuses_write": False, "level": .37}
    volume.GetMasterVolumeLevelScalar.side_effect = lambda: state["level"]
    volume.GetMute.side_effect = lambda: state["muted"]

    def set_mute(muted, _context):
        if not state["refuses_write"]:
            state["muted"] = bool(muted)

    def set_level(level, _context):
        if not state["refuses_write"]:
            state["level"] = level

    volume.SetMute.side_effect = set_mute
    volume.SetMasterVolumeLevelScalar.side_effect = set_level
    return api, endpoint, volume, state


async def check_mute_http(client, base, events):
    api, endpoint, volume, state = fake_volume_endpoint()
    checks = []

    async def ask(text, expected_status="replied"):
        started = perf_counter()
        async with client.post(base + "/api/message", json={"text":text}) as response:
            assert response.status == 200
            key = (await response.json())["turns"][-1]["id"]
        turn, _, final = await settle(events.content, key, started, expected_status)
        assert turn["source"] == "desktop" and turn["cloud_requests"]["total"] == 0, turn
        checks.append({"request":text, "status":turn["status"], "reply":turn["reply"], "error":turn["error"],
                       "writes":volume.SetMute.call_count, "final_ms":final})
        return turn

    with patch.object(desktop, "AudioUtilities", api):
        assert (await ask("系统静音"))["reply"] == "系统音量为 37%，当前已静音。"
        assert state["muted"] and volume.SetMute.call_count == 1
        assert (await ask("把电脑的声音静音吧"))["reply"] == "系统音量为 37%，当前已静音。"
        assert volume.SetMute.call_count == 1
        assert (await ask("取消系统静音"))["reply"] == "系统音量为 37%，当前未静音。"
        assert not state["muted"] and volume.SetMute.call_count == 2
        state["refuses_write"] = True
        failed = await ask("把系统声音静音", "error")
        assert not failed["reply"] and "未能核实" in failed["error"], failed
        assert not state["muted"] and volume.SetMute.call_count == 3
        api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.return_value = None
        missing = await ask("系统静音", "error")
        assert not missing["reply"] and "没有默认" in missing["error"], missing
        assert volume.SetMute.call_count == 3
    assert api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.call_count == 5
    assert endpoint.Activate.call_count == 4
    assert [call.args for call in volume.SetMute.call_args_list] == [(True, None), (False, None), (True, None)]
    api.GetSpeakers.assert_not_called()
    volume.SetMasterVolumeLevelScalar.assert_not_called()
    return checks


async def settle(stream, turn_id, started, expected_status="replied"):
    first = None
    async with asyncio.timeout(8):
        async for line in stream:
            if not line.startswith(b"data: "):
                continue
            turns = json.loads(line[6:])["turns"]
            if not turns or turns[-1]["id"] != turn_id:
                continue
            turn = turns[-1]
            elapsed = (perf_counter() - started) * 1000
            if first is None and (turn.get("partial_reply") or turn["reply"]):
                first = elapsed
            if turn["status"] != "thinking":
                assert turn["status"] == expected_status and (expected_status == "error" or first is not None), turn
                return turn, round(first, 3) if first is not None else None, round(elapsed, 3)
    raise AssertionError(f"Event stream closed before {turn_id}")


async def audio_turn(client, base, stream, text, status="replied"):
    start = perf_counter()
    async with client.post(base + "/api/message", json={"text":text}) as response:
        assert response.status == 200
        key = (await response.json())["turns"][-1]["id"]
    turn, first, elapsed = await settle(stream, key, start, status)
    assert turn["cloud_requests"]["total"] == 0 and not turn.get("media"), turn
    return turn, {"request":text, "first_ms":first, "final_ms":elapsed, "reply":turn["reply"], "error":turn["error"]}


async def check_mute_readback(client, base, stream):
    expected_level, expected_mute = legacy_read()
    answer = f"系统音量为 {expected_level}%，当前{'已' if expected_mute else '未'}静音。"
    checks = []
    for text, kinds in (("我电脑静音了吗？", ["volume"]), ("扬声器是静音的么？", ["volume"]),
                        ("电脑现在有没有静音", ["volume"]), ("音量多大？电脑静音了吗？", ["volume"]),
                        ("现在几点，系统静音了吗？", ["clock", "volume"]),
                        ("系统静音了吗现在几点", ["volume", "clock"])):
        turn, result = await audio_turn(client, base, stream, text)
        assert [row["kind"] for row in turn["local_overview"]] == kinds, turn
        assert next(row["reply"] for row in turn["local_overview"] if row["kind"] == "volume") == answer, turn
        assert not turn.get("desktop_audio_muted"), turn
        checks.append({**result, "readings":kinds})
    assert legacy_read() == (expected_level, expected_mute)
    return checks


async def check_relative_volume(client, base, stream):
    api, endpoint, volume, state = fake_volume_endpoint()
    checks = []
    cases = (
        ("把电脑音量调小一点。", .37, False, .32),
        ("电脑声音太小了，调大一点。", .32, False, .37),
        ("电脑声音再大一点。", .37, True, .42),
        ("把系统音量调高十个百分点。", .95, True, 1.),
        ("把系统音量调低九十个百分点。", .40, False, 0.),
        ("把电脑音量调大一点。", 1., False, 1.),
    )
    with patch.object(desktop, "AudioUtilities", api):
        for text, level, muted, result in cases:
            state.update(level=level, muted=muted)
            writes = volume.SetMasterVolumeLevelScalar.call_count
            turn, row = await audio_turn(client, base, stream, text)
            expected = f"系统音量为 {round(result * 100)}%，当前{'已' if muted else '未'}静音。"
            assert turn["source"] == "desktop" and turn["reply"] == expected, turn
            assert turn["local_overview"] == [{"kind":"volume", "reply":expected}], turn
            assert abs(state["level"] - result) < 1e-6 and state["muted"] is muted, state
            assert volume.SetMasterVolumeLevelScalar.call_count == writes + (level != result)
            checks.append({**row, "initial":level, "measured":state["level"], "muted":state["muted"]})
        state.update(level=.37, refuses_write=True)
        failed, row = await audio_turn(client, base, stream, "把电脑音量调小一点。", "error")
        assert not failed["reply"] and not failed.get("local_overview") and "音量" in failed["error"], failed
        assert state["level"] == .37
        checks.append(row)
        api.GetDeviceEnumerator.return_value.GetDefaultAudioEndpoint.return_value = None
        missing, row = await audio_turn(client, base, stream, "把电脑音量调小一点。", "error")
        assert not missing["reply"] and "没有默认" in missing["error"], missing
        checks.append(row)
    assert endpoint.Activate.call_count == len(checks) - 1
    volume.SetMute.assert_not_called()
    api.GetSpeakers.assert_not_called()
    return checks


async def check_volume_browser(base, folder, candidate_app=None):
    from playwright.async_api import async_playwright, expect

    api, _, volume, state = fake_volume_endpoint()
    screenshots = []
    async with async_playwright() as playwright:
        browser = await open_browser(playwright)
        try:
            await confirm_muted_edge(browser)
            with patch.object(desktop, "AudioUtilities", api):
                for width in (1440, 768, 375):
                    context = await browser.new_context(viewport={"width":width, "height":864},
                                                        reduced_motion="reduce", service_workers="block")
                    try:
                        async def route_request(route):
                            url = route.request.url
                            if not url.startswith(base + "/"):
                                await route.abort()
                            elif candidate_app and url.split("?", 1)[0] == base + "/static/app.js":
                                await route.fulfill(body=candidate_app.read_bytes(), content_type="text/javascript; charset=utf-8")
                            else:
                                await route.continue_()

                        await context.route("**/*", route_request)
                        page = await context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.add_init_script('localStorage.setItem("moss.voice.enabled", "off")')
                        await page.goto(base + "/?chat=1&scene=off")
                        composer = page.get_by_test_id("composer")
                        submit = page.get_by_test_id("send")
                        panel = page.get_by_test_id("local-status-panel")
                        row = panel.locator('[data-reading-kind="volume"]')
                        state.update(muted=False, level=.37)
                        for text, value, detail in (("把电脑音量调小一点。", "32%", "当前未静音"),
                                                    ("我电脑静音了吗？", "32%", "当前已静音")):
                            state["muted"] = detail == "当前已静音"
                            await composer.fill(text)
                            await submit.click()
                            await expect(panel).to_be_visible()
                            await expect(panel).to_have_attribute("data-phase", "complete")
                            await expect(row.locator(".status-reading-value")).to_have_text(value)
                            note = row.locator(".status-reading-detail")
                            await expect(note).to_have_text(detail)
                            assert (await note.get_attribute("data-muted") is not None) is state["muted"]
                            name = f"系统音量-{width}-{detail}.png"
                            await panel.screenshot(path=folder / name, animations="disabled")
                            screenshots.append(name)
                            await page.keyboard.press("Escape")
                            await expect(panel).to_be_hidden()
                        assert not errors, errors
                    finally:
                        await context.close()
        finally:
            await browser.close()
    assert volume.SetMasterVolumeLevelScalar.call_count == 3
    volume.SetMute.assert_not_called()
    return screenshots


async def run(mute_only=False, browser=False, candidate_app=None):
    folder = ROOT / "runs" / datetime.now().strftime("volume-endpoint-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0"}
    outcome = folder / "result.json"
    report = {"passed": False, "model_connections": 0, "fixtures": [], "samples": []}
    print(outcome, flush=True)

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    fast_read = desktop._read_default_volume
    try:
        check_endpoints()
        report["mute_parser"] = check_mute_parser()
        report["fixtures"] = ["different default devices", "unmuted and muted", "missing endpoint", "activation error", "no writes"]
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            sink = await stack.enter_async_context(await asyncio.start_server(guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
                    os.environ[prefix + "_API_KEY"] = "isolated-qa"
            data = folder / "data"
            moss = Moss(data, model_url, "unused")
            stack.push_async_callback(moss.close)
            stop = asyncio.Event()
            port = available_port()
            app = application(moss, lambda: "standby", data, port, "volume-qa", stop)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", port).start()
            base = f"http://127.0.0.1:{port}"
            client = await stack.enter_async_context(ClientSession())
            events = await stack.enter_async_context(client.get(base + "/api/events"))
            assert events.status == 200 and events.content_type == "text/event-stream"

            async def ask(mode, text):
                async with client.post(base + "/api/new-chat", json={}) as response:
                    assert response.status == 200
                read = legacy_read if mode == "previous" else fast_read
                with patch.object(desktop, "_read_default_volume", read):
                    start = perf_counter()
                    async with client.post(base + "/api/message", json={"text": text}) as response:
                        assert response.status == 200
                        key = (await response.json())["turns"][-1]["id"]
                    turn, first, final = await settle(events.content, key, start)
                assert turn["cloud_requests"]["total"] == 0, turn
                if text == SINGLE:
                    assert turn["source"] == "desktop" and not turn.get("local_overview")
                    value = turn["reply"]
                else:
                    items = turn["local_overview"]
                    assert turn["source"] == "context" and [item["kind"] for item in items] == ["clock", "battery", "volume"]
                    value = items[-1]["reply"]
                return {"mode": mode, "request": "volume" if text == SINGLE else "overview", "first_ms": first,
                        "final_ms": final, "volume": value}

            try:
                report["mocked_desktop_mute"] = await check_mute_http(client, base, events)
                report["mocked_relative_volume"] = await check_relative_volume(client, base, events.content)
                if not mute_only:
                    report["real_mute_state"] = await check_mute_readback(client, base, events.content)
                    await ask("previous", SINGLE)
                    await ask("direct", SINGLE)
                    for mode in ("previous", "direct", "direct", "previous") * 5:
                        for text in (SINGLE, QUESTION):
                            report["samples"].append(await ask(mode, text))
                if browser:
                    report["screenshots"] = await check_volume_browser(base, folder, candidate_app)
                    if candidate_app:
                        report["candidate_app_sha256"] = sha256(candidate_app.read_bytes()).hexdigest()
            finally:
                stop.set()
                moss._notify()

        assert not report["model_connections"]
        if not mute_only:
            assert len({row["volume"] for row in report["samples"]}) == 1
            report["median_ms"] = {}
            for request in ("volume", "overview"):
                report["median_ms"][request] = {}
                for mode in ("previous", "direct"):
                    rows = [row for row in report["samples"] if row["request"] == request and row["mode"] == mode]
                    report["median_ms"][request][mode] = {key: round(median(row[key] for row in rows), 3)
                                                          for key in ("first_ms", "final_ms")}
        report["passed"] = True
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mute-only", action="store_true", help="仅运行模拟 Windows 音频设备的静音路由测试")
    parser.add_argument("--browser", action="store_true", help="使用静音 Edge 核对系统音量弹窗")
    parser.add_argument("--candidate-app", type=Path, help="静音 Edge 使用的候选 app.js")
    args = parser.parse_args()
    asyncio.run(run(args.mute_only, args.browser, args.candidate_app))
