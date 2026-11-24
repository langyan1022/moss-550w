import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_memory import send
from verify_transport_announcement import cache_only_speech


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("music-projection-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18354, model_url="http://127.0.0.1:1/v1")
    with patch.dict(os.environ, MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0"):
        await server.start()
    report = {"passed": False, "cases": {}, "errors": []}
    try:
        async with async_playwright() as p:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            browser = await p.chromium.launch(executable_path=str(edge), headless=True,
                      args=["--mute-audio","--autoplay-policy=document-user-activation-required"])
            report["edge"] = browser.version
            await confirm_muted_edge()
            for name,size in [("desktop",{"width":1440,"height":900}), ("mobile",{"width":390,"height":844})]:
                context = await browser.new_context(viewport=size, is_mobile=name=="mobile", has_touch=name=="mobile",
                                                    reduced_motion="reduce")
                await context.route("**/api/{speak,speak-batch,prepare-speech}", cache_only_speech)
                page = await context.new_page()
                page.set_default_timeout(90000)
                page.on("pageerror", lambda e: report["errors"].append(str(e)))
                await page.add_init_script("""window.audioEvents = [];
                  for (const key of ['moss-speech-start','moss-speech-end','moss-media-start','moss-media-pause'])
                    window.addEventListener(key, e => {
                      const id = e.detail.turnId || e.detail.id;
                      const audio = document.querySelector('.media-audio[data-audio-turn="'+id+'"]');
                      audioEvents.push({type:key,t:performance.now(),id,volume:audio?.volume,shortCue:e.detail.shortCue});
                    });
                  document.addEventListener('submit', e => {
                    if(e.target.id==='composer') audioEvents.push({type:'submit',active:navigator.userActivation.isActive,
                          trusted:e.isTrusted,t:performance.now()});
                  },true);""")
                await page.goto(server.url)
                await page.locator("#moss-model[data-ready=true]").wait_for(timeout=110000)
                assert await page.locator("#dialogue").is_hidden() and await page.locator("#tools-panel").is_hidden()
                await page.screenshot(path=str(folder / (name+"-home.png")))
                await page.locator("#history-toggle").click()
                if name == "mobile":
                    await page.get_by_test_id("new-chat").click()
                    await page.get_by_test_id("turn").first.wait_for(state="detached")
                await page.screenshot(path=str(folder / (name+"-chat.png")))
                await send(page,server,"我想听刀郎的歌")
                turn = (await server.state())["turns"][-1]
                media = turn["media"]
                assert media["autoplay"] and media["play_mode"] == "track" and media["online_calls"] == 0
                selected = media["selected"]
                assert selected["media_id"] == media["items"][0]["media_id"]
                audio = page.locator(f'[data-audio-turn="{turn["id"]}"]')
                await page.wait_for_function("""() => [...document.querySelectorAll('.media-audio')].some(
                  a => !a.muted && a.volume>0 && !a.paused && a.readyState>=2 && a.currentTime>.25 && a.duration>10)""")
                position1 = await audio.evaluate("a=>a.currentTime")
                await page.wait_for_function("t => document.querySelector('.media-audio').currentTime > t + .35",arg=position1)
                actual = await audio.evaluate("a=>({t:a.currentTime,duration:a.duration,volume:a.volume,muted:a.muted,src:a.currentSrc})")
                assert actual["src"].endswith(selected["media_id"]) and 0 < actual["t"] < 15
                panel = page.get_by_test_id("media-projection")
                if name == "mobile":
                    await page.locator("#workspace-media-return").click()
                await panel.wait_for()
                assert await panel.get_by_test_id("track-choice").count() == len(media["items"])
                await panel.locator("#tools-pin").click()
                assert await panel.locator("#tools-pin").get_attribute("aria-pressed") == "true"
                area = await panel.bounding_box()
                assert area["x"] >= 0 and area["x"] + area["width"] <= size["width"]+1
                await page.wait_for_function(
                    "id=>audioEvents.some(e=>e.id===id&&e.type==='moss-speech-end')", arg=turn["id"], timeout=15000)
                notes = await page.evaluate("audioEvents")
                assert any(e["type"] == "submit" and e["active"] and e["trusted"] for e in notes)
                current = [e for e in notes if e.get("id") == turn["id"]]
                assert [e["type"] for e in current] == ["moss-speech-start", "moss-media-start", "moss-speech-end"], current
                assert current[0]["shortCue"] and 0 < current[1]["volume"] <= .15, current
                assert await audio.evaluate("a=>!a.paused && a.volume===1")
                await page.screenshot(path=str(folder / (name+"-music.png")))
                await panel.get_by_test_id("track-choice").nth(1).click()
                second = media["items"][1]["media_id"]
                await page.wait_for_function("id=>[...document.querySelectorAll('.media-audio')].some(a=>a.currentSrc.endsWith(id)&&!a.paused&&a.currentTime>.25)",arg=second)
                audio = page.get_by_test_id("media-audio").last
                await panel.locator("#tools-close").click()
                paused_on_close = name == "desktop"
                assert await panel.is_hidden() and await audio.evaluate("a=>a.paused") == paused_on_close
                await page.get_by_test_id("open-media").last.click()
                assert await panel.is_visible() and await audio.evaluate("a=>a.paused") == paused_on_close
                report["cases"][name] = {"title":selected["title"],"position_before":position1,"playing":actual,
                                     "projection_rect":area,"events":notes,"second_id":second,"online_calls":0}
                await context.close()
            context = await browser.new_context(reduced_motion="reduce")
            await context.route("**/api/{speak,speak-batch,prepare-speech}", cache_only_speech)
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            async def read_without_activation(expression):
                result = await cdp.send("Runtime.evaluate",{"expression":expression,"returnByValue":True,"userGesture":False})
                return result["result"].get("value")
            await page.goto(server.url)
            async with asyncio.timeout(20):
                while not await read_without_activation("!!document.querySelector('.connection.online')"):
                    await asyncio.sleep(.1)
            assert not await read_without_activation("navigator.userActivation.hasBeenActive")
            state = await server.state()
            status,_ = await server.post("/api/message",{"chat_id":state["chat_id"],"text":"那就放这个片段"})
            assert status == 200
            panel = page.get_by_test_id("media-projection")
            async with asyncio.timeout(45):
                while not await read_without_activation("document.querySelector('#tool-player .player-feedback')?.textContent.includes('点击播放键')"):
                    await asyncio.sleep(.1)
            audio = page.get_by_test_id("media-audio").last
            assert await audio.evaluate("a=>a.paused")
            await panel.get_by_role("button",name="播放",exact=True).click()
            await page.wait_for_function("""()=>{const a=[...document.querySelectorAll('.media-audio')].at(-1);
                return !a.paused && !a.muted && a.currentTime>30.1;}""")
            report["autoplay_blocked"] = {"no_prior_gesture":True,"visible_recovery":"点击播放键",
                  "position_after_user_click":await audio.evaluate("a=>a.currentTime")}
            await context.close()
            await browser.close()
        assert not report["errors"]
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder)


if __name__ == "__main__":
    asyncio.run(run())
