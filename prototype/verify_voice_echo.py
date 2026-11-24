import argparse
import asyncio
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import wave

import aiohttp
from aiohttp import web
import numpy as np
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from serve import application
from verify_voice_barge_in import METER
from verify_voice_turn_taking import PROBE, make_inputs
from voice_output import RECORDED_LINES


RATE = 16000
FIRST = "等一下。"
SECOND = "好，先到这里。"
THIRD = "不客气。"
PAUSE = .8
READ = f"朗读下面这段话：{FIRST}[pause 800ms]{SECOND}[pause 350ms]{THIRD}"
ARMS = ("echo_only", "quiet_after_echo_phrase", "quiet_with_echo")


def samples(binary):
    with wave.open(BytesIO(binary), "rb") as stream:
        assert stream.getsampwidth() == 2
        rate, channels = stream.getframerate(), stream.getnchannels()
        pcm = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
    mono = pcm.reshape(-1, channels).mean(axis=1) / 32768
    return np.interp(np.arange(round(len(mono)*RATE/rate))*rate/RATE, np.arange(len(mono)), mono)


def level(signal):
    return float(np.sqrt(np.mean(signal**2)))


def place(track, signal, seconds):
    index = round(seconds*RATE)
    track[index:index+len(signal)] += signal


def room(track, signal, start, gain):
    for delay, weight in ((.045, .72), (.124, .21), (.232, .09)):
        place(track, signal*gain*weight, start+delay)


def produce_inputs(folder, first, second):
    stop = samples((folder/"stop.wav").read_bytes())
    active = np.flatnonzero(np.abs(stop) > .0025)
    assert len(active)
    stop = stop[active[0]:active[-1]+1]
    quiet = stop * (.011/level(stop))
    # Capture is already running when the player starts. The second cue has an
    # explicit silence after the first line for both the real player and the room signal.
    first_at = 1.23
    second_at = first_at + len(first)/RATE + PAUSE
    user_at = second_at + .20
    details = {}
    for arm in ARMS:
        signal = np.zeros(10*RATE)
        if arm == "echo_only":
            room(signal, first, first_at+.15, .038/level(first))
        else:
            if arm == "quiet_with_echo":
                room(signal, second, second_at, .009/level(second))
            place(signal, quiet, user_at)
        assert np.max(np.abs(signal)) < 1
        with wave.open(str(folder/f"mic-{arm}.wav"), "wb") as out:
            out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            out.writeframes(np.rint(signal*32767).astype("<i2").tobytes())
        details[arm] = {"first_echo_start_s":first_at+.15 if arm == "echo_only" else None,
                        "second_echo_start_s":second_at if arm == "quiet_with_echo" else None,
                        "user_start_s":user_at if arm != "echo_only" else None,
                        "user_active_rms_dbfs":round(20*np.log10(level(quiet)),2) if arm != "echo_only" else None,
                        "echo_only_source_rms_dbfs":round(20*np.log10(.038),2) if arm == "echo_only" else None,
                        "mixed_source_rms_dbfs":round(20*np.log10(.009),2) if arm == "quiet_with_echo" else None}
    return details


async def exercise(pw, url, folder, arm, source):
    edge = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
    browser = await pw.chromium.launch(executable_path=str(edge), headless=True, args=[
        "--mute-audio", "--disable-gpu", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        "--use-file-for-fake-audio-capture="+str(folder/f"mic-{arm}.wav")+"%noloop"])
    log = {"arm":arm,"external_web_requests":[],"page_errors":[]}
    try:
        await confirm_muted_edge(browser)
        page = await browser.new_page(permissions=["microphone"], reduced_motion="reduce")
        page.set_default_timeout(18000)
        await page.add_init_script(METER + PROBE + """
probe.segments=[];
addEventListener('moss-speech-start',({detail})=>
  probe.segments.push({text:detail.text, at:performance.now(), seconds:detail.seconds}));
""")
        page.on("pageerror", lambda error: log["page_errors"].append(str(error)))
        page.on("request", lambda req: log["external_web_requests"].append(req.url)
                if req.url.startswith(("http:", "https:")) and not req.url.startswith(url+"/") else None)
        played = []
        page.on("response", lambda resp: played.append(resp) if resp.url.endswith("/api/speak") else None)
        await page.goto(url+"/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        await page.get_by_test_id("new-chat").click()
        await page.wait_for_function("()=>!document.querySelector('[data-testid=turn]')")
        await page.get_by_test_id("composer").fill(READ)
        await page.locator(".preferences summary").click()
        # 在同一个网页时钟上安排提交，文件麦克风开始后无需等待测试驱动回合。
        await page.evaluate("""()=>{
          const timer=setInterval(()=>{
            const send=document.querySelector('[data-testid=send]');
            if(!probe.captureAt || !document.querySelector('#wake-interrupt').checked || send.disabled ||
               performance.now()-probe.captureAt<1025) return;
            clearInterval(timer);
            send.click();
          },10);
        }""")
        await page.locator("#wake-listen").check()
        await page.locator("#wake-interrupt").check()
        await page.wait_for_function("()=>probe.voice.some(v=>['end','stop','error'].includes(v.kind))")
        await page.wait_for_timeout(570)
        observed = await page.evaluate("""()=>({
            first_capture:probe.captureAt, segments:probe.segments, speech:probe.voice, asr:probe.asr,
            messages:probe.posts, status:document.getElementById('wake-status').textContent,
            input:probe.input.filter(row=>row[1]>.002), pcm:probe.rows
        })""")
        log.update(observed)
        (folder/f"{arm}-signals.json").write_text(json.dumps(log,ensure_ascii=False), encoding="utf-8")
        end = next(event for event in observed["speech"] if event["kind"] in {"end","stop","error"})
        assert end["kind"] != "error", log["speech"]
        assert not log["external_web_requests"] and not log["page_errors"]
        assert len(log["messages"]) == 1 and log["speech"][0]["live"] == 1 and log["input"]
        assert [segment["text"] for segment in log["segments"][:2]] == [FIRST, SECOND], log["segments"]
        bodies = [await response.body() for response in played]
        assert len(bodies) >= 2 and bodies == source[:len(bodies)]
        if end["kind"] == "end":
            assert [segment["text"] for segment in log["segments"]] == [FIRST,SECOND,THIRD]
        voice_from = log["segments"][0]["at"] if arm == "echo_only" else log["segments"][1]["at"]
        voice_until = min(end["at"], voice_from + 2550)
        active = [t for t,_ in log["input"] if voice_from <= t <= voice_until]
        assert active, (arm, voice_from, voice_until)
        result = {"arm":arm, "outcome":end["kind"], "asr":log["asr"], "segments":log["segments"],
                  "mic_voice_after_segment_ms":round(active[0]-voice_from,1),
                  "near_end_followup_opened":"接着说" in log["status"]}
        if end["kind"] == "stop":
            last = max(at for at, rms in log["pcm"] if rms>.001 and at <= end["at"]+100)
            silence = next(at for at,rms in log["pcm"] if at>=max(end["at"],last) and rms<=.001)
            result.update(mic_voice_end_to_silence_ms=round(silence-active[-1],1))
        return result
    finally:
        await browser.close()


async def run(observe, recorded_line, max_stop_ms):
    folder = ROOT/"runs"/datetime.now().strftime("voice-echo-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    print(folder, flush=True)
    os.environ.update(MOSS_MEDIA_DB=str(folder/"media.sqlite3"), MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0",
                      MOSS_WINDOWS_REMINDERS="0", MOSS_MEM0_ENABLED="0", MOSS_PROVIDER_DIR=str(folder/"providers"))
    report = {"passed":False, "observing":observe, "environment":"Edge 静音，文件麦克风，已知延迟和电平叠加原版 MOSS 音频",
              "cases":[]}
    key = ("reply",FIRST)
    data = folder/"data"
    data.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        moss = Moss(data,"http://127.0.0.1:9/v1","unused",prefer_cloud=False)
        app = application(moss, lambda:"standby", data, port, "isolated", asyncio.Event())
        if recorded_line:
            # 使用上一次真实 OmniVoice 请求的音频，复用日常播报的录音缓存入口。
            previous = json.loads((recorded_line.parent/"result.json").read_text(encoding="utf-8"))
            digest = hashlib.sha256(recorded_line.read_bytes()).hexdigest()
            assert digest == previous["assistant_sha256"] and key not in RECORDED_LINES
            RECORDED_LINES[key] = str(recorded_line)
            report["recorded_omnivoice"] = {"path":str(recorded_line),"sha256":digest}

            @web.middleware
            async def known_speech(request, handler):
                if request.path == "/api/speak":
                    body = await request.json()
                    if (body["category"],body["text"]) not in RECORDED_LINES:
                        return web.json_response({"error":"此轮验收只播放已经取证的播报音频。"},status=409)
                return await handler(request)

            app.middlewares.append(known_speech)
        runner = web.AppRunner(app, access_log=None)
        try:
            make_inputs(folder)
            await runner.setup()
            await web.SockSite(runner, sock).start()
            await check_echo(url, folder, report, observe, recorded_line, max_stop_ms)
        finally:
            await runner.cleanup()
            await moss.close()
            if recorded_line:
                del RECORDED_LINES[key]
            (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")


async def check_echo(url, folder, report, observe, recorded_line, max_stop_ms):
    async with aiohttp.ClientSession() as client:
        async with client.post(url+"/api/speak", json={"text":FIRST,"category":"reply"}) as response:
            assert response.status == 200, await response.text()
            first = await response.read()
            report["synthesis_timing"] = response.headers.get("Server-Timing")
            if recorded_line:
                assert "cache;dur=" in report["synthesis_timing"] and first == recorded_line.read_bytes()
        second = (ROOT/"voice/lines/done.wav").read_bytes()
        third = (ROOT/"voice/lines/welcome.wav").read_bytes()
        (folder/"assistant-line.wav").write_bytes(first)
        report["assistant_sha256"] = hashlib.sha256(first).hexdigest()
        report["inputs"] = produce_inputs(folder, samples(first), samples(second))
        async with client.post(url+"/api/prepare-voice", json={}) as response:
            assert response.status == 204, await response.text()
    async with async_playwright() as pw:
        for arm in ARMS:
            result = await exercise(pw, url, folder, arm, [first,second,third])
            report["cases"].append(result)
            print(result, flush=True)
    echo, quiet, mixed = report["cases"]
    for item in (quiet, mixed):
        assert any("等一下" in (x["text"] or "") for x in item["asr"]), item
    assert echo["asr"] and echo["outcome"] == "end" and not echo["near_end_followup_opened"], echo
    if not observe:
        for item in (quiet, mixed):
            assert item["outcome"] == "stop" and item["mic_voice_end_to_silence_ms"] < max_stop_ms, item
    report["passed"] = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--recorded-line", type=lambda value:Path(value).resolve(strict=True))
    parser.add_argument("--max-stop-ms", type=int, default=1600)
    args = parser.parse_args()
    asyncio.run(run(args.observe, args.recorded_line, args.max_stop_ms))
