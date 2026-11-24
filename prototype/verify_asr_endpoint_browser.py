"""Compare foreground endpoint and preview timing with actual CPU SenseVoice and muted Edge."""

import argparse
import asyncio
from array import array
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from statistics import median
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
import core
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_context_voice_e2e import make_voice


ASSETS = ("agent-voice.js", "voice-endpoint.js")
CASES = ("closed", "append", "correction")
CLOCK = "2031年08月17日，星期日，15:42:17。"
BATTERY = "电量 63%（使用电池）。"
VOLUME = "系统音量为 37%，当前未静音。"
CORRECTED = "隔离验收已收到完整纠正。"
RATE = 16000
PROBE = """
localStorage.setItem('moss.voice.enabled','off');
window.endpointQa = {asr:[], posts:[], endpoints:[], waves:[], pending:[], voice:[]};
window.qaEndpoint = (elapsed, lastVoice) => {
  endpointQa.endpoints.push({at:performance.now(), elapsed, lastVoice});
};
const hexDigest = async wave => Array.from(
  new Uint8Array(await crypto.subtle.digest('SHA-256', await wave.arrayBuffer())),
  byte => byte.toString(16).padStart(2,'0')).join('');
window.qaWave = wave => {
  const row = {at:performance.now(), bytes:wave.size};
  endpointQa.waves.push(row);
  endpointQa.pending.push(hexDigest(wave).then(digest => row.sha256 = digest));
  return wave;
};
const Worklet = window.AudioWorkletNode;
window.AudioWorkletNode = class extends Worklet {
  constructor(context, name, options) {
    super(context, name, options);
    if (name !== 'moss-voice-capture') return;
    this.port.addEventListener('message', ({data}) => {
      if (!(data instanceof Float32Array) || !data.length) return;
      endpointQa.frameMs = data.length / context.sampleRate * 1000;
      const rms = Math.sqrt(data.reduce((sum, sample) => sum + sample * sample, 0) / data.length);
      if (rms >= .002) endpointQa.voice.push(performance.now());
    });
  }
};
const realFetch = window.fetch;
window.fetch = async function(url, options) {
  if (url !== '/api/transcribe' && url !== '/api/message') return realFetch.apply(this, arguments);
  const row = {at:performance.now()};
  if (url === '/api/transcribe') {
    row.preview = new Headers(options.headers).get('X-MOSS-ASR-Preview') === '1';
    row.bytes = options.body.size;
    endpointQa.asr.push(row);
    endpointQa.pending.push(hexDigest(options.body).then(digest => row.sha256 = digest));
  } else {
    row.text = JSON.parse(options.body).text;
    endpointQa.posts.push(row);
  }
  const response = await realFetch.apply(this, arguments);
  row.status = response.status;
  row.headersAt = performance.now();
  if (url === '/api/transcribe') {
    const read = response.json.bind(response);
    response.json = async (...args) => {
      const body = await read(...args);
      row.completedAt = performance.now();
      row.text = body.text;
      return body;
    };
  }
  return response;
};
window.endpointSnapshot = async () => {
  await Promise.allSettled(endpointQa.pending);
  const {pending, ...result} = endpointQa;
  return result;
};
"""


def words(text):
    return re.sub(r"[\W_]+", "", text)


def trimmed_clause(folder, text):
    folder.mkdir()
    make_voice(folder, text=text)
    with wave.open(str(folder / "command.wav"), "rb") as source:
        assert source.getparams()[:3] == (1, 2, RATE)
        samples = array("h", source.readframes(source.getnframes()))
    # Preserve the whole first and last active 10 ms windows of each clause.
    block = RATE // 100
    heard = [start for start in range(0, len(samples), block)
             if sum(value * value for value in samples[start:start + block]) >= block * 65**2]
    assert heard, text
    return samples[heard[0]:heard[-1] + block].tobytes()


def fixtures(folder):
    prefix = trimmed_clause(folder / "prefix", "现在几点了？")
    ending = trimmed_clause(folder / "append", "电脑还剩多少电？音量多大？")
    correction = trimmed_clause(folder / "correction", "不对，我问的是电脑还剩多少电。")
    waves = {}
    for name, after, gap in (("closed", b"", 0), ("append", ending, 1.12), ("correction", correction, 1.31)):
        path = folder / (name + ".wav")
        with wave.open(str(path), "wb") as target:
            target.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            target.writeframes(bytes(RATE) + prefix + bytes(round(gap * RATE) * 2) + after + bytes(RATE * 7))
        waves[name] = {"path":path, "gap_s":gap, "sha256":sha256(path.read_bytes()).hexdigest()}
    return waves


def script_set(path):
    parent = path if path.is_dir() else path.parent
    files = {name: parent / name for name in ASSETS}
    if path.is_file():
        files["agent-voice.js"] = path
    if not files["voice-endpoint.js"].is_file():
        files["voice-endpoint.js"] = ROOT / "static/voice-endpoint.js"
    return {name: file.read_text(encoding="utf-8") for name, file in files.items()}


def instrument(name, source):
    if name == "agent-voice.js":
        before = 'return new Blob([buffer], {type: "audio/wav"});'
        after = 'return window.qaWave(new Blob([buffer], {type: "audio/wav"}));'
    else:
        before = 'return "done";'
        after = 'return (window.qaEndpoint(elapsed,lastVoice), "done");'
    assert source.count(before) == 1, name
    return source.replace(before, after)


async def trial(playwright, server, assets, variant, case, fixture, report):
    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    browser = await playwright.chromium.launch(executable_path=str(edge), headless=True,
        args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
              "--disable-renderer-backgrounding", "--use-fake-device-for-media-stream",
              "--use-fake-ui-for-media-stream", f"--use-file-for-fake-audio-capture={fixture['path']}%noloop"])
    row = {"variant":variant, "case":case, "assets":{}, "blocked":[], "errors":[]}
    report["trials"].append(row)
    try:
        await confirm_muted_edge(browser)
        context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
        page = await context.new_page()
        page.on("pageerror", lambda error: row["errors"].append(str(error)))

        async def isolate(route):
            url = urlsplit(route.request.url)
            if (url.scheme in {"http", "https"} and not route.request.url.startswith(server.url + "/")) or url.path in {
                    "/api/speak", "/api/speak-batch", "/api/prepare-speech"}:
                row["blocked"].append(route.request.url)
                return await route.abort()
            if url.path in {f"/static/{name}" for name in ASSETS}:
                name = Path(url.path).name
                source = assets[name]
                row["assets"][name] = sha256(source.encode("utf-8")).hexdigest()
                return await route.fulfill(content_type="text/javascript", body=instrument(name, source))
            return await route.continue_()

        await context.route("**/*", isolate)
        await page.add_init_script(PROBE)
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(timeout=12000)
        await page.bring_to_front()
        async with page.expect_response(server.url + "/api/new-chat", timeout=5000) as created:
            await page.get_by_test_id("new-chat").click()
        assert (await created.value).ok
        receive_start = len(report["received"])
        await page.locator("#mic").click()
        await page.locator("#mic[aria-pressed=true]").wait_for(timeout=8000)
        await page.wait_for_function("endpointQa.posts.some(row=>row.status===200)", timeout=18000)
        await page.get_by_test_id("final").wait_for(timeout=7000)
        await page.wait_for_timeout(200)
        row.update(await page.evaluate("endpointSnapshot()"))
        row["received"] = report["received"][receive_start:]
        endpoint, = row["endpoints"]
        post, = row["posts"]
        turn, = (await server.state())["turns"]
        assert turn["status"] == "replied" and turn["cloud_requests"]["total"] == 0, turn
        assert turn["user"] == post["text"] and not row["errors"] and not row["blocked"], row
        assert set(row["assets"]) == set(ASSETS), row
        assert 1.55 <= endpoint["elapsed"] - endpoint["lastVoice"] <= 1.55 + row["frameMs"]/1000 + .01, row
        clip_end = [at for at in row["voice"] if at <= endpoint["at"]]
        assert clip_end and post["at"] >= endpoint["at"] > clip_end[-1], row
        if fixture["gap_s"]:
            measured_gap = max(second - first for first, second in zip(clip_end, clip_end[1:]))
            assert (fixture["gap_s"]-.12)*1000 <= measured_gap <= (fixture["gap_s"]+.17)*1000, row
            row["measured_gap_ms"] = round(measured_gap, 1)
        asr, = [item for item in row["asr"] if item.get("sha256") == row["waves"][-1]["sha256"]]
        assert asr["status"] == 200 and words(asr["text"]) == words(post["text"]) and asr["completedAt"] <= post["at"], row
        assert sorted(item["sha256"] for item in row["received"]) == sorted(item["sha256"] for item in row["asr"]), row
        text = words(post["text"])
        assert "现在几点" in text, row
        if case == "closed":
            assert turn["reply"] == CLOCK, turn
        else:
            assert re.search(r"电脑(?:还剩|还有|还剩余|有)多少电", text), row
            if case == "append":
                assert "音量多大" in text and [item["kind"] for item in turn["local_overview"]] == ["clock","battery","volume"], turn
                assert turn["reply"] == "\n".join((CLOCK, BATTERY, VOLUME)), turn
            else:
                assert "不对" in text and "我问的是" in text and turn["reply"] == CORRECTED, turn
        row.update(final_asr_preview=asr["preview"], transcript=post["text"], reply=turn["reply"],
                   input_to_final_asr_completed_ms=round(asr["completedAt"] - clip_end[-1], 1),
                   final_asr_completed_to_post_ms=round(post["at"] - asr["completedAt"], 1),
                   input_to_post_ms=round(post["at"] - clip_end[-1], 1),
                   endpoint_to_post_ms=round(post["at"] - endpoint["at"], 1))
        assert [item["preview"] for item in row["asr"]] == (
            [True] if case == "closed" else [True, variant == "candidate"]), row
        if case != "closed":
            assert row["asr"][0]["sha256"] != asr["sha256"], row
        print(case, variant, [item["preview"] for item in row["asr"]], row["input_to_post_ms"], flush=True)
    finally:
        await browser.close()


async def run(baseline, candidate, cases, order):
    folder = ROOT / "runs" / datetime.now().strftime("asr-endpoint-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed":False, "model_connections":0, "trials":[], "received":[], "correction_fixture_calls":[]}
    try:
        wave_files = fixtures(folder)
        report["fixtures"] = {case:{**fixture, "path":str(fixture["path"])} for case, fixture in wave_files.items()}
        scripts = {"baseline":script_set(baseline), "candidate":script_set(candidate)}
        assert scripts["baseline"] != scripts["candidate"], "两个浏览器脚本版本相同"
        catalog = folder / "empty-catalog.sqlite3"
        MediaLibrary(catalog)
        os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                          LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                          MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0",
                          MOSS_SPEECH_MIC_WARM="0", CUDA_VISIBLE_DEVICES="")
        original_answer = core.holo_answer

        async def isolated_correction(moss, turn, token, **kwargs):
            text = words(turn["user"])
            if "我问的是" in text and "多少电" in text and "现在几点" in text:
                report["correction_fixture_calls"].append(turn["user"])
                return CORRECTED
            return await original_answer(moss, turn, token, **kwargs)

        async def battery(intent):
            assert intent["metric"] == "battery"
            return BATTERY

        async def volume(intent):
            assert intent["action"] == "get_volume"
            return VOLUME

        async def forbid_model(_reader, writer):
            report["model_connections"] += 1
            writer.close()
            await writer.wait_closed()

        @web.middleware
        async def observe(request, handler):
            response = await handler(request)
            if request.path == "/api/transcribe":
                data = await request.read()
                report["received"].append({"preview":request.headers.get("X-MOSS-ASR-Preview") == "1",
                                           "sha256":sha256(data).hexdigest(), "status":response.status})
            return response

        async with await asyncio.start_server(forbid_model, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            server = BrowserServer(folder, free_port(), model_url=model_url)
            moss = core.Moss(server.data, model_url, "isolated-unused", prefer_cloud=False)
            app = application(moss, lambda:"standby", server.data, server.port, "isolated", asyncio.Event())
            app.middlewares.append(observe)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", server.port).start()
                async with aiohttp.ClientSession(trust_env=False) as client:
                    async with client.post(server.url + "/api/prepare-voice", json={}) as result:
                        assert result.status == 204, await result.text()
                with (patch("assistant_brain.local_clock_reply", return_value=CLOCK),
                      patch("local_overview.local_clock_reply", return_value=CLOCK),
                      patch("local_overview.device_status", battery), patch("local_overview.run_desktop", volume),
                      patch("core.holo_answer", isolated_correction)):
                    async with async_playwright() as playwright:
                        for case in cases:
                            for label in order:
                                variant = "baseline" if label == "a" else "candidate"
                                await trial(playwright, server, scripts[variant], variant, case, wave_files[case], report)
            finally:
                await runner.cleanup()
                await moss.close()
        assert report["model_connections"] == 0
        report["comparison"] = {}
        for case in cases:
            rows = [row for row in report["trials"] if row["case"] == case]
            assert len({words(row["transcript"]) for row in rows}) == len({row["reply"] for row in rows}) == 1, rows
            report["comparison"][case] = {}
            for metric in ("input_to_final_asr_completed_ms", "final_asr_completed_to_post_ms",
                           "input_to_post_ms", "endpoint_to_post_ms"):
                by_variant = {name:round(median(row[metric] for row in rows if row["variant"] == name), 1)
                              for name in ("baseline", "candidate")}
                report["comparison"][case][metric] = {**by_variant,
                    "saved":round(by_variant["baseline"] - by_variant["candidate"], 1)}
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="用真实本机 ASR 对照前台连续语音的输入尾音至聊天提交。")
    parser.add_argument("--baseline", type=Path, default=ROOT / "static")
    parser.add_argument("--candidate", type=Path, required=True, help="候选目录或 agent-voice.js 文件")
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--order", choices=("ab", "abba"), default="ab")
    args = parser.parse_args()
    asyncio.run(run(args.baseline.resolve(), args.candidate.resolve(), tuple(args.case or CASES), args.order))
