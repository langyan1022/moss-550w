import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge


ARCHIVE = ROOT / "runs/media-browser-20261120-123607/data/session.sqlite3"
OBSERVER = """window.playlistLog=[];
  let target, last, raf;
  window.expectChoice = title => {
    target={title,old:new Set([...document.querySelectorAll('.turn')].map(n=>n.dataset.turnId))};
    last=null;
  };
  function begin(e) {
    if (!target || !e.isTrusted) return;
    cancelAnimationFrame(raf);
    last={title:target.title,start:performance.now(),trusted:e.isTrusted,gesture:navigator.userActivation.isActive,
          speech:[],starts:[],visual:[]}; playlistLog.push(last);
    function frame() {
      if(!last) return;
      const elapsed=performance.now()-last.start;
      const row=[...document.querySelectorAll('.turn')].find(n=>!target.old.has(n.dataset.turnId));
      const pane=document.querySelector('#tools-panel'), header=pane.querySelector('.player-heading strong');
      const transport=document.querySelector('#projection-transport');
      const sp=document.querySelector('#projection-speech');
      const visible=(!pane.hidden && transport&&!transport.hidden)? transport.textContent : '';
      const label=(!pane.hidden && sp&&!sp.hidden)? sp.textContent : '';
      const previous=last.visual.at(-1);
      if((visible||label) && (!previous || previous.music!==visible || previous.speech!==label))
        last.visual.push({ms:elapsed,music:visible,speech:label});
      const pending=document.querySelector('#input-hint')?.textContent.includes('正在提交') ||
                    (!pane.hidden && pane.querySelector('.player-feedback')?.textContent.includes('正在选择'));
      if(last.feedback_ms==null && (pending || row)) last.feedback_ms=elapsed;
      if(last.title_ms==null && row && (row.querySelector('.final')?.textContent.includes(target.title) ||
         (!pane.hidden && pane.dataset.turnId===row.dataset.turnId && header?.textContent===target.title))) last.title_ms=elapsed;
      raf=requestAnimationFrame(frame);
    } raf=requestAnimationFrame(frame);
  }
  document.addEventListener('submit',e=>{if(e.target.id==='composer')begin(e)},true);
  document.addEventListener('click',e=>{if(e.target.closest('[data-testid=track-choice]'))begin(e)},true);
  for (const name of ['moss-speech-pending','moss-speech-start','moss-speech-end','moss-speech-skipped','moss-media-start'])
    window.addEventListener(name,e=>{
      const id=e.detail.turnId || e.detail.id;
      if(!last || target.old.has(id)) return;
      const event={type:name,elapsed_ms:performance.now()-last.start,...e.detail};
      (name==='moss-media-start'?last.starts:last.speech).push(event);
    });
"""


def restore_list(data):
    data.mkdir(parents=True,exist_ok=False)
    with sqlite3.connect(ARCHIVE.as_uri()+"?mode=ro",uri=True) as original, sqlite3.connect(data/"session.sqlite3") as copy:
        original.backup(copy)
        rowid, text = copy.execute("SELECT rowid,doc FROM turns WHERE json_extract(doc,'$.user')=? ORDER BY rowid LIMIT 1",
                                  ("我想听刀郎的歌",)).fetchone()
        old = json.loads(text)
        assert "6. 西海情歌" in old["reply"] and not old["media"]["autoplay"] and len(old["media"]["items"]) == 6
        copy.execute("DELETE FROM turns WHERE rowid>?",(rowid,))
    return old,hashlib.sha256(text.encode("utf-8")).hexdigest()


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("playlist-recovery-%Y%m%d-%H%M%S")
    server = BrowserServer(folder,18376,model_url="http://127.0.0.1:18278/v1")
    old, old_hash = restore_list(server.data)
    items = old["media"]["items"]
    report = {"passed":False,"archive":str(ARCHIVE),"unchanged_old_row_sha256":old_hash,"old_id":old["id"],
              "ordered_ids":[item["media_id"] for item in items],"cases":[],"page_errors":[]}
    await server.start()
    requests = []
    receipts = []
    try:
        async with async_playwright() as p:
            edge = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
            browser = await p.chromium.launch(executable_path=str(edge),headless=True,
                                             args=["--mute-audio","--autoplay-policy=document-user-activation-required"])
            await confirm_muted_edge()
            report["qa_output_muted"] = True
            page = await browser.new_page(viewport={"width":1440,"height":900},reduced_motion="reduce")
            await page.add_init_script(OBSERVER)
            page.set_default_timeout(120000)
            page.on("pageerror",lambda e:report["page_errors"].append(str(e)))
            page.on("request",lambda r:requests.append(r.post_data_json["text"]) if r.url.endswith("/api/speak") else None)
            page.on("request",lambda r:receipts.append(r.post_data_json) if r.url.endswith("/api/media-event") else None)
            await page.goto(server.url)
            await page.locator("#history-toggle").click()
            archive_row = page.locator(f'[data-turn-id="{old["id"]}"]')
            assert await archive_row.get_by_test_id("final").inner_text() == old["reply"]
            await archive_row.get_by_test_id("open-media").click()
            assert await page.get_by_test_id("track-choice").count() == 6
            await page.screenshot(path=str(folder/"old-six.png"))
            pid1 = server.pid
            await server.stop()
            await server.start()
            assert server.pid != pid1
            await page.reload()
            await archive_row.get_by_test_id("final").wait_for()
            assert not await page.evaluate("[...document.querySelectorAll('.media-audio')].some(a=>!a.paused)")
            report["process_restarted"] = True

            async def exercise(text,number,mode):
                item = items[number-1]
                count = len((await server.state())["turns"])
                n = len(requests)
                await page.evaluate("title=>expectChoice(title)",item["title"])
                if mode == "click":
                    await page.get_by_test_id("media-projection").get_by_test_id("track-choice").nth(number-1).click()
                else:
                    await page.get_by_test_id("composer").fill(text)
                    await page.get_by_test_id("send").click()
                row = page.get_by_test_id("turn").nth(count)
                await row.get_by_test_id("final").wait_for()
                state = await server.state()
                turn = state["turns"][-1]
                assert len(state["turns"]) == count+1 and turn["user"] == text
                media = turn["media"]
                assert media["selected"]["media_id"] == item["media_id"] and media["online_calls"] == 0
                assert item["title"] in turn["reply"] and [i["media_id"] for i in media["items"]] == report["ordered_ids"]
                if mode != "question":
                    assert media["autoplay"] and media["play_mode"] == "track" and not media.get("playback")
                await page.wait_for_function("""id=>{const events=playlistLog.at(-1)?.speech.filter(e=>e.id===id)||[];
                     return events.some(e=>e.type==='moss-speech-skipped') || (events.some(e=>e.type==='moss-speech-start')
                             && events.some(e=>e.type==='moss-speech-end'));}""",arg=turn["id"])
                entry = {"input":text,"mode":mode,"id":turn["id"],"index":number,"title":item["title"],
                         "reply":turn["reply"],"synthesis":requests[n:],"online_calls":media["online_calls"]}
                voice_log = await page.evaluate("playlistLog.at(-1).speech")
                assert any(item["title"] in sentence for sentence in entry["synthesis"]) or any(event.get("shortCue") for event in voice_log)
                if mode == "question":
                    assert not media["autoplay"] and "浏览器" in turn["reply"]
                    assert await page.evaluate("[...document.querySelectorAll('.media-audio')].some(a=>!a.paused)")
                else:
                    await page.wait_for_function("""id=>{const a=document.querySelector('[data-audio-turn="'+id+'"]');
                         return a&&!a.paused&&!a.muted&&a.currentTime>.35&&a.duration>15;}""",arg=turn["id"])
                    a = row.get_by_test_id("media-audio")
                    actual = await a.evaluate("a=>({t:a.currentTime,duration:a.duration,volume:a.volume,src:a.currentSrc,muted:a.muted})")
                    assert actual["src"].endswith(item["media_id"]) and "#t=" not in actual["src"]
                    pane = page.get_by_test_id("media-projection")
                    assert await pane.is_visible() and await pane.locator(".player-heading strong").inner_text() == item["title"]
                    chosen = await pane.locator('article[aria-current] button span').inner_text()
                    assert chosen == item["title"]
                    response = await page.request.get(actual["src"],headers={"Range":"bytes=0-127"})
                    prefix = await response.body()
                    with Path(item["path"]).open("rb") as fp:
                        assert response.status==206 and prefix==fp.read(128)
                    entry.update(browser_audio=actual,first_128_sha256=hashlib.sha256(prefix).hexdigest())
                    expected_event = {"chat_id":turn["chat_id"],"turn_id":turn["id"],"media_id":item["media_id"],"event":"started"}
                    matching = [r for r in receipts if all(r.get(k) == v for k, v in expected_event.items())]
                    assert len(matching) == 1 and matching[0]["play_id"]
                    if not report["cases"]:
                        await page.screenshot(path=str(folder/"second-playing.png"))
                timing = await page.evaluate("playlistLog.at(-1)")
                assert timing["trusted"] and timing["gesture"] and timing["feedback_ms"]>=0 and timing["title_ms"]>=0
                assert len(timing["starts"]) == (0 if mode=="question" else 1)
                if timing["starts"]:
                    assert timing["starts"][0]["title"] == item["title"]
                    assert any("正在播放" in f["music"] and item["title"] in f["music"] for f in timing["visual"])
                entry["timing"]=timing
                report["cases"].append(entry)
                print(text,item["title"],round(timing["title_ms"]),"ms",flush=True)

            await exercise("第二首",2,"cold")
            await exercise("下一首",3,"next")
            await exercise("播放第 2 首",2,"click")
            await exercise("现在播放的是哪首歌？",2,"question")
            await exercise("下一首",3,"warm")
            await exercise("第二首",2,"warm")
            report.update(passed=not report["page_errors"],edge=browser.version,audio_receipts=receipts)
            await browser.close()
        assert report["passed"]
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder)


if __name__=="__main__":
    asyncio.run(run())
