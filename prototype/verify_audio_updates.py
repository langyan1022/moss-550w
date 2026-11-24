import asyncio
from datetime import datetime
import json

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, open_browser
from verify_memory import new_chat, send


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("audio-updates-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18349, model_url="http://127.0.0.1:18278/v1")
    await server.start()
    report = {"passed": False}
    try:
        async with async_playwright() as p:
            browser = await open_browser(p)
            page = await browser.new_page(reduced_motion="reduce")
            await page.add_init_script("""const original = HTMLMediaElement.prototype.play;
              HTMLMediaElement.prototype.play = function() {
                if (this.tagName === 'AUDIO') {
                  this.muted = true;
                  const calls=JSON.parse(sessionStorage.getItem('testAudioCalls') || '[]');
                  calls.push(this.getAttribute('src'));
                  sessionStorage.setItem('testAudioCalls', JSON.stringify(calls));
                }
                return original.call(this);
              };""")
            await page.goto(server.url + "?chat=1")
            async def calls():
                return await page.evaluate("JSON.parse(sessionStorage.getItem('testAudioCalls') || '[]')")
            async def send_snapshot(snapshot):
                await page.evaluate("s=>accept(s,true)", snapshot)
            await send(page, server, "帮我找刀郎音乐")
            records = (await server.state())["turns"][-1]["media"]["items"][:2]
            a, b = records
            assert a["media_id"] != b["media_id"] and not await calls()
            await send(page, server, "播放第二首的片段")
            view = await server.state()
            original_chat = view["chat_id"]
            player = view["turns"][-1]["media"]["playback"]
            assert player["status"] == "played" and player["media_id"] == b["media_id"]
            await page.wait_for_function("JSON.parse(sessionStorage.getItem('testAudioCalls') || '[]').length === 1")
            actual_calls = await calls()
            assert b["media_id"] in actual_calls[0] and "#t=" in actual_calls[0]
            await server.stop()
            await page.locator("#connection-label").filter(has_text="正在重连").wait_for(timeout=20000)
            await server.start()
            await page.locator(".connection.online").wait_for()
            assert await calls() == actual_calls
            await new_chat(page, server)
            await page.get_by_test_id("history").click()
            await page.get_by_test_id("chat-item").filter(has_text="刀郎").click()
            await page.get_by_test_id("turn").last.wait_for()
            assert (await server.state())["chat_id"] == original_chat and await calls() == actual_calls
            await page.reload()
            await page.get_by_test_id("media-audio").last.wait_for(state="attached")
            assert await calls() == actual_calls
            report["real_clip"] = {"id": b["media_id"], "title": b["title"], "clip_s": player["clip_s"],
                                   "attempts_before_and_after_reconnect_switch_reload": list(actual_calls)}
            base = await server.state()
            rev = base["revision"] + 100
            turn = {**base["turns"][-1], "id":"ui-only-ordinary", "status":"thinking", "reply":None,
                    "media":{"selected":a, "autoplay":False}}
            async def step(row):
                nonlocal rev
                rev += 1
                state = {**base, "revision":rev, "turns":[*base["turns"],row]}
                await send_snapshot(state)
                return state
            await step(turn)
            audio = page.locator('[data-turn-id="ui-only-ordinary"] audio')
            await audio.evaluate("a=>a.dataset.identity='same-node'")
            assert (await audio.get_attribute("src")).endswith(a["media_id"])
            clip = {**player, "command_id":"ui-only-first", "clip_start_s":4, "clip_s":3}
            updated = {**turn, "media":{"selected":b, "playback":clip, "autoplay":True}}
            await step(updated)
            assert await audio.get_attribute("data-identity") == "same-node"
            assert (await audio.get_attribute("src")).endswith(b["media_id"] + "#t=4,7")
            await step(updated)
            assert await calls() == actual_calls and await audio.evaluate("a=>a.paused")
            changed_bounds = {**updated, "status":"replied", "reply":"已处理。", "media":{
                              **updated["media"], "playback":{**clip, "command_id":"ui-only-new-clip", "clip_start_s":8, "clip_s":6}}}
            await step(changed_bounds)
            await page.locator("#tool-player .player-toggle").click()
            await page.wait_for_function("""() => {const a=document.querySelector('[data-turn-id=ui-only-ordinary] audio');
              return !a.paused && a.currentTime > 8 && a.currentTime < 14; }""")
            assert (await audio.get_attribute("src")).endswith(b["media_id"] + "#t=8,14")
            assert len(await calls()) == 2
            await page.locator("#interrupt").click()
            assert await audio.evaluate("a=>a.paused")
            await step({**changed_bounds,"status":"cancelled"})
            assert await audio.evaluate("a=>a.paused")
            native = {**turn,"id":"ui-only-native","media":None,"native_receipts":[{"tool":"search_media",
                      "result":{"matches":records,"selected_id":a["media_id"]}}]}
            await step(native)
            native_audio = page.locator('[data-turn-id="ui-only-native"] audio')
            assert (await native_audio.get_attribute("src")).endswith(a["media_id"])
            native["native_receipts"].append({"tool":"play_media","result":{**clip,"command_id":"ui-only-native","clip_s":2}})
            await step(native)
            await step({**native,"status":"replied","reply":"已处理。"})
            assert (await native_audio.get_attribute("src")).endswith(b["media_id"] + "#t=4,6")
            assert len(await calls()) == 2 and await native_audio.evaluate("a=>a.paused")
            # 断线期间产生的新回执，在重新连接的第一个完整状态中只恢复音频控件。
            rev += 1
            missed = {**native,"id":"ui-only-missed"}
            restored = {**base,"revision":rev,"turns":[*base["turns"],missed]}
            await page.evaluate("""s=>{
                  stream.dispatchEvent(new Event('error')); stream.dispatchEvent(new Event('open'));
                  stream.dispatchEvent(new MessageEvent('state',{data:JSON.stringify(s)}));
                }""", restored)
            assert await page.locator('[data-turn-id="ui-only-missed"] audio').count() == 1
            assert len(await calls()) == 2
            report["injected_snapshots"] = {"ordinary_change_same_element":True,"same_track_new_clip_played":True,
                      "interrupt_button_paused":True,"cancel_paused":True,
                      "native_clip":b["media_id"] + "#t=4,6", "first_reconnect_snapshot_silent":True, "attempts":await calls()}
            await browser.close()
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder)


if __name__ == "__main__":
    asyncio.run(run())
