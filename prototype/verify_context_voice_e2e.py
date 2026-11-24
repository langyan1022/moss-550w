import argparse
import asyncio
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import wave

from aiohttp import web
from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from serve import application
from verify_context_reply import free_port


TITLE = "MOSS VOICE REPLY QA 7421"
HANDLE = "moss-isolated-reply-7421"
VOICE_COMMAND = "用这段生成回复。"
EDITED = "周五可以改到下午三点，在二十七号会议室开会吗？"
GOAL = "周五不行，改到周一上午十点，仍用二十七号会议室。只需告知，不必反问。"
OMIT = "PRIVATE OMIT 84512"

MONITOR = """
localStorage.setItem('moss.voice.enabled', 'off');
window.replyQa = {display:[], audio:[], asr:[], sent:[], speech:[]};
const capture = navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getDisplayMedia = async options => {
  const stream = await capture(options);
  const track = stream.getVideoTracks()[0];
  const target = track.getCaptureHandle?.()?.handle;
  const surface = track.getSettings().displaySurface;
  replyQa.display.push({track,target,surface});
  if ((target && target !== 'moss-isolated-reply-7421') || surface !== 'browser') {
    stream.getTracks().forEach(item => item.stop());
    throw new Error('验收只接受独立合成标签页。');
  }
  return stream;
};
const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async options => {
  const stream = await getMedia(options);
  replyQa.audio.push(...stream.getAudioTracks());
  return stream;
};
const originalFetch = window.fetch;
window.fetch = async function(url, options) {
  const start = performance.now();
  if (url === '/api/message') replyQa.sent.push({body:JSON.parse(options.body),start});
  if (url === '/api/speak' || url === '/api/speak-batch') replyQa.speech.push(url);
  const response = await originalFetch.apply(this, arguments);
  if (url === '/api/transcribe') {
    const data = await response.clone().json();
    replyQa.asr.push({text:data.text,error:data.error,status:response.status,
                      seconds:(options.body.size - 44) / 32000,ms:performance.now()-start,
                      ready:performance.now()});
  }
  return response;
};
"""


def make_voice(folder, *, text=VOICE_COMMAND):
    raw = folder / "command.wav"
    output = folder / "file-microphone.wav"
    script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$voice = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $voice.SelectVoice('Microsoft Huihui Desktop')
    $voice.Rate = -1
    $pcm = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000, 16, 1)
    $voice.SetOutputToWaveFile($env:MOSS_CONTEXT_QA_WAVE, $pcm)
    $voice.Speak($env:MOSS_CONTEXT_QA_PHRASE)
} finally { $voice.Dispose() }
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW,
                   env={**os.environ, "MOSS_CONTEXT_QA_WAVE": str(raw), "MOSS_CONTEXT_QA_PHRASE": text})
    with wave.open(str(raw), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        phrase_audio = audio.readframes(audio.getnframes())
    with wave.open(str(output), "wb") as timeline:
        timeline.setparams((1, 2, 16000, 0, "NONE", "NONE"))
        timeline.writeframes(b"\0" * 16000 + phrase_audio + b"\0" * 128000)
    return output


async def run(folder):
    folder.mkdir(parents=True, exist_ok=True)
    marker = str(11000 + secrets.randbelow(88000))
    capture_title = TITLE + " " + marker
    path = make_voice(folder)
    port = free_port()
    server = BrowserServer(folder, port)
    os.environ.update(MOSS_KEV_ENABLED="0", MOSS_WINDOWS_REMINDERS="0", MOSS_SPEECH_PREPARE="0",
                      MOSS_MEDIA_DB=str(folder/"catalog.sqlite3"), CUDA_VISIBLE_DEVICES="")
    moss = Moss(folder/"data", "http://127.0.0.1:9/v1", "unused")
    await moss.select_model("deepseek")
    seen = []
    approved = False
    report = {"passed": False, "cloud_requests": seen, "fixture_audio": "Windows Huihui → Edge file microphone",
              "physical_microphone": False, "gpu_used_by_test": False}

    async def audit(request):
        if not approved or len(seen) >= 2 or request.url.host != "api.deepseek.com" or request.url.scheme != "https":
            raise ValueError("隔离验收拦截了未经批准的模型请求。")
        body = json.loads(request.content)
        assert [message["role"] for message in body["messages"]] == ["system", "user"] and not body.get("tools")
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["chat"]["messages"] == [{"from": "her", "text": EDITED}] and payload["goal"] == GOAL, payload
        assert "84512" not in request.content.decode("utf-8") and "另外的草稿" not in request.content.decode("utf-8")
        seen.append({"provider": request.url.host, "shape": "check" if "choices" in payload else "draft",
                     "incoming": payload["chat"]["messages"][0]["text"], "goal": payload["goal"]})

    moss.model_client()._client.event_hooks["request"].append(audit)
    app = application(moss, lambda: "standby", folder/"data", port, "isolated", asyncio.Event())

    async def fixture(_):
        return web.Response(text=f"""<!doctype html><title>{capture_title}</title>
<script>navigator.mediaDevices.setCaptureHandleConfig({{
handle:'{HANDLE}',exposeOrigin:true,permittedOrigins:[location.origin]}});</script>
<body style="background:white;font:47px Arial;color:black;padding:34px">
<p>RIVER {marker}: Friday 3 PM in Room 27.</p><p>{OMIT}</p></body>""",
                            content_type="text/html")
    app.router.add_get("/qa-voice-selected-tab", fixture)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        available = psutil.virtual_memory().available / 2**30
        report["available_gib_before_edge"] = round(available, 2)
        assert available > 4, f"本机剩余 {available:.2f} GiB，尚不足以开启测试。"
        async with async_playwright() as pw:
            exe = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            flags = ["--mute-audio", "--disable-gpu", "--disable-background-timer-throttling",
                     "--disable-renderer-backgrounding", f"--auto-select-tab-capture-source-by-title={capture_title}",
                     "--enable-usermedia-screen-capturing", "--use-fake-device-for-media-stream",
                     f"--use-file-for-fake-audio-capture={path}"]
            browser = await pw.chromium.launch(executable_path=str(exe), headless=True, args=flags)
            try:
                await confirm_muted_edge(browser)
                context = await browser.new_context(permissions=["microphone"], reduced_motion="reduce")
                sheet = await context.new_page()
                await sheet.goto(server.url+"/qa-voice-selected-tab")
                page = await context.new_page()
                await page.add_init_script(MONITOR)
                external = []
                page.on("request", lambda req: external.append(req.url)
                        if req.url.startswith(("http:", "https:")) and not req.url.startswith(server.url+"/") else None)
                await page.goto(server.url+"/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for(timeout=15000)
                composer = page.get_by_test_id("composer")
                await composer.fill("识别当前窗口的文字")
                await page.get_by_test_id("send").click()
                row = page.get_by_test_id("turn").last
                await row.get_by_test_id("context-action").click()
                output = row.get_by_test_id("context-output")
                await output.wait_for(timeout=22000)
                raw = await output.inner_text()
                assert "RIVER" in raw and marker in raw and "27" in raw and "84512" in raw, raw
                capture = await page.evaluate("""()=>replyQa.display.map(x=>({
target:x.target??null,surface:x.surface,ended:x.track.readyState
}))""")
                assert (len(capture) == 1 and capture[0]["surface"] == "browser" and capture[0]["ended"] == "ended"
                        and capture[0]["target"] in {HANDLE, None}), capture
                origin = (await server.state())["turns"][-1]
                chosen = await output.evaluate("""el=>{
  const text=el.firstChild, end=text.data.indexOf('PRIVATE');
  const range=new Range();range.setStart(text,0);range.setEnd(text,end);
  const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
  return selection.toString().trim();
}""")
                assert "27" in chosen and "84512" not in chosen, chosen
                await row.get_by_role("button", name="用这段文字拟回复").click()
                editor = row.get_by_role("textbox", name="对方来信")
                assert await editor.input_value() == chosen, await editor.input_value()
                await editor.fill(EDITED)
                await row.get_by_role("textbox", name="回复要求（可选）").fill(GOAL)
                assert not seen and len((await server.state())["turns"]) == 1
                report.update(capture=capture, ocr_fixture_title=capture_title, ocr_nonce=marker,
                              ocr_verified=raw, selected_before_edit=chosen,
                              edited_excerpt=EDITED, reply_goal=GOAL, pre_voice_cloud_requests=len(seen))

                approved = True
                await page.locator("#mic").click()
                await page.wait_for_function("()=>replyQa.asr.length >= 1", timeout=40000)
                asr = await page.evaluate("()=>replyQa.asr")
                assert len(asr) == 1 and asr[0]["status"] == 200 and "生成回复" in asr[0]["text"], asr
                async with asyncio.timeout(65):
                    while True:
                        turns = (await server.state())["turns"]
                        if len(turns) > 1 and turns[-1]["status"] != "thinking":
                            break
                        await asyncio.sleep(.05)
                reply = turns[-1]
                assert len(turns) == 2 and reply["source"] == "jev" and reply["status"] == "replied", reply
                assert reply["reply_context"] == {"action":"screen_text", "turn_id":origin["id"], "goal":GOAL}, reply
                assert [call["shape"] for call in seen] == ["draft", "check"] and reply["jev"]["goal_checked"], reply
                candidate = reply["reply"]
                assert re.search(r"(?:周一|星期一).{0,12}(?:上午\s*(?:十|10)\s*[点時时]|10:00)", candidate), candidate
                assert re.search(r"(?:二十七|27)\s*号?会议室", candidate) and not re.search(r"[？?]|吗|可否", candidate), candidate
                answer = page.get_by_test_id("turn").last
                await expect(answer.get_by_text("来信来源 · 窗口识别", exact=True)).to_be_visible()
                await expect(answer.get_by_text(candidate, exact=True)).to_be_visible()
                await expect(answer.locator(".user-message")).to_contain_text("回复要求 · " + GOAL)
                assert await composer.input_value() == ""
                events = await page.evaluate("""()=>({asr:replyQa.asr,sent:replyQa.sent,
  microphone:replyQa.audio.map(x=>x.readyState),synthesis:replyQa.speech,
  captures:replyQa.display.length})
""")
                expected = {"chat_id":origin["chat_id"], "text":"帮我回复：“" + EDITED + "”",
                            "reply_context_turn":origin["id"], "reply_context_goal":GOAL,
                            "reply_context_request_id":reply["id"]}
                assert len(events["sent"]) == 2 and all(events["sent"][-1]["body"].get(k) == v for k,v in expected.items()), events
                assert not external and not events["synthesis"] and events["captures"] == 1 and events["microphone"] == ["live"], events
                await page.locator("#mic").click()
                await page.wait_for_function("()=>replyQa.audio.every(track=>track.readyState==='ended')")
                audio_tracks = await page.evaluate("()=>replyQa.audio.map(track=>track.readyState)")
                elapsed = datetime.fromisoformat(reply["replied_at"]) - datetime.fromisoformat(reply["created_at"])
                report.update(passed=True, asr=events["asr"], external_browser_requests=external,
                              voice_to_post_ms=round(events["sent"][-1]["start"]-events["asr"][0]["ready"], 2),
                              jev_server_turn_elapsed_ms=round(elapsed.total_seconds()*1000),
                              reply=reply["reply"], candidate_count=reply["jev"]["candidate_count"],
                              no_reread_or_foreign_capture=True, normal_ui_visible=True,
                              microphone_stayed_open=True, audio_tracks=audio_tracks)
            finally:
                if not report["passed"] and "page" in locals():
                    report["failure_ui"] = await page.evaluate("""()=>({
asr:replyQa?.asr,sent:replyQa?.sent,displays:replyQa?.display.map(x=>({target:x.target,surface:x.surface})),
status:document.querySelector('#mic-state')?.textContent
})""")
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
        (folder/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(folder, report["passed"], flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT/"runs"/datetime.now().strftime("context-voice-e2e-%Y%m%d-%H%M%S-%f"))
    args = parser.parse_args()
    asyncio.run(run(args.out.resolve()))
