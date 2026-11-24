"""Measure weather wait and speech overlap in muted Edge."""

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
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_speech_continuity import MEASURE
from voice_output import MossSpeech


CLOCK = "2026年11月22日，星期日，15:42:10。"
SPOKEN_CLOCK = "现在下午三点四十二分。"
QUESTION = "现在几点？北京市朝阳区明天的天气怎么样？"
DAILY_WEATHER = "北京市朝阳区明天（2026-11-23）大致晴朗，最低 -1.0 °C，最高 6.7 °C，最高降水概率 0%。"
SHORT_WEATHER = "北京明天晴，气温七度。"
FORECAST_SECONDS = 1.15
SYNTHESIS_SECONDS = .74
# This run keeps one voice worker and uses no simultaneous ASR or 3D scene.
MIN_FREE_GPU_MIB = 3072
SCRIPTS = {"/":"conversation.html", "/static/agent-terminal.js":"agent-terminal.js",
           "/static/agent-speech.js":"agent-speech.js", "/static/speech-delivery.js":"speech-delivery.js"}

WATCH = """
localStorage.setItem('moss.voice.enabled','on');
window.leadQA = {at:0, partial:[], requests:[], final:null};
document.addEventListener('submit', e => {
  if(e.target.id === 'composer') leadQA.at=performance.now();
}, true);
window.addEventListener('moss-agent-state', e => {
  const current=e.detail.turns.at(-1);
  if(!current || !leadQA.at || leadQA.final) return;
  if(current.status==='thinking' && current.local_overview_partial?.length)
    leadQA.partial.push({at:performance.now(), marker:current.local_overview_speech_lead, kinds:current.local_overview_partial.map(row=>row.kind)});
  else if(['replied','cancelled'].includes(current.status))
    leadQA.final={at:performance.now(), turn:current};
});
const leadFetch = window.fetch;
window.fetch = async (url, options) => {
  if (url !== '/api/speak') return leadFetch(url,options);
  const row = {at:performance.now(), ...JSON.parse(options.body), aborted:false};
  leadQA.requests.push(row);
  options.signal.addEventListener('abort', () => {row.aborted=true}, {once:true});
  return leadFetch(url,options);
};
"""


async def run(candidate, *, cuda=False):
    prefix = "local-weather-voice-cuda" if cuda else "local-weather-voice-lead"
    folder = ROOT / "runs" / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    proof = folder / "result.json"
    print(proof, flush=True)
    chosen = {url:(candidate / file).read_bytes() for url,file in SCRIPTS.items()}
    fixture = (ROOT / "voice/lines/done.wav").read_bytes()
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed":False, "model_connections":0, "arms":[], "page_errors":[],
              "candidate_asset_sha256":{url:sha256(data).hexdigest() for url,data in chosen.items()},
              "forecast_seconds":FORECAST_SECONDS, "cuda":cuda}
    if not cuda:
        report.update(fixture_synthesis_seconds=SYNTHESIS_SECONDS, fixture_wave_sha256=sha256(fixture).hexdigest())
    environment = {"MOSS_MEDIA_DB":str(catalog), "MOSS_PROVIDER_DIR":str(folder / "providers"),
                   "LOCALAPPDATA":str(folder / "profile"), "MOSS_MEM0_ENABLED":"0", "MOSS_WINDOWS_REMINDERS":"0",
                   "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0", "MOSS_SPEECH_MIC_WARM":"0"}
    stopped = asyncio.Event()
    row = None

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def weather(*args, **kwargs):
        row["forecast_at"] = perf_counter()
        try:
            await asyncio.sleep(3 if row["case"] == "cancel" else FORECAST_SECONDS)
        except asyncio.CancelledError:
            row["forecast_cancelled"] = True
            raise
        return (SHORT_WEATHER if row["case"] == "compact" else DAILY_WEATHER), None

    async def local_output(intent):
        await asyncio.sleep(.12)
        return "系统音量为 32%，当前未静音。"

    lock = asyncio.Lock()

    async def speak(_speaker, text, disconnected, category="reply", cached_only=False, *, timings=None, room_amount=100):
        state = row
        request = {"text":text, "room":room_amount}
        state["synthesis"].append(request)
        async with lock:
            for _ in range(round(SYNTHESIS_SECONDS / .02)):
                if disconnected():
                    request["cancelled"] = True
                    raise ConnectionAbortedError()
                await asyncio.sleep(.02)
            request["completed"] = True
            return fixture

    async def check(browser, moss, base, variant, scenario):
        nonlocal row
        row = {"variant":variant, "case":scenario, "synthesis":[]}
        report["arms"].append(row)
        await moss.new_chat()
        context = await browser.new_context(viewport={"width":1440, "height":900}, reduced_motion="reduce", service_workers="block")
        hits = set()
        try:
            async def route_request(route):
                url = urlsplit(route.request.url)
                if not route.request.url.startswith(base + "/"):
                    await route.abort()
                elif variant == "candidate" and url.path in chosen:
                    mime = "text/html" if url.path == "/" else "text/javascript"
                    await route.fulfill(body=chosen[url.path], content_type=mime + "; charset=utf-8")
                    hits.add(url.path)
                else:
                    await route.continue_()

            await context.route("**/*", route_request)
            page = await context.new_page()
            audio = []
            page.on("response", lambda reply:audio.append(reply) if reply.url == base + "/api/speak" and reply.ok else None)
            await page.add_init_script(MEASURE + WATCH)
            page.on("pageerror", lambda exc:report["page_errors"].append(str(exc)))
            await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            await page.bring_to_front()
            query = ({"weather-first":"明天北京天气怎么样？现在几点？",
                      "device-only":"现在几点？系统音量多大？"}).get(scenario, QUESTION)
            await page.get_by_test_id("composer").fill(query)
            await page.get_by_test_id("send").click()
            if scenario in {"room-change", "cancel"}:
                await page.wait_for_function("leadQA.requests.length === 1")
                assert not await page.evaluate("Boolean(leadQA.final) || Boolean(speechTest.clips.length)")
                if scenario == "cancel":
                    await page.locator("#local-status-cancel").click()
                    await page.wait_for_function("leadQA.final?.turn?.status === 'cancelled' && leadQA.requests[0].aborted")
                else:
                    await page.evaluate("localStorage.setItem('moss.speech.space.percent', '50')")
            if scenario != "cancel":
                await page.wait_for_function("speechTest.events.some(e=>e.type==='end'||e.type==='error')",
                                             timeout=50000 if cuda else 18000)
            state = await page.evaluate("({trace:leadQA, clips:speechTest.clips, events:speechTest.events})")
            row.update(state)
            trace, clips = state["trace"], state["clips"]
            assert trace["at"] and trace["final"] and "local_overview_speech_lead" not in trace["final"]["turn"], row
            if variant == "candidate":
                assert hits == set(chosen), hits
            if scenario == "cancel":
                assert row.get("forecast_cancelled") and not clips and len(trace["requests"]) == 1, row
                return
            assert state["events"][-1]["type"] == "end" and clips and trace["final"]["turn"]["status"] == "replied", row
            assert all(clip["scheduled"] >= trace["final"]["at"] - .1 for clip in clips), row
            row["first_audio_ms"] = round(clips[0]["scheduled"] - trace["at"] + 1000 * (clips[0]["first"] - clips[0]["start"]), 1)
            row["first_audio_after_final_ms"] = round(row["first_audio_ms"] - (trace["final"]["at"] - trace["at"]), 1)
            requests = trace["requests"]
            if scenario == "compound":
                expected = [SPOKEN_CLOCK, DAILY_WEATHER] if variant == "candidate" else [SPOKEN_CLOCK + DAILY_WEATHER]
                assert len(clips) == len(requests) == len(expected)
                assert [request["text"] for request in requests] == expected, row
                lead = trace["final"]["at"] - requests[0]["at"]
                assert lead >= 700 if variant == "candidate" else lead <= 10, row
                row["speak_lead_ms"] = round(lead, 1)
                if len(clips) == 2:
                    row["scheduled_gap_ms"] = round(1000 * (clips[1]["start"] - clips[0]["end"]), 1)
                    row["audible_gap_ms"] = round(1000 * (clips[1]["first"] - clips[0]["last"]), 1)
                    assert row["scheduled_gap_ms"] <= 250, row
                assert len(audio) == len(expected) and all(
                    reply.headers.get("content-type", "").startswith("audio/wav") for reply in audio)
                rows = [await reply.body() for reply in audio]
                row["wave_sha256"] = [sha256(data).hexdigest() for data in rows]
                if cuda:
                    row["waves"] = []
                    for index, data in enumerate(rows, 1):
                        name = f"{len(report['arms'])}-{variant}-{index}.wav"
                        (folder / name).write_bytes(data)
                        row["waves"].append(name)
            elif scenario == "compact":
                expected = [SPOKEN_CLOCK, SHORT_WEATHER] if variant == "candidate" else [SPOKEN_CLOCK + SHORT_WEATHER]
                assert len(clips) == len(requests) == len(expected)
                assert [req["text"] for req in requests] == expected and not any(req["aborted"] for req in requests), row
            elif scenario == "room-change":
                assert len(clips) == 2 and len(requests) == 3 and requests[0]["aborted"], row
                assert all(req["room_amount"] == 50 for req in requests[1:]), row
            else:
                assert all(not entry.get("marker") for entry in trace["partial"]), row
                assert requests[0]["at"] >= trace["final"]["at"] - .1, row
        finally:
            await context.close()

    async def notify(moss):
        stopped.set()
        moss._notify()

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch("assistant_brain.fetch_weather", weather))
            stack.enter_context(patch("local_overview.local_clock_reply", return_value=CLOCK))
            stack.enter_context(patch("local_overview.run_desktop", local_output))
            if not cuda:
                stack.enter_context(patch.object(MossSpeech, "render", speak))
            guard_model = await stack.enter_async_context(await asyncio.start_server(guard, "127.0.0.1", 0))
            model = f"http://127.0.0.1:{guard_model.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model
                    os.environ[prefix + "_API_KEY"] = "isolated-qa"
            port, data = free_port(), folder / "data"
            moss = Moss(data, model, "isolated")
            stack.push_async_callback(moss.close)
            actual = MossSpeech(data, idle_seconds=180) if cuda else None
            if actual:
                stack.enter_context(patch("serve.MossSpeech", return_value=actual))
                original_worker_reply = actual._worker_reply

                async def audited(request, cancel, disconnected, timeout):
                    reply = await original_worker_reply(request, cancel, disconnected, timeout)
                    info = reply["info"]
                    assert reply["code"] == 0 and info["device"] == info["decoder"] == "cuda" and info["steps"] == 16, info
                    if row:
                        row["synthesis"].append({"text":request["text"], "worker_pid":actual.worker.pid,
                                                 "generated_ms":1000 * info["generation_seconds"]})
                    return reply

                actual._worker_reply = audited
            runner = web.AppRunner(application(moss, lambda:"standby", data, port, "weather-voice-lead", stopped),
                                   access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            stack.push_async_callback(notify, moss)
            await web.TCPSite(runner, "127.0.0.1", port).start()
            playwright = await stack.enter_async_context(async_playwright())
            browser = await open_browser(playwright)
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)
            base = f"http://127.0.0.1:{port}"

            async def pair():
                if actual:
                    report["prime_ms"] = await warm_worker(base)
                    report["worker_pid"] = actual.worker.pid
                for variant in ("baseline", "candidate", "candidate", "baseline"):
                    if actual:
                        assert actual.worker.pid == report["worker_pid"] and actual.worker.returncode is None
                        actual.lines.clear()
                    await check(browser, moss, base, variant, "compound")

            if actual:
                from verify_spoken_speech_prepare_ab import (other_speaker, resource_row, resources,
                                                           warm_worker, watch_resources)

                sample = await resources()
                report.update(resource_row(sample), required_free_gpu_mib=MIN_FREE_GPU_MIB)
                if min(sample["ram_mib"], sample["commit_mib"]) < 8192 or sample["gpu_mib"] < MIN_FREE_GPU_MIB or other_speaker():
                    raise RuntimeError(f"语音对照资源不足或已有合成进程。{sample}")
                task = asyncio.create_task(pair())
                try:
                    await watch_resources(task, report)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                assert all(len(arm["synthesis"]) == (2 if arm["variant"] == "candidate" else 1) and
                           all(item["worker_pid"] == report["worker_pid"]
                           for item in arm["synthesis"]) for arm in report["arms"]), report
            else:
                await pair()
                await check(browser, moss, base, "baseline", "compact")
                for scenario in ("compact", "weather-first", "device-only", "room-change", "cancel"):
                    await check(browser, moss, base, "candidate", scenario)
        assert not report["page_errors"] and report["model_connections"] == 0, report
        report["first_audio_ms"] = {variant:median(data["first_audio_ms"] for data in report["arms"]
                                                  if data["variant"] == variant and data["case"] == "compound")
                                    for variant in ("baseline", "candidate")}
        report["first_audio_after_final_ms"] = {variant:median(data["first_audio_after_final_ms"] for data in report["arms"]
                                                              if data["variant"] == variant and data["case"] == "compound")
                                                for variant in ("baseline", "candidate")}
        report["identical_wave_within_variant"] = {
            variant:len({tuple(data["wave_sha256"]) for data in report["arms"]
                         if data["case"] == "compound" and data["variant"] == variant}) == 1
            for variant in ("baseline", "candidate")
        }
        assert all(report["identical_wave_within_variant"].values()) and (report["first_audio_after_final_ms"]["baseline"] -
                                                   report["first_audio_after_final_ms"]["candidate"]) >= 500, report
        if not cuda:
            report["compact_first_audio_after_final_ms"] = {
                data["variant"]:data["first_audio_after_final_ms"] for data in report["arms"] if data["case"] == "compact"
            }
        report["passed"] = True
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        proof.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--cuda", action="store_true", help="使用真实热态 CUDA 合成")
    args = parser.parse_args()
    asyncio.run(run(args.candidate, cuda=args.cuda))
