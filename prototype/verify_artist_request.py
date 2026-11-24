import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from memory_store import install_schema
from verify_memory import new_chat, send


OBSERVER = """window.attempts=[];
function beginAttempt(text,e){
  attempts.push({text,start:performance.now(),trusted:e.isTrusted,
    pending_ms:null,visible_ms:null,reply_ms:null,old:new Set([...document.querySelectorAll('.turn')].map(n=>n.dataset.turnId)),
    speech:[],music:[],clicks:[]});
}
document.addEventListener('submit',e=>{
  if(e.target.id==='composer')beginAttempt(document.getElementById('message').value,e);
},true);
document.addEventListener('click',e=>{
  const retry=e.target.closest('[data-testid=retry-music]');
  if(retry){beginAttempt(retry.closest('.turn').querySelector('.user-message').textContent,e);return;}
  const a=attempts.at(-1);if(a)a.clicks.push(e.target.id||e.target.tagName);
},true);
for(const type of ['moss-speech-start','moss-media-start'])
  window.addEventListener(type,e=>{const a=attempts.at(-1);if(a)a[type==='moss-media-start'?'music':'speech']
    .push({...e.detail,ms:Math.round(performance.now()-a.start)});});
new MutationObserver(()=>{
  const a=attempts.at(-1);if(!a)return;
  const row=[...document.querySelectorAll('.turn')].find(n=>!a.old.has(n.dataset.turnId));
  const panel=document.getElementById('tools-panel');
  if(panel&&!panel.hidden&&/正在选曲|正在检索曲目/.test(document.getElementById('projection-transport')?.textContent)&&a.pending_ms===null)
    a.pending_ms=Math.round(performance.now()-a.start);
  if(row?.querySelector('[data-testid=final]')&&a.reply_ms===null)a.reply_ms=Math.round(performance.now()-a.start);
  if(panel&&!panel.hidden&&panel.dataset.turnId===row?.dataset.turnId&&panel.querySelector('.media-choice')&&a.visible_ms===null)
    a.visible_ms=Math.round(performance.now()-a.start);
}).observe(document,{subtree:true,childList:true,attributes:true,characterData:true});"""


async def run(archive=None):
    folder = ROOT/"runs"/datetime.now().strftime("artist-auto-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder,18464)
    report = {"passed":False,"cases":[],"errors":[]}
    if archive:
        original = json.loads(archive.read_text(encoding="utf-8"))
        assert original["status"]=="error" and not original.get("native_receipts") and not original.get("receipt")
        server.data.mkdir(parents=True)
        with sqlite3.connect(server.data/"session.sqlite3") as db:
            install_schema(db)
            db.execute("INSERT INTO chats VALUES(?,?,?)",(original["chat_id"],original["user"],original["created_at"]))
            db.execute("UPDATE state SET active_chat=? WHERE id=1",(original["chat_id"],))
            db.execute("INSERT INTO turns(id,doc,chat_id) VALUES(?,?,?)",(original["id"],json.dumps(original,ensure_ascii=False),original["chat_id"]))
        report["archive"] = {"id":original["id"],"error":original["error"],"sha256":hashlib.sha256(archive.read_bytes()).hexdigest()}
    await server.start()
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            await confirm_muted_edge(browser)
            report["muted_device"] = True
            page = await browser.new_page(viewport={"width":1440,"height":900},reduced_motion="reduce")
            page.set_default_timeout(75000)
            await page.add_init_script(OBSERVER)
            page.on("pageerror",lambda error: report["errors"].append(str(error)))
            await page.goto(server.url+"?chat=1",wait_until="domcontentloaded")
            await page.locator(".preferences summary").click()
            await page.wait_for_function("document.querySelector('#agent-model').value === 'local'")
            await page.locator(".preferences summary").click()
            models = await (await page.request.get(server.url+"/api/models")).json()
            report["model"] = next(m for m in models["models"] if m["id"] == "local")
            assert report["model"]["available"]
            panel = page.get_by_test_id("media-projection")

            async def exercise(prompt, index, play, retry=None):
                old = await server.state()
                if retry:
                    await retry.click()
                    answer = page.get_by_test_id("turn").nth(len(old["turns"])).get_by_test_id("final")
                    await answer.wait_for()
                    reply = await answer.inner_text()
                else:
                    reply = await send(page,server,prompt)
                state = await server.state()
                turn = state["turns"][-1]
                assert len(state["turns"])==len(old["turns"])+1 and turn["status"]=="replied"
                assert turn["source"]=="media" and turn["model_id"]=="local"
                media = turn["media"]
                items, selected = media["items"], media["selected"]
                assert len(items)>=2 and selected["media_id"]==items[index]["media_id"]
                assert all(i.get("artist")=="刘德华" or "刘德华" in Path(i["path"]).parts for i in items)
                assert all(Path(i["path"]).drive.casefold()=="g:" for i in items)
                assert selected["title"] in reply and not any(x in reply for x in ("？","?","请告诉我","是否播放"))
                assert media["online_calls"]==0 and media["autoplay"] is play
                await panel.locator(".media-choice button").first.wait_for()
                labels = await panel.locator(".media-choice button > span").all_inner_texts()
                assert labels==[i["title"] for i in items]
                node = page.locator(f'[data-audio-turn="{turn["id"]}"]')
                if play:
                    await page.wait_for_function("""id=>{const a=document.querySelector('[data-audio-turn="'+id+'"]');
                        return a&&!a.paused&&a.currentTime>.15&&a.duration>20&&a.readyState>=3;}""",arg=turn["id"])
                    pos = await node.evaluate("a=>a.currentTime")
                    await page.wait_for_function("""([id,t])=>document.querySelector('[data-audio-turn="'+id+'"]').currentTime>t+.2""",
                                                 arg=[turn["id"],pos])
                else:
                    await page.wait_for_function("""id=>document.querySelector('[data-audio-turn="'+id+'"]')?.duration>20""",arg=turn["id"])
                audio = await node.evaluate("a=>({src:a.currentSrc,pos:a.currentTime,duration:a.duration,paused:a.paused})")
                assert audio["src"].endswith("/media/"+selected["media_id"]) and audio["paused"] is not play
                if not play:
                    assert audio["pos"]==0 and not await page.evaluate("[...document.querySelectorAll('.media-audio')].some(a=>!a.paused)")
                ranged = await page.request.get(audio["src"],headers={"Range":"bytes=0-127"})
                header = await ranged.body()
                with Path(selected["path"]).open("rb") as original:
                    assert ranged.status==206 and header==original.read(128)
                timeline = await page.evaluate("()=>{const {old,start,...rest}=attempts.at(-1);return rest}")
                assert not timeline["clicks"] and timeline["trusted"] and timeline["visible_ms"] is not None
                assert len(timeline["music"])==int(play)
                if play:
                    assert timeline["music"][0]["turnId"]==turn["id"]
                report["cases"].append({"prompt":prompt,"input":"retry" if retry else "message","reply":reply,"chat_id":state["chat_id"],"turn":turn["id"],
                      "source":turn["source"],"titles":labels,
                      "selected":selected,"audio":audio,"file_header_sha256":hashlib.sha256(header).hexdigest(),"timing":timeline})
                print(prompt, audio, timeline["visible_ms"],flush=True)
                return labels

            if archive:
                archived = page.locator(f'[data-turn-id="{original["id"]}"]')
                retry = archived.get_by_test_id("retry-music")
                await retry.wait_for()
                await page.screenshot(path=str(folder/"original-offline.png"))
                await exercise(original["user"],0,True,retry)
                await page.screenshot(path=str(folder/"retry-playing.png"))
                recovered = (await server.state())["turns"]
                assert recovered[0]["id"]==original["id"] and recovered[0]["error"]==original["error"]
                assert len(recovered)==2 and recovered[1]["user"]==original["user"]
                await new_chat(page,server)
            titles = await exercise("我想听刘德华的歌",0,True)
            await page.screenshot(path=str(folder/"artist-playing.png"))
            assert titles==await exercise("下一首",1,True)
            await new_chat(page,server)
            await page.locator(".preferences summary").click()
            await page.get_by_role("checkbox",name="MOSS 语音播报").uncheck()
            await page.locator(".preferences summary").click()
            assert titles==await exercise("只看看刘德华的歌",0,False)
            await page.screenshot(path=str(folder/"artist-browse.png"))
            await new_chat(page,server)
            await page.locator(".preferences summary").click()
            await page.get_by_role("checkbox",name="MOSS 语音播报").check()
            await page.locator(".preferences summary").click()
            assert titles==await exercise("来一手刘德华的歌",0,True)
            assert report["cases"][-1]["timing"]["pending_ms"] is not None
            await page.screenshot(path=str(folder/"artist-voice-text.png"))
            assert not report["errors"]
            report["passed"] = True
            await browser.close()
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder,flush=True)


if __name__=="__main__":
    import argparse

    parser=argparse.ArgumentParser()
    parser.add_argument("--archive",type=Path)
    asyncio.run(run(parser.parse_args().archive))
