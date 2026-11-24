import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from unittest.mock import patch

from aiohttp import web
from playwright.async_api import async_playwright, expect

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from serve import application
from verify_context_reply import free_port
from verify_context_voice_e2e import make_voice
from verify_voice_receipts import listen


PROMPT = "请解释为什么秋天树叶会变黄。"
CORRECTION = "先停止回答，改问现在几点。"
CLOCK = "2026年11月22日，星期日，12:34:56。"
DRAFT = "这是一份要继续编辑的草稿。"
PARTIAL = "秋天日照缩短，树叶中的叶绿素"
MODEL = "spoken-correction-loopback"
ASSETS = {"/": "conversation.html", "/static/app.js": "app.js",
          "/static/agent-terminal.js": "agent-terminal.js", "/static/agent-voice.js": "agent-voice.js"}
MONITOR = """
localStorage.setItem('moss.voice.enabled', 'off');
window.correctionInput = {frames:0, voicedFrames:0, captureAt:null, bindings:[], requests:[]};
const NativeWorklet = window.AudioWorkletNode;
window.AudioWorkletNode = class extends NativeWorklet {
  constructor(context, name, options) {
    super(context, name, options);
    if (name !== 'moss-voice-capture') return;
    correctionInput.captureAt = performance.now();
    this.port.addEventListener('message', ({data}) => {
      if (!(data instanceof Float32Array) || !data.length) return;
      correctionInput.frames++;
      let power = 0;
      for (const sample of data) power += sample * sample;
      if (power / data.length >= .002 ** 2) correctionInput.voicedFrames++;
    });
  }
};
const nativeFetch = window.fetch;
const measured = new Set(['/api/message', '/api/cancel', '/api/prepare-voice', '/api/transcribe']);
window.fetch = async function(url, options) {
  if (!measured.has(url)) return nativeFetch.apply(this, arguments);
  const row = {url, at:performance.now()};
  if (url === '/api/message' || url === '/api/cancel') row.body = JSON.parse(options.body);
  if (url === '/api/transcribe') {
    row.bytes = options.body.size;
    row.preview = options.headers['X-MOSS-ASR-Preview'] === '1';
  }
  correctionInput.requests.push(row);
  const response = await nativeFetch.apply(this, arguments);
  row.status = response.status;
  row.done = performance.now();
  if (url === '/api/transcribe') row.text = (await response.clone().json()).text;
  return response;
};
"""


def words(text):
    return re.sub(r"[\s，,。.!！?？、；;：:]+", "", text)


async def browser_case(playwright, base, fixture, sources, moss, opened, disconnected, report):
    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    browser = await playwright.chromium.launch(
        executable_path=str(edge), headless=True,
        args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
              "--disable-renderer-backgrounding", "--use-fake-device-for-media-stream",
              "--use-fake-ui-for-media-stream", f"--use-file-for-fake-audio-capture={fixture}%noloop"])
    try:
        await confirm_muted_edge(browser)
        context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
        page = await context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        await page.add_init_script(MONITOR)

        async def route_request(route):
            url = route.request.url
            path = urlsplit(url).path
            if url.startswith(("http://", "https://")) and not url.startswith(base + "/"):
                report["external_requests"].append(url)
                await route.abort()
            elif path in {"/api/speak", "/api/speak-batch", "/api/prepare-speech"}:
                report["synthesis_attempts"].append(path)
                await route.abort()
            elif path in sources:
                report["loaded_candidates"].append(path)
                await route.fulfill(body=sources[path], content_type="text/html" if path == "/" else "text/javascript")
            else:
                await route.continue_()

        await context.route("**/*", route_request)
        try:
            await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            await page.bring_to_front()
            composer = page.get_by_test_id("composer")
            await composer.fill(PROMPT)
            await page.get_by_test_id("send").click()
            await asyncio.wait_for(opened.wait(), timeout=8)
            old, = moss.state()["turns"]
            assert old["status"] == "thinking" and old["conversation_replaceable"] is True, old
            await page.wait_for_function("id => window.mossReplaceableReply?.() === id", arg=old["id"])
            old_card = page.locator(f"[data-turn-id='{old['id']}']")
            await expect(old_card.get_by_test_id("partial-reply")).to_have_text(PARTIAL)
            await expect(composer).to_have_value("")
            await composer.fill(DRAFT)
            await page.evaluate("""() => {
              const lookup = window.mossReplaceableReply;
              window.mossReplaceableReply = () => {
                const id = lookup();
                correctionInput.bindings.push({id, at:performance.now()});
                return id;
              };
              correctionInput.micButtonAt = performance.now();
            }""")
            assert not disconnected.is_set()
            await page.locator("#mic").click()
            await page.locator("#mic[aria-pressed=true]").wait_for()
            await page.wait_for_function("""() => correctionInput.requests.filter(x => x.url === '/api/message'
              && x.status === 200).length === 2""", timeout=25000)
            await expect(page.get_by_test_id("turn").last.get_by_test_id("final")).to_have_text(CLOCK)
            await expect(old_card.get_by_test_id("status")).to_have_text("本轮已取消。")
            await expect(old_card.get_by_test_id("partial-reply")).to_have_count(0)
            await expect(composer).to_have_value(DRAFT)
            await asyncio.wait_for(disconnected.wait(), timeout=3)
            await page.locator("#mic").click()
            await page.locator("#mic[aria-pressed=false]").wait_for()

            cancelled, replacement = moss.state()["turns"]
            assert cancelled["id"] == old["id"] and cancelled["status"] == "cancelled" and not cancelled.get("reply"), cancelled
            assert replacement["status"] == "replied" and replacement["reply"] == CLOCK, replacement
            assert replacement["chat_id"] == old["chat_id"] and replacement.get("holo_tools") == ["get_time"], replacement
            assert words(replacement["user"]) == "现在几点" and not replacement["cloud_requests"]["total"], replacement
            report["turns"] = [cancelled, replacement]
            report["draft_preserved"] = True
        finally:
            if not page.is_closed():
                report["browser_observation"] = await page.evaluate("window.correctionInput")
        trace = report["browser_observation"]
        assert trace["frames"] > 0 and trace["voicedFrames"] >= 3, trace
        first, = trace["bindings"]
        assert first["id"] == old["id"] and trace["micButtonAt"] <= trace["captureAt"] <= first["at"], trace
        old_post, cancel, new_post = [item for item in trace["requests"] if item["url"] in {"/api/message", "/api/cancel"}]
        assert old_post["body"]["text"] == PROMPT and old_post["status"] == 200, trace
        assert cancel["url"] == "/api/cancel" and cancel["status"] == 200, trace
        assert cancel["body"] == {"chat_id": old["chat_id"], "turn_id": old["id"], "conversation_only": True}, trace
        assert new_post["url"] == "/api/message" and new_post["status"] == 200, trace
        assert new_post["body"]["chat_id"] == old["chat_id"] and words(new_post["body"]["text"]) == "现在几点", trace
        assert first["at"] < cancel["at"] <= cancel["done"] <= new_post["at"], trace
        transcripts = [item for item in trace["requests"] if item["url"] == "/api/transcribe"]
        recognized = [item for item in transcripts if item.get("text")]
        assert recognized and all(item["status"] == 200 and item["bytes"] > 44 for item in transcripts), transcripts
        assert words(recognized[-1]["text"]).removeprefix("先") == words(CORRECTION).removeprefix("先"), transcripts
        assert first["at"] < recognized[-1]["at"] < cancel["at"], trace
        assert set(report["loaded_candidates"]) == set(ASSETS)
        assert not report["external_requests"] and not report["page_errors"] and not report["synthesis_attempts"], report
    finally:
        await browser.close()


async def run(candidate):
    folder = ROOT / "runs" / datetime.now().strftime("spoken-correction-input-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "physical_microphone": False, "fixture_audio": "Windows Huihui → Edge AudioWorklet → real SenseVoice",
              "external_requests": [], "synthesis_attempts": [], "page_errors": [], "loaded_candidates": [], "model_calls": []}
    opened, disconnected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def complete(request):
        payload = await request.json()
        report["model_calls"].append({"user": payload["messages"][-1], "stream": payload.get("stream")})
        assert report["model_calls"] == [{"user": {"role": "user", "content": PROMPT}, "stream": True}], report["model_calls"]
        assert payload["model"] == MODEL, payload["model"]
        chunk = {"id": "spoken-correction-probe", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": PARTIAL}, "finish_reason": None}]}
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        opened.set()
        try:
            while not release.is_set():
                if request.transport is None or request.transport.is_closing():
                    disconnected.set()
                    break
                await asyncio.sleep(.03)
        except ConnectionError:
            disconnected.set()
        return response

    try:
        sources = {url: (candidate / name).read_bytes() for url, name in ASSETS.items()}
        report["candidate_sha256"] = {url: hashlib.sha256(body).hexdigest() for url, body in sources.items()}
        fixture = make_voice(folder, text=CORRECTION)
        report["fixture_sha256"] = hashlib.sha256(fixture.read_bytes()).hexdigest()
        catalog = folder / "empty-catalog.sqlite3"
        MediaLibrary(catalog)
        isolated = dict(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                        LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                        MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0",
                        MOSS_SPEECH_MIC_WARM="0", CUDA_VISIBLE_DEVICES="")
        with patch.dict(os.environ, isolated), patch("assistant_brain.local_clock_reply", return_value=CLOCK):
            async with AsyncExitStack() as stack:
                model = web.Application()
                model.router.add_post("/v1/chat/completions", complete)
                model_url = await listen(stack, model) + "/v1"
                data, port = folder / "data", free_port()
                moss = Moss(data, model_url, MODEL)
                stack.push_async_callback(moss.close)
                app = application(moss, lambda: "standby", data, port, "isolated", asyncio.Event())
                base = await listen(stack, app, port)
                try:
                    async with async_playwright() as playwright:
                        await browser_case(playwright, base, fixture, sources, moss, opened, disconnected, report)
                    assert disconnected.is_set() and len(report["model_calls"]) == 1 and not release.is_set(), report
                finally:
                    release.set()
        report["old_stream_disconnected"] = True
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="用文件麦克风验收正在回复时的口语改问。")
    parser.add_argument("--candidate", type=Path, default=ROOT / "runs/spoken-correction-candidate")
    asyncio.run(run(parser.parse_args().candidate.resolve()))
