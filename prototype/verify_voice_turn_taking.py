import argparse
import asyncio
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from time import perf_counter
import wave

import aiohttp
import numpy as np
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_voice_barge_in import METER


RATE = 16000
CASES = ("stop", "named_stop", "late_stop", "backchannel", "double_backchannel", "keep_talking",
         "other_command", "negated_stop", "stop_in_sentence", "disabled")
STOP_CASES = {"stop": "等一下", "named_stop": "停一下", "late_stop": "等一下"}
LONG_READING = "朗读下面这段话：" + "[pause 800ms]".join(["好，先到这里。"] * 4)
PROBE = """
Object.assign(probe,{voice:[],input:[],asr:[],posts:[]});
const createSourceForQA=AudioContext.prototype.createMediaStreamSource;
AudioContext.prototype.createMediaStreamSource=function(stream) {
  const source=createSourceForQA.call(this,stream);
  const tap=this.createAnalyser(); tap.fftSize=256;
  source.connect(tap); probe.inputTap=tap; probe.captureAt=performance.now();
  return source;
};
const inputFrames=new Float32Array(256);
setInterval(()=>{
  if(!probe.inputTap) return;
  probe.inputTap.getFloatTimeDomainData(inputFrames);
  let sum=0; for(const x of inputFrames) sum+=x*x;
  probe.input.push([performance.now(),Math.sqrt(sum/inputFrames.length)]);
},10);
for(const kind of ['start','end','stop','error']) addEventListener('moss-speech-'+kind,e=>
  probe.voice.push({kind,at:performance.now(),id:e.detail.id,live:probe.tracks.filter(t=>t.readyState==='live').length}));
const fetchForQA=window.fetch;
window.fetch=async function(url,opts){
  const started=performance.now();
  if(url==='/api/message') probe.posts.push({at:started,text:JSON.parse(opts.body).text});
  const response=await fetchForQA.apply(this,arguments);
  if(url==='/api/transcribe'){
    const pcm=new Int16Array(await opts.body.arrayBuffer(),44);
    let last=0;
    for(let i=0;i+160<=pcm.length;i+=160){
      let square=0;
      for(let j=i;j<i+160;j++) square+=pcm[j]*pcm[j];
      if(square>160*130**2) last=i+160;
    }
    const body=await response.clone().json();
    probe.asr.push({started,at:performance.now(),text:body.text,bytes:opts.body.size,
      tailMs:last ? (pcm.length-last)/16 : null});
  }
  return response;
};
"""


def make_inputs(folder):
    source = """
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$speaker=[System.Speech.Synthesis.SpeechSynthesizer]::new()
$format=[System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,16,1)
try {
  $speaker.SelectVoice('Microsoft Huihui Desktop')
  $lines=@{stop='等一下。'; named_stop='莫斯，停一下。'; backchannel='嗯。'; double_backchannel='嗯嗯。';
           keep_talking='嗯，你继续。'; other_command='暂停音乐。'; head='你'; tail='在吗？';
           negated_stop='不用停一下，请继续。'; stop_in_sentence='这是口令的例子。'}
  foreach ($name in $lines.Keys) {
    $speaker.SetOutputToWaveFile((Join-Path $env:MOSS_VOICE_FIXTURE ($name+'.wav')),$format)
    $speaker.Speak($lines[$name])
    $speaker.SetOutputToNull()
  }
} finally { $speaker.Dispose() }
"""
    subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", base64.b64encode(
        source.encode("utf-16le")).decode("ascii")], check=True, env={**os.environ, "MOSS_VOICE_FIXTURE": str(folder)})
    words = {}
    for name in (*(case for case in CASES if case not in {"disabled", "late_stop"}), "head", "tail"):
        with wave.open(str(folder / f"{name}.wav"), "rb") as wav:
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        active = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 80)
        assert len(active), name
        words[name] = pcm[active[0]:min(active[-1]+1, len(pcm))]
    for name in CASES:
        signal = np.zeros(12*RATE, dtype="<i2")
        cue = "stop" if name in {"disabled", "late_stop"} else name
        begin = {"named_stop":1.65, "late_stop":6.35, "keep_talking":1.65}.get(name, 1.95)
        start = int(begin*RATE)
        if name == "stop_in_sentence":
            stop = words["stop"]
            tail = start + len(stop) + round(.16 * RATE)
            signal[start:start+len(stop)] = stop
            signal[tail:tail+len(words[cue])] = words[cue]
        else:
            signal[start:start+len(words[cue])] = words[cue]
        if name in STOP_CASES:
            head = round((9.35 if name == "late_stop" else 5)*RATE)
            end = head + len(words["head"])
            signal[head:end] = words["head"]
            tail = end + int(1.15*RATE)
            signal[tail:tail+len(words["tail"])] = words["tail"]
        with wave.open(str(folder / f"mic-{name}.wav"), "wb") as out:
            out.setparams((1,2,RATE,0,"NONE","not compressed"))
            out.writeframes(signal.tobytes())


async def check_case(pw, server, folder, case, baseline, check_interrupt_audio, voice_script=None):
    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    flags = ["--mute-audio","--disable-gpu","--disable-background-timer-throttling","--disable-renderer-backgrounding",
             "--use-fake-device-for-media-stream","--use-fake-ui-for-media-stream",
             "--use-file-for-fake-audio-capture="+str(folder/f"mic-{case}.wav")+"%noloop"]
    browser = await pw.chromium.launch(executable_path=str(edge), headless=True, args=flags)
    logs = {"case":case, "external":[], "errors":[]}
    try:
        await confirm_muted_edge(browser)
        page = await browser.new_page(permissions=["microphone"], reduced_motion="reduce")
        page.set_default_timeout(15000)
        if voice_script is not None:
            logs["voice_script_reads"] = 0

            async def serve_voice(route):
                logs["voice_script_reads"] += 1
                await route.fulfill(body=voice_script, content_type="text/javascript")

            await page.route(re.compile(r"/static/agent-voice\.js(?:\?.*)?$"), serve_voice)
        await page.add_init_script(METER + PROBE)
        page.on("pageerror", lambda exc: logs["errors"].append(str(exc)))
        page.on("request", lambda req: logs["external"].append(req.url)
                if req.url.startswith(("http:", "https:")) and not req.url.startswith(server.url+"/") else None)
        await page.goto(server.url+"/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        if voice_script is not None:
            assert logs["voice_script_reads"] == 1, logs
        await page.get_by_test_id("new-chat").click()
        await page.wait_for_function("()=>!document.querySelector('[data-testid=turn]')")
        await page.locator(".preferences summary").click()
        await page.locator("#wake-listen").check()
        if not baseline and case != "disabled":
            await page.locator("#wake-interrupt").check()
        await page.locator(".preferences summary").click()
        extended = case in {"late_stop", "negated_stop", "stop_in_sentence"}
        await page.get_by_test_id("composer").fill(LONG_READING if extended else "算了")
        await page.wait_for_function("()=>performance.now()-probe.captureAt>=1050")
        async with page.expect_response(lambda r:r.url.endswith("/api/speak")) as original:
            await page.get_by_test_id("send").click()
        assert await (await original.value).body() == (ROOT/"voice/lines/done.wav").read_bytes()
        await page.wait_for_function("()=>probe.voice.some(v=>['end','stop','error'].includes(v.kind))")
        if case in STOP_CASES and not baseline:
            await page.wait_for_function("()=>probe.posts.length===2", timeout=14000)
            await page.wait_for_function("()=>probe.voice.some(v=>v.kind==='end' && v.id!==probe.voice[0].id)")
        elif case in {"keep_talking", "negated_stop", "stop_in_sentence"} and not baseline:
            expected = "例子" if case == "stop_in_sentence" else "继续"
            await page.wait_for_function("word=>probe.asr.some(r=>r.text.includes(word))", arg=expected, timeout=8000)
        else:
            await page.wait_for_timeout(550)
        state = await server.state()
        logs["turns"] = [{k:t.get(k) for k in ("user","reply","status","cloud_requests")} for t in state["turns"]]
    finally:
        if "page" in locals():
            logs.update(await page.evaluate("""()=>({
              capture:probe.captureAt,voice:probe.voice,asr:probe.asr,posts:probe.posts,
              input:probe.input.filter(r=>r[1]>.004),pcm:probe.rows,ended:probe.ended,
              status:document.getElementById('wake-status')?.textContent
            })"""))
            (folder/f"{case}.json").write_text(json.dumps(logs,ensure_ascii=False,indent=2),encoding="utf-8")
        await browser.close()
    assert not logs["external"] and not logs["errors"], logs
    first = logs["voice"][0]
    assert first["kind"] == "start", logs["voice"]
    terminal = next(v for v in logs["voice"] if v["id"] == first["id"] and v["kind"] in {"end", "stop", "error"})
    if baseline or case == "disabled":
        assert terminal["kind"] == "end" and first["live"] == 0 and len(logs["posts"]) == 1
    else:
        limit = first["at"]+(9000 if case == "late_stop" else 2600)
        voice = [t for t, rms in logs["input"] if first["at"] <= t <= min(limit, terminal["at"])]
        assert first["live"] == 1 and voice, (case, first, logs["input"][:6])
        if case in STOP_CASES:
            if case == "late_stop":
                assert voice[0] - first["at"] > 4500, (first, voice[0])
                logs["speech_onset_after_playback_ms"] = round(voice[0] - first["at"], 1)
            if terminal["kind"] == "stop":
                expected = [t for t, rms in logs["pcm"] if rms>.001 and t <= terminal["at"]+150]
                silence = next(t for t,rms in logs["pcm"] if t>=max(terminal["at"],expected[-1]) and rms<=.001)
                assert silence - voice[-1] < 1600, (terminal, silence, voice[-1])
                logs["voice_end_to_pcm_ms"] = round(silence-voice[-1],1)
            else:
                assert case == "named_stop" and terminal["kind"] == "end", logs["voice"]
                logs["completed_before_stop_ms"] = round(logs["asr"][0]["at"] - terminal["at"], 1)
            assert len(logs["asr"]) == 2 and STOP_CASES[case] in logs["asr"][0]["text"], logs["asr"]
            if check_interrupt_audio and case == "stop":
                assert 80 <= logs["asr"][0]["tailMs"] <= 310, logs["asr"][0]
            assert logs["turns"][-1]["reply"] == "我在。" and "你在吗" in logs["posts"][1]["text"], logs["turns"]
            assert (logs["asr"][1]["bytes"]-44)/32000 >= 3.0, logs["asr"]
            logs["followup_end_to_submit_ms"] = round(logs["posts"][1]["at"]-logs["input"][-1][0],1)
        else:
            assert terminal["kind"] == "end" and len(logs["posts"]) == 1, (case, logs["voice"], logs["posts"])
            if case == "keep_talking":
                assert first["at"] < logs["asr"][0]["started"] < terminal["at"], logs["asr"]
                assert logs["status"] == "等待唤醒", logs["status"]
    assert all(row["cloud_requests"]["total"] == 0 for row in logs["turns"]), logs["turns"]
    return {k:v for k,v in logs.items() if k not in {"pcm","input"}}


async def run(baseline, cases, check_interrupt_audio, voice_script=None):
    folder = ROOT/"runs"/datetime.now().strftime("voice-turn-taking-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    print(folder, flush=True)
    os.environ.update(MOSS_MEDIA_DB=str(folder/"media.sqlite3"), MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_MEM0_ENABLED="0", MOSS_PROVIDER_DIR=str(folder/"providers"))
    report = {"passed":False,"baseline":baseline,"cases":[],
              "check_interrupt_audio":check_interrupt_audio,
              "assistant_wav_sha256":hashlib.sha256((ROOT/"voice/lines/done.wav").read_bytes()).hexdigest()}
    if voice_script is not None:
        report["voice_script_sha256"] = hashlib.sha256(voice_script).hexdigest()
    make_inputs(folder)
    server = BrowserServer(folder, 18873, model_url="http://127.0.0.1:9/v1")
    try:
        await server.start()
        prepared = perf_counter()
        async with aiohttp.ClientSession() as client:
            async with client.post(server.url+"/api/prepare-voice", json={}) as response:
                assert response.status == 204, await response.text()
        report["recognizer_startup_ms"] = round((perf_counter()-prepared)*1000, 1)
        async with async_playwright() as pw:
            for case in cases:
                report["cases"].append(await check_case(pw, server, folder, case, baseline, check_interrupt_audio,
                                                        voice_script))
                print(case, "已通过", flush=True)
        report["passed"] = True
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--check-interrupt-audio", action="store_true")
    parser.add_argument("--voice-script", type=Path, help="仅在隔离页面中加载此候选语音脚本")
    args = parser.parse_args()
    source = args.voice_script.read_bytes() if args.voice_script else None
    asyncio.run(run(args.baseline, args.case or CASES, args.check_interrupt_audio, source))
