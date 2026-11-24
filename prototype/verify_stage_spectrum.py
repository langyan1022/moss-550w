import asyncio
import argparse
from array import array
from datetime import datetime
import hashlib
import json
from math import pi, sin
import os
from pathlib import Path
import shutil
from unittest.mock import patch
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_lens import scene
from verify_memory import send
from verify_speech_paragraph_prefetch import wav as test_wav
from voice_output import MossSpeech


SNAPSHOT = """() => {
  const canvas = document.querySelector('#stage-audio'), caption = document.querySelector('#caption');
  function pixels(c) {
    const data = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
    let count = 0, checksum = 0;
    for (let i = 3; i < data.length; i += 4) if (data[i]) {
      count++; checksum = (checksum + i * data[i]) >>> 0;
    }
    return [count,checksum];
  }
  const box = e => {
    const r = e.getBoundingClientRect();
    return [r.left,r.top,r.right,r.bottom].map(Math.round);
  };
  return {stage:pixels(canvas), side:pixels(document.querySelector('#output-fft')),
    visible:canvas.checkVisibility({visibilityProperty:true}), mode:canvas.dataset.mode,
    caption:!caption.hidden && caption.textContent, chartBox:box(canvas), textBox:box(caption),
    drawer:document.querySelector('#dialogue').hidden ? null : box(document.querySelector('#dialogue')),
    width:innerWidth, height:innerHeight};
}"""


HOME_SNAPSHOT = """() => {
  const canvas = document.querySelector('#stage-audio'), lane = canvas.closest('.stage-audio-lane');
  const caption = document.querySelector('#caption');
  const mode = document.querySelector('#stage-signal-mode'), source = document.querySelector('#stage-signal-source');
  const box = el => {
    const b = el.getBoundingClientRect();
    return [b.left,b.top,b.right,b.bottom].map(Math.round);
  };
  // The overlay is normally click-through. Change hit-testing only during this synchronous
  // measurement to ask Edge which composited element covers the HUD at nine locations.
  function hitTest(el) {
    const old = el.style.pointerEvents, r = el.getBoundingClientRect(), hits = [];
    el.style.pointerEvents = 'auto';
    try {
      for (const x of [.04,.5,.96]) for (const y of [.15,.5,.85]) {
        const top = document.elementFromPoint(r.left+x*r.width,r.top+y*r.height);
        hits.push(top===el || el.contains(top));
      }
    } finally { el.style.pointerEvents = old; }
    return hits.filter(Boolean).length;
  }
  let count=0, total=0, weight=0;
  if (canvas.width && canvas.height) {
    const img = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
    for(let i=3;i<img.length;i+=4) if(img[i]>180) {
      count++; const x=((i-3)/4)%canvas.width;
      total+=x*img[i]; weight+=img[i];
    }
  }
  const playing = [...document.querySelectorAll('.media-audio')].find(a=>!a.paused);
  return {width:innerWidth,height:innerHeight,mode:mode?.textContent,source:source?.textContent,
    signal:lane.dataset.signal,chart:box(canvas),caption:caption.hidden?null:box(caption),
    visible:canvas.checkVisibility({visibilityProperty:true,opacityProperty:true}),
    canvasHits:hitTest(canvas),modeHits:mode?hitTest(mode):0,sourceHits:source?hitTest(source):0,
    pixels:count,centroid:weight ? total/weight/canvas.width : null,
    panels:['#dialogue','#tools-panel'].filter(id=>document.querySelector(id).checkVisibility({visibilityProperty:true})),
    workspace:document.querySelector('main').dataset.mobileWorkspace || null,
    inert:['#dialogue','#tools-panel'].filter(id=>document.querySelector(id).inert),
    playing:playing?{time:playing.currentTime,url:playing.currentSrc}:null};
}"""

SURFACE_SNAPSHOT = """() => {
  const selectors = [
    '.stage-audio-lane', '#caption', '.stage-signal', '.stage-signal figcaption', '#stage-audio',
    '.stage-processing', '.audio-module', '.audio-module h2', '.audio-module .track',
    '#tools-panel[data-resource-kind=music]', '#tools-panel[data-resource-kind=music] > .projection-heading',
    '#tools-panel[data-resource-kind=music] > .projection-body',
    '#tools-panel[data-resource-kind=music] #projection-transport', '#tools-panel[data-resource-kind=music] .media-choice',
    '#tool-player:has(.projection-player[data-kind=audio])', '.projection-player[data-kind=audio]',
    '.projection-player[data-kind=audio] .player-controls', '.player-spectrum'
  ];
  const painted = [];
  const checked = [];
  for (const selector of selectors) {
    for (const node of document.querySelectorAll(selector)) {
      checked.push(selector);
      for (const pseudo of [null, '::before', '::after']) {
        const css = getComputedStyle(node, pseudo);
        if (pseudo && (css.content === 'none' || css.content === 'normal' || css.display === 'none')) continue;
        if (css.backgroundColor !== 'rgba(0, 0, 0, 0)' || css.backgroundImage !== 'none' ||
            css.borderWidth !== '0px' || (css.outlineStyle !== 'none' && css.outlineWidth !== '0px') ||
            css.boxShadow !== 'none' ||
            css.backdropFilter !== 'none') {
          painted.push({selector, pseudo, background:css.background, border:css.borderWidth, outline:css.outline,
            shadow:css.boxShadow, backdrop:css.backdropFilter});
        }
      }
    }
  }
  return {checked, painted};
}"""


def tone_catalog(folder):
    artist = folder / "catalog" / "频谱测试员"
    artist.mkdir(parents=True)
    rate = 16000
    def tone(hz):
        return array("h", (round(11000*sin(2*pi*hz*i/rate)) for i in range(rate*4))).tobytes()
    # Alternate distant bands in a real track; idle UI animation cannot pass this check.
    samples = tone(230) + tone(3300)
    with wave.open(str(artist / "双音阶梯.wav"), "wb") as wav:
        wav.setparams((1,2,rate,0,"NONE",""))
        wav.writeframes(samples*10)
    index = folder / "library.sqlite3"
    library = MediaLibrary(index)
    library.configure([{"id":"qa", "kind":"local", "path":str(folder/"catalog")}])
    assert library.scan()[0]["count"] == 1
    return index


async def check_home(browser, server, folder, result, *, cached_announcement=None, recorded_only=False):
    page = await browser.new_page(viewport={"width":1440,"height":900}, has_touch=True, reduced_motion="no-preference")
    page.set_default_timeout(45000)
    frames = result["home"] = {}
    music_phrase = "为你播放频谱测试员的《双音阶梯》。"
    if recorded_only:
        async def recorded_speech(route):
            data = route.request.post_data_json
            allowed = (data.get("category"), data.get("text")) == ("reply", "好，先到这里。") or (
                cached_announcement and (data.get("category"), data.get("text")) == ("media", music_phrase))
            if allowed:
                await route.continue_()
            else:
                result["errors"].append("出现未预期的合成请求")
                await route.abort()
        await page.route("**/api/speak", recorded_speech)
    page.on("pageerror", lambda e: result["errors"].append(str(e)))
    async def capture(name, signal, *, panels=None, with_caption=False):
        try:
            frame = await page.wait_for_function("""s=>{
                const f=window.hud();
                return f.signal===s && f.visible && f.canvasHits===9 && f.modeHits===9 && f.sourceHits===9 &&
                    (s==='idle' ? f.pixels===0 : f.pixels>15) && f;
            }""", arg=signal, timeout=15000)
            f = frames[name] = await frame.json_value()
        except Exception:
            f = frames[name] = await page.evaluate(HOME_SNAPSHOT)
            raise
        finally:
            await page.screenshot(path=str(folder/(name+".png")), timeout=15000)
        surfaces = f["surfaces"] = await page.evaluate(SURFACE_SNAPSHOT)
        assert not surfaces["painted"], (name, surfaces["painted"])
        if signal == "music":
            assert {"#tools-panel[data-resource-kind=music]", ".player-spectrum"} <= set(surfaces["checked"]), (name, surfaces)
        if signal == "music" and "#tools-panel" in f["panels"]:
            controls = f["player_in_view"] = await page.locator("#tools-panel .player-controls").evaluate("""controls => {
              const panel = controls.closest('#tools-panel').getBoundingClientRect();
              const row = controls.getBoundingClientRect();
              return row.height > 0 && row.top >= panel.top && row.bottom <= panel.bottom;
            }""")
            assert controls, (name, f)
        assert f["mode"] == "实时频谱" and f["source"] and f["width"]>=f["chart"][2]>f["chart"][0]>=0, f
        assert f["height"]>=f["chart"][3]>f["chart"][1]>f["height"]*.50, f
        if with_caption:
            assert f["caption"] and f["caption"][3] <= f["chart"][1], f
        if panels is not None:
            assert f["panels"] == panels, f
        return f
    try:
        await page.goto(server.url+"/", wait_until="domcontentloaded")
        await page.wait_for_selector("#moss-model[data-ready='true']", timeout=120000)
        worker = next(w for w in page.workers if "moss-render" in w.url)
        live = (await scene(worker))["modelRelease"]
        export = json.loads((ROOT/"static/conversation-assets/moss-exterior.json").read_text(encoding="utf-8"))
        assert live["sourceSha256"] == export["sourceSha256"] and live["visibleTriangles"] > 0
        result["model"] = {k:live[k] for k in ("sourceFile","sourceSha256","visibleMeshes","visibleTriangles")}
        await page.wait_for_function("!document.querySelector('main').hasAttribute('data-opening')", timeout=30000)
        await page.evaluate("window.hud="+HOME_SNAPSHOT+"; void 0")
        assert await page.evaluate("!localStorage.getItem('moss.conversation.monitorMode') && !localStorage.getItem('moss.voice.enabled')")
        await capture("home-idle", "idle")
        standby = page.locator(".standby-wave")
        assert await standby.is_visible()
        particle = standby.locator(".standby-particle")
        old_particle = await particle.evaluate("node => getComputedStyle(node).strokeDashoffset")
        await page.wait_for_function("(value) => getComputedStyle(document.querySelector('.standby-particle')).strokeDashoffset !== value",
                                     arg=old_particle, timeout=3000)
        await page.locator("#history-toggle").click()
        await page.evaluate("""chatId => {
            const caption = document.querySelector('#caption');
            let spoken;
            window.addEventListener('moss-speech-start', ({detail}) => {
                spoken = detail;
                // A request from another view does not take ownership of this tab's speech.
                fetch('/api/message', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({chat_id:chatId, text:'谢谢'})}).then(reply => {
                    window.followingReplyStatus = reply.status;
                });
            }, {once:true});
            window.addEventListener('moss-speech-progress', ({detail}) => {
                if (detail.id === spoken?.id) spoken = detail;
            });
            window.addEventListener('moss-agent-state', ({detail}) => {
                const latest = detail.turns.at(-1);
                if (detail.chat_id !== chatId || latest?.user !== '谢谢' || !latest.reply || window.followingCaption) return;
                window.followingCaption = {newReply:latest.reply, audible:spoken?.caption,
                    displayed:caption.hidden ? null : caption.textContent, active:mossSpeech.active(),
                    signal:caption.closest('.stage-audio-lane').dataset.signal,
                    canInterrupt:!document.querySelector('#interrupt').disabled};
            });
        }""", (await server.state())["chat_id"])
        async with page.expect_response(lambda r: r.url.endswith("/api/speak")) as awaited:
            await send(page, server, "算了")
        response = await awaited.value
        result["voice_sha256"] = hashlib.sha256(await response.body()).hexdigest()
        assert response.status == 200 and result["voice_sha256"] == hashlib.sha256((ROOT/"voice/lines/done.wav").read_bytes()).hexdigest()
        await capture("home-voice", "voice", with_caption=True)
        assert not await standby.is_visible()
        following = await page.wait_for_function("window.followingReplyStatus && window.followingCaption", timeout=10000)
        frame = frames["home-voice-during-new-reply"] = await following.json_value()
        assert await page.evaluate("window.followingReplyStatus") == 200, frame
        assert frame["active"] and frame["canInterrupt"] and frame["signal"] == "voice", frame
        assert frame["audible"] and frame["displayed"] == frame["audible"] != frame["newReply"], frame
        await page.wait_for_function("!mossSpeech.active() && hud().signal==='idle'", timeout=60000)
        await page.wait_for_function("document.querySelector('#interrupt').disabled", timeout=3000)
        if recorded_only and not cached_announcement:
            await page.locator(".preferences summary").click()
            await page.get_by_role("checkbox", name="MOSS 语音播报").uncheck()
            await page.locator(".preferences summary").click()
        if cached_announcement:
            async with page.expect_response(lambda r: r.url.endswith("/api/speak")) as announcement:
                await send(page, server, "频谱测试员的双音阶梯")
            actual = await announcement.value
            result["announcement_sha256"] = hashlib.sha256(await actual.body()).hexdigest()
            assert actual.status == 200 and result["announcement_sha256"] == cached_announcement
        else:
            await send(page, server, "频谱测试员的双音阶梯")
        await page.wait_for_function("!mossSpeech.active() && hud().playing?.time>.3", timeout=60000)
        assert await page.locator("#interrupt").is_enabled()
        for selector, button in (("#tools-panel","#music-now"),("#dialogue","#history-toggle")):
            if not await page.locator(selector).is_visible():
                await page.locator(button).click()
        desktop = await capture("home-desktop-music", "music", panels=["#dialogue","#tools-panel"])
        assert desktop["workspace"] is None and not desktop["inert"], desktop
        async def measure_tone(seconds, side):
            await page.locator(".media-audio").evaluate_all("(es,t)=>es.find(a=>!a.paused).currentTime=t", seconds)
            await page.wait_for_function("""([start,side])=>{
                const f=hud(), t=f.playing?.time;
                return f.signal==='music' && t>start+.2 && t<start+3 &&
                    (side==='low' ? f.centroid!==null && f.centroid<.48 : f.centroid>.55);
            }""", arg=[seconds,side], timeout=7000)
            return await capture("home-"+side+"-tone","music")
        low = await measure_tone(.25,"low")
        high = await measure_tone(4.25,"high")
        assert high["centroid"]-low["centroid"]>.18, (low,high)
        await page.locator(".preferences summary").click()
        await page.get_by_role("radio", name="通信波形").check()
        await page.locator(".preferences summary").click()
        frame = await page.wait_for_function("""() => {
            const f = hud();
            return f.signal === 'music' && f.mode === '通信波形' && f.pixels > 15 && f;
        }""")
        frames["home-waveform"] = waveform = await frame.json_value()
        waveform["surfaces"] = await page.evaluate(SURFACE_SNAPSHOT)
        assert not waveform["surfaces"]["painted"], waveform
        await page.screenshot(path=str(folder/"home-waveform.png"), timeout=15000)
        await page.locator(".preferences summary").click()
        await page.get_by_role("radio", name="实时频谱").check()
        await page.locator(".preferences summary").click()
        await page.set_viewport_size({"width":375,"height":812})
        await page.wait_for_function("hud().workspace === 'chat' && matchMedia('(pointer:coarse)').matches")
        chat = await capture("home-mobile-chat", "music", panels=["#dialogue"])
        assert chat["inert"] == ["#tools-panel"], chat
        prompt = page.get_by_test_id("composer")
        draft = "待会儿继续听这一首"
        await prompt.fill(draft)
        log = page.locator("#log")
        scroll = await log.evaluate("log=>{log.scrollTop = (log.scrollHeight-log.clientHeight)/3; return log.scrollTop}")
        track = await page.evaluate_handle("()=>[...document.querySelectorAll('.media-audio')].find(a=>!a.paused)")
        media_return = page.locator("#workspace-media-return")
        await media_return.tap()
        await page.wait_for_function("hud().workspace === 'media'")
        media = await capture("home-mobile-media", "music", panels=["#tools-panel"])
        assert await page.locator("#tools-panel").evaluate("panel => Number(getComputedStyle(panel).opacity) === 1")
        assert await page.locator("#moss-model").evaluate("model => Number(getComputedStyle(model).opacity) <= .2")
        assert media["inert"] == ["#dialogue"], media
        assert await track.evaluate("el=>el.isConnected && !el.paused")
        assert media["playing"]["url"] == chat["playing"]["url"] and media["playing"]["time"] > chat["playing"]["time"]
        await page.keyboard.press("Escape")
        await page.wait_for_function("hud().workspace === 'chat' && document.activeElement.id === 'workspace-media-return'")
        assert await prompt.input_value() == draft
        assert abs(await log.evaluate("log=>log.scrollTop") - scroll) <= 2
        await media_return.tap()
        await page.locator("#tools-close").tap()
        await page.wait_for_function("hud().workspace === 'chat' && !document.querySelector('#workspace-media-return').hidden")
        assert await track.evaluate("el=>el.isConnected && !el.paused")
        assert await prompt.input_value() == draft
        await media_return.tap()
        await page.wait_for_function("hud().workspace === 'media'")
        assert await track.evaluate("el=>el.isConnected && !el.paused")
        await track.dispose()
        await page.locator("#tool-player .player-toggle").click()
        await page.wait_for_function("!hud().playing && !mossSpeech.active()")
        await page.wait_for_function("document.querySelector('#interrupt').disabled", timeout=3000)
        await capture("home-stopped", "idle")
    finally:
        await page.close()


async def check_audible_lanes(browser, server, result, *, file_mic=False):
    text = "窗外的风停了，屋里的灯慢慢亮起来。"
    voice_wav = test_wav(35 if file_mic else 16)
    page = await browser.new_page(viewport={"width":1440, "height":900}, reduced_motion="reduce",
                                  permissions=["microphone"] if file_mic else [])
    page.set_default_timeout(6000)
    page.on("pageerror", lambda error: result["errors"].append(str(error)))
    probe = ""
    if file_mic:
        from verify_duplex_voice_browser import PROBE, make_voice

        probe = PROBE
        fixture = server.folder / "resume"
        fixture.mkdir()
        await asyncio.to_thread(make_voice, fixture, text="继续播放这首")

        async def resume_fixture(route):
            await route.fulfill(path=fixture / "command.wav", content_type="audio/wav")

        await page.route("**/fixture/resume", resume_fixture)
    await page.add_init_script(probe + """
        localStorage.setItem('moss.voice.enabled','off');
        window.laneSpeech = [];
        for (const type of ['start','progress','end','stop','error']) {
          addEventListener('moss-speech-'+type, ({detail}) => {
            laneSpeech.push({type, id:detail.id, position:detail.position});
          });
        }
        window.lanePosition = id => laneSpeech.findLast(entry => entry.id===id && entry.type==='progress')?.position ?? 0;
    """)
    requests = []

    async def recorded_voice(route):
        body = route.request.post_data_json
        requests.append(body)
        if body.get("text") == text and body.get("category") == "reply":
            await route.fulfill(body=voice_wav, content_type="audio/wav")
        else:
            await route.fulfill(status=204)

    await page.route("**/api/{speak,speak-batch}", recorded_voice)
    try:
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        await page.locator(".connection.online").wait_for()
        assert "双音阶梯" in await send(page, server, "频谱测试员的双音阶梯")
        music = (await server.state())["turns"][-1]
        node = page.locator(f'audio[data-audio-turn="{music["id"]}"]').last
        track = await node.element_handle()
        await page.wait_for_function("audio=>!audio.paused && audio.currentTime>.25", arg=track)

        await page.locator(".preferences summary").click()
        await page.get_by_role("checkbox", name="MOSS 语音播报").check()
        await page.locator(".preferences summary").click()
        if file_mic:
            await page.bring_to_front()
            await page.locator("#mic").click()
            await page.locator("#mic[aria-pressed=true]").wait_for(timeout=15000)
        assert await send(page, server, "请朗读以下文字：" + text) == text
        voice = (await server.state())["turns"][-1]
        voice_id = voice["id"]
        assert voice["source"] == "narration", voice
        await page.wait_for_function("id=>mossSpeech.active() && lanePosition(id)>.15", arg=voice_id)
        assert await track.evaluate("audio=>audio.isConnected && !audio.paused")

        async def voice_progresses():
            progress = await page.evaluate("id=>lanePosition(id)", voice_id)
            await page.wait_for_function("([id,start])=>mossSpeech.active() && lanePosition(id)>start+.25",
                                         arg=[voice_id, progress], timeout=3500)
            return await page.evaluate("id=>lanePosition(id)", voice_id)

        async def receipt(command, event):
            async with asyncio.timeout(6):
                while True:
                    turn = (await server.state())["turns"][-1]
                    control = turn.get("transport") or {}
                    if turn["user"] == command and control.get("event"):
                        break
                    await asyncio.sleep(.03)
            assert turn["user"] == command and turn["source"] == "transport" and turn["status"] == "replied", turn
            assert control["event"] == event and control["target_turn"] == music["id"], control
            assert control["media_id"] == music["media"]["selected"]["media_id"], control
            assert control["position"] > 0, control
            return control

        pause_command = "莫斯，暂停一下这首歌"
        assert "已暂停" in await send(page, server, pause_command)
        paused = await receipt(pause_command, "paused")
        stopped_at = await track.evaluate("audio=>{ if (!audio.paused) throw Error('Pause was not applied'); return audio.currentTime; }")
        voice_while_paused = await voice_progresses()
        assert await track.evaluate("(audio,time)=>audio.paused && Math.abs(audio.currentTime-time)<.05", stopped_at)

        resume_command = "继续播放这首"
        if file_mic:
            await page.evaluate("sayFixture('resume')")
            await page.wait_for_function("audio=>!audio.paused", arg=track, timeout=24000)
            await page.wait_for_function("""() => duplexQa.calls.some(row => row.url==='/api/message' &&
                row.text.includes('继续播放这首') && row.status === 200)""",
                                         timeout=6000)
            mic = await page.evaluate("duplexSnapshot()")
            spoken_input, = [row for row in mic["queued"] if row["name"] == "resume"]
            recognition = next(row for row in mic["calls"] if row["url"] == "/api/transcribe" and
                               row.get("status") == 200 and "继续播放这首" in row.get("text", ""))
            submitted = next(row for row in mic["calls"] if row["url"] == "/api/message" and
                             "继续播放这首" in row["text"] and row["status"] == 200)
            assert spoken_input["at"] < recognition["at"] <= submitted["at"]
            assert spoken_input["playing"] and recognition["playing"] and submitted["playing"]
            assert recognition["audioBytes"] > 44 and recognition["live"] == submitted["live"] == 1, mic
            resume_command = submitted["text"]
            result["file_mic"] = {"recognized": recognition["text"], "submitted": resume_command,
                                  "asr_wav_bytes": recognition["audioBytes"],
                                  "live_while_recognizing": recognition["live"]}
        else:
            await page.evaluate("""async text=>{
              const script = document.querySelector('script[src^="/static/agent-terminal.js"]');
              await (await import(script.src)).submitRecognizedText(text);
            }""", resume_command)
            await page.wait_for_function("audio=>!audio.paused", arg=track)
        resumed = await receipt(resume_command, "playing")
        voice_while_resumed = await voice_progresses()
        assert voice_while_resumed > voice_while_paused
        assert await track.evaluate("(audio,time)=>!audio.paused && audio.currentTime>time+.15", stopped_at)

        before_stop = await track.evaluate("audio=>audio.currentTime")
        await page.get_by_test_id("composer").fill("停止播报")
        await page.get_by_test_id("send").click()
        await page.wait_for_function("id=>!mossSpeech.active() && laneSpeech.some(row=>row.type==='stop'&&row.id===id)",
                                     arg=voice_id)
        await page.wait_for_function("""([audio,time])=>audio.isConnected && !audio.paused && audio.currentTime>time+.15""",
                                     arg=[track,before_stop])
        events = await page.evaluate("laneSpeech.filter(row=>row.type!=='progress').map(({type,id})=>({type,id}))")
        assert events == [{"type":"start","id":voice_id}, {"type":"stop","id":voice_id}], events
        assert len(requests) == 1 and requests[0]["text"] == text, requests
        turns = (await server.state())["turns"]
        assert len(turns) == 4 and all(turn["cloud_requests"]["total"] == 0 for turn in turns), turns
        result["audible_lanes"] = {"pause":paused, "resume":resumed, "original_voice_events":events,
                                    "voice_while_paused_seconds":voice_while_paused,
                                    "voice_after_resume_seconds":voice_while_resumed, "tts_requests":len(requests),
                                    "playing_after_voice_stop":True}
        if file_mic:
            await page.locator("#mic").click()
            await page.wait_for_function("duplexQa.tracks.length===1 && duplexQa.tracks[0].readyState==='ended'")
    finally:
        await page.close()


async def check_media_signal_states(browser, server, folder, result):
    states = result["media_signal_states"] = {}
    for missing_capture in (False, True):
        page = await browser.new_page(viewport={"width":1440, "height":900}, reduced_motion="reduce")
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        probe = "localStorage.setItem('moss.voice.enabled','off');"
        if missing_capture:
            probe += """
            for (const name of ['captureStream', 'mozCaptureStream'])
              Object.defineProperty(HTMLMediaElement.prototype, name, {configurable:true, value:undefined});
            """
        await page.add_init_script(probe)
        try:
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for(timeout=10000)
            await page.evaluate("""() => {
              window.readMediaSignal = () => {
                const canvas = document.querySelector('#stage-audio');
                const caption = document.querySelector('#caption');
                const source = document.querySelector('#stage-signal-source');
                const standby = document.querySelector('.standby-wave');
                function ink(chart) {
                  if (!chart?.width || !chart.height) return 0;
                  const rgba = chart.getContext('2d').getImageData(0, 0, chart.width, chart.height).data;
                  let count = 0;
                  for (let i=3; i<rgba.length; i+=4) if (rgba[i]) count++;
                  return count;
                }
                return {signal:canvas.closest('.stage-audio-lane').dataset.signal, label:source.textContent,
                  pixels:ink(canvas), playerPixels:ink(document.querySelector('.player-spectrum')),
                  width:canvas.width, height:canvas.height, standby:standby.checkVisibility(),
                  caption:caption.hidden ? '' : caption.textContent};
              };
            }""")
            assert "双音阶梯" in await send(page, server, "频谱测试员的双音阶梯")
            turn = (await server.state())["turns"][-1]
            audio = page.locator(f'audio[data-audio-turn="{turn["id"]}"]').last
            track = await audio.element_handle()
            assert await track.evaluate("a=>!a.captureStream && !a.mozCaptureStream") == missing_capture
            await page.wait_for_function("a=>!a.paused && a.readyState>=2 && a.currentTime>.25", arg=track, timeout=12000)
            media_id = turn["media"]["selected"]["media_id"]

            async def capture(name, signal, label, *, playing=True):
                try:
                    ready = await page.wait_for_function("""([signal,label])=>{
                      const s=readMediaSignal();
                      const idle=signal==='idle', live=signal==='music';
                      return s.width>0 && s.height>0 && s.signal===signal && s.label===label &&
                        (live ? s.pixels>15 && s.playerPixels>15 : s.pixels===0 && s.playerPixels===0) && s.standby===idle &&
                        (idle ? !s.caption : s.caption.includes('双音阶梯')) && s;
                    }""", arg=[signal, label], polling=95, timeout=6000)
                    state = states[name] = await ready.json_value()
                except Exception:
                    states[name] = await page.evaluate("readMediaSignal()")
                    raise
                finally:
                    await page.screenshot(path=str(folder / f"{name}.png"), timeout=12000)
                state["media"] = await track.evaluate("""a=>({id:a.dataset.mediaId, position:a.currentTime,
                    url:new URL(a.currentSrc).pathname, connected:a.isConnected, paused:a.paused,
                    muted:a.muted, volume:a.volume})""")
                assert state["media"]["id"] == media_id and state["media"]["url"] == "/media/" + media_id, state
                assert state["media"]["connected"] and state["media"]["paused"] == (not playing), state
                return state["media"]["position"]

            async def continues(position):
                await page.wait_for_function("([a,t])=>!a.paused && a.currentTime>t+.18", arg=[track, position])

            if missing_capture:
                before = await capture("media-spectrum-unavailable", "unavailable", "音乐频谱不可用")
                await continues(before)
            else:
                initial = await capture("media-spectrum-live", "music", "音乐")
                await track.evaluate("a=>{ a.muted=true; }")
                muted = await capture("media-spectrum-muted", "muted", "音乐已静音")
                assert muted >= initial
                await continues(muted)
                await track.evaluate("a=>{ a.muted=false; }")
                restored = await capture("media-spectrum-unmuted", "music", "音乐")
                assert restored > muted
                await track.evaluate("a=>{ a.volume=0; }")
                zero = await capture("media-spectrum-volume-zero", "muted", "音乐已静音")
                await continues(zero)
                await track.evaluate("a=>{ a.volume=.7; }")
                assert await capture("media-spectrum-volume-restored", "music", "音乐") > zero
            await track.evaluate("a=>a.pause()")
            paused_name = "media-no-capture-paused" if missing_capture else "media-spectrum-paused"
            await capture(paused_name, "idle", "待机", playing=False)
        finally:
            await page.close()
        if not missing_capture:
            assert (await server.post("/api/new-chat", {}))[0] == 200


async def main(home_only=False, announcement_cache=None, *, audible_lanes=False, file_mic=False,
               media_signal_states=False):
    folder = ROOT / "runs" / datetime.now().strftime("stage-spectrum-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    server = BrowserServer(folder, 18581, model_url="http://127.0.0.1:9/v1")
    cached = None
    if announcement_cache:
        path = MossSpeech(server.data)._announcement_path("为你播放频谱测试员的《双音阶梯》。")
        path.parent.mkdir(parents=True)
        shutil.copyfile(announcement_cache/path.name, path)
        cached = hashlib.sha256(path.read_bytes()).hexdigest()
    environment = {"MOSS_MEDIA_DB":str(tone_catalog(folder))}
    if audible_lanes or media_signal_states:
        environment.update(MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0")
    if file_mic:
        environment["CUDA_VISIBLE_DEVICES"] = ""
    with patch.dict(os.environ, environment):
        await server.start()
    result = {"passed": False, "sizes": {}, "errors": []}
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            await confirm_muted_edge(browser)
            try:
                if media_signal_states:
                    await check_media_signal_states(browser, server, folder, result)
                    assert not result["errors"], result["errors"]
                    result["passed"] = True
                    return
                if audible_lanes:
                    await check_audible_lanes(browser, server, result, file_mic=file_mic)
                    assert not result["errors"], result["errors"]
                    result["passed"] = True
                    return
                await check_home(browser, server, folder, result, cached_announcement=cached, recorded_only=home_only)
                assert not result["errors"], result["errors"]
                if home_only:
                    result["passed"] = True
                    return
                assert (await server.post("/api/new-chat",{}))[0] == 200
                page = await browser.new_page(viewport={"width":1440, "height":900}, reduced_motion="reduce")
                page.set_default_timeout(180000)
                page.on("pageerror", lambda e: result["errors"].append(str(e)))
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                await page.evaluate("window.readStage = " + SNAPSHOT + "; void 0;")
                await page.wait_for_selector("#moss-model[data-ready]", timeout=120000)
                await send(page, server, "记住：北京今天局部多云，早晚有些冷，出门记得穿一件厚外套。")

                async def observe(name, mode):
                    await page.wait_for_function("""() => {
                      const s=readStage(); return s.visible && s.caption && s.stage[0]>20 && s.side[0]>5;
                    }""")
                    frame = await page.evaluate(SNAPSHOT)
                    result["sizes"][name] = frame
                    assert frame["mode"] == mode, frame
                    x1, y1, x2, y2 = frame["chartBox"]
                    assert 0 <= x1 < x2 <= frame["width"] and frame["textBox"][3] <= y1 < y2 <= frame["height"], frame
                    if frame["drawer"]:
                        left, top, right, bottom = frame["drawer"]
                        assert x2 <= left or right <= x1 or y2 <= top or bottom <= y1, frame
                    await page.wait_for_function("""old => {
                      const s=readStage(); return s.stage[0]>20 && s.stage[1]!==old;
                    }""", arg=frame["stage"][1], timeout=5000)
                    await page.screenshot(path=str(folder / (name + ".png")), timeout=15000)
                    await page.wait_for_function("() => !mossSpeech.active() && readStage().stage[0]===0")
                    frame["idle_pixels"] = (await page.evaluate(SNAPSHOT))["stage"][0]

                await observe("desktop", "spectrum")
                await page.locator("#close-chat").click()
                await page.set_viewport_size({"width":390, "height":844})
                await page.get_by_role("button", name="重播播报").click()
                await observe("mobile", "spectrum")
                await page.locator(".preferences summary").click()
                await page.get_by_role("radio", name="通信波形").check()
                await page.locator(".preferences summary").click()
                await page.get_by_role("button", name="重播播报").click()
                await observe("waveform", "waveform")
                await page.get_by_role("button", name="重播播报").click()
                await page.wait_for_function("() => mossSpeech.active() && readStage().stage[0]>20")
                await page.locator("#interrupt").click()
                await page.wait_for_function("() => !mossSpeech.active() && readStage().stage[0]===0")
                result["interrupted_pixels"] = (await page.evaluate(SNAPSHOT))["stage"][0]
                assert not result["errors"], result["errors"]
                result["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, result["passed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home-only", action="store_true")
    parser.add_argument("--announcement-cache", type=Path, help="复用此前实测、与当前音色版本一致的双音阶梯报幕 WAV")
    parser.add_argument("--audible-lanes", action="store_true", help="仅测本机音乐和独立播报的语音及文字控制")
    parser.add_argument("--file-mic", action="store_true", help="双音源测试用文件麦克风和真实本机识别接入继续播放")
    parser.add_argument("--media-signal-states", action="store_true", help="只测真实媒体频谱、静音、采集不可用和暂停")
    args = parser.parse_args()
    if args.file_mic and not args.audible_lanes:
        parser.error("--file-mic 需要 --audible-lanes")
    asyncio.run(main(args.home_only, args.announcement_cache, audible_lanes=args.audible_lanes, file_mic=args.file_mic,
                     media_signal_states=args.media_signal_states))
