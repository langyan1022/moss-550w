import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import wave

import numpy as np
from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from serve import application
from voice.pronunciation import apply_pronunciations
from voice.spoken_numbers import spoken_numbers
from voice_output import RECORDED_LINES


def recorded_cases(verse, prose, folder):
    data = json.loads((verse / "result.json").read_text(encoding="utf-8"))
    assert data["passed"]
    clips, cases = {}, []
    for title in ("登鹳雀楼", "早发白帝城"):
        parts = data["plans"][title]["verse"]
        requests = [r for r in data["requests"] if r["poem"] == title and r["category"] == "verse"]
        asset = verse / data["acoustic"][title]["verse"]["file"]
        assert [r["text"] for r in requests] == [p["spoken"] for p in parts]
        paths = []
        with wave.open(str(asset), "rb") as merged:
            for number, (request, part) in enumerate(zip(requests, parts)):
                path = folder / f"{title}-{number}.wav"
                with wave.open(str(path), "wb") as out:
                    out.setparams(merged.getparams())
                    out.writeframes(merged.readframes(request["samples"]))
                assert hashlib.sha256(path.read_bytes()).hexdigest() == request["sha256"], path
                clips["verse", part["spoken"]] = path
                paths.append(str(path))
            assert not merged.readframes(1)
        cases.append({"name":title, "user":"请朗诵古诗《" + title + "》", "text":"\n\n".join(p["caption"] for p in parts), "clips":paths,
                      "expected":parts[-1]["caption"].replace("\n", "").replace("，","，|").replace("。","。|").strip("|").split("|")})
    for title, ci in data.get("ci_audio", {}).items():
        path = verse / ci["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ci["sha256"]
        clips["verse",ci["spoken"]] = path
        cases.append({"name":title, "user":"请朗诵宋词《"+title+"》", "text":ci["caption"],
                      "clips":[str(path)], "allow_grouping":True})
    continuity = json.loads((prose / "result.json").read_text(encoding="utf-8"))
    text = continuity["requests"][0]["text"]
    clips["reply", text] = prose / "line-1.wav"
    cases.append({"name":"普通多句", "user":"记住刚才的安排", "text":text, "clips":[str(prose/"line-1.wav")],
                  "expected":[line for line in text.replace("。","。|").replace("？","？|").split("|") if line]})
    cases.append({"name":"短句", "user":"算了", "text":"好，先到这里。", "expected":["好，先到这里。"]})
    return clips, cases


def pause_depth(path, second):
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").reshape(-1,wav.getnchannels())
        samples = samples.astype(np.float64).mean(axis=1) / 32768
    count = round(rate*.01)
    def energy(chunk):
        return float(np.sqrt(np.mean(chunk**2)))
    global_level = np.percentile([energy(samples[i:i+count]) for i in range(0,len(samples),count)], 90)
    quiet = min(energy(samples[i:i+count]) for i in range(max(0,round((second-.16)*rate)),
                                                          min(len(samples)-count,round((second+.04)*rate)), count))
    return round(20*np.log10(max(quiet,1e-7)/max(global_level,1e-7)),2)


OBSERVE = """
window.captionRun={events:[], captions:[], starts:[], errors:[]};
const actualStart=AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start=function(...args){
  const outcome=actualStart.apply(this,args);
  captionRun.starts.push(performance.now());
  return outcome;
};
for(const kind of ['start','progress','end','stop','error']){
  window.addEventListener('moss-speech-'+kind,({detail})=>{
    const at=performance.now();
    queueMicrotask(()=>{
      const el=document.querySelector('#caption'), visible=el.hidden ? '' : el.textContent;
      const row={kind,at,clip:detail.sentence||0,position:detail.position||0,visible,text:el.textContent};
      captionRun.events.push(row);
      if(visible!==captionRun.captions.at(-1)?.visible) captionRun.captions.push(row);
    });
  });
}
"""


async def run(verse, prose):
    folder = ROOT / "runs" / datetime.now().strftime("timed-captions-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    clips, cases = recorded_cases(verse, prose, folder)
    added = []
    for (category, text), path in clips.items():
        normalized = apply_pronunciations(text, model="omnivoice")
        key = category, spoken_numbers(normalized)
        assert key not in RECORDED_LINES
        RECORDED_LINES[key] = str(path.resolve())
        added.append(key)
    data = folder / "data"
    moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    runner = web.AppRunner(application(moss, lambda:"standby", data, port, "isolated", asyncio.Event()), access_log=None)
    report = {"passed":False,"method":"前端重放已保存的朗诵与多句播报输入，真实 /api/speak 录音缓存、Web Audio 与页面字幕。",
              "cases":[], "clips":{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in clips.values()}}
    blocked = []
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script(OBSERVE)
                async def allow_recorded(route):
                    body = route.request.post_data_json
                    normalized = spoken_numbers(apply_pronunciations(body["text"], model="omnivoice"))
                    if (body["category"], normalized) in RECORDED_LINES:
                        await route.continue_()
                    else:
                        blocked.append(body["text"])
                        await route.abort()
                await page.route("**/api/speak", allow_recorded)
                await page.goto(f"http://127.0.0.1:{port}/?chat=1&scene=off")
                await page.wait_for_function("Boolean(window.mossSpeech)")
                await page.locator("#history-toggle").click()
                for case, interrupt in [(case, False) for case in cases] + [(cases[0], True)]:
                    # 使用已保存的原文重放，经过页面正常的播报入口。
                    await page.evaluate("""async input=>{
                      const state=await (await fetch('/api/state')).json();
                      const turn={id:crypto.randomUUID(),user:input.user,reply:input.text,status:'replied'};
                      captionRun={events:[],captions:[],starts:[]};
                      mossSpeech.userAction('message',{text:turn.user},state);
                      dispatchEvent(new CustomEvent('moss-agent-state',{detail:{...state, turns:[turn]}}));
                    }""", case)
                    if case is cases[0] and not interrupt:
                        await page.wait_for_function("captionRun.captions.some(c=>c.visible==='黄河入海流。')",timeout=8000)
                        await page.screenshot(path=str(folder/"诗句播放.png"))
                    if interrupt:
                        await page.wait_for_function("captionRun.captions.filter(c=>c.visible).length>=2",timeout=7000)
                        await page.locator("#interrupt").click()
                    termination = "stop" if interrupt else "end"
                    await page.wait_for_function("kind=>captionRun.events.some(e=>e.kind===kind)", arg=termination, timeout=25000)
                    if interrupt:
                        await page.wait_for_timeout(800)
                    observed = await page.evaluate("captionRun")
                    edges = observed["events"]
                    shown = [e["visible"] for e in observed["captions"] if e["visible"]]
                    starts = [e["at"] for e in edges if e["kind"]=="start"]
                    result = {"name":case["name"], "interrupted":interrupt, "captions":observed["captions"],
                              "utterances":len(starts), "after_play_start_ms":[round(a-b,2) for a,b in zip(starts,observed["starts"])]}
                    result["nearby_pause_below_speech_db"] = [
                        pause_depth(case["clips"][e["clip"]-1], e["position"]) for e in observed["captions"] if e["kind"]=="progress"]
                    report["cases"].append(result)
                    assert len(starts) == len(observed["starts"]) > 0 and not any(e["kind"]=="error" for e in edges)
                    assert edges[-1]["kind"] == termination and edges[-1]["text"] == "" and not edges[-1]["visible"], result
                    assert all(db < -15 for db in result["nearby_pause_below_speech_db"]), result
                    if interrupt:
                        assert shown == case["expected"][:len(shown)] and len(shown) < len(case["expected"]), result
                    elif case.get("allow_grouping"):
                        assert len(shown) >= 3 and "".join(shown) == case["text"], result
                        assert all(line[-1] in "，。！？；" for line in shown), result
                    else:
                        assert shown[-len(case["expected"]):] == case["expected"], (result, case["expected"])
                    assert not blocked, blocked
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
        for key in added:
            del RECORDED_LINES[key]
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verse", required=True, type=Path)
    parser.add_argument("--prose", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.verse.resolve(), args.prose.resolve()))
