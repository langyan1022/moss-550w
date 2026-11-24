"""Check bundled speech receipts using real Edge and isolated loopback services."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import hashlib
import io
import json
import os
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import ClientSession, ClientTimeout, web
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_speech_delivery import inspect_audio
from voice_output import MossSpeech


DRAFT = "验收草稿：明天核对书目。"
PROMPT = f"帮我保存一份本地草稿，内容逐字写：{DRAFT}"
MODEL = "voice-receipt-fixture"
CONFIRM = {"category": "confirm", "text": "草稿准备好了，现在保存吗？"}
SAVED = {"category": "reply", "text": "已保存。"}
LINES = (
    (SAVED, "saved-draft.wav"),
    ({"category": "notice", "text": "这次没能完成。详情在对话里。"}, "action-incomplete.wav"),
    ({"category": "reply", "text": "文件已归类，位置在对话里。"}, "file-organized.wav"),
    ({"category": "reply", "text": "好的。"}, "acknowledge.wav"),
)
TRACE = """
localStorage.setItem('moss.voice.enabled', 'on');
window.receiptSpeech = [];
for (const type of ['start', 'end', 'error']) {
  addEventListener('moss-speech-' + type, e => receiptSpeech.push({type, ...e.detail}));
}
"""


async def listen(stack, app, port=0):
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    stack.push_async_callback(runner.cleanup)
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return f"http://127.0.0.1:{runner.addresses[0][1]}"


async def run(transcribe):
    folder = ROOT / "runs" / datetime.now().strftime("voice-receipts-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    report = {"passed": False, "model_requests": 0, "synthesis_starts": 0, "browser": [], "audio": []}
    assets = ROOT / "voice" / "lines"
    expected_text = DRAFT.encode("utf-8")
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)

    async def complete(request):
        report["model_requests"] += 1
        body = await request.json()
        assert report["model_requests"] == 1 and not body.get("stream"), body
        assert body["model"] == MODEL and body["messages"][-1] == {"role": "user", "content": PROMPT}, body
        assert any(tool["function"]["name"] == "save_draft" for tool in body["tools"]), body
        call = {"id": "voice-receipt-save", "type": "function", "function": {
            "name": "save_draft", "arguments": json.dumps({"text": DRAFT}, ensure_ascii=False)}}
        return web.json_response({"id": "voice-receipt-model", "object": "chat.completion", "created": 0,
                                  "model": MODEL, "choices": [{"index": 0, "finish_reason": "tool_calls",
                                  "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})

    async def deny_worker_start(_speech):
        report["synthesis_starts"] += 1
        raise RuntimeError("固定回执触发了实时合成。")

    try:
        recordings = {filename: (assets / filename).read_bytes() for _, filename in LINES}
        environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                       "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                       "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch.object(MossSpeech, "_start", deny_worker_start))
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"

            model = web.Application()
            model.router.add_post("/v1/chat/completions", complete)
            model_url = await listen(stack, model) + "/v1"
            app_data = folder / "data"
            moss = Moss(app_data, model_url, MODEL)
            stack.push_async_callback(moss.close)
            assert moss.model_id == "local"

            waiting_for_write = asyncio.Event()
            release_write = asyncio.Event()
            real_write, real_receipt = moss._write_confirmed_draft, moss._receipt
            readbacks = []

            async def hold_write(*args, **kwargs):
                waiting_for_write.set()
                await release_write.wait()
                return await real_write(*args, **kwargs)

            def observe_readback(turn):
                result = real_receipt(turn)
                readbacks.append(result)
                return result

            stack.enter_context(patch.object(moss, "_write_confirmed_draft", hold_write))
            stack.enter_context(patch.object(moss, "_receipt", observe_readback))
            stop = asyncio.Event()
            port = free_port()
            base = await listen(stack, application(moss, lambda: "standby", app_data, port, "voice-receipt-qa", stop), port)
            stack.callback(stop.set)
            stack.callback(release_write.set)
            http = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=30), trust_env=False))
            outside, errors = [], []

            async def inspect_request(route):
                request = route.request
                if urlsplit(request.url).scheme in ("http", "https") and not request.url.startswith(base + "/"):
                    outside.append(request.url)
                    await route.abort()
                    return
                if request.url == base + "/api/speak":
                    payload = request.post_data_json
                    report["browser"].append({"category": payload["category"], "text": payload["text"],
                                              "prior_verified_readbacks": len(readbacks)})
                await route.continue_()

            def is_saved(response):
                if response.url != base + "/api/speak":
                    return False
                payload = response.request.post_data_json
                return all(payload.get(key) == value for key, value in SAVED.items())

            async with async_playwright() as playwright, AsyncExitStack() as browser_stack:
                browser = await open_browser(playwright)
                browser_stack.push_async_callback(browser.close)
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.route("**/*", inspect_request)
                await page.add_init_script(TRACE)
                await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.get_by_test_id("composer").fill(PROMPT)
                await page.get_by_test_id("send").click()
                row = page.get_by_test_id("turn").last
                approve = row.get_by_test_id("approve")
                await approve.wait_for()
                pending, = moss.state()["turns"]
                turn_id = pending["id"]
                assert pending["status"] == "pending" and pending["preview"] == DRAFT and not pending["receipt"], pending
                await page.wait_for_function("id => receiptSpeech.some(e => e.id === id && e.type === 'end')", arg=turn_id)
                assert report["browser"] == [{**CONFIRM, "prior_verified_readbacks": 0}], report["browser"]

                async with page.expect_response(lambda r: r.url == base + "/api/approve") as approval_response:
                    await approve.click()
                approval = await approval_response.value
                assert approval.ok and (await approval.json())["turns"][-1]["status"] == "saving"
                async with asyncio.timeout(5):
                    await waiting_for_write.wait()
                assert not readbacks and not list(moss.drafts.iterdir())
                assert all(entry["text"] != SAVED["text"] for entry in report["browser"])
                assert not await row.get_by_test_id("receipt").count()

                async with page.expect_response(is_saved) as saved_response:
                    release_write.set()
                spoken = await saved_response.value
                assert spoken.ok and spoken.headers["x-moss-delivery"] == SAVED["category"]
                assert await spoken.body() == recordings["saved-draft.wav"]
                await row.get_by_test_id("receipt").wait_for()
                assert (await row.get_by_test_id("final").inner_text()).strip() == SAVED["text"]
                await page.wait_for_function("""id => receiptSpeech.filter(e => e.id === id && e.type === 'end').length === 2""", arg=turn_id)
                events = await page.evaluate("receiptSpeech")
                starts = [{"text": item["text"], "category": item["category"]} for item in events if item["type"] == "start"]
                assert starts == [CONFIRM, SAVED] and not any(item["type"] == "error" for item in events), events
                assert report["browser"][-1] == {**SAVED, "prior_verified_readbacks": 1}, report["browser"]
                saved, = moss.state()["turns"]
                receipt, = readbacks
                assert saved["status"] == "replied" and saved["reply_source"] == "receipt" and saved["receipt"] == receipt
                assert receipt["bytes"] == len(expected_text) and receipt["sha256"] == hashlib.sha256(expected_text).hexdigest()
                assert saved["cloud_requests"]["total"] == 0 and not outside and not errors, (outside, errors)
                async with http.get(f"{base}/drafts/{receipt['filename']}") as download:
                    assert download.status == 200 and await download.read() == expected_text
                report["draft"] = {"turn_id": turn_id, "receipt_sha256": receipt["sha256"], "audio_events": events}

            for payload, filename in LINES:
                start = perf_counter()
                async with http.post(base + "/api/speak", json=payload) as response:
                    assert response.status == 200 and response.content_type == "audio/wav"
                    assert response.headers["X-MOSS-Delivery"] == payload["category"]
                    assert "cache;dur=" in response.headers["Server-Timing"]
                    data = await response.read()
                elapsed = round((perf_counter() - start) * 1000, 2)
                assert data == recordings[filename], filename
                with wave.open(io.BytesIO(data), "rb") as wav:
                    channels, rate = wav.getnchannels(), wav.getframerate()
                    assert channels == 2, (filename, channels)
                seconds, asr_wav = inspect_audio(data)
                result = {**payload, "asset": filename, "channels": channels, "sample_rate": rate,
                          "seconds": seconds, "ms": elapsed, "sha256": hashlib.sha256(data).hexdigest()}
                if transcribe:
                    async with http.post(base + "/api/transcribe", data=asr_wav,
                                         headers={"Content-Type": "audio/wav"}) as response:
                        response.raise_for_status()
                        transcript = (await response.json())["text"]
                    assert "".join(filter(str.isalnum, transcript)) == "".join(filter(str.isalnum, payload["text"])), transcript
                    result["transcript"] = transcript
                report["audio"].append(result)
            assert report["model_requests"] == 1 and report["synthesis_starts"] == 0
            assert not (app_data / "voice-prompts").exists(), app_data
            report["passed"] = True
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcribe", action="store_true", help="另外使用本机 ASR 核对四段录音的文字")
    asyncio.run(run(parser.parse_args().transcribe))
