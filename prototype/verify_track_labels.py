import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_memory import send
from verify_playlist_recovery import OBSERVER, restore_list


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("track-label-%Y%m%d-%H%M%S")
    lookup_only = "--lookup-only" in sys.argv
    server = BrowserServer(folder,18378 if lookup_only else 18377,model_url="http://127.0.0.1:18278/v1")
    old,_ = restore_list(server.data)
    choice = old["media"]["items"][4]
    label = "西海情歌 · 刀郎"
    report = {"passed":False,"cases":[],"errors":[],"archive_turn":old["id"]}
    await server.start()
    try:
        async with async_playwright() as p:
            edge = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
            browser = await p.chromium.launch(executable_path=str(edge), headless=True,
                          args=["--mute-audio","--autoplay-policy=document-user-activation-required"])
            await confirm_muted_edge()
            report["qa_output_muted"] = True
            page = await browser.new_page(viewport={"width":1440,"height":900},reduced_motion="reduce")
            page.set_default_timeout(180000)
            await page.add_init_script(OBSERVER)
            short_responses = []
            page.on("pageerror",lambda e:report["errors"].append(str(e)))
            page.on("response",lambda r:short_responses.append(r.status) if r.url.endswith("/api/speak") and
                    r.headers.get("x-moss-announcement") == "short" else None)
            await page.goto(server.url)
            await page.locator("#history-toggle").click()
            panel = page.get_by_test_id("media-projection")
            cache = server.data/"voice-announcements"
            assert not list(cache.glob("*.wav"))
            for phase in (() if lookup_only else ("cold","warm")):
                await page.evaluate("expectChoice('西海情歌')")
                text = await send(page,server,label)
                row = (await server.state())["turns"][-1]; media=row["media"]; tid=row["id"]
                assert media["selected"]["media_id"] == choice["media_id"] and media["autoplay"] and len(media["items"])==6
                await page.wait_for_function("""id=>{const e=playlistLog.at(-1).speech;return e.some(x=>x.id===id&&x.type==='moss-speech-start')
                  &&e.some(x=>x.id===id&&x.type==='moss-speech-end');}""",arg=tid)
                a = page.locator(f'[data-audio-turn="{tid}"]')
                await page.wait_for_function("""id=>{const a=document.querySelector('[data-audio-turn="'+id+'"]');
                   return a&&!a.paused&&a.currentTime>.25&&a.duration>100;}""",arg=tid)
                await page.wait_for_function("playlistLog.at(-1).visual.some(f=>f.music.includes('正在播放'))")
                assert await panel.locator(".player-heading strong").inner_text() == choice["title"]
                labels = await panel.locator(".media-choice button small").all_inner_texts()
                assert len(set(labels[4:]))==2 and all(".flac" in x.lower() for x in labels[4:])
                assert all(Path(i["path"]).name in text for i in media["items"][4:])
                if phase=="cold":
                    await page.screenshot(path=str(folder/"desktop-live.png"))
                    async with asyncio.timeout(220):
                        while not list(cache.glob("*.wav")):
                            await asyncio.sleep(.25)
                    await page.wait_for_timeout(1700)
                else:
                    await page.set_viewport_size({"width":390,"height":844})
                    await page.screenshot(path=str(folder/"mobile-live.png"))
                timeline = await page.evaluate("playlistLog.at(-1)")
                starts = [s for s in timeline["speech"] if s["type"]=="moss-speech-start"]
                assert len(starts)==1 and bool(starts[0].get("shortCue")) == (phase=="cold")
                assert len(timeline["starts"])==1 and timeline["starts"][0]["title"]=="西海情歌"
                assert any("正在选曲" in f["music"] for f in timeline["visual"])
                assert short_responses == [200]
                report["cases"].append({"phase":phase,"file":choice["path"],"version_labels":labels[4:],"reply":text,
                        "timeline":timeline,"playing":await a.evaluate("a=>({position:a.currentTime,duration:a.duration,src:a.currentSrc})")})
                print(phase,starts[0].get("shortCue"),flush=True)
            if await panel.is_visible():
                await panel.locator("#tools-close").click()
            await page.locator(".preferences summary").click()
            await page.get_by_role("checkbox",name="MOSS 语音播报").uncheck()
            await page.locator(".preferences summary").click()
            await page.evaluate("expectChoice('喀什噶尔胡杨')")
            answer = await send(page,server,"第二首叫什么，谁唱的？")
            assert "喀什噶尔胡杨" in answer and "刀郎" in answer
            assert await panel.is_hidden() and not await page.evaluate("playlistLog.at(-1).starts.length || playlistLog.at(-1).visual.length")
            await page.get_by_test_id("new-chat").click()
            await page.get_by_test_id("turn").first.wait_for(state="detached")
            await page.evaluate("expectChoice('西海情歌')")
            await send(page,server,label)
            last = (await server.state())["turns"][-1]["media"]
            report["lookup_dom"] = await page.evaluate("""()=>({panelHidden:document.querySelector('#tools-panel').hidden,
                count:document.querySelectorAll('#tool-results .media-choice').length, turns:document.querySelectorAll('.turn').length,
                state:document.querySelector('#projection-transport').textContent, row:document.querySelector('.turn')?.textContent,
                projectionCount:document.querySelectorAll('.projection-link').length})""")
            print("lookup",report["lookup_dom"],flush=True)
            await page.wait_for_function("document.querySelectorAll('#tool-results .media-choice').length===2",timeout=15000)
            await page.wait_for_function("""()=>{const a=document.querySelector('.media-audio');return a?.paused&&a.duration>1&&
                      document.querySelector('#projection-transport').textContent.includes('可试听');}""",timeout=40000)
            await page.screenshot(path=str(folder/"search-ready.png"))
            timeline = await page.evaluate("playlistLog.at(-1)")
            assert not last["autoplay"] and len(last["items"])==2 and not timeline["starts"]
            assert any("正在检索" in f["music"] for f in timeline["visual"])
            assert all(not any(t in f["music"] for t in ("正在选曲","正在载入","正在播放","缓冲中")) for f in timeline["visual"])
            report.update(passed=not report["errors"],question_without_player=answer,
                   new_chat_search={"count":len(last["items"]),"autoplay":False,"visible":timeline["visual"],
                   "audio":await page.get_by_test_id("media-audio").evaluate("a=>({paused:a.paused,position:a.currentTime,duration:a.duration})")},generic_cue_responses=short_responses)
            await browser.close()
        assert report["passed"]
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder)


if __name__=="__main__":
    asyncio.run(run())
