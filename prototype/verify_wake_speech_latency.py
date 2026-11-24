import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import median
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import async_playwright
from psutil import NoSuchProcess
from pydantic import SecretStr

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from device_status import memory_sample
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application
from verify_context_reply import free_port
from verify_context_voice_e2e import make_voice
from verify_duplex_voice_browser import PROBE as INPUT_PROBE
from verify_speech_chat_prepare import first_audible
from verify_speech_cold_stages import standby_resources
from verify_speech_long_reading import PROBE as OUTPUT_PROBE
from verify_speech_prepare import workers
from verify_speech_prepare_ab import free_gpu, other_speaker
from verify_spoken_speech_prepare_ab import ACTIVITY_PROBE, ACTIVITY_QUESTION, server_timings
from verify_voice_wake import make_inputs
from voice_output import MossSpeech


SCRIPTS = ("agent-voice.js", "agent-speech.js", "agent-terminal.js")
BATTERY = "电量 61%（使用电池）。"
VOLUME = "系统音量为 43%，当前未静音。"
SPEECH = "电量61%，使用电池。系统音量43%，未静音。"
WAKE_GAP_MS = 2000
MIB = 2**20
WAKE_PROBE = """
window.wakeQA = {accepted:null, preflight:[]};

// Exclude file input sources from the answer's playback metrics.
const measuredStart = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...args) {
  return (this.context === input?.context ? originalStart : measuredStart).apply(this,args);
};

const duplexFetch = window.fetch;
window.fetch = async function(url, options) {
  if (url !== '/api/prepare-speech') return duplexFetch.apply(this,arguments);
  const event = {at:performance.now(), body:JSON.parse(options.body),
    statusText:document.querySelector('#wake-status').textContent,
    focus:document.hasFocus(), hidden:document.hidden};
  wakeQA.preflight.push(event);
  const response = await duplexFetch.apply(this,arguments);
  event.status = response.status;
  event.complete = performance.now();
  return response;
};

new MutationObserver(() => {
  if (wakeQA.accepted !== null || !document.querySelector('#wake-status')?.textContent.includes('已唤醒')) return;
  wakeQA.accepted = performance.now();
  if (window.wakeQuestionDelay)
    setTimeout(() => sayFixture('question'), window.wakeQuestionDelay);
}).observe(document, {subtree:true, childList:true, characterData:true});
"""


async def battery(intent):
    assert intent["metric"] == "battery", intent
    return BATTERY


async def volume(intent):
    assert intent["action"] == "get_volume", intent
    return VOLUME


def normalized(text):
    return re.sub(r"[\W_]+", "", text or "").lower()


async def resources():
    memory = memory_sample()
    return {"ram": memory.available // MIB, "commit": memory.available_pagefile // MIB,
            "gpu": await asyncio.to_thread(free_gpu)}


async def check_unused_prepare_releases(server):
    clock = asyncio.get_running_loop().time
    start = clock()
    peak_rss = peak_private = 0.0
    while clock() - start < 18:
        try:
            sample = await standby_resources(server)
        except NoSuchProcess:
            continue
        assert not sample["cuda_pids"], sample
        if not sample["pids"]:
            return {"released_after_ms": round((clock() - start) * 1000, 1),
                    "peak_rss_mib": peak_rss, "peak_private_mib": peak_private}
        peak_rss = max(peak_rss, sample["rss_mib"])
        peak_private = max(peak_private, sample["private_mib"])
        await asyncio.sleep(.5)
    raise AssertionError("唤醒后没有请求语音合成，CPU 进程仍未释放。")


async def collect(page, server, moss, scripts, fixtures, row, *, preflight_only):
    selected = row["variant"]
    enabled = selected != "speech-disabled"
    ahead = selected == "candidate"
    should_speak = enabled and not preflight_only
    ask_question = should_speak or not enabled
    row["outside_requests"] = outside = []
    row["page_errors"] = errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    responses = []
    page.on("response", lambda response: responses.append(response)
            if response.url == server.url + "/api/speak" else None)
    row["injected_scripts"] = injected = []
    row["unexpected_speech_requests"] = unexpected_speech = []

    async def local_only(route):
        request = urlsplit(route.request.url)
        if request.scheme in {"http", "https"} and not route.request.url.startswith(server.url + "/"):
            outside.append(route.request.url)
            await route.abort()
        elif not should_speak and request.path in {"/api/speak", "/api/speak-batch"}:
            unexpected_speech.append(request.path)
            await route.abort()
        elif request.path.startswith("/fixture/") and request.path.removeprefix("/fixture/") in fixtures:
            await route.fulfill(body=fixtures[request.path.removeprefix("/fixture/")], content_type="audio/wav")
        elif request.path.startswith("/static/") and request.path.removeprefix("/static/") in scripts:
            name = request.path.removeprefix("/static/")
            injected.append(name)
            await route.fulfill(body=scripts[name], content_type="text/javascript")
        else:
            await route.continue_()

    await page.context.route("**/*", local_only)
    await page.add_init_script(OUTPUT_PROBE + INPUT_PROBE + ACTIVITY_PROBE + WAKE_PROBE + (
        f"localStorage.setItem('moss.voice.enabled','{'on' if enabled else 'off'}');"
        f"window.wakeQuestionDelay = {WAKE_GAP_MS if ask_question else 0};"
    ))
    try:
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(timeout=15000)
        assert set(injected) == set(SCRIPTS), injected
        state = await server.state()
        assert state["speech_prepare"] is True and state["speech_mic_warm"] is False, state
        await page.bring_to_front()
        await page.locator(".preferences summary").click()
        wake = page.locator("#wake-listen")
        spoken = page.get_by_role("checkbox", name="MOSS 语音播报")
        assert (await spoken.is_checked()) == enabled
        await wake.check()
        await page.wait_for_function("document.getElementById('wake-status').textContent === '等待唤醒'")
        await page.wait_for_function(
            "duplexQa.calls.some(row => row.url === '/api/prepare-voice' && row.status === 204)",
            timeout=45000)
        row["before_input_pids"] = workers(server)
        assert not row["before_input_pids"], row["before_input_pids"]

        await page.evaluate("sayFixture('ignored')")
        ignored = await page.wait_for_function("""() => duplexQa.calls.find(row =>
            row.url === '/api/transcribe' && row.status === 200 && typeof row.text === 'string')""", timeout=16000)
        assert "媒体库" in (await ignored.json_value())["text"]
        assert not await page.evaluate("wakeQA.preflight.length || wakeQA.accepted !== null")
        assert not moss.state()["turns"] and not workers(server)

        await page.evaluate("sayFixture('wake')")
        await page.wait_for_function("wakeQA.accepted !== null", timeout=16000)
        if ahead:
            await page.wait_for_function("wakeQA.preflight.some(row => row.status === 204)", timeout=1900)
        before_command = row["after_wake_resources"] = await standby_resources(server)
        assert bool(before_command["pids"]) == ahead and not before_command["cuda_pids"], before_command
        assert not moss.state()["turns"] and not responses
        if should_speak:
            await page.wait_for_function("readingFinished()", timeout=140000)
        elif ask_question:
            final = page.get_by_test_id("turn").last.get_by_test_id("final")
            await final.wait_for(timeout=16000)
            assert (await final.inner_text()).strip() == BATTERY + "\n" + VOLUME
            await page.wait_for_function(
                "duplexQa.calls.some(row => row.url === '/api/message' && row.status === 200)")
        elif ahead:
            row["unused_prepare"] = await check_unused_prepare_releases(server)
        else:
            await page.wait_for_function("performance.now() - wakeQA.accepted >= 2300")
    finally:
        trace = row["browser"] = await page.evaluate("""() => ({
            playback:globalThis.collectReadingMetrics?.(), input:globalThis.micInput,
            duplex:globalThis.duplexSnapshot?.(), wake:globalThis.wakeQA
        })""")

    assert not errors and not outside and not unexpected_speech, row
    accepted = trace["wake"]["accepted"]
    duplex = trace["duplex"]
    asr = [entry for entry in duplex["calls"] if entry["url"] == "/api/transcribe"]
    assert len(asr) == 2 + ask_question and all(entry["status"] == 200 for entry in asr), asr
    assert re.fullmatch(r"你好(?:莫斯|摩斯|墨斯|moss)", normalized(asr[1]["text"])), asr
    preflight = trace["wake"]["preflight"]
    assert len(preflight) == ahead, preflight
    if ahead:
        prepared, = preflight
        assert prepared["body"] == {} and prepared["status"] == 204, prepared
        assert prepared["focus"] and not prepared["hidden"] and "已唤醒" in prepared["statusText"], prepared
        assert asr[1]["at"] < prepared["at"] <= accepted + 25, prepared
        if ask_question:
            assert prepared["complete"] < duplex["queued"][-1]["at"], prepared
            row["prepare_before_command_ms"] = round(duplex["queued"][-1]["at"] - prepared["at"], 1)
    if not should_speak:
        assert not responses and not trace["playback"]["clips"] and not trace["playback"]["events"], row
    if not ask_question:
        assert not moss.state()["turns"], row
        return

    queued = duplex["queued"]
    assert [entry["name"] for entry in queued] == ["ignored", "wake", "question"], queued
    command_at = queued[-1]["at"]
    row["wake_to_command_ms"] = round(command_at - accepted, 1)
    assert WAKE_GAP_MS - 15 <= row["wake_to_command_ms"] < WAKE_GAP_MS + 400, queued
    assert accepted < command_at < asr[2]["at"], asr
    assert normalized(asr[2]["text"]) == normalized(ACTIVITY_QUESTION), asr

    turn, = moss.state()["turns"]
    assert turn["status"] == "replied" and turn["source"] == "context", turn
    assert turn["user"] == asr[2]["text"] and turn["reply"] == BATTERY + "\n" + VOLUME, turn
    assert [(item["kind"], item["reply"]) for item in turn["local_overview"]] == [
        ("battery", BATTERY), ("volume", VOLUME)], turn
    assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, turn
    row.update(user=turn["user"], reply=turn["reply"], cloud_requests=turn["cloud_requests"]["total"])
    message, = [entry for entry in duplex["calls"] if entry["url"] == "/api/message"]
    assert message["status"] == 200 and message["text"] == turn["user"], message
    if not should_speak:
        return

    response, = responses
    payload = row["speech_request"] = response.request.post_data_json
    assert response.ok and payload == {"text": SPEECH, "category": "reply", "prefer_cached": False}, payload
    stats = row["server_timing_ms"] = server_timings(response.headers)
    assert stats["load"] > 0 and stats["generate"] > 0 and ("spawn" in stats) != ahead, stats
    job, = row["worker_jobs"]
    assert job["device"] == "cuda" and job["mode"] == "single" and not job["mic_lease"], job
    row["reply_worker_pids"] = workers(server)
    assert set(before_command["pids"]) <= set(row["reply_worker_pids"]), row
    assert job["pid"] in row["reply_worker_pids"], job
    audio = await response.body()
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE", response.headers
    recorded = server.folder / "reply.wav"
    recorded.write_bytes(audio)
    with wave.open(str(recorded), "rb") as wav:
        row["pcm_sha256"] = hashlib.sha256(wav.readframes(wav.getnframes())).hexdigest()
    row["wav_sha256"] = hashlib.sha256(audio).hexdigest()

    playback = trace["playback"]
    clip, = playback["clips"]
    assert playback["events"][-1]["type"] == "end" and clip["first"] < clip["last"], playback
    frames = [stamp for stamp in trace["input"]["voice"] if command_at <= stamp < asr[2]["at"]]
    assert frames, trace["input"]
    row["input_tail_to_asr_ms"] = round(asr[2]["at"] - frames[-1], 1)
    row["asr_to_message_ms"] = round(message["at"] - asr[2]["at"], 1)
    row["first_pcm_from_input_tail_ms"], row["first_pcm_clock"] = first_audible(clip, frames[-1])
    row["first_pcm_from_message_ms"], _ = first_audible(clip, message["at"])
    assert row["first_pcm_from_message_ms"] > 0, row
    assert row["first_pcm_clock"] == "web_audio_output_timestamp", clip


async def measure(browser, folder, model_url, scripts, fixtures, row, *, preflight_only, min_gpu_mib):
    synthesize = not preflight_only and row["variant"] != "speech-disabled"
    folder.mkdir()
    clock = asyncio.get_running_loop().time
    waiting_since = clock()
    start = await resources()
    while synthesize and start["gpu"] < min_gpu_mib and clock() - waiting_since < 20 and not other_speaker():
        await asyncio.sleep(1)
        start = await resources()
    row["resource_wait_ms"] = round((clock() - waiting_since) * 1000, 1)
    row["start_free_mib"] = start
    row["min_free_mib"] = start.copy()
    if min(start["ram"], start["commit"]) < 8192 or (synthesize and start["gpu"] < min_gpu_mib) or other_speaker():
        raise RuntimeError(f"语音测试需要独占资源。可用资源 MiB {start}。")

    profiles = {
        key: Profile(key, label, model_url, model, SecretStr("isolated-only" if key == "deepseek" else ""))
        for key, (label, _, __, model) in SPECS.items()
    }
    with patch("core.profiles", return_value=profiles):
        moss = Moss(folder / "data", model_url, "isolated-unused", prefer_cloud=True)
    assert moss.model_id == "deepseek"
    server = BrowserServer(folder, free_port(), model_url=model_url)
    server.pid = os.getpid()
    app = application(moss, lambda: "standby", server.data, server.port, "isolated", asyncio.Event(),
                      speech_standby_seconds=0)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    row["worker_jobs"] = jobs = []
    worker_reply = MossSpeech._worker_reply

    async def trace_job(speech, request, *args, **kwargs):
        pid = speech.worker.pid
        lease = speech._mic_key is not None
        result = await worker_reply(speech, request, *args, **kwargs)
        info = result.get("info", {})
        jobs.append({"pid": pid, "device": info.get("device"), "decoder": info.get("decoder"),
                     "mode": info.get("mode"), "mic_lease": lease, "preload_job": bool(request.get("prepare"))})
        return result

    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", server.port).start()
        context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
        try:
            page = await context.new_page()
            with patch.object(MossSpeech, "_worker_reply", trace_job):
                task = asyncio.create_task(collect(page, server, moss, scripts, fixtures, row,
                                                   preflight_only=preflight_only))
                try:
                    while not task.done():
                        free = await resources()
                        for key, value in free.items():
                            row["min_free_mib"][key] = min(row["min_free_mib"][key], value)
                        if min(free["ram"], free["commit"]) < 4500 or (synthesize and free["gpu"] < 512):
                            raise RuntimeError(f"资源不足，已停止语音测试。可用资源 MiB {free}。")
                        await asyncio.wait({task}, timeout=.8)
                    await task
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        finally:
            await context.close()
    finally:
        await runner.cleanup()
        await moss.close()
        row["after_shutdown_pids"] = workers(server)
        (folder / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not row["after_shutdown_pids"], row["after_shutdown_pids"]
    assert not any(job["preload_job"] for job in jobs), jobs
    if not synthesize:
        assert not jobs, jobs


def variant_scripts(path):
    return {name: (path / name).read_bytes() for name in SCRIPTS}


def compare_synthesized_rounds(rows):
    rounds = [row for row in rows if row["variant"] != "speech-disabled"]
    # SenseVoice may omit a comma without altering the ordered words or the local facts.
    assert len({normalized(row["user"]) for row in rounds}) == 1, [row["user"] for row in rounds]
    for key in ("reply", "pcm_sha256", "wav_sha256"):
        assert len({row[key] for row in rounds}) == 1, [(row["variant"], row[key]) for row in rounds]
    summary = {}
    for key in ("first_pcm_from_input_tail_ms", "first_pcm_from_message_ms"):
        medians = {group: round(median(row[key] for row in rounds if row["variant"] == group), 1)
                   for group in ("baseline", "candidate")}
        summary[key] = {**medians, "saved": round(medians["baseline"] - medians["candidate"], 1)}
    return summary


async def run(baseline, candidate, order, folder, *, preflight_only=False, min_gpu_mib=3800):
    variants = {"baseline": variant_scripts(baseline), "candidate": variant_scripts(candidate)}
    if variants["candidate"] == variants["baseline"]:
        raise ValueError("候选与基线的浏览器脚本相同。")
    folder.mkdir(parents=True)
    print(folder, flush=True)
    report = {"passed": False, "model_connections": 0, "rounds": [],
              "input": "Windows Huihui WAV fixtures → Edge Web Audio microphone stream → SenseVoice",
              "preflight_only": preflight_only, "min_start_gpu_mib": min_gpu_mib,
              "output": "disabled for this run" if preflight_only else "CUDA OmniVoice → muted Edge AudioContext PCM",
              "wake_gap_ms": WAKE_GAP_MS, "standby_seconds": 0, "question": ACTIVITY_QUESTION,
              "scripts": {group: {name: hashlib.sha256(data).hexdigest() for name, data in scripts.items()}
                          for group, scripts in variants.items()}}

    async def reject_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        wake_dir, question_dir = folder / "wake-fixtures", folder / "question-fixture"
        make_inputs(wake_dir)
        question_dir.mkdir()
        make_voice(question_dir, text=ACTIVITY_QUESTION)
        fixtures = {"ignored": (wake_dir / "background.wav").read_bytes(),
                    "wake": (wake_dir / "wake.wav").read_bytes(),
                    "question": (question_dir / "command.wav").read_bytes()}
        catalog = folder / "empty-catalog.sqlite3"
        MediaLibrary(catalog)
        settings = {"MOSS_MEDIA_DB": str(catalog), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                    "LOCALAPPDATA": str(folder / "profile"), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                    "MOSS_SPEECH_PREPARE": "1", "MOSS_SPEECH_STANDBY": "0", "MOSS_SPEECH_MIC_WARM": "0",
                    "MOSS_KEV_ENABLED": "0"}
        async with await asyncio.start_server(reject_model, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    settings[prefix + "_BASE_URL"] = model_url
            with (patch.dict(os.environ, settings),
                  patch.multiple("local_overview", device_status=battery, run_desktop=volume)):
                async with async_playwright() as playwright:
                    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                    browser = await playwright.chromium.launch(
                        executable_path=str(edge), headless=True,
                        args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                              "--disable-renderer-backgrounding", "--use-fake-ui-for-media-stream"])
                    try:
                        await confirm_muted_edge(browser)
                        compared = ("baseline", "candidate") if preflight_only else order
                        for index, label in enumerate((*compared, "speech-disabled"), 1):
                            row = {"variant": label}
                            report["rounds"].append(row)
                            scripts = variants["baseline" if label == "baseline" else "candidate"]
                            await measure(browser, folder / f"{index}-{label}", model_url, scripts, fixtures, row,
                                          preflight_only=preflight_only, min_gpu_mib=min_gpu_mib)
                            print(label, row.get("first_pcm_from_input_tail_ms", "已验证"), flush=True)
                    finally:
                        await browser.close()
        assert not report["model_connections"], report["model_connections"]
        if not preflight_only:
            report["median_ms"] = compare_synthesized_rounds(report["rounds"])
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="比较唤醒后短答首次音频的浏览器端到端延迟。")
    parser.add_argument("--baseline", type=Path, default=ROOT / "static",
                        help="包含三个基线浏览器脚本的目录，默认使用部署脚本")
    parser.add_argument("--candidate", type=Path, required=True, help="包含三个候选浏览器脚本的目录")
    parser.add_argument("--order", choices=("ab", "abba"), default="abba", help="合成测试的基线与候选运行顺序")
    parser.add_argument("--preflight-only", action="store_true",
                        help="用真实语音识别验证唤醒预导入和关闭播报后的文字答复")
    parser.add_argument("--min-gpu-mib", type=int, default=3800,
                        help="短答换序测试每轮所需的空闲显存")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "runs" / datetime.now().strftime("wake-speech-latency-%Y%m%d-%H%M%S-%f"))
    args = parser.parse_args()
    # Match the existing short-reply benchmark floor under live resource monitoring.
    if args.min_gpu_mib < 3200:
        parser.error("本短答对照每轮至少需要 3200 MiB 空闲显存")
    order = tuple({"a": "baseline", "b": "candidate"}[letter] for letter in args.order)
    asyncio.run(run(args.baseline.resolve(), args.candidate.resolve(), order, args.out.resolve(),
                    preflight_only=args.preflight_only, min_gpu_mib=args.min_gpu_mib))
