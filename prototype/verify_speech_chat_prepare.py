"""Measure the first synthesized reply from ordinary Holo chat in muted Edge."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from statistics import median
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from device_status import GIB, memory_sample
from local_media.library import MediaLibrary
from verify_context_reply import free_port
from verify_memory import send
from verify_omnivoice_optional_imports import watch
from verify_speech_long_reading import PROBE
from verify_speech_prepare_ab import free_gpu, other_speaker


CACHE = ROOT / "runs/verse-reference-20261120-221137-572766/data/voice-prompts"
QUESTION = "为什么月亮有时白天也能看见？用一句话回答。"
REPLY = "月亮一直在反射阳光，白天只要在地平线上方也能看见。"
MODEL = "moss-qa-loopback"
MODEL_DELAY = 2.5
TRACK_REQUESTS = """
window.speechChatQA = {calls:[], final:null};
const originalFetch = window.fetch;
window.fetch = async function(input, options) {
  const path = new URL(input, location.href).pathname;
  if (!['/api/prepare-speech','/api/speak','/api/speak-batch'].includes(path)) {
    return originalFetch.apply(this, arguments);
  }
  const call = {path, started:performance.now(), body:JSON.parse(options.body)};
  speechChatQA.calls.push(call);
  const response = await originalFetch.apply(this, arguments);
  call.status = response.status;
  call.responded = performance.now();
  return response;
};
new MutationObserver(() => {
  if (speechChatQA.final === null && document.querySelector('[data-testid=final]')) {
    speechChatQA.final = performance.now();
  }
}).observe(document, {subtree:true, childList:true, characterData:true, attributes:true, attributeFilter:['data-testid']});
"""


@asynccontextmanager
async def model_loopback(calls):
    async def complete(request):
        body = await request.json()
        assert body["stream"] is True and body["model"] == MODEL, body
        assert body["messages"][-1] == {"role": "user", "content": QUESTION}, body["messages"][-1]
        arrival = perf_counter()
        await asyncio.sleep(MODEL_DELAY)
        chunk = {"id": "moss-qa-reply", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}]}
        calls.append({"delay_ms": round((perf_counter() - arrival) * 1000, 1), "stream": True})
        sse = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
        return web.Response(text=sse, content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", complete)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = free_port()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        await runner.cleanup()


def first_audible(clip, submitted):
    output = clip["output"]
    assert clip["first"] is not None, clip
    if output["performanceTime"] > 0 and output["contextTime"] <= clip["start"]:
        at = output["performanceTime"] + (clip["first"] - output["contextTime"]) * 1000
        return round(at - submitted, 1), "web_audio_output_timestamp"
    at = clip["scheduled"] + (clip["first"] - clip["start"]) * 1000
    return round(at - submitted, 1), "web_audio_schedule_estimate"


async def trial(browser, folder, enabled, row, *, check_guards=False):
    start_gpu = await asyncio.to_thread(free_gpu)
    deadline = perf_counter() + 25
    while start_gpu < 3200 and perf_counter() < deadline:
        await asyncio.sleep(1)
        start_gpu = await asyncio.to_thread(free_gpu)
    memory = memory_sample()
    row["start_available_mib"] = memory.available // 2**20
    row["start_commit_mib"] = memory.available_pagefile // 2**20
    row["start_free_gpu_mib"] = start_gpu
    assert min(memory.available, memory.available_pagefile) >= 8 * GIB, row
    # Disable browser rendering on the GPU and use the shared live resource monitor below.
    assert start_gpu >= 3200 and not other_speaker(), row
    folder.mkdir()
    profile = folder / "data"
    shutil.copytree(CACHE, profile / "voice-prompts")
    catalog = folder / "media.sqlite3"
    MediaLibrary(catalog)
    calls = row["model_calls"] = []
    blocked = row["blocked_requests"] = []
    errors = row["page_errors"] = []
    runtime_env = {"MOSS_SPEECH_PREPARE": str(int(enabled)), "MOSS_SPEECH_STANDBY": "0",
                   "MOSS_MEM0_ENABLED": "0", "MOSS_KEV_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "LOCALAPPDATA": str(folder / "appdata"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog)}
    async with model_loopback(calls) as model_url:
        server = BrowserServer(folder, free_port(), model_url=model_url, model=MODEL)
        context = await browser.new_context(reduced_motion="reduce")
        async def only_local(route):
            target = urlsplit(route.request.url)
            if target.scheme in ("http", "https") and target.netloc != urlsplit(server.url).netloc:
                blocked.append(route.request.url)
                await route.abort()
            else:
                await route.continue_()
        try:
            with patch.dict(os.environ, runtime_env):
                await server.start()
            state = await server.state()
            assert state["speech_prepare"] is enabled and not state["turns"] and not state["memories"], state
            await context.route("**/*", only_local)
            await context.add_init_script("localStorage.setItem('moss.voice.enabled','on');" + PROBE + TRACK_REQUESTS)
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            responses = []
            page.on("response", lambda response: responses.append(response) if response.url == server.url + "/api/speak" else None)
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            resources = row["resources"] = {
                "start_gpu_free_mib": row["start_free_gpu_mib"], "min_gpu_free_mib": row["start_free_gpu_mib"],
                "min_free_mib": row["start_available_mib"], "peak_worker_rss_mib": 0,
            }

            async def speak_and_wait():
                assert await send(page, server, QUESTION) == REPLY
                await page.wait_for_function("readingFinished()", timeout=100000)

            await watch(server, speak_and_wait(), resources)
            assert not resources.get("stopped"), resources
            trace = row["trace"] = await page.evaluate("collectReadingMetrics()")
            proof = row["browser_requests"] = await page.evaluate("speechChatQA")
            assert not blocked and not errors, row
            assert [event["type"] for event in trace["events"]] == ["pending", "start", "end"], trace["events"]
            assert len(trace["clips"]) == len(responses) == len(calls) == 1, row
            assert calls[0]["delay_ms"] >= MODEL_DELAY * 1000 - 20, calls
            response, = responses
            assert response.status == 200, response.status
            audio = await response.body()
            assert len(audio) > 44 and audio.startswith(b"RIFF") and audio[8:12] == b"WAVE"
            (folder / "reply.wav").write_bytes(audio)
            row["wav_sha256"] = sha256(audio).hexdigest()
            timing = response.headers.get("server-timing", "")
            row["server_timing_ms"] = {label: float(value) for label, value in
                                       re.findall(r"([\w-]+);dur=([0-9.]+)", timing)}
            assert "generate" in row["server_timing_ms"] and "cache" not in row["server_timing_ms"], timing
            log = (folder / "web-1.log").read_text(encoding="utf-8", errors="replace")
            assert re.search(r"OmniVoice weights=\S+ device=cuda decoder=cuda", log), log[-2000:]
            row["cuda_inference"] = True
            turn, = (await server.state())["turns"]
            assert (turn["source"], turn["status"], turn["reply"]) == ("holo", "replied", REPLY), turn
            assert turn["cloud_requests"]["total"] == 0, turn["cloud_requests"]
            submitted = trace["submitted"]
            row["reply_final_ms"] = round(proof["final"] - submitted, 1)
            row["first_audible_ms"], row["first_audible_clock"] = first_audible(trace["clips"][0], submitted)
            row["final_to_audible_ms"] = round(row["first_audible_ms"] - row["reply_final_ms"], 1)
            prepares = [call for call in proof["calls"] if call["path"] == "/api/prepare-speech"]
            speaking, = (call for call in proof["calls"] if call["path"] == "/api/speak")
            assert speaking["body"]["text"] == REPLY and speaking["body"]["category"] == "reply", speaking
            if enabled:
                prep, = prepares
                assert prep["body"] == {} and prep["status"] == 204, prep
                assert submitted <= prep["started"] < min(proof["final"], speaking["started"]), proof
                assert "spawn" not in row["server_timing_ms"], row["server_timing_ms"]
                row["prepare_ms"] = round(prep["started"] - submitted, 1)
                row["prepare_lead_ms"] = round(speaking["started"] - prep["started"], 1)
            else:
                assert not prepares and "spawn" in row["server_timing_ms"], row
                row["prepare_ms"] = row["prepare_lead_ms"] = None
            if check_guards:
                before = len(proof["calls"])
                assert await send(page, server, "你在吗？") == "我在。"
                await page.wait_for_function("collectReadingMetrics().events.filter(e=>e.type==='end').length === 2")
                guard = await page.evaluate(f"speechChatQA.calls.slice({before})")
                greeting, = guard
                assert greeting["path"] == "/api/speak" and greeting["body"]["category"] == "warm", guard
                assert await responses[-1].body() == (ROOT / "voice/lines/here.wav").read_bytes()
                assert len(calls) == 1
                row["cached_greeting_no_preimport"] = True
                passive = await context.new_page()
                passive.on("pageerror", lambda error: errors.append(str(error)))
                await passive.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                await passive.get_by_test_id("turn").nth(1).get_by_test_id("final").wait_for()
                await page.bring_to_front()
                await page.locator(".preferences summary").click()
                await page.get_by_role("checkbox", name="MOSS 语音播报").uncheck()
                start_calls = await page.evaluate("speechChatQA.calls.length")
                await page.locator(".preferences summary").click()
                assert await send(page, server, QUESTION) == REPLY
                assert await page.evaluate("speechChatQA.calls.length") == start_calls and len(calls) == 2
                row["disabled_voice_no_preimport_or_audio"] = True
                passive_reply = passive.get_by_test_id("turn").nth(2).get_by_test_id("final")
                assert await passive_reply.inner_text() == REPLY and not await passive.evaluate("speechChatQA.calls")
                row["unowned_tab_no_preimport_or_audio"] = True
                await passive.close()
                await page.locator(".preferences summary").click()
                await page.get_by_role("checkbox", name="MOSS 语音播报").check()
                await page.locator(".preferences summary").click()
                await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
                try:
                    assert await send(page, server, QUESTION) == REPLY
                    await page.wait_for_function("collectReadingMetrics().events.filter(e=>e.type==='end').length === 3")
                    hidden, = await page.evaluate(f"speechChatQA.calls.slice({start_calls})")
                    assert hidden["path"] == "/api/speak" and hidden["status"] == 200 and len(calls) == 3, hidden
                    row["simulated_hidden_tab_no_preimport"] = True
                finally:
                    await page.evaluate("delete document.hidden")
                assert not blocked and not errors, row
        finally:
            await context.close()
            await server.stop()


async def main(pair):
    folder = ROOT / "runs" / datetime.now().strftime("speech-chat-prepare-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    order = (False, True) if pair else (False, True, True, False)
    cache = {path.name: sha256(path.read_bytes()).hexdigest() for path in CACHE.glob("*.pt")}
    report = {"passed": False, "order": order, "question": QUESTION, "reply": REPLY,
              "model_delay_seconds": MODEL_DELAY, "reference_prompts": cache, "trials": []}
    try:
        assert cache, CACHE
        async with async_playwright() as playwright:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
                args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                      "--disable-renderer-backgrounding"])
            try:
                await confirm_muted_edge(browser)
                for index, enabled in enumerate(order, 1):
                    row = {"enabled": enabled}
                    report["trials"].append(row)
                    await trial(browser, folder / f"{index}-{'on' if enabled else 'off'}",
                                enabled, row, check_guards=index == 2)
                    print(f"{index} prepare={enabled} first audible {row['first_audible_ms']} ms", flush=True)
            finally:
                await browser.close()
        rows = report["trials"]
        assert len({row["wav_sha256"] for row in rows}) == 1, rows
        report["first_audible_median_ms"] = {name: round(median(
            row["first_audible_ms"] for row in rows if row["enabled"] is enabled), 1)
            for name, enabled in (("before", False), ("prepare", True))}
        report["paired_savings_ms"] = [round(rows[0]["first_audible_ms"] - rows[1]["first_audible_ms"], 1)]
        if not pair:
            report["paired_savings_ms"].append(round(rows[3]["first_audible_ms"] - rows[2]["first_audible_ms"], 1))
        report["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="用隔离回环 Holo 模型和静音 Edge 测普通短回复首声。")
    parser.add_argument("--pair", action="store_true", help="先执行一组前后对照，默认执行 ABBA 四组。")
    asyncio.run(main(parser.parse_args().pair))
