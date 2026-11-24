import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from random import Random
import secrets
from shutil import copyfile
import socket
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
RECOVERY_GATE = """
    const OriginalEventSource = window.EventSource;
    window.recoveryGate = {turn:null, held:0, release() {
      this.open = true;
      for (const replay of this.waiting.splice(0)) replay();
    }, waiting:[], open:false};
    window.EventSource = class extends OriginalEventSource {
      addEventListener(name, listener, options) {
        if (name !== 'state') return super.addEventListener(name, listener, options);
        return super.addEventListener(name, event => {
          const gate = window.recoveryGate;
          if (!gate.open && gate.turn && JSON.parse(event.data).turns.some(row =>
              row.id === gate.turn && row.media?.recovered_from)) {
            gate.held++;
            gate.waiting.push(() => listener.call(this, event));
          } else listener.call(this, event);
        }, options);
      }
    };
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve_share(folder, port, ready):
    from impacket.ntlm import compute_lmhash, compute_nthash
    from impacket.smbserver import SimpleSMBServer
    server = SimpleSMBServer(listenAddress="127.0.0.1", listenPort=port)
    server.setSMB2Support(True)
    server.addShare("MEDIA", str(folder), readOnly="yes")
    password = os.environ["MOSS_QA_SMB_PASSWORD"]
    server.addCredential(os.environ["MOSS_QA_SMB_USER"], 0, compute_lmhash(password), compute_nthash(password))
    open_file = os.open

    def open_binary(path, flags, *args, **kwargs):
        # Impacket replaces the Windows flags with O_RDONLY for a read-only share.
        return open_file(path, flags | getattr(os, "O_BINARY", 0), *args, **kwargs)

    ready.write_text(json.dumps({"pid":os.getpid(), "parent":os.getppid()}), encoding="utf-8")
    with patch("impacket.smbserver.os.open", new=open_binary):
        server.start()


@contextmanager
def share(folder, port, attempt):
    import psutil

    ready = folder.parent / f"smb-{attempt}.pid"
    with (folder.parent / f"smb-{attempt}.log").open("wb") as log:
        python = ROOT.parent/"alternatives/hiroute/media_library/.venv/Scripts/python.exe"
        proc = subprocess.Popen([str(python), str(Path(__file__).resolve()), "--share", str(folder),
                                 "--port", str(port), "--ready", str(ready)], stdout=log, stderr=log,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        worker = None

        def stop():
            if worker is not None:
                if worker.is_running():
                    worker.terminate()
                    worker.wait(10)
            elif proc.poll() is None:
                proc.terminate()
            proc.wait(10)

        try:
            deadline = monotonic()+12
            while not ready.is_file():
                if proc.poll() is not None or monotonic() >= deadline:
                    raise RuntimeError(f"隔离 SMB 未能启动，参见 {log.name}")
                sleep(.05)
            owner = json.loads(ready.read_text(encoding="utf-8"))
            assert proc.pid in {owner["pid"], owner["parent"]}
            candidate = psutil.Process(owner["pid"])
            assert candidate.pid == proc.pid or candidate.ppid() == proc.pid
            worker = candidate
            yield worker.pid, stop
        finally:
            stop()


@contextmanager
def move_local_during_read(path):
    parked = path.with_suffix(".temporarily-unavailable")
    assert path.is_file() and not parked.exists(), path
    try:
        yield lambda: path.rename(parked)
    finally:
        if parked.exists():
            parked.rename(path)


async def browser_receipt(http, port, text, media_id, set_scope, *, fault=None, expected_source="nas",
                          expect_failure=False, stop_race=False):
    from playwright.async_api import async_playwright
    from browser_lab import confirm_muted_edge, open_browser

    origin = f"http://127.0.0.1:{port}"
    report = {"browser_errors": [], "blocked_requests": [], "media_responses": [], "media_events": [],
              "messages": [], "recovery_requests": []}
    fault_lock = asyncio.Lock()
    recovery_committed = asyncio.Event()
    release_recovery = asyncio.Event()

    async def snapshot():
        async with http.get("/api/state") as response:
            response.raise_for_status()
            return await response.json()

    async with async_playwright() as playwright:
        browser = await open_browser(playwright)
        try:
            await confirm_muted_edge(browser)
            page = await browser.new_page(reduced_motion="reduce", service_workers="block")
            await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            if stop_race:
                report["replacement_reads"] = []
                await page.add_init_script(RECOVERY_GATE)
            page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
            def record(response):
                path = urlsplit(response.url).path
                if path.startswith("/media/"):
                    report["media_responses"].append([path, response.status])
                if path == "/api/media-event":
                    report["media_events"].append({**response.request.post_data_json, "status":response.status})
            page.on("response", record)
            def record_request(request):
                if stop_race and urlsplit(request.url).path == "/media/" + media_id:
                    report["replacement_reads"].append(request.method)
                if request.method != "POST":
                    return
                if request.url == origin + "/api/message":
                    report["messages"].append(request.post_data_json["text"])
                if request.url == origin + "/api/media-event" and request.post_data_json.get("recover") is True:
                    report["recovery_requests"].append(request.post_data_json)
            page.on("request", record_request)

            async def local_only(route):
                absolute = urlsplit(route.request.url)
                if (absolute.scheme in {"http", "https"} and absolute.netloc != urlsplit(origin).netloc) or \
                        absolute.path in {"/api/speak", "/api/speak-batch", "/api/prepare-speech"}:
                    report["blocked_requests"].append(absolute.path)
                    await route.abort()
                    return
                if fault and route.request.method == "GET" and absolute.path == "/media/" + fault[0]:
                    async with fault_lock:
                        if "first_selection" not in report:
                            initial_id, _, inject = fault
                            turn = (await snapshot())["turns"][-1]
                            assert turn["id"] not in previous and turn["user"] == text and turn["media"]["autoplay"], turn
                            assert turn["media"]["selected"]["media_id"] == initial_id, turn
                            if stop_race:
                                await page.evaluate("id => { recoveryGate.turn = id; }", turn["id"])
                            await asyncio.to_thread(inject)
                            report["first_selection"] = {"turn_id":turn["id"], "media_id":initial_id,
                                                          "range":route.request.headers.get("range")}
                if (stop_race and route.request.method == "POST" and absolute.path == "/api/media-event" and
                        route.request.post_data_json.get("recover") is True):
                    upstream = await route.fetch()
                    committed = (await upstream.json())["turns"][-1]
                    assert upstream.status == 200 and committed["id"] == report["first_selection"]["turn_id"], committed
                    assert committed["media"]["selected"]["media_id"] == media_id and not committed["browser_playback"], committed
                    report["recovery_committed"] = {"turn_id":committed["id"], "media_id":media_id,
                                                     "recovered_from":committed["media"]["recovered_from"]}
                    recovery_committed.set()
                    await release_recovery.wait()
                    await route.fulfill(response=upstream)
                    return
                await route.continue_()

            await page.route("**/*", local_only)
            await page.goto(origin+"/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            previous = {row["id"] for row in (await snapshot())["turns"]}
            set_scope("静音 Edge 实际开播")
            expected_event = "failed" if expect_failure else "started"
            try:
                if stop_race:
                    assert fault and not expect_failure
                    await page.get_by_test_id("composer").fill(text)
                    await page.get_by_test_id("send").click()
                    await asyncio.wait_for(recovery_committed.wait(), 10)
                    await page.wait_for_function("recoveryGate.held>0", timeout=5000)
                    report["withheld_state_events_before_stop"] = await page.evaluate("recoveryGate.held")
                    played = report["first_selection"]
                    entry = page.locator(f'audio[data-audio-turn="{played["turn_id"]}"]').last
                    assert await entry.get_attribute("data-media-id") == played["media_id"], report
                    stop = "停止播放"
                    async with page.expect_response(lambda response: response.url == origin + "/api/message" and
                            response.request.post_data_json.get("text") == stop, timeout=7000) as stopping:
                        await page.get_by_test_id("composer").fill(stop)
                        await page.get_by_test_id("send").click()
                    response = await stopping.value
                    report["stop_status"] = response.status
                    report["stop_target"] = response.request.post_data_json.get("queued_player")
                    answer = await response.json()
                    report["stop_body"] = answer.get("error") if not response.ok else answer["turns"][-1]
                    assert report["stop_target"] == {"turn_id":played["turn_id"], "media_id":played["media_id"]}, report
                    assert response.ok, report
                    stop_id = report["stop_body"]["id"]
                    reply = page.locator(f'[data-turn-id="{stop_id}"]').get_by_test_id("final")
                    await reply.wait_for(timeout=7000)
                    assert "已取消待播放" in await reply.inner_text(), report
                    stop_turn = (await snapshot())["turns"][-1]
                    assert stop_turn["transport"]["event"] == "queue_cancelled" and stop_turn["transport"]["cancelled_queue"], stop_turn
                    report["stop_receipt"] = stop_turn["transport"]
                    report["stop_reply"] = stop_turn["reply"]
                    async with page.expect_response(lambda response: response.url == origin + "/api/media-event" and
                            response.request.post_data_json.get("recover") is True, timeout=5000) as released:
                        release_recovery.set()
                        await page.evaluate("recoveryGate.release()")
                    assert (await released.value).ok
                    await page.wait_for_timeout(450)
                    turns = [row for row in (await snapshot())["turns"] if row["id"] not in previous]
                    assert len(turns) == 2 and turns[0]["id"] == played["turn_id"], turns
                    assert not any((row.get("browser_playback") or {}).get("event") == "started" for row in turns)
                    assert not any(row["event"] == "started" for row in report["media_events"]), report
                    assert ["/media/" + fault[0], fault[1]] in report["media_responses"], report
                    assert not report["replacement_reads"] and len(report["recovery_requests"]) == 1, report
                    assert report["messages"] == [text, stop] and not report["browser_errors"] and not report["blocked_requests"], report
                    return report
                async with page.expect_response(
                    lambda response: response.url == origin + "/api/media-event" and
                    (response.request.post_data_json or {}).get("event") == expected_event,
                    timeout=10000 if fault else 30000,
                ) as event:
                    await page.get_by_test_id("composer").fill(text)
                    await page.get_by_test_id("send").click()
                response = await event.value
                payload = response.request.post_data_json
                assert response.status == 200 and payload["media_id"] == media_id, payload
                if not expect_failure:
                    view = await page.wait_for_function("""({turn, id}) => {
                    const node = [...document.querySelectorAll('audio.media-audio')].find(
                        audio => audio.dataset.audioTurn === turn && audio.dataset.mediaId === id);
                    if (!node || node.currentTime < .12 || node.readyState < 2) return null;
                    return {path:new URL(node.currentSrc).pathname, current_time:node.currentTime,
                        ready_state:node.readyState, duration:node.duration};
                    }""", arg={"turn":payload["turn_id"],"id":media_id})
                    report["audio"] = await view.json_value()
                    assert report["audio"]["path"] == "/media/" + media_id, report
                else:
                    await page.wait_for_timeout(750)
                    assert not any(event["event"] == "started" for event in report["media_events"]), report
                state = await snapshot()
                turn, = [row for row in state["turns"] if row["id"] not in previous]
                assert turn["id"] == payload["turn_id"] and report["messages"] == [text], report
                receipt = turn["browser_playback"]
                assert receipt["event"] == expected_event and receipt["media_id"] == media_id
                assert receipt["play_id"] == payload.get("play_id")
                item = turn["media"]["selected"]
                assert item["media_id"] == media_id and item["source_kind"] == expected_source
                assert not turn["media"]["online_calls"]
                assert turn["cloud_requests"]["total"] == 0
                assert not report["browser_errors"] and not report["blocked_requests"]
                if fault:
                    original_id, failed_status, _ = fault
                    original_path = "/media/" + original_id
                    assert report["first_selection"]["turn_id"] == turn["id"]
                    assert [original_path, failed_status] in report["media_responses"], report
                    if expect_failure:
                        assert all(path == original_path for path, _ in report["media_responses"]), report
                        assert not report["recovery_requests"], report
                    else:
                        assert ["/media/" + media_id, 206] in report["media_responses"], report
                        old = {"chat_id":turn["chat_id"], "turn_id":turn["id"], "media_id":original_id,
                               "event":"started", "play_id":secrets.token_hex(16),
                               "started_at_us":int(datetime.now().timestamp()*1_000_000)}
                        async with http.post("/api/media-event", json=old) as delayed:
                            report["delayed_initial_started"] = delayed.status
                            assert delayed.status == 409, await delayed.text()
                        revised = next(row for row in (await snapshot())["turns"] if row["id"] == turn["id"])
                        assert revised["browser_playback"] == receipt and revised["media"]["selected"]["media_id"] == media_id, revised
                else:
                    assert turn["media"]["consulted_sources"] == ["disk","nas"] and turn["media"]["unsupported_audio"] == [".dsf"]
                    assert report["media_responses"] and all(path=="/media/"+media_id and status in {200,206}
                        for path,status in report["media_responses"]), report["media_responses"]
                report["receipt"] = receipt
                report["turn"] = turn["id"]
                report["selected"] = {key:item[key] for key in ("media_id", "source_kind", "artist", "title")}
                return report
            except Exception as error:
                raise AssertionError(report) from error
            finally:
                release_recovery.set()
                set_scope("")
        finally:
            await browser.close()


async def browser_midstream_receipt(http, port, text, primary_id, fallback_id, local, moss, set_scope,
                                   *, scenario="interrupted", new_track=None):
    from playwright.async_api import Error, async_playwright
    from browser_lab import confirm_muted_edge, open_browser
    from local_media.library import SourceUnavailableError

    origin = f"http://127.0.0.1:{port}"
    primary_path, fallback_path = "/media/" + primary_id, "/media/" + fallback_id
    follows_clock = scenario in {"after_clock", "stop_after_clock", "pause_after_clock", "change_after_clock"}
    follows_weather = scenario == "after_weather"
    cross_turn = follows_clock or follows_weather
    resumes = scenario in {"matched", "after_clock", "after_weather"}
    prefix_bytes = (768 if cross_turn else 256) * 1024
    waiting, disconnect = Event(), Event()
    media = moss.media
    original_open = media._open_media
    parked = local.with_suffix(".temporarily-unavailable")
    first_get_lock = asyncio.Lock()
    committed, release_recovery, recovery_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stale_request = asyncio.Event()
    stop_race = scenario in {"stop", "stop_after_clock", "pause_after_clock"}
    change_race = scenario == "change_after_clock"
    report = {
        "method": "实际播放超过一秒时在隔离 SMB 的读取层注入断流。",
        "scenario": scenario,
        "prefix_bytes": prefix_bytes, "source_read_sizes": [], "media_requests": [], "media_responses": [],
        "events": [], "playback_reports": [], "recovery_requests": [],
        "blocked_requests": [], "browser_errors": [], "injected_reads": [],
    }

    class InterruptedReader:
        def __init__(self, source):
            self.source = source
            self.remaining = prefix_bytes

        def seek(self, *args):
            return self.source.seek(*args)

        def close(self):
            self.source.close()

        def read(self, size):
            if self.remaining:
                data = self.source.read(min(size, self.remaining))
                report["source_read_sizes"].append(len(data))
                self.remaining -= len(data)
                return data
            waiting.set()
            if not disconnect.wait(20):
                raise OSError("等待浏览器实际开播超时")
            report["injected_reads"].append("smb_stream_read")
            raise OSError("隔离验收注入共享媒体读取故障")

    def open_media(key):
        if key != primary_id:
            return original_open(key)
        if disconnect.is_set():
            raise SourceUnavailableError("隔离验收注入共享媒体重新打开失败")
        source, mime = original_open(key)
        raw = getattr(source, "raw", source)
        report["source_handle"] = f"{type(raw).__module__}.{type(raw).__name__}"
        return InterruptedReader(source), mime

    async def current_turn(turn_id):
        async with http.get("/api/state") as response:
            response.raise_for_status()
            return next(row for row in (await response.json())["turns"] if row["id"] == turn_id)

    async def submit_message(page, message):
        async with page.expect_response(lambda r: r.url == origin + "/api/message" and
                r.request.post_data_json.get("text") == message, timeout=7000) as submitted:
            await page.get_by_test_id("composer").fill(message)
            await page.get_by_test_id("send").click()
        response = await submitted.value
        assert response.ok, await response.text()
        return response.request.post_data_json, (await response.json())["turns"][-1]["id"]

    assert local.is_file() and not parked.exists(), local
    local.rename(parked)
    media._open_media = open_media
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce", service_workers="block")
                await page.add_init_script("""
                    localStorage.setItem('moss.voice.enabled', 'off');
                    window.mossMidstreamEvents = [];
                    for (const type of ['playing', 'waiting', 'pause', 'error', 'ended']) {
                      document.addEventListener(type, event => {
                        const audio = event.target;
                        if (!audio.matches?.('audio.media-audio') || !event.isTrusted) return;
                        mossMidstreamEvents.push({
                          type, at_ms:performance.now(), media_id:audio.dataset.mediaId, turn_id:audio.dataset.audioTurn,
                          position_s:audio.currentTime, error:audio.error?.code ?? null
                        });
                      }, true);
                    }
                """)
                if stop_race:
                    await page.add_init_script(RECOVERY_GATE)
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))

                async def allow_local(route):
                    target = urlsplit(route.request.url)
                    if (target.scheme in {"http", "https"} and target.netloc != urlsplit(origin).netloc) or \
                            target.path in {"/api/speak", "/api/speak-batch", "/api/prepare-speech"}:
                        report["blocked_requests"].append(target.path)
                        await route.abort()
                        return
                    if target.path.startswith("/media/"):
                        report["media_requests"].append({"path":target.path, "range":route.request.headers.get("range")})
                    if target.path == primary_path and route.request.method == "GET":
                        async with first_get_lock:
                            if "first_selection" not in report:
                                async with http.get("/api/state") as snapshot:
                                    state = await snapshot.json()
                                assert state.get("media_resume") is True
                                turn = state["turns"][-1]
                                assert turn["user"] == text and turn["media"]["selected"]["media_id"] == primary_id, turn
                                assert not turn.get("browser_playback"), turn
                                if scenario != "interrupted":
                                    parked.rename(local)
                                if stop_race:
                                    await page.evaluate("id => { recoveryGate.turn = id; }", turn["id"])
                                report["first_selection"] = {"turn_id":turn["id"], "media_id":primary_id,
                                                             "alternate_ready":local.is_file()}
                    if target.path == "/api/media-event" and route.request.method == "POST":
                        payload = route.request.post_data_json
                        if payload.get("recover") is True:
                            report["recovery_requests"].append(payload)
                            if change_race:
                                stale_request.set()
                                await release_recovery.wait()
                                try:
                                    await route.abort()
                                except Error:
                                    pass
                                finally:
                                    recovery_finished.set()
                                return
                            if stop_race:
                                try:
                                    upstream = await route.fetch()
                                    state = await upstream.json()
                                    selected = next(row for row in state["turns"] if row["id"] == payload["turn_id"])
                                    assert upstream.ok and selected["media"]["selected"]["media_id"] == fallback_id, selected
                                    assert selected["media"]["recovered_from"] == primary_id and not selected["browser_playback"], selected
                                    report["recovery_committed"] = {"turn_id":selected["id"],
                                        "media_id":fallback_id, "position_s":selected["media"]["resume_position_s"]}
                                    committed.set()
                                    await release_recovery.wait()
                                    try:
                                        await route.fulfill(response=upstream)
                                        report["held_recovery_response"] = "released"
                                    except Error:
                                        # Stopping may already have aborted the requesting fetch.
                                        report["held_recovery_response"] = "aborted"
                                finally:
                                    recovery_finished.set()
                                return
                    await route.continue_()

                def playback_report(response, event, turn_id=None, *, media_id=primary_id, recover=False):
                    request = response.request
                    if request.method != "POST" or request.url != origin + "/api/media-event":
                        return False
                    body = request.post_data_json or {}
                    return (body.get("event") == event and body.get("media_id") == media_id
                            and (turn_id is None or body.get("turn_id") == turn_id)
                            and (not recover or body.get("recover") is True))

                def record_response(response):
                    path = urlsplit(response.url).path
                    if path.startswith("/media/"):
                        report["media_responses"].append({"path":path, "status":response.status,
                                                          "content_range":response.headers.get("content-range")})
                    if response.request.method == "POST" and response.url == origin + "/api/media-event":
                        report["playback_reports"].append({**response.request.post_data_json, "status":response.status})

                page.on("response", record_response)
                await page.route("**/*", allow_local)
                await page.goto(origin + "/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for()
                set_scope("静音 Edge 共享播放中断")
                async with page.expect_response(lambda r: playback_report(r, "started"), timeout=20000) as start:
                    await page.get_by_test_id("composer").fill(text)
                    await page.get_by_test_id("send").click()
                response = await start.value
                assert response.ok, await response.text()
                turn_id = response.request.post_data_json["turn_id"]
                before = await current_turn(turn_id)
                assert before["media"]["selected"]["source_kind"] == "nas" and before["media"]["retry_track_source"], before
                assert before["browser_playback"]["play_id"] == response.request.post_data_json["play_id"], before
                job = moss._track_resume_job
                if scenario == "interrupted":
                    assert not report["first_selection"]["alternate_ready"], report
                else:
                    assert report["first_selection"]["alternate_ready"], report
                    assert job is not None, report
                if job is not None:
                    proved = bool(await asyncio.wait_for(asyncio.shield(job), 5))
                    report["proved_same_bytes"] = proved
                    assert proved == (scenario not in {"different", "interrupted"}), report
                playing = await page.wait_for_function("""({id, turn}) => {
                    const audio = [...document.querySelectorAll('audio.media-audio')].find(node =>
                        node.dataset.mediaId === id && node.dataset.audioTurn === turn);
                    if (!audio || audio.paused || !audio.played.length || audio.currentTime < 1) return null;
                    return {position_s:audio.currentTime, duration_s:audio.duration, at_ms:performance.now(),
                            path:new URL(audio.currentSrc).pathname};
                }""", arg={"id":primary_id,"turn":turn_id}, timeout=10000)
                report["before_failure"] = await playing.json_value()
                assert report["before_failure"]["path"] == primary_path, report
                assert await asyncio.to_thread(waiting.wait, 5), "正式流读取还没有到达注入点"
                report["before_failure"]["smb_bytes_read"] = sum(report["source_read_sizes"])
                assert report["source_handle"].startswith("smbclient.") and report["before_failure"]["smb_bytes_read"] > 16 * 1024, report
                assert any(row["path"] == primary_path and row["status"] == 206 and row["content_range"]
                           for row in report["media_responses"]), report
                if parked.exists():
                    parked.rename(local)
                source_audio = page.locator(f'audio[data-audio-turn="{turn_id}"][data-media-id="{primary_id}"]')
                side_turn = None
                if cross_turn:
                    progress = await source_audio.evaluate("audio => audio.currentTime")
                    if follows_clock:
                        side_key, question = "clock_turn", "现在几点了"
                        clock_value = "2031年08月17日，星期日，15:42:17。"
                        with patch("assistant_brain.local_clock_reply", return_value=clock_value), patch(
                                "local_overview.local_clock_reply", return_value=clock_value):
                            side_payload, side_id = await submit_message(page, question)
                            final = page.locator(f'[data-turn-id="{side_id}"]').get_by_test_id("final")
                            await final.wait_for(timeout=7000)
                            assert await final.inner_text() == clock_value, report
                        side_turn = await current_turn(side_id)
                        assert side_turn["reply"] == clock_value and "get_time" in side_turn.get("holo_tools", []), side_turn
                    else:
                        from verify_weather_popup_browser import NAMED, sample

                        side_key, question = "weather_turn", NAMED
                        provider_calls = []

                        async def forecast(city, period, place, *, part_of_day=None, question=""):
                            provider_calls.append({"city":city, "period":period, "place":place})
                            return sample(city, period, place, part_of_day=part_of_day, question=question)

                        dialog = page.get_by_test_id("weather-panel")
                        with patch("assistant_brain.fetch_weather", forecast):
                            side_payload, side_id = await submit_message(page, question)
                            await dialog.wait_for(state="visible", timeout=7000)
                        side_turn = await current_turn(side_id)
                        assert len(provider_calls) == 1 and side_turn["weather"]["city"] == "北京市", side_turn
                        assert side_turn["weather"]["period"] == "tomorrow" and "get_weather" in side_turn.get("holo_tools", []), side_turn
                        assert await dialog.evaluate("node => node.open && node.matches(':modal')"), report
                        assert "北京市" in await dialog.get_by_role("heading", level=2).inner_text(), report
                        assert side_turn["weather"]["condition"] in await dialog.locator(".weather-condition").inner_text(), report
                    assert side_turn["user"] == question and side_turn["status"] == "replied", side_turn
                    assert side_turn["cloud_requests"]["total"] == 0, side_turn
                    assert not side_turn.get("media") and not side_turn.get("transport"), side_turn
                    assert side_payload["player"] == {"state":"playing", "turn_id":turn_id, "media_id":primary_id}, report
                    advanced = await page.wait_for_function("""({turnId, mediaId, position}) => {
                        const audio = [...document.querySelectorAll('audio.media-audio')].find(node =>
                            node.dataset.mediaId === mediaId && node.dataset.audioTurn === turnId);
                        return audio && !audio.paused && audio.currentTime > position + .1 && audio.currentTime;
                    }""", arg={"turnId":turn_id,"mediaId":primary_id,"position":progress}, timeout=5000)
                    report[side_key] = {"id":side_id, "reply":side_turn["reply"], "source":side_turn["source"],
                                       "cloud_requests":0, "playback_before_s":progress,
                                       "playback_after_s":await advanced.json_value()}
                    if follows_weather:
                        assert await dialog.evaluate("node => node.open && node.matches(':modal')"), report
                        report[side_key]["modal_verified"] = True
                        report[side_key]["provider_calls"] = provider_calls
                        await dialog.locator("#weather-close").click()
                        await dialog.wait_for(state="hidden", timeout=5000)
                if scenario == "paused":
                    controls = page.locator("#tool-player .player-toggle")
                    if not await controls.is_visible():
                        await page.locator("#music-now").click()
                    assert await controls.get_attribute("aria-label") == "暂停", report
                    await controls.click()
                    await page.wait_for_function("""turnId => window.mossMidstreamEvents.some(
                        event => event.type === 'pause' && event.turn_id === turnId && !event.error
                    )""", arg=turn_id, timeout=5000)
                    report["deliberate_pause_s"] = await source_audio.evaluate("audio => audio.paused && audio.currentTime")
                    assert report["deliberate_pause_s"] >= 1, report
                if resumes:
                    async with page.expect_response(
                            lambda r: playback_report(r, "started", turn_id, media_id=fallback_id), timeout=16000) as started:
                        disconnect.set()
                    replacement = await started.value
                    assert replacement.ok, await replacement.text()
                elif stop_race:
                    disconnect.set()
                    await asyncio.wait_for(committed.wait(), 12)
                    await page.wait_for_function("recoveryGate.held>0", timeout=5000)
                    action = "pause" if scenario == "pause_after_clock" else "stop"
                    stop = "暂停播放" if action == "pause" else "停止播放"
                    async with page.expect_response(lambda r: r.url == origin + "/api/message" and
                            r.request.post_data_json.get("text") == stop, timeout=7000) as stopping:
                        await page.get_by_test_id("composer").fill(stop)
                        await page.get_by_test_id("send").click()
                    stopped = await stopping.value
                    assert stopped.ok, await stopped.text()
                    report["stop_target"] = stopped.request.post_data_json.get("queued_player")
                    assert report["stop_target"] == {"turn_id":turn_id, "media_id":primary_id}, report
                    stop_id = (await stopped.json())["turns"][-1]["id"]
                    reply = page.locator(f'[data-turn-id="{stop_id}"]').get_by_test_id("final")
                    await reply.wait_for(timeout=7000)
                    assert "已取消待播放" in await reply.inner_text(), report
                    report["stop_receipt"] = (await current_turn(stop_id))["transport"]
                    assert report["stop_receipt"]["action"] == action, report
                    assert report["stop_receipt"]["event"] == "queue_cancelled", report
                    assert report["stop_receipt"]["cancelled_queue"] is True, report
                    release_recovery.set()
                    await page.evaluate("recoveryGate.release()")
                    await asyncio.wait_for(recovery_finished.wait(), 5)
                    await page.wait_for_timeout(450)
                elif change_race:
                    assert new_track and new_track["media_id"] not in {primary_id, fallback_id}
                    disconnect.set()
                    await asyncio.wait_for(stale_request.wait(), 6)
                    async with page.expect_response(lambda r: playback_report(
                            r, "started", media_id=new_track["media_id"]), timeout=14000) as started:
                        selected_payload, selected_id = await submit_message(page, new_track["text"])
                    response = await started.value
                    assert response.ok and response.request.post_data_json["turn_id"] == selected_id, report
                    selected = await current_turn(selected_id)
                    assert selected["media"]["selected"]["media_id"] == new_track["media_id"], selected
                    assert selected["browser_playback"]["event"] == "started" and selected["cloud_requests"]["total"] == 0, selected
                    original_recovery, = report["recovery_requests"]
                    async with http.post("/api/media-event", json=original_recovery) as replay:
                        outcome = await replay.json()
                        assert replay.status == 200 and outcome == {"ok": True}, outcome
                    report["stale_recovery_rejected"] = True
                    report["new_track"] = {"turn_id":selected_id, "receipt":selected["browser_playback"],
                                          "text":selected_payload["text"], "title":selected["media"]["selected"]["title"]}
                    assert await current_turn(selected_id) == selected, report
                    release_recovery.set()
                    await asyncio.wait_for(recovery_finished.wait(), 5)
                    await page.wait_for_timeout(450)
                else:
                    async with page.expect_response(lambda r:
                            playback_report(r, "failed", turn_id, recover=scenario != "paused"),
                            timeout=20000) as failed:
                        disconnect.set()
                    response = await failed.value
                    assert response.ok, await response.text()
                    await page.wait_for_function(
                        "document.querySelector('#music-now')?.textContent.includes('播放中断')", timeout=5000)
                report["events"] = await page.evaluate("mossMidstreamEvents")
                report["ui_state"] = await page.locator("#music-now").text_content()
                assert any(event["type"] == "error" and event["media_id"] == primary_id and event["error"] in (2, 3, 4)
                           for event in report["events"]), report
                after = await current_turn(turn_id)
                report["after_failure"] = {"receipt":after["browser_playback"],
                                           "selected_media_id":after["media"]["selected"]["media_id"],
                                           "recovered_from":after["media"].get("recovered_from")}
                assert report["injected_reads"], report
                native, *_ = [event for event in report["events"] if event["type"] == "error" and event["media_id"] == primary_id]
                if scenario == "paused":
                    assert not report["recovery_requests"] and await source_audio.evaluate("audio => audio.paused"), report
                    assert any(event["type"] == "pause" and event["turn_id"] == turn_id and not event["error"]
                               and event["at_ms"] < native["at_ms"] for event in report["events"]), report
                else:
                    recover, = report["recovery_requests"]
                    assert recover["play_id"] == before["browser_playback"]["play_id"] and recover["position_s"] >= 1, report
                    assert abs(native["position_s"] - recover["position_s"]) <= .25, report
                if resumes:
                    assert after["media"]["selected"]["media_id"] == fallback_id and after["media"]["recovered_from"] == primary_id, report
                    assert abs(after["media"]["resume_position_s"] - recover["position_s"]) <= .01, report
                    old_play, new_play = [row for row in report["playback_reports"] if row["event"] == "started"]
                    assert after["browser_playback"]["play_id"] == new_play["play_id"] != old_play["play_id"], report
                    playing, = [event for event in report["events"] if event["type"] == "playing" and event["media_id"] == fallback_id]
                    assert abs(playing["position_s"] - recover["position_s"]) <= .35, report
                    assert any(row["path"] == fallback_path and row["status"] == 206 for row in report["media_responses"]), report
                    advanced = await page.wait_for_function("""({turnId, mediaId, offset}) => {
                        const audio = [...document.querySelectorAll('audio.media-audio')].find(node =>
                            node.dataset.mediaId === mediaId && node.dataset.audioTurn === turnId);
                        return audio && !audio.paused && audio.currentTime >= offset + .15 && audio.currentTime;
                    }""", arg={"turnId":turn_id,"mediaId":fallback_id,"offset":recover["position_s"]}, timeout=5000)
                    report["resumed_progress_s"] = await advanced.json_value()
                else:
                    if stop_race:
                        assert not after["browser_playback"] and after["media"]["selected"]["media_id"] == fallback_id, report
                        assert "已取消待播放" in report["ui_state"], report
                    else:
                        if change_race:
                            title = report["new_track"]["title"]
                            assert report["ui_state"] in {f"音乐 · 正在播放《{title}》", f"音乐 · 播放结束《{title}》"}, report
                        else:
                            assert "播放中断" in report["ui_state"], report
                        assert after["browser_playback"] == before["browser_playback"], report
                        assert after["media"]["selected"]["media_id"] == primary_id and not after["media"].get("recovered_from"), report
                    assert all(row["path"] != fallback_path for row in report["media_requests"]), report
                    assert not any(row["media_id"] == fallback_id and row["event"] == "started"
                                   for row in report["playback_reports"]), report
                if side_turn:
                    assert await current_turn(side_turn["id"]) == side_turn, report
                    report[side_key]["unchanged"] = True
                    if resumes:
                        async with http.get("/api/state") as snapshot:
                            assert (await snapshot.json())["turns"][-1] == side_turn, report
                assert before["cloud_requests"]["total"] == after["cloud_requests"]["total"] == 0, report
                assert not report["blocked_requests"] and not report["browser_errors"], report
                return report
            except Exception:
                if not page.is_closed():
                    report["events"] = await page.evaluate("mossMidstreamEvents || []")
                    report["audio_at_failure"] = await page.locator("audio.media-audio").evaluate_all("""nodes => nodes.map(node => ({
                        id:node.dataset.mediaId, turn:node.dataset.audioTurn, position:node.currentTime,
                        paused:node.paused, readyState:node.readyState, played:node.played.length
                    }))""")
                raise
            finally:
                disconnect.set()
                release_recovery.set()
                set_scope("")
                await browser.close()
    except Exception as error:
        raise AssertionError(report) from error
    finally:
        disconnect.set()
        media._open_media = original_open
        if parked.exists():
            parked.rename(local)


def long_track_pair(disk, nas, title, artist, *, different=False):
    from mutagen.flac import FLAC
    from soundfile import SoundFile

    def write_track(path, seed):
        with SoundFile(str(path), "w", samplerate=48000, channels=1, subtype="PCM_16") as track:
            track.buffer_write(Random(seed).randbytes(48000 * 12 * 2), dtype="int16")
        tagged = FLAC(path)
        tagged["title"] = title
        tagged["artist"] = artist
        tagged.save(padding=lambda _: 2048)

    # FLAC puts readable title and artist tags before the audio frames on SMB.
    remote, local = nas / (title + ".flac"), disk / (title + ".flac")
    write_track(remote, 2026)
    assert remote.stat().st_size > 256 * 1024, remote
    if different:
        write_track(local, 2027)
        target_size = max(remote.stat().st_size, local.stat().st_size)
        for file in (remote, local):
            padding = 2048 + target_size - file.stat().st_size
            FLAC(file).save(padding=lambda _: padding)
            assert file.stat().st_size == target_size
        assert remote.read_bytes() != local.read_bytes()
    else:
        copyfile(remote, local)
    return local


async def run(browser=False, browser_race=False, *, browser_stop_race=False, browser_midstream=False,
              browser_resume=False, browser_resume_cross_turn=False):
    from aiohttp import ClientSession, web
    import smbclient
    from core import Moss
    from local_media.library import MediaLibrary
    from serve import application

    sys.path.append(str(ROOT.parent/"alternatives/hiroute/media_library"))
    from verify import sample

    result = ROOT/"runs"/datetime.now().strftime("media-http-recovery-%Y%m%d-%H%M%S-%f")
    disk, nas = result/"disk", result/"share"
    disk.mkdir(parents=True)
    nas.mkdir()
    name = "试音潮汐"
    artist = "隔离合奏"
    local = sample(disk, "磁盘录制.wav", name, artist)
    remote = sample(nas, "共享录制.wav", name, artist)
    with remote.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\x70\x04\x70\xfb" * 12)
    library = MediaLibrary(result/"index.sqlite3")
    os.environ.update(MOSS_MEDIA_DB=str(library.db_path), MOSS_QA_SMB_USER="media_test",
                      MOSS_QA_SMB_PASSWORD=secrets.token_urlsafe(20), MOSS_PROVIDER_DIR=str(result/"providers"),
                      LOCALAPPDATA=str(result/"profile"), MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0")
    smb_port, web_port = free_port(), free_port()
    library.configure([{"id":"disk","kind":"local","path":str(disk)},
        {"id":"nas","kind":"nas","path":r"\\127.0.0.1\MEDIA","smb":{"port":smb_port,
            "username_env":"MOSS_QA_SMB_USER","password_env":"MOSS_QA_SMB_PASSWORD"}}])
    data = result/"data"
    moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
    runner = web.AppRunner(application(moss,lambda:"standby",data,web_port,"isolated",asyncio.Event()), access_log=None)
    await runner.setup()
    await web.TCPSite(runner,"127.0.0.1",web_port).start()
    report = {"passed":False,"method":"正式消息入口、同一服务的 HTTP Range、独立高端口只读 SMB2、随机合成曲目。"
              "点歌进程仅允许访问测试站点及回环 SMB，监控 DNS、连接、数据报、URL 请求、子进程。",
              "loopback_ports":{"http":web_port,"smb":smb_port},"smb_pids":[],"steps":[],
              "external":[],"unexpected_subprocess":[]}
    scope = ""
    def set_scope(label):
        nonlocal scope
        scope = label

    def audit(event, args):
        if not scope:
            return
        if event == "urllib.Request" or event == "subprocess.Popen":
            report["unexpected_subprocess" if event == "subprocess.Popen" else "external"].append([scope,event])
            raise PermissionError("隔离验收捕获了意外访问")
        if event not in {"socket.getaddrinfo","socket.connect","socket.sendto"}:
            return
        address = args[0] if event == "socket.getaddrinfo" else args[1][0]
        if address not in {"127.0.0.1","localhost","::1"}:
            report["external"].append([scope,str(address)])
            raise PermissionError("隔离验收禁止外部连接")
        port = args[1] if event == "socket.getaddrinfo" else args[1][1]
        if port not in {smb_port, web_port}:
            report["external"].append([scope,"unexpected_loopback",port])
            raise PermissionError("隔离验收禁止访问其他本机服务")
    sys.addaudithook(audit)
    try:
        async with ClientSession(base_url=f"http://127.0.0.1:{web_port}", trust_env=False) as http:
            async def query(text=None):
                text = text or f"只列出{artist}的{name}的版本，不要播放"
                async with http.post("/api/message",json={"chat_id":moss.chat_id,"text":text}) as resp:
                    assert resp.status==200, await resp.text()
                    state=await resp.json()
                async with asyncio.timeout(15):
                    while state["turns"][-1]["status"]=="thinking":
                        await asyncio.sleep(.02)
                        async with http.get("/api/state") as resp:
                            state=await resp.json()
                turn=state["turns"][-1]
                assert turn["status"]=="replied" and "media" in turn and not turn["media"]["online_calls"], turn
                return turn

            async def fetch(key, path, expected):
                async with http.get("/media/"+key,headers={"Range":"bytes=64-255"}) as resp:
                    content=await resp.read()
                    assert resp.status==expected,(resp.status,content[:60])
                    if expected==206:
                        assert content==path.read_bytes()[64:256]
                        return hashlib.sha256(content).hexdigest()
            def selected(turn, kind):
                row=turn["media"]["items"][0]
                assert row["source_kind"]==kind and row["artist"]==artist and row["title"]==name
                return row["media_id"]

            with share(nas,smb_port,1) as (pid, _):
                report["smb_pids"].append(pid)
                scope="第一次索引和点歌"
                assert all(s["state"]=="ready" for s in library.scan())
                turn=await query()
                disk_key=selected(turn,"local")
                assert turn["media"]["consulted_sources"]==["disk"],turn["media"]
                report["steps"].append({"name":"同名音乐优先本地","source":"disk","id":disk_key,
                                        "range_206":await fetch(disk_key,local,206)})
                local.rename(disk/"隔离临时改名.wav")
                scope="磁盘文件临时不可用"
                from_list=await query(f"播放{name} · {artist}")
                assert from_list["media"]["autoplay"] and from_list["media"]["selected"]["source_kind"]=="nas"
                turn=await query()
                nas_key=selected(turn,"nas")
                assert from_list["media"]["selected"]["media_id"]==nas_key
                nas_hash=await fetch(nas_key,remote,206)
                assert report["steps"][0]["range_206"]!=nas_hash
                report["steps"].append({"name":"本地副本失效，改用共享","id":nas_key,"range_206":nas_hash})
                scope=""
            scope="共享完全离线"
            turn=await query()
            assert not turn["media"]["items"] and not turn["media"]["autoplay"]
            assert "暂时无法打开" in turn["reply"] and any(
                s["id"] == "nas" and s["state"] == "offline" for s in turn["media"]["unavailable_sources"])
            await fetch(nas_key,remote,503)
            report["steps"].append({"name":"共享下线","range":503,"reply":turn["reply"]})
            scope=""
            with share(nas,smb_port,2) as (pid, _):
                report["smb_pids"].append(pid)
                scope="原地址恢复"
                turn=await query()
                assert selected(turn,"nas")==nas_key and await fetch(nas_key,remote,206)==nas_hash
                report["steps"].append({"name":"共享原地址恢复","id":nas_key,"range_206":nas_hash})
                (disk/"隔离临时改名.wav").rename(local)
                restored=await query(f"播放{name} · {artist}")
                assert restored["media"]["autoplay"] and restored["media"]["selected"]["media_id"]==disk_key
                turn=await query()
                assert selected(turn,"local")==disk_key and await fetch(disk_key,local,206)==report["steps"][0]["range_206"]
                report["steps"].append({"name":"磁盘副本恢复后重新优先","id":disk_key})
                if browser_stop_race:
                    scope=""
                    with move_local_during_read(local) as lose_copy:
                        report["browser_stop_race"] = await browser_receipt(http,web_port,f"播放{artist}的{name}",
                            nas_key,set_scope,fault=(disk_key,404,lose_copy),stop_race=True)
                if browser_race:
                    scope=""
                    with move_local_during_read(local) as lose_copy:
                        race = report["browser_race"] = await browser_receipt(http,web_port,f"播放{artist}的{name}",nas_key,set_scope,
                                                                            fault=(disk_key,404,lose_copy))
                    assert (race["selected"]["artist"], race["selected"]["title"]) == (artist,name), race
                    scope="复核共享实际字节及原始序号"
                    race["nas_range_206"] = await fetch(nas_key,remote,206)
                    assert race["nas_range_206"] == nas_hash and race["nas_range_206"] != report["steps"][0]["range_206"]
                    listed = await query()
                    assert selected(listed,"local") == disk_key
                    scope=""
                    with move_local_during_read(local) as lose_copy:
                        report["browser_race_numbered"] = await browser_receipt(http,web_port,"播放第 1 首",disk_key,set_scope,
                            fault=(disk_key,404,lose_copy), expected_source="local", expect_failure=True)
                scope="浏览器不支持磁盘副本的后缀"
                # The production gate uses suffixes. Keep the tagged fixture bytes
                # so both versions index to the exact same title and artist.
                unsupported = local.with_suffix(".dsf")
                local.rename(unsupported)
                assert all(s["state"]=="ready" for s in library.scan())
                listed=await query()
                disk_dsf=selected(listed,"local")
                assert not listed["media"]["autoplay"] and not listed["media"]["selected"]
                assert listed["media"]["consulted_sources"]==["disk"]
                requested=await query(f"播放{artist}的{name}")
                assert selected(requested,"nas")==nas_key and requested["media"]["selected"]["media_id"]==nas_key
                assert requested["media"]["autoplay"] and requested["media"]["consulted_sources"]==["disk","nas"]
                assert requested["media"]["unsupported_audio"]==[".dsf"] and await fetch(nas_key,remote,206)==nas_hash
                assert "已播放" not in requested["reply"]
                await query()
                labelled=await query(f"播放{name} · {artist}")
                assert labelled["media"]["autoplay"] and labelled["media"]["selected"]["media_id"]==nas_key
                assert labelled["media"]["unsupported_audio"]==[".dsf"]
                await query()
                ordinal=await query("播放第1首")
                assert not ordinal["media"]["autoplay"] and not ordinal["media"]["selected"]
                assert selected(ordinal,"local")==disk_dsf and "格式暂不能" in ordinal["reply"]
                report["steps"].append({"name":"原曲名和明确标签选中可播放共享版本，浏览与序号保留磁盘项目",
                                        "local_list_id":disk_dsf,"nas_play_id":nas_key,"range_206":nas_hash})
                if browser:
                    scope=""
                    report["browser"] = await browser_receipt(http,web_port,f"播放{artist}的{name}",nas_key,set_scope)
                scope="仅有浏览器不支持的版本"
                hidden = remote.with_suffix(".missing")
                remote.rename(hidden)
                try:
                    assert all(s["state"]=="ready" for s in library.scan())
                    only=await query(f"播放{artist}的{name}")
                    assert "曲库里有" in only["reply"] and "格式暂不能" in only["reply"], only
                    assert not only["media"]["autoplay"] and only["media"]["unsupported_audio"]==[".dsf"]
                    report["steps"].append({"name":"仅有不受支持的版本时说明格式限制","reply":only["reply"]})
                finally:
                    hidden.rename(remote)
                    unsupported.rename(local)
                smbclient.reset_connection_cache()
                scope=""
            if browser_race:
                with share(nas,smb_port,3) as (pid, stop_share):
                    report["smb_pids"].append(pid)
                    scope="反向回退前重新索引两个副本"
                    assert all(s["state"]=="ready" for s in library.scan())
                    parked = local.with_suffix(".temporarily-unavailable")
                    assert not parked.exists()
                    local.rename(parked)

                    def disconnect_then_restore_local():
                        stop_share()
                        smbclient.reset_connection_cache(fail_on_error=False)
                        parked.rename(local)

                    try:
                        scope=""
                        reverse = report["browser_race_nas_to_local"] = await browser_receipt(
                            http,web_port,f"播放{artist}的{name}",disk_key,set_scope,
                            fault=(nas_key,503,disconnect_then_restore_local), expected_source="local")
                    finally:
                        if parked.exists():
                            parked.rename(local)
                    assert (reverse["selected"]["artist"], reverse["selected"]["title"]) == (artist,name), reverse
                    scope="复核本机回退字节与共享离线"
                    reverse["local_range_206"] = await fetch(disk_key,local,206)
                    assert reverse["local_range_206"] == report["steps"][0]["range_206"] != nas_hash
                    await fetch(nas_key,remote,503)
                    scope=""
            if browser_midstream or browser_resume or browser_resume_cross_turn:
                with share(nas, smb_port, 4) as (pid, _):
                    report["smb_pids"].append(pid)
                    cases = []
                    if browser_midstream:
                        cases.append(("interrupted", "共享播放中断验收"))
                    if browser_resume:
                        cases.extend((("matched", "共享原版续播验收"), ("different", "共享异版续播验收"),
                                      ("stop", "共享取消续播验收"), ("paused", "共享暂停续播验收")))
                    if browser_resume_cross_turn:
                        cases.extend((("after_clock", "共享报时后续播验收"), ("after_weather", "共享查天气后续播验收"),
                                      ("stop_after_clock", "共享报时后停止验收"),
                                      ("pause_after_clock", "共享报时后暂停验收"),
                                      ("change_after_clock", "共享报时后换歌验收")))
                    for scenario, title in cases:
                        local_long = long_track_pair(disk, nas, title, artist, different=scenario == "different")
                        remote_long = nas / local_long.name
                        scope = "共享播放中断验收前索引两个音轨"
                        assert all(source["state"] == "ready" for source in library.scan())
                        with closing(library.connect()) as db:
                            fixture_rows = {row["source"]: dict(row) for row in db.execute(
                                "SELECT * FROM media WHERE rel=?", (local_long.name,))}
                            disk_row, nas_row = fixture_rows["disk"], fixture_rows["nas"]
                            assert disk_row["kind"] == nas_row["kind"] == "audio"
                            assert disk_row["size"] == nas_row["size"] == local_long.stat().st_size
                            assert (nas_row["title"], nas_row["artist"]) == (disk_row["title"], disk_row["artist"]) == (title, artist)
                        listing = f"只列出{artist}的{title}的版本，不要播放"
                        local_item = (await query(listing))["media"]["items"][0]
                        with move_local_during_read(local_long) as hide_local:
                            hide_local()
                            nas_item = (await query(listing))["media"]["items"][0]
                        assert (local_item["source_kind"], nas_item["source_kind"]) == ("local", "nas")
                        assert local_item["title"] == nas_item["title"] == title
                        scope = "共享播放长 Range 字节核对"
                        async with http.get("/media/" + nas_item["media_id"], headers={"Range":"bytes=0-262143"}) as reply:
                            payload = await reply.read()
                            assert reply.status == 206 and payload == remote_long.read_bytes()[:262144], (reply.status, len(payload))
                            verified_range = {"status":reply.status, "bytes":len(payload),
                                              "sha256":hashlib.sha256(payload).hexdigest()}
                        scope=""
                        result_case = await browser_midstream_receipt(
                            http, web_port, f"播放{artist}的{title}", nas_item["media_id"], local_item["media_id"],
                            local_long, moss, set_scope, scenario=scenario,
                            new_track={"media_id":disk_key, "text":f"播放{artist}的{name}"} if scenario == "change_after_clock" else None)
                        if scenario == "interrupted":
                            report["midstream_range"] = verified_range
                            report["browser_midstream"] = result_case
                        else:
                            result_case["range_206"] = verified_range
                            category = "browser_resume_cross_turn" if scenario.endswith(("_clock", "_weather")) else "browser_resume"
                            report.setdefault(category, {})[scenario] = result_case
        report["passed"]=not report["external"] and not report["unexpected_subprocess"]
        assert report["passed"]
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        scope=""
        await runner.cleanup()
        await moss.close()
        (result/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding="utf-8")
        print(result,report["passed"],flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--share",type=Path)
    parser.add_argument("--port",type=int)
    parser.add_argument("--ready",type=Path)
    parser.add_argument("--browser",action="store_true",help="另外通过静音 Edge 核对正式开播回执")
    parser.add_argument("--browser-race",action="store_true",help="验证浏览器开播时本机磁盘与隔离共享双向回退")
    parser.add_argument("--browser-stop-race",action="store_true",help="验证服务端已切换备用文件但浏览器未收到时取消待播")
    parser.add_argument("--browser-midstream",action="store_true",help="记录共享歌曲实际开播后的读取中断和原回执")
    parser.add_argument("--browser-resume",action="store_true",help="验证共享断点续播、同名异版拒绝、续播取消及尊重暂停")
    parser.add_argument("--browser-resume-cross-turn",action="store_true",help="验证查时或天气后的续播以及之后的停止、暂停和换曲")
    args=parser.parse_args()
    if args.share:
        serve_share(args.share,args.port,args.ready)
    else:
        asyncio.run(run(args.browser,args.browser_race,browser_stop_race=args.browser_stop_race,
                        browser_midstream=args.browser_midstream,browser_resume=args.browser_resume,
                        browser_resume_cross_turn=args.browser_resume_cross_turn))
