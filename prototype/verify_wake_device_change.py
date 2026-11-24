import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import wave

import numpy as np
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge


def microphone_file(folder, samples):
    rate = 16000
    data = np.zeros(24*rate, dtype="<i2")
    for start, name in ((.4,"background"), (4.7,"wake"), (9.4,"command")):
        with wave.open(str(samples/f"{name}.wav"), "rb") as source:
            assert (source.getnchannels(), source.getframerate(), source.getsampwidth()) == (1,rate,2)
            clip = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        begin = round(start*rate)
        data[begin:begin+len(clip)] = np.rint(clip*.05).astype("<i2")
    path = folder/"microphone.wav"
    with wave.open(str(path), "wb") as target:
        target.setparams((1,2,rate,0,"NONE","not compressed"))
        target.writeframes(data.tobytes())
    return path


async def exercise(browser, server, old_source, candidate):
    assert (await server.post("/api/new-chat", {}))[0] == 200
    page = await browser.new_page(permissions=["microphone"], reduced_motion="reduce")
    if candidate:
        async def candidate_asset(route):
            name = Path(urlsplit(route.request.url).path).name
            await route.fulfill(path=candidate / name, content_type="text/javascript")
        for name in ("app.js", "agent-terminal.js"):
            await page.route(f"**/static/{name}*", candidate_asset)
    if old_source:
        await page.route(re.compile(r"/static/agent-voice\.js(?:\?.*)?$"),
                         lambda route:route.fulfill(path=old_source, content_type="text/javascript"))
    await page.add_init_script("""
localStorage.setItem('moss.voice.enabled','off');
window.handoff={tracks:[],words:[],changes:[],messages:[]};
const capture=AudioContext.prototype.createMediaStreamSource;
AudioContext.prototype.createMediaStreamSource=function(stream) {
  handoff.tracks.push(stream.getAudioTracks()[0]);
  return capture.call(this,stream);
};
const transfer=window.fetch;
window.fetch=async function(url,options) {
  if(url==='/api/message') handoff.messages.push(JSON.parse(options.body).text);
  const response=await transfer.apply(this,arguments);
  if(url==='/api/transcribe') handoff.words.push((await response.clone().json()).text);
  return response;
};
document.addEventListener('input',ev=>{
  if (ev.target.id==='message') handoff.changes.push(ev.target.value);
});
""")
    try:
        await page.goto(server.url+"/?chat=1", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        await page.get_by_test_id("composer").fill("手动草稿")
        await page.locator(".preferences summary").click()
        wake = page.locator("#wake-listen")
        await wake.check()
        await page.wait_for_function("()=>document.querySelector('#wake-status').textContent.includes('已唤醒')",timeout=24000)
        assert await page.get_by_test_id("composer").input_value()=="手动草稿"
        first_count = await page.evaluate("handoff.words.length")
        await page.locator("#mic-device").select_option(index=1)
        await page.wait_for_function("""()=>handoff.tracks.length===2 && handoff.tracks[0].readyState==='ended' &&
handoff.tracks[1].readyState==='live' && handoff.tracks[1].getSettings().deviceId===document.querySelector('#mic-device').value &&
document.querySelector('#wake-status').textContent==='等待唤醒'""")
        window_state = await page.locator("#wake-status").inner_text()
        await page.wait_for_function("n=>handoff.words.length>n",arg=first_count,timeout=15000)
        # 换设备后的背景语音已识别，只有重新唤醒后的指令可以提交。
        if old_source:
            await page.wait_for_function("()=>handoff.messages.some(text=>text.includes('媒体库'))",timeout=3000)
        else:
            await page.wait_for_function("()=>document.querySelector('#wake-status').textContent.includes('已唤醒')",timeout=15000)
            await page.wait_for_function("()=>handoff.messages.some(text=>text.includes('在吗'))",timeout=10000)
        await page.get_by_test_id("turn").last.get_by_test_id("final").wait_for(timeout=10000)
        await wake.uncheck()
        await page.wait_for_function("()=>handoff.tracks.every(t=>t.readyState==='ended')")
        result = await page.evaluate("""()=>({words:handoff.words,changes:handoff.changes,messages:handoff.messages,
tracks:handoff.tracks.map(t=>t.readyState),composer:document.querySelector('#message').value})""")
        result["state_after_change"] = window_state
        result["first_new_transcript"] = result["words"][first_count]
        assert "媒体库" in result["first_new_transcript"] and result["composer"] == "手动草稿", result
        assert len(result["messages"]) == len((await server.state())["turns"]) == 1, result
        assert len(result["changes"]) == 1 and ("媒体库" if old_source else "在吗") in result["messages"][0], result
        return result
    except Exception:
        print(await page.evaluate("""()=>({words:handoff.words,changes:handoff.changes,messages:handoff.messages,
tracks:handoff.tracks.map(t=>t.readyState),status:document.querySelector('#wake-status')?.textContent})"""),flush=True)
        raise
    finally:
        await page.close()


async def run(folder, samples, old_source, candidate=None):
    folder.mkdir(exist_ok=True, parents=True)
    mic_file = microphone_file(folder,samples)
    os.environ.update(MOSS_MEDIA_DB=str(folder/"media.sqlite3"), MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0")
    server = BrowserServer(folder, 18966, model_url="http://127.0.0.1:9/v1")
    report = {"passed":False,"input_sha256":hashlib.sha256(mic_file.read_bytes()).hexdigest(),
              "new_js_sha256":hashlib.sha256((ROOT/"static/agent-voice.js").read_bytes()).hexdigest()}
    await server.start()
    try:
        async with async_playwright() as pw:
            exe = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
            browser = await pw.chromium.launch(executable_path=str(exe), headless=True, args=[
                "--disable-gpu","--mute-audio","--disable-background-timer-throttling","--disable-renderer-backgrounding",
                "--use-fake-device-for-media-stream","--use-fake-ui-for-media-stream",
                "--use-file-for-fake-audio-capture="+str(mic_file)])
            try:
                await confirm_muted_edge(browser)
                if old_source:
                    report["old_js_sha256"] = hashlib.sha256(old_source.read_bytes()).hexdigest()
                    report["before"] = await exercise(browser,server,old_source,candidate)
                report["after"] = await exercise(browser,server,None,candidate)
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
        print(folder, report["passed"],flush=True)


if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out",type=Path,default=ROOT/"runs"/datetime.now().strftime("wake-device-%Y%m%d-%H%M%S-%f"))
    parser.add_argument("--inputs",required=True,type=Path)
    parser.add_argument("--before-js",type=Path)
    parser.add_argument("--candidate",type=Path,help="Override staged voice submission browser assets")
    args = parser.parse_args()
    asyncio.run(run(args.out.resolve(), args.inputs.resolve(), args.before_js.resolve() if args.before_js else None, args.candidate))
