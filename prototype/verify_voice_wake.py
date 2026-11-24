import argparse
import asyncio
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
import wave

import numpy as np
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from voice_input import LocalVoice


def make_inputs(folder):
    folder.mkdir(parents=True, exist_ok=True)
    source = """
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$speaker=[System.Speech.Synthesis.SpeechSynthesizer]::new()
$format=[System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,16,1)
try {
  $speaker.SelectVoice('Microsoft Huihui Desktop')
  $lines=@{background='打开媒体库。'; inline='你好，莫斯，打开媒体库。'; wake='你好，莫斯。'; command='你在吗？'}
  foreach ($name in $lines.Keys) {
    $speaker.SetOutputToWaveFile((Join-Path $env:MOSS_WAKE_FIXTURE ($name+'.wav')),$format)
    $speaker.Speak($lines[$name])
    $speaker.SetOutputToNull()
  }
} finally { $speaker.Dispose() }
"""
    subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", base64.b64encode(
        source.encode("utf-16le")).decode("ascii")], check=True, env={**os.environ, "MOSS_WAKE_FIXTURE":str(folder)})
    timeline = np.zeros(36*16000, dtype="<i2")
    for name, start in (("background",.8), ("inline",6), ("wake",13), ("command",18)):
        with wave.open(str(folder/f"{name}.wav"), "rb") as wav:
            signal = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        offset = int(start*16000)
        timeline[offset:offset+len(signal)] = np.rint(signal*.05).astype("<i2")
    with wave.open(str(folder/"timeline.wav"), "wb") as wav:
        wav.setparams((1,2,16000,0,"NONE","not compressed"))
        wav.writeframes(timeline.tobytes())


async def run(folder, fixtures_only, candidate=None):
    make_inputs(folder)
    model = LocalVoice()
    try:
        transcripts = {key:await model.transcribe((folder/f"{key}.wav").read_bytes())
                       for key in ("background","inline","wake","command")}
    finally:
        await model.close()
    print("本机语音识别", transcripts, flush=True)
    if fixtures_only:
        return
    server = BrowserServer(folder, 18963, model_url="http://127.0.0.1:9/v1")
    os.environ.update(MOSS_MEDIA_DB=str(folder/"media.sqlite3"), MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0")
    report = {"passed":False,"source":"Windows Huihui 系统语音","gain":.05,"cpu_transcripts":transcripts}
    await server.start()
    try:
        async with async_playwright() as pw:
            exe = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
            flags = ["--mute-audio","--disable-gpu","--disable-background-timer-throttling","--disable-renderer-backgrounding",
                     "--use-fake-device-for-media-stream","--use-fake-ui-for-media-stream",
                     "--use-file-for-fake-audio-capture="+str(folder/"timeline.wav")]
            browser = await pw.chromium.launch(executable_path=str(exe), headless=True, args=flags)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(permissions=["microphone"], reduced_motion="reduce")
                page.set_default_timeout(14000)
                if candidate:
                    async def candidate_asset(route):
                        name = Path(urlsplit(route.request.url).path).name
                        await route.fulfill(path=candidate / name, content_type="text/javascript")
                    for name in ("app.js", "agent-terminal.js"):
                        await page.route(f"**/static/{name}*", candidate_asset)
                await page.add_init_script("""
localStorage.setItem('moss.voice.enabled','off');
window.captureQA={tracks:[],events:[],asr:[],sent:[],frozen:[],external:[]};
const makeStream=AudioContext.prototype.createMediaStreamSource;
AudioContext.prototype.createMediaStreamSource=function(stream) {
  captureQA.tracks.push(stream.getAudioTracks()[0]);
  return makeStream.call(this,stream);
};
for(const kind of ['pending','start','end','stop','error']) addEventListener('moss-speech-'+kind,()=>{
  queueMicrotask(()=>captureQA.events.push({kind,live:captureQA.tracks.filter(t=>t.readyState==='live').length}));
});
const originalFetch=window.fetch;
window.fetch=async function(url,opts) {
  const started=performance.now();
  if(url==='/api/message') captureQA.sent.push(JSON.parse(opts.body).text);
  const response=await originalFetch.apply(this,arguments);
  if(url==='/api/transcribe') {
    const body=await response.clone().json();
    captureQA.asr.push({text:body.text,error:body.error,seconds:(opts.body.size-44)/32000,start_ms:started});
  }
  return response;
};
""")
                external = []
                page.on("request", lambda request: external.append(request.url) if request.url.startswith(("http:", "https:"))
                        and not request.url.startswith(server.url+"/") else None)
                await page.goto(server.url+"/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for()
                composer = page.get_by_test_id("composer")
                await composer.fill("手动输入的草稿")
                await page.locator(".preferences summary").click()
                wake = page.locator("#wake-listen")
                assert not await wake.is_checked() and not await page.evaluate("captureQA.tracks.length")
                await wake.check()
                await page.wait_for_function("()=>captureQA.asr.length>=1", timeout=28000)
                assert await page.locator("#mic span").inner_text()=="直接说话"
                assert await composer.input_value()=="手动输入的草稿"
                assert not await page.evaluate("captureQA.sent.length")
                await page.wait_for_function("()=>captureQA.sent.some(text=>text.includes('媒体库'))",timeout=20000)
                await page.get_by_test_id("turn").first.get_by_test_id("final").wait_for()
                report["preserved_composer"] = await composer.input_value()
                assert report["preserved_composer"] == "手动输入的草稿" and len((await server.state())["turns"]) == 1
                await page.get_by_role("checkbox", name="MOSS 语音播报").check()
                await page.wait_for_function("()=>document.querySelector('#wake-status').textContent.includes('已唤醒')",timeout=16000)
                await page.wait_for_function("()=>captureQA.events.some(e=>e.kind==='end')",timeout=22000)
                await page.wait_for_function("()=>captureQA.tracks.length>=2 && captureQA.tracks.at(-1).readyState==='live'")
                turn = (await server.state())["turns"]
                report["turns"] = [{"user":t["user"],"reply":t["reply"]} for t in turn]
                assert len(turn)==2 and turn[-1]["reply"]=="我在。" and "媒体库" in turn[0]["user"], turn
                assert await composer.input_value() == "手动输入的草稿"
                await wake.uncheck()
                await page.wait_for_function("()=>captureQA.tracks.every(t=>t.readyState==='ended')")
                assert await page.locator("#mic span").inner_text()=="开启麦克风"
                report["browser"] = await page.evaluate("""()=>({asr:captureQA.asr,sent:captureQA.sent,
events:captureQA.events,tracks:captureQA.tracks.map(t=>t.readyState)})""")
                report["external_web_requests"] = external
                observed = report["browser"]
                assert not external and len(observed["asr"])==4 and len(observed["sent"])==2
                assert any(e["kind"]=="start" and e["live"]==0 for e in observed["events"])
                report["passed"] = True
            finally:
                if "page" in locals() and not report["passed"]:
                    report["failure_ui"] = await page.evaluate("""()=>({asr:captureQA?.asr, events:captureQA?.events,
sent:captureQA?.sent, status:document.getElementById('wake-status')?.textContent})""")
                await browser.close()
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures-only",action="store_true")
    parser.add_argument("--candidate",type=Path,help="Override staged voice submission browser assets")
    parser.add_argument("--out",type=Path,default=ROOT/"runs"/datetime.now().strftime("voice-wake-%Y%m%d-%H%M%S-%f"))
    args = parser.parse_args()
    asyncio.run(run(args.out.resolve(), args.fixtures_only, args.candidate))
