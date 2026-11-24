import argparse
import asyncio
from contextlib import nullcontext
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_context_voice_e2e import make_voice
from voice_output import MossSpeech


FIRST = "算了"
NEXT = "现在几点"
REPLY = "好，先到这里。"
DRAFT = "明早核对书目。"
DRAFT_PROMPT = "帮我保存一份本地草稿，内容逐字写：" + DRAFT
CONFIRM = "草稿准备好了，现在保存吗？"
PROBE = """
localStorage.setItem('moss.voice.enabled','on');
window.duplexQa = {tracks:[], clicks:[], queued:[], calls:[], playback:[]};
let input;
navigator.mediaDevices.getUserMedia = async function() {
  const context = new AudioContext();
  await context.resume();
  input = {context, output:context.createMediaStreamDestination()};
  duplexQa.tracks.push(input.output.stream.getAudioTracks()[0]);
  return input.output.stream;
};
window.sayFixture = async name => {
  const {context,output} = input;
  const wave = await (await fetch('/fixture/' + name)).arrayBuffer();
  const buffer = await context.decodeAudioData(wave);
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(output);
  const at = performance.now();
  source.start();
  duplexQa.queued.push({name, at, playing:window.mossSpeech.active(), duration:buffer.duration});
};
const originalFetch = window.fetch;
window.fetch = async function(url, options) {
  if (!['/api/transcribe','/api/message','/api/speak','/api/prepare-voice','/api/cancel'].includes(url))
    return originalFetch.apply(this, arguments);
  const entry = {url, at:performance.now(), playing:window.mossSpeech?.active(), live:duplexQa.tracks.filter(t=>t.readyState==='live').length};
  duplexQa.calls.push(entry);
  if (url === '/api/message') entry.text = JSON.parse(options.body).text;
  if (url === '/api/cancel') entry.turnId = JSON.parse(options.body).turn_id;
  if (url === '/api/transcribe') entry.audioBytes = options.body.size;
  const response = await originalFetch.apply(this, arguments);
  entry.status = response.status;
  if (url === '/api/transcribe') {
    entry.text = (await response.clone().json()).text;
    entry.playingOnReply = window.mossSpeech?.active();
  }
  return response;
};
for (const name of ['start','stop','end','error']) {
  addEventListener('moss-speech-' + name, event => duplexQa.playback.push({
    name, at:performance.now(), text:event.detail.text, id:event.detail.id,
    live:duplexQa.tracks.filter(t=>t.readyState==='live').length
  }));
}
document.addEventListener('click', event => {
  if (event.target.closest('#mic')) duplexQa.clicks.push(performance.now());
}, true);
window.duplexSnapshot = () => ({...duplexQa, tracks:duplexQa.tracks.map(t=>({id:t.id, state:t.readyState}))});
"""


def recorded_fixture():
    line = ROOT / "voice/lines/done.wav"
    with wave.open(str(line), "rb") as source:
        params = source.getparams()
        pcm = source.readframes(source.getnframes())
    output = io.BytesIO()
    with wave.open(output, "wb") as repeated:
        repeated.setparams(params)
        repeated.writeframes(pcm * 5)
    return line, output.getvalue()


async def check_cancel(page, moss, report):
    await page.wait_for_function("duplexQa.calls.some(row=>row.url==='/api/prepare-voice'&&row.status===204)",
                                 timeout=35000)
    await page.get_by_test_id("composer").fill(DRAFT_PROMPT)
    async with page.expect_response(lambda response: response.url.endswith("/api/speak")) as spoken:
        await page.get_by_test_id("send").click()
    response = await spoken.value
    assert response.ok and response.request.post_data_json["category"] == "confirm"
    assert await response.body() == (ROOT / "voice/lines/confirm-draft.wav").read_bytes()
    await page.wait_for_function("duplexQa.playback.some(row=>row.name==='start')", timeout=7000)
    pending, = moss.state()["turns"]
    assert pending["status"] == "pending" and pending["preview"] == DRAFT and pending["call_id"]
    assert not pending["receipt"] and not list(moss.drafts.iterdir())
    key = pending["id"]
    context = {"chatId":pending["chat_id"], "turnId":key, "heardInPlayback":False}
    rejected = await page.evaluate("""async context => {
        const decide = window.mossSubmitOverlappingDecision;
        const results = [];
        for (const request of [
            {original:'确认保存，不。', filtered:'确认保存'},
            {original:'不要取消保存。', filtered:'取消保存'},
            {original:'取消。', filtered:'', heardInPlayback:true},
            {original:'取消。', filtered:'', turnId:'older-draft'},
            {original:'取消。', filtered:'', chatId:'older-chat'}
        ]) results.push(await decide({...context,...request}));
        return results;
    }""", context)
    assert rejected == [False] * 5, rejected
    assert await page.evaluate("""mossSpeech.active() &&
        !duplexQa.calls.some(row => row.url==='/api/approve' || row.url==='/api/cancel')""")

    unsent = "还没发送的文字"
    composer = page.get_by_test_id("composer")
    await composer.fill(unsent)
    await page.evaluate("sayFixture('cancel')")
    recognition = await page.wait_for_function(r"""word => duplexQa.calls.find(row=>row.url==='/api/transcribe' &&
        row.status===200 && row.text?.replace(/[\s\p{P}]+/gu,'')===word && row.playingOnReply!==undefined)""",
                                               arg=report["fixture_decline"], timeout=8000)
    recognized = await recognition.json_value()
    report["short_cancel_asr"] = recognized
    assert recognized["live"] == 1 and recognized["playing"], recognized
    await page.wait_for_function("""id=>duplexQa.calls.some(row=>row.url==='/api/cancel' &&
        row.turnId===id && row.status===200)""", arg=key, timeout=5000)
    await page.wait_for_function("""id=>duplexQa.playback.some(row=>row.id===id && row.name==='stop')""", arg=key)
    cancelled, = moss.state()["turns"]
    assert cancelled["id"] == key and cancelled["status"] == "cancelled", cancelled
    assert not cancelled["receipt"] and not list(moss.drafts.iterdir())
    assert await composer.input_value() == unsent
    for action in ("approve", "cancel"):
        url = urlsplit(page.url)._replace(path="/api/" + action, query="", fragment="").geturl()
        stale = await page.request.post(url, data={"turn_id":key, "pending_draft_only":True})
        assert stale.status == 409 and "不在等待确认" in (await stale.json())["error"]

    # Allow the actual playback echo tail to expire before issuing an unrelated utterance.
    await page.wait_for_function("""id=>performance.now() - duplexQa.playback.find(row=>row.id===id &&
        row.name==='stop')?.at > 450""", arg=key)
    await composer.fill("")
    await page.evaluate("sayFixture('next')")
    await page.wait_for_function("""() => duplexQa.calls.some(row=>row.url==='/api/message' &&
        row.status===200 && row.text.includes('你在吗'))""", timeout=12000)
    await page.get_by_test_id("turn").last.get_by_test_id("final").wait_for()
    stopped, followup = moss.state()["turns"]
    assert stopped["id"] == key and stopped["status"] == "cancelled" and followup["reply"] == "我在。"
    trace = await page.evaluate("duplexSnapshot()")
    assert [row["turnId"] for row in trace["calls"] if row["url"] == "/api/cancel"] == [key], trace
    assert len(trace["tracks"]) == len(trace["clicks"]) == 1 and trace["tracks"][0]["state"] == "live"
    assert any(row["url"] == "/api/transcribe" and row["live"] == 1 and
               "你在吗" in (row.get("text") or "") for row in trace["calls"]), trace
    assert not list(moss.drafts.iterdir()) and all(turn["cloud_requests"]["total"] == 0 for turn in (stopped, followup))
    report.update(draft_id=key, cancelled_once=True, files_written=0, followup=followup["reply"],
                  unsent_text_preserved=True, rejected_overlapping_decisions=len(rejected), stale_decisions_rejected=2)


async def check(playwright, base, report, *, pending_draft=None):
    browser = await playwright.chromium.launch(
        executable_path=str(Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"),
        headless=True, args=["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                            "--disable-renderer-backgrounding", "--use-fake-ui-for-media-stream"])
    try:
        await confirm_muted_edge(browser)
        context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce", service_workers="block")
        page = await context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

        async def local_only(route):
            url = route.request.url
            if urlsplit(url).scheme in {"http", "https"} and not url.startswith(base + "/"):
                report["outside_requests"].append(url)
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", local_only)
        await page.add_init_script(PROBE)
        await page.goto(base + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for(timeout=15000)
        await page.bring_to_front()
        mic = page.locator("#mic")
        await mic.click()
        await page.locator("#mic[aria-pressed=true]").wait_for(timeout=15000)
        if pending_draft is not None:
            await check_cancel(page, pending_draft, report)
            await mic.click()
            await page.wait_for_function("duplexQa.tracks.length===1 && duplexQa.tracks[0].readyState==='ended'")
            assert not report["outside_requests"] and not report["page_errors"], report
            return
        await page.evaluate("sayFixture('first')")
        await page.wait_for_function("duplexQa.playback.some(e=>e.name==='start')", timeout=45000)
        await page.evaluate("sayFixture('echo')")
        await page.wait_for_function("""duplexQa.calls.filter(e=>e.url==='/api/transcribe' &&
            e.status===200 && e.text).length >= 2""", timeout=15000)
        await page.wait_for_function("""() => {
            const echo = duplexQa.queued.find(entry => entry.name === 'echo');
            return performance.now() >= echo.at + echo.duration * 1000;
        }""")
        echo = await page.evaluate("""() => ({
            calls:duplexQa.calls.filter(e=>e.url==='/api/message'),
            asr:duplexQa.calls.filter(e=>e.url==='/api/transcribe').at(-1),
            playing:mossSpeech.active()})""")
        assert len(echo["calls"]) == 1 and echo["asr"]["playing"] and echo["playing"], echo
        recognized_echo = re.sub(r"\W", "", echo["asr"]["text"])
        assert recognized_echo and recognized_echo in re.sub(r"\W", "", REPLY), echo
        await page.evaluate("sayFixture('next')")
        await page.wait_for_function("""duplexQa.calls.filter(e=>e.url==='/api/message' && e.status===200).length===2 &&
            duplexQa.playback.some(e=>e.name==='stop')""", timeout=18000)
        during = await page.evaluate("duplexSnapshot()")
        track, = during["tracks"]
        assert track["state"] == "live" and len(during["clicks"]) == 1, during
        assert all(e["playing"] for e in during["queued"] if e["name"] in {"echo", "next"}), during
        first, next_call = (row for row in during["calls"] if row["url"] == "/api/message")
        assert "算了" in first["text"] and "现在几点" in next_call["text"], during
        start = next(row for row in during["playback"] if row["name"] == "start")
        stop = next(row for row in during["playback"] if row["name"] == "stop" and row["id"] == start["id"])
        assert start["at"] < stop["at"] <= next_call["at"] and start["live"] == stop["live"] == 1, during
        report["interrupt_from_followup_ms"] = round(stop["at"] - during["queued"][-1]["at"], 1)

        await mic.click()
        await page.wait_for_function("""duplexQa.tracks.length === 1 && duplexQa.tracks[0].readyState==='ended' &&
            document.getElementById('mic').getAttribute('aria-pressed')==='false'""")
        assert len(await page.evaluate("duplexQa.clicks")) == 2
        assert not report["outside_requests"] and not report["page_errors"], report
    finally:
        if "page" in locals():
            report["browser"] = await page.evaluate("window.duplexSnapshot?.()")
        await browser.close()


async def run(*, cancel_draft=False, cancel_word="取消"):
    prefix = "voice-duplex-cancel-draft" if cancel_draft else "voice-duplex-browser"
    folder = ROOT / "runs" / datetime.now().strftime(prefix + "-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "model_connections": 0, "outside_requests": [], "page_errors": [],
              "fixture": "Windows Mandarin WAV + recorded MOSS echo via an Edge MediaStream; real ASR"}
    paths = {}
    if cancel_draft:
        report["fixture_decline"] = cancel_word
    clips = (("cancel", cancel_word), ("next", "你在吗")) if cancel_draft else (("first", FIRST), ("next", NEXT))
    for name, phrase in clips:
        target = folder / name
        target.mkdir()
        make_voice(target, text=phrase)
        paths[name] = target / "command.wav"
    if not cancel_draft:
        paths["echo"], output = recorded_fixture()
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0", CUDA_VISIBLE_DEVICES="")

    async def reject_cloud(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def fixture(request):
        name = request.match_info["name"]
        if name not in paths:
            raise web.HTTPNotFound()
        return web.FileResponse(paths[name], headers={"Content-Type": "audio/wav"})

    async def render_fixture(_speaker, text, disconnected, category="reply", cached_only=False, *, timings=None):
        assert not disconnected() and text
        return output if REPLY in text else paths["echo"].read_bytes()

    async def draft_model(request):
        data = await request.json()
        report["proposal_calls"] = report.get("proposal_calls", 0) + 1
        assert report["proposal_calls"] == 1 and not data.get("stream")
        assert data["messages"][-1] == {"role": "user", "content": DRAFT_PROMPT}, data
        assert any(tool["function"]["name"] == "save_draft" for tool in data["tools"]), data
        call = {"id": "duplex-draft-fixture", "type": "function", "function": {
            "name": "save_draft", "arguments": json.dumps({"text": DRAFT}, ensure_ascii=False)}}
        return web.json_response({"id": call["id"], "object": "chat.completion", "created": 0, "model": data["model"],
                                  "choices": [{"index": 0, "finish_reason": "tool_calls",
                                               "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})

    try:
        async with await asyncio.start_server(reject_cloud, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            port = free_port()
            local_model = f"http://127.0.0.1:{port}/v1" if cancel_draft else model_url
            moss = Moss(folder / "data", local_model, "isolated-unused")
            app = application(moss, lambda: "standby", folder / "data", port, "duplex", asyncio.Event())
            app.router.add_get("/fixture/{name}", fixture)
            if cancel_draft:
                app.router.add_post("/v1/chat/completions", draft_model)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                with nullcontext() if cancel_draft else patch.object(MossSpeech, "render", render_fixture):
                    async with async_playwright() as playwright:
                        await check(playwright, f"http://127.0.0.1:{port}", report,
                                    pending_draft=moss if cancel_draft else None)
                if cancel_draft:
                    assert report["proposal_calls"] == 1
                else:
                    first, second = moss.state()["turns"]
                    assert first["reply"] == REPLY and "现在几点" in second["user"] and second["reply"]
                    assert not any(turn["cloud_requests"]["total"] for turn in (first, second))
                assert not report["model_connections"], report
                report["passed"] = True
            finally:
                await runner.cleanup()
                await moss.close()
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cancel-draft", action="store_true", help="用真实本机识别测试确认播报期间的短句取消")
    parser.add_argument("--cancel-word", choices=("取消", "算了"), default="取消")
    args = parser.parse_args()
    asyncio.run(run(cancel_draft=args.cancel_draft, cancel_word=args.cancel_word))
