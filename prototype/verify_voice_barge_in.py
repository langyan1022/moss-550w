import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
from statistics import median
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_memory import send


# 用独立分析器测量实际播报图上的 PCM。采样不修改当前音频链的输出。
METER = """
window.probe = {rows:[], starts:0, clicks:[], tap:null, ended:[], tracks:[]};
const createInput = AudioContext.prototype.createMediaStreamSource;
AudioContext.prototype.createMediaStreamSource = function(stream) {
  probe.tracks.push(stream.getAudioTracks()[0]);
  return createInput.call(this,stream);
};
const nativeFetch = window.fetch;
window.fetch = function(url, options) {
  if (url === '/api/transcribe' && options?.body instanceof Blob) {
    probe.header = options.body.slice(0,44).arrayBuffer().then(b=>Array.from(new Uint8Array(b)));
    probe.uploadSize = options.body.size;
  }
  return nativeFetch.apply(this,arguments);
};
const connect = AudioNode.prototype.connect;
AudioNode.prototype.connect = function(target,...args) {
  const result = connect.call(this,target,...args);
  if (this instanceof AnalyserNode && target === this.context.destination) {
    const tap = this.context.createAnalyser(); tap.fftSize = 64;
    connect.call(this,tap);
    probe.tap=tap;
  }
  return result;
};
const start = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...args) {
  this.addEventListener('ended',()=>probe.ended.push(performance.now()),{once:true});
  return start.apply(this,args);
};
addEventListener('moss-speech-start',()=>probe.starts++);
document.addEventListener('click',event=>{
  if (event.target.closest('#mic')) probe.clicks.push(performance.now());
},true);
const samples=new Float32Array(64);
setInterval(()=>{
  if (!probe.tap) return;
  probe.tap.getFloatTimeDomainData(samples);
  let sum=0; for (const sample of samples) sum+=sample*sample;
  probe.rows.push([performance.now(),Math.sqrt(sum/samples.length)]);
},4);
"""


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("voice-barge-in-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18872, model_url="http://127.0.0.1:9/v1")
    server.data.mkdir(parents=True)
    # 恢复既有隔离测试实际生成的待确认草稿。其余样本来自 Windows 系统录音和内置播报。
    previous = max((ROOT / "runs").glob("deepseek-draft-recovery-*/prepare/session.sqlite3"))
    with closing(sqlite3.connect(previous.as_uri()+"?mode=ro", uri=True)) as src, closing(
            sqlite3.connect(server.data / "session.sqlite3")) as dst:
        src.backup(dst)
    mic_file = max((ROOT / "runs").glob("terminal-*/edge-microphone.wav"))
    spoken = ROOT / "voice/lines/done.wav"
    os.environ.update(MOSS_KEV_ENABLED="0", MOSS_MEDIA_DB=str(folder/"media.sqlite3"))
    report = {"passed":False, "samples":[], "cancellations":[], "input":str(mic_file), "recording":str(spoken),
              "recording_sha256":hashlib.sha256(spoken.read_bytes()).hexdigest()}
    print(folder, flush=True)
    await server.start()
    try:
        async with async_playwright() as pw:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            flags = ["--mute-audio","--disable-background-timer-throttling","--disable-renderer-backgrounding",
                     "--use-fake-device-for-media-stream","--use-fake-ui-for-media-stream",
                     "--use-file-for-fake-audio-capture="+str(mic_file)]
            browser = await pw.chromium.launch(executable_path=str(edge), headless=True, args=flags)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(permissions=["microphone"], reduced_motion="reduce")
                page.set_default_timeout(12000)
                await page.add_init_script(METER)
                errors, commands = [], []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("request", lambda r: commands.append(r.url.rsplit("/",1)[-1]) if r.method=="POST" else None)
                await page.goto(server.url+"/?chat=1", wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for()
                mic = page.locator("#mic")
                pending = (await server.state())["turns"][-1]
                assert pending["status"]=="pending" and pending["call_id"]
                await mic.click()
                await page.wait_for_function("()=>document.getElementById('mic').ariaPressed==='true'")
                await page.locator("#interrupt").click()
                after = (await server.state())["turns"][-1]
                assert after["status"]=="pending" and after["call_id"]==pending["call_id"] and not after["receipt"]
                report["draft_preserved"] = after["call_id"]
                await page.get_by_test_id("new-chat").click()
                await page.wait_for_function("()=>!document.querySelector('[data-testid=turn]')")
                for index in range(6):
                    await page.evaluate("()=>{probe.rows=[];probe.tap=null;probe.ended=[]}")
                    async with page.expect_response(lambda r:r.url.endswith("/api/speak")) as recording:
                        await send(page, server, "算了")
                    assert hashlib.sha256(await (await recording.value).body()).hexdigest() == report["recording_sha256"]
                    await page.wait_for_function("()=>probe.rows.slice(-3).length===3&&probe.rows.slice(-3).every(r=>r[1]>.01)")
                    await mic.click(force=True)
                    await page.wait_for_function("()=>probe.ended.length && document.getElementById('mic').ariaPressed==='true'")
                    await page.wait_for_function("()=>performance.now()-probe.clicks.at(-1)>100")
                    samples = await page.evaluate("""()=>{const click=probe.clicks.at(-1);
                      return {click,ended:probe.ended.at(-1),rows:probe.rows.filter(r=>r[0]>click-75)}}""")
                    click = samples["click"]
                    last = max(t for t, level in samples["rows"] if level>.001)
                    silence = next(t for t, level in samples["rows"] if t >= max(click,last) and level<=.001)
                    assert last>=click-65 and silence-click < 180, (last-click, silence-click)
                    report["samples"].append({"last_signal_ms":round(last-click,2), "pcm_silent_ms":round(silence-click,2),
                                              "source_ended_ms":round(samples["ended"]-click,2)})
                    await page.locator("#interrupt").click()
                    await server.post("/api/remember", {"text":f"本次隔离测试序号 {index}。"})
                count = await page.evaluate("probe.starts")
                await page.wait_for_timeout(2400)
                assert await page.evaluate("probe.starts") == count == 6
                report["state_refresh_replays"] = 0
                await mic.click()
                await page.wait_for_function("()=>document.getElementById('mic').ariaPressed==='true'")
                await page.locator(".preferences summary").click()
                choice = page.locator("#mic-device")
                await page.wait_for_function("()=>document.getElementById('mic-device').options.length>1")
                await choice.select_option(index=1)
                await page.wait_for_function("""()=>probe.tracks.at(-2)?.readyState==='ended' &&
                    probe.tracks.at(-1)?.readyState==='live' && document.getElementById('mic').ariaPressed==='true' &&
                    probe.tracks.at(-1).getSettings().deviceId===document.getElementById('mic-device').value""")
                report["recording_device_change"] = {"previous":"ended", "selected":"live"}
                await page.locator(".preferences summary").click()
                await page.locator("#interrupt").click()
                # 将真实浏览器上传限速。服务端收到完整 WAV 前切换、取消；不初始化识别模型。
                cdp = await page.context.new_cdp_session(page)
                await cdp.send("Network.enable")
                await cdp.send("Network.emulateNetworkConditionsByRule", {"offline":False,"matchedNetworkConditions":[{
                    "urlPattern":server.url+"/api/transcribe","latency":0,"downloadThroughput":-1,"uploadThroughput":32}]})
                for action in ("cancel", "new_chat", "input_device"):
                    await mic.click()
                    await page.wait_for_function("()=>document.getElementById('mic').ariaPressed==='true'")
                    await page.wait_for_timeout(800)
                    async with page.expect_request(lambda r:r.url.endswith("/api/transcribe")) as captured:
                        await mic.click()
                    assert (await captured.value).headers["content-type"] == "audio/wav"
                    header = bytes(await page.evaluate("probe.header"))
                    with wave.open(io.BytesIO(header)) as wav:
                        frames = wav.getnframes()
                        assert wav.getframerate()==16000 and frames>2000
                        assert 44 + 2*frames == await page.evaluate("probe.uploadSize")
                    async with page.expect_event("requestfailed", predicate=lambda r:r.url.endswith("/api/transcribe")) as failed:
                        if action == "cancel":
                            await mic.click()
                        elif action == "new_chat":
                            await page.get_by_test_id("new-chat").click()
                        else:
                            await page.locator(".preferences summary").click()
                            await choice.select_option(index=0)
                            await page.locator(".preferences summary").click()
                    reason = (await failed.value).failure
                    assert "ABORTED" in reason, reason
                    if action != "cancel":
                        assert not (await server.state())["turns"]
                    assert not await page.get_by_test_id("composer").input_value()
                    report["cancellations"].append({"action":action,"failure":reason,"pcm_frames":frames})
                assert not errors and "cancel" not in commands and "approve" not in commands, (errors,commands)
                assert commands.count("message")==6 and not list((server.data/"drafts").iterdir())
                values = sorted(s["pcm_silent_ms"] for s in report["samples"])
                report.update(passed=True,p50_ms=median(values),p95_ms=values[math.ceil(.95*len(values))-1])
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
