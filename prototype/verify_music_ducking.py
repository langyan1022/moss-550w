import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from serve import application


async def run(music, verse, manual_only):
    folder = ROOT / "runs" / datetime.now().strftime("music-ducking-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    record = json.loads((verse / "result.json").read_text(encoding="utf-8"))
    poem = next(iter(record["ci_audio"].values()))
    wav = verse / poem["file"]
    assert hashlib.sha256(wav.read_bytes()).hexdigest() == poem["sha256"] and music.is_file()
    moss = Moss(folder / "data", "http://127.0.0.1:9/v1", "unused")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = application(moss, lambda: "standby", folder / "data", port, "isolated", asyncio.Event())

    async def track(request):
        return web.FileResponse(music)

    app.router.add_get("/qa/music", track)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    report = {"music": str(music.resolve()), "poem_wav": str(wav.resolve()), "sha256": poem["sha256"],
              "method": "静音 Edge，用保存的真实 WAV 响应语音请求，播放本地音乐，使用页面的播放器、音量滑块和打断按钮。",
              "cases": [], "passed": False}
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                async def recorded(route):
                    body = route.request.post_data_json
                    if body["text"] != poem["spoken"] or body["category"] != "verse":
                        await route.abort()
                        return
                    await route.fulfill(path=wav, content_type="audio/wav")
                await page.route("**/api/speak", recorded)
                await page.goto(f"http://127.0.0.1:{port}/?scene=off&chat=1", wait_until="domcontentloaded")
                await page.wait_for_function("Boolean(window.mossSpeech)")
                await page.evaluate("""async ()=> {
                  const {projectionPlayer}=await import('/conversation-assets/projection-player.js');
                  const a=document.createElement('audio');
                  a.src='/qa/music'; a.className='media-audio'; a.preload='auto';
                  const controls=projectionPlayer(a);
                  controls.id='qa-music'; controls.style='position:fixed;left:1em;bottom:1em;z-index:2147483647;width:480px';
                  document.body.append(a,controls);
                  window.mix={a,events:[], voices:[], point:()=>({time:a.currentTime,volume:a.volume,
                    paused:a.paused,clock:performance.now(),src:a.currentSrc})};
                  for(const k of ['pause','playing','ended','seeking','seeked','volumechange'])
                    a.addEventListener(k,()=>mix.events.push({kind:k,...mix.point()}));
                  for(const k of ['pending','start','end','stop','error'])
                    addEventListener('moss-speech-'+k,({detail})=>mix.voices.push({kind:k,id:detail.id,category:detail.category}));
                }""")
                await page.locator("#qa-music .player-toggle").click()
                await page.wait_for_function("mix.a.currentTime>.1 && mix.a.duration>30")
                await page.evaluate("mix.a.currentTime=12")
                await page.wait_for_function("mix.a.currentTime>=12 && !mix.a.seeking")
                cases = [("用户调低后打断", .75, ["ArrowLeft", "ArrowLeft"], .05, True)]
                if not manual_only:
                    cases += [("朗诵结束", .7, [], .7, False), ("打断朗诵", .8, [], .8, True),
                              ("低音量时调小", .1, ["ArrowLeft"], .05, True),
                              ("用户调低又调回", .75, ["ArrowLeft", "ArrowRight"], .15, True)]
                for name, initial, keys, expected, interrupt in cases:
                    await page.evaluate("v=>{mix.a.volume=v}", initial)
                    await page.wait_for_function("v=>Number(document.querySelector('#qa-music .player-volume').value)===v", arg=initial)
                    start = await page.evaluate("""async reply=>{
                      const state=await (await fetch('/api/state')).json();
                      mix.events=[]; mix.voices=[];
                      const p=mix.point();
                      mix.recite=()=>{
                        const turn={id:crypto.randomUUID(),user:'请朗诵这首宋词。',reply,status:'replied'};
                        mossSpeech.userAction('message',{text:turn.user},state);
                        dispatchEvent(new CustomEvent('moss-agent-state',{detail:{...state,turns:[turn]}}));
                      };
                      mix.recite();
                      return p;
                    }""", poem["caption"])
                    await page.wait_for_function("mix.voices.some(v=>v.kind==='start' && v.category==='verse')")
                    await page.wait_for_function("mix.a.currentTime>0 && !mix.a.paused")
                    during = await page.evaluate("mix.point()")
                    for key in keys:
                        await page.locator("#qa-music .player-volume").press(key)
                    chosen = await page.evaluate("mix.a.volume")
                    if interrupt:
                        await page.locator("#interrupt").click()
                    end = "stop" if interrupt else "end"
                    await page.wait_for_function("k=>mix.voices.some(v=>v.kind===k)", arg=end, timeout=25000)
                    await page.wait_for_timeout(320)
                    after = await page.evaluate("mix.point()")
                    events = await page.evaluate("mix.events")
                    voices = await page.evaluate("mix.voices")
                    progressed = after["time"] - start["time"]
                    elapsed = (after["clock"] - start["clock"]) / 1000
                    passed = (not after["paused"] and start["src"] == after["src"] and abs(progressed-elapsed) < .22
                              and abs(during["volume"] - min(initial,.15)) < .0001 and abs(after["volume"]-expected) < .0001
                              and all(e["kind"] == "volumechange" for e in events)
                              and all(e["kind"] != "error" for e in voices))
                    report["cases"].append({"name":name, "initial":initial, "during":during["volume"], "chosen":chosen,
                        "expected":expected, "after":after["volume"], "start_position":start["time"],
                        "end_position":after["time"], "wall_seconds":round(elapsed,3), "events":events, "voice":voices,
                        "paused":after["paused"], "passed":passed})
                if not manual_only:
                    await page.evaluate("()=>{mix.a.volume=.75; mix.voices=[]; mix.recite()}")
                    await page.wait_for_function("mix.voices.some(e=>e.kind==='start') && mix.a.volume===.15")
                    await page.evaluate("document.querySelector('#interrupt').addEventListener('click',mix.recite,{once:true})")
                    await page.locator("#interrupt").click()
                    await page.wait_for_function("mix.voices.filter(e=>e.kind==='start').length===2")
                    await page.wait_for_timeout(220)
                    during = await page.evaluate("mix.point()")
                    await page.locator("#interrupt").click()
                    await page.wait_for_timeout(220)
                    after = await page.evaluate("mix.point()")
                    report["cases"].append({"name":"打断同时开始下一轮朗诵", "during":during, "after":after,
                        "passed":during["volume"] == .15 and after["volume"] == .75 and not after["paused"]})
                    await page.locator("#interrupt").click()
                    await page.wait_for_function("mix.a.paused")
                    after = await page.evaluate("mix.point()")
                    report["cases"].append({"name":"仅播放音乐时点击打断", "after":after,
                                            "passed":after["paused"] and after["volume"] == .75})
                report["passed"] = all(row["passed"] for row in report["cases"])
                assert report["passed"], [r["name"] for r in report["cases"] if not r["passed"]]
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("music", type=Path)
    parser.add_argument("verse", type=Path)
    parser.add_argument("--manual-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.music, args.verse, args.manual_only))
