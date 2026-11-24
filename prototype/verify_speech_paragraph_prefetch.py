"""Check first-paragraph speech preparation with gated loopback SSE and muted Edge."""

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import io
import json
import math
import os
from pathlib import Path
import struct
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import Error, async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_context_reply import free_port
from verify_speech_stream_lead import MEASURE, PASSAGES


QUESTION = "用两段描述桌上的地图和窗边的便条，段间保留空行。"
FIRST, SECOND = PASSAGES[:2]
REVISED = "桌上搁着一本薄薄的旅行手册。书页上夹着一封没有署名的信。"
MODEL = "moss-paragraph-fixture"
DURATION = {FIRST: .23, SECOND: .32, REVISED: .55}
PROBE = """
localStorage.setItem('moss.voice.enabled', 'on');
window.paragraphLead = {partials:[], final:null, cancelled:null, voice:[]};
window.addEventListener('moss-agent-state', event => {
  const turn = event.detail.turns.at(-1);
  if (!turn) return;
  const ms = performance.now();
  if (turn.status === 'thinking') paragraphLead.partials.push({text:turn.partial_reply || '', ms});
  if (turn.status === 'replied') paragraphLead.final = {text:turn.reply, ms};
  if (turn.status === 'cancelled') paragraphLead.cancelled = ms;
});
const paragraphFetch = window.fetch;
window.fetch = async function (input, options) {
  if (input !== '/api/speak') return paragraphFetch.apply(this, arguments);
  const row = {text:JSON.parse(options.body).text, ms:performance.now()};
  paragraphLead.voice.push(row);
  options.signal.addEventListener('abort', () => { row.aborted = performance.now(); }, {once:true});
  const response = await paragraphFetch.apply(this, arguments);
  row.response = performance.now();
  return response;
};
"""


@dataclass
class Round:
    kind: str
    close: asyncio.Event = field(default_factory=asyncio.Event)
    wrote_boundary: asyncio.Event = field(default_factory=asyncio.Event)
    second: asyncio.Event = field(default_factory=asyncio.Event)
    final: asyncio.Event = field(default_factory=asyncio.Event)
    voice: asyncio.Event = field(default_factory=asyncio.Event)
    release_audio: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list = field(default_factory=list)
    synthesis: list = field(default_factory=list)
    boundary_gap_ms: float | None = None
    second_sent: bool = False

    def release(self):
        for signal in (self.close, self.second, self.final, self.release_audio):
            signal.set()


def wav(duration):
    rate = 16_000
    samples = [round(3100 * math.sin(2 * math.pi * 340 * tick / rate)) for tick in range(round(rate * duration))]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setparams((1, 2, rate, len(samples), "NONE", "not compressed"))
        target.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def chunk(delta, finish=None):
    message = {"id": "para-qa", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return ("data: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode("utf-8")


async def run(expected, script):
    folder = ROOT / "runs" / datetime.now().strftime(f"speech-paragraph-prefetch-{expected}-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    report = {"passed": False, "expected": expected, "model": "loopback", "speech": "synthetic WAV",
              "cases": [], "outside_requests": [], "page_errors": []}
    audio = {text: wav(length) for text, length in DURATION.items()}
    active = None

    async def complete(request):
        case = active
        body = await request.json()
        last = body["messages"][-1]
        assert case and body["stream"] and body["model"] == MODEL, body
        case.requests.append(last["role"])
        if last["role"] == "tool":
            assert case.kind == "changed" and last["tool_call_id"] == "para-qa-clock", last
            revised = chunk({"role": "assistant", "content": REVISED + "\n\n" + SECOND}, "stop")
            return web.Response(body=revised + b"data: [DONE]\n\n", content_type="text/event-stream")
        assert last == {"role": "user", "content": QUESTION}, last
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        first = chunk({"role": "assistant", "content": FIRST})
        boundary = chunk({"content": "\n\n"})
        try:
            async with asyncio.timeout(15):
                if expected == "boundary" and case.kind == "stable":
                    # Two deltas in one write make the boundary arrive inside the application's 150 ms throttle.
                    await response.write(first + boundary)
                    case.boundary_gap_ms = 0.0
                else:
                    await response.write(first)
                    started = perf_counter()
                    await case.close.wait()
                    await asyncio.sleep(.17)
                    await response.write(boundary)
                    case.boundary_gap_ms = round((perf_counter() - started) * 1000, 1)
                case.wrote_boundary.set()
                await case.second.wait()
                await asyncio.sleep(.17)
                await response.write(chunk({"content": SECOND}))
                case.second_sent = True
                await case.final.wait()
                if case.kind == "changed":
                    assert "get_time" in [tool["function"]["name"] for tool in body.get("tools", [])], body
                    tool = {"id": "para-qa-clock", "index": 0, "type": "function",
                            "function": {"name": "get_time", "arguments": "{}"}}
                    await response.write(chunk({"tool_calls": [tool]}, "tool_calls"))
                else:
                    await response.write(chunk({}, "stop"))
                await response.write(b"data: [DONE]\n\n")
        except ConnectionResetError:
            if case.kind != "cancelled":
                raise
        return response

    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0",
                   "PYTHONDONTWRITEBYTECODE": "1"}
    app = web.Application()
    app.router.add_post("/v1/chat/completions", complete)
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    app_url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1"
    server = BrowserServer(folder, free_port(), model_url=app_url, model=MODEL)

    async def verify(browser, kind):
        nonlocal active
        case = active = Round(kind)
        code, _ = await server.post("/api/new-chat", {})
        assert code == 200
        context = await browser.new_context(service_workers="block", reduced_motion="reduce")

        async def route_request(route):
            url = urlsplit(route.request.url)
            if url.scheme not in {"http", "https"}:
                await route.continue_()
            elif url.hostname != "127.0.0.1" or url.port != server.port:
                report["outside_requests"].append(route.request.url)
                await route.abort()
            elif url.path == "/static/agent-speech.js" and script:
                await route.fulfill(path=str(script), content_type="text/javascript")
            elif url.path == "/api/speak":
                text = route.request.post_data_json["text"]
                assert text in audio, text
                case.synthesis.append(text)
                case.voice.set()
                if kind == "cancelled":
                    await case.release_audio.wait()
                try:
                    await route.fulfill(body=audio[text], content_type="audio/wav")
                except Error:
                    if kind != "cancelled":
                        raise
            elif url.path in {"/api/prepare-speech", "/api/speak-batch"}:
                raise AssertionError(f"Unexpected speech engine request {url.path}")
            else:
                await route.continue_()

        async def observe(text):
            return await page.wait_for_function("text => paragraphLead.partials.some(row => row.text === text)", arg=text)

        async def no_audio():
            assert await page.evaluate("speechTest.clips.length === 0 && !speechTest.events.some(row=>row.type==='start')")

        async def wait_voice():
            async with asyncio.timeout(6):
                await case.voice.wait()

        try:
            await context.route("**/*", route_request)
            await context.add_init_script(MEASURE + PROBE)
            page = await context.new_page()
            page.set_default_timeout(5500)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            composer = page.get_by_test_id("composer")
            send = page.get_by_test_id("send")
            await composer.fill(QUESTION)
            await send.click()
            if expected == "baseline" or kind != "stable":
                await observe(FIRST)
                assert not case.synthesis
                case.close.set()
            await observe(FIRST + "\n\n")
            assert case.wrote_boundary.is_set() and not case.second_sent

            if expected == "baseline":
                await asyncio.sleep(.35)
                assert not case.synthesis, "Baseline opens preparation only with text in a second segment"
                case.second.set()
                await observe(FIRST + "\n\n" + SECOND)
            await wait_voice()
            assert case.synthesis == [FIRST] and case.second_sent is (expected == "baseline")
            if kind != "cancelled":
                await page.wait_for_function("paragraphLead.voice.at(0)?.response !== undefined")
            await no_audio()

            if kind == "cancelled":
                async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/cancel") as cancellation:
                    await composer.fill("停止回复")
                    await send.click()
                assert (await cancellation.value).ok
                await page.wait_for_function("paragraphLead.cancelled !== null && paragraphLead.voice[0].aborted !== undefined")
                case.release()
                await page.wait_for_timeout(250)
                await no_audio()
                state = await server.state()
                assert state["turns"][-1]["status"] == "cancelled" and not await page.evaluate("paragraphLead.final")
                assert case.synthesis == [FIRST] and case.requests == ["user"]
            else:
                case.second.set()
                await observe(FIRST + "\n\n" + SECOND)
                await no_audio()
                case.final.set()
                await page.wait_for_function("speechTest.events.some(row=>row.type==='start') && paragraphLead.final !== null")
                first = REVISED if kind == "changed" else FIRST
                trace = await page.evaluate("({lead:paragraphLead, clips:speechTest.clips, events:speechTest.events})")
                start = next(row for row in trace["events"] if row["type"] == "start")
                assert start["text"] == first and start["ms"] >= trace["lead"]["final"]["ms"], trace
                assert trace["lead"]["final"]["text"] == first + "\n\n" + SECOND
                assert trace["clips"][0]["first"] is not None and abs(trace["clips"][0]["duration"] - DURATION[first]) < .02, trace
                assert case.synthesis.count(FIRST) == case.synthesis.count(first) == 1, case.synthesis
                if kind == "changed":
                    assert trace["lead"]["voice"][0].get("aborted") is not None, trace
                    assert case.requests == ["user", "tool"], case.requests
                else:
                    assert case.requests == ["user"], case.requests
                state = await server.state()
                assert state["turns"][-1]["status"] == "replied"
            assert state["turns"][-1]["cloud_requests"]["total"] == 0
            report["cases"].append({"case": kind, "boundary_gap_ms": case.boundary_gap_ms,
                                    "model_roles": case.requests, "synthesis": case.synthesis,
                                    "trace": await page.evaluate("({lead:paragraphLead, clips:speechTest.clips, events:speechTest.events})")})
        finally:
            case.release()
            await context.close()

    try:
        with patch.dict(os.environ, environment):
            await server.start()
        assert (await server.state())["model_id"] == "local"
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                for kind in ("stable", "cancelled", "changed"):
                    await verify(browser, kind)
                assert not report["outside_requests"] and not report["page_errors"], report
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        await runner.cleanup()
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("boundary", "baseline"), default="boundary")
    parser.add_argument("--script", type=Path, help="Use a saved agent-speech.js for comparison")
    args = parser.parse_args()
    asyncio.run(run(args.expect, args.script))
