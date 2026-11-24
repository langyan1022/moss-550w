import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
from unittest.mock import patch
import wave

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send
from verify_playlist_recovery import restore_list
from voice_output import MossSpeech


def seed_full_announcement(data, item):
    speech = MossSpeech(data)
    artist = (item["artist"] + "的") if item.get("artist") else ""
    caption = f"为你播放{artist}《{item['title']}》。"
    path = speech._announcement_path(speech._spoken(caption, "media"))
    path.parent.mkdir(exist_ok=True)
    # A five-second WAV tests the named cache branch without synthesis or live cache data.
    with wave.open(str(path), "wb") as fixture:
        fixture.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        fixture.writeframes(bytes(5 * 16000 * 2))
    return caption


async def cache_only_speech(route, *, holds=None, expected_text=None):
    request = route.request
    if request.url.endswith("/api/speak") and request.method == "POST":
        body = request.post_data_json
        if body["category"] == "media" and body.get("prefer_cached") is True:
            if holds is not None and not holds.empty() and (expected_text is None or body["text"] == expected_text):
                intercepted, release = holds.get_nowait()
                intercepted.set()
                await asyncio.wait_for(release.wait(), 15)
            await route.continue_()
            return
    await route.fulfill(status=204)


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("queued-control-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18483, model_url="http://127.0.0.1:18278/v1")
    old_list, _ = restore_list(server.data)
    full_caption = seed_full_announcement(server.data, old_list["media"]["items"][0])
    report = {"passed":False, "errors":[], "cases":[]}
    with patch.dict(os.environ, MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0"):
        await server.start()
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            await confirm_muted_edge(browser)
            context = await browser.new_context(viewport={"width":1440, "height":900}, reduced_motion="reduce")
            holds = asyncio.Queue()

            async def held_speech(route):
                await cache_only_speech(route, holds=holds, expected_text=full_caption)

            await context.route("**/api/{speak,speak-batch,prepare-speech}", held_speech)
            page = await context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            await page.add_init_script("""window.mediaTrace=[]; window.nativePlay=[];
              const play = HTMLMediaElement.prototype.play;
              HTMLMediaElement.prototype.play = function(...args) {
                const promise = play.apply(this, args);
                if (this.dataset.audioTurn) {
                  const attempt = {id:this.dataset.audioTurn, result:'pending'};
                  nativePlay.push(attempt);
                  promise.then(() => { attempt.result='started'; }, error => { attempt.result=error.name; });
                }
                return promise;
              };
              for (const event of ['moss-speech-start','moss-speech-end','moss-speech-stop',
                  'moss-speech-skipped','moss-speech-error','moss-media-start']) {
                window.addEventListener(event,e=>mediaTrace.push({
                  event,id:e.detail.turnId||e.detail.id,at:performance.now(),shortCue:e.detail.shortCue===true
                }));
              }""")
            captured = asyncio.Queue()
            sibling_done = asyncio.Event()
            async def delay_control(route):
                body = route.request.post_data_json
                memory = body["text"].startswith("记住：")
                if memory or body["text"] in ("停止音乐", "暂停音乐"):
                    captured.put_nowait(body)
                    await asyncio.sleep(1.4)
                    if memory:
                        await asyncio.wait_for(sibling_done.wait(), 20)
                await route.continue_()
            await page.route("**/api/message", delay_control)
            path = server.url + "/?chat=1&scene=off"
            await page.goto(path, wait_until="domcontentloaded")
            speech = page.get_by_label("MOSS 语音播报")

            async def voice(enabled):
                if await speech.is_checked() != enabled:
                    await speech.evaluate("e=>e.click()")

            async def wait_event(turn_id, event):
                try:
                    await page.wait_for_function(
                        "([id,event])=>mediaTrace.some(e=>e.id===id&&e.event===event)",
                        arg=[turn_id, event], polling=50, timeout=8000)
                except PlaywrightTimeoutError:
                    report["event_timeout"] = await page.evaluate("""([id, event])=>({
                      id, event, visibility:document.visibilityState, trace:mediaTrace.filter(e=>e.id===id)
                    })""", [turn_id, event])
                    raise

            @asynccontextmanager
            async def waiting_for_announcement(text):
                await voice(True)
                intercepted, release = asyncio.Event(), asyncio.Event()
                holds.put_nowait((intercepted, release))
                try:
                    await send(page, server, text)
                    turn_id = (await server.state())["turns"][-1]["id"]
                    await asyncio.wait_for(intercepted.wait(), 3)
                    trace = await page.evaluate("id=>mediaTrace.filter(e=>e.id===id)", turn_id)
                    assert not trace, trace
                    yield turn_id
                finally:
                    release.set()

            async def control(text, target):
                sending = asyncio.create_task(send(page, server, text))
                payload = await asyncio.wait_for(captured.get(), 6)
                assert payload["queued_player"]["turn_id"] == target
                await voice(False)
                reply = await sending
                turn = (await server.state())["turns"][-1]
                trace = await page.evaluate("id=>mediaTrace.filter(e=>e.id===id)", target)
                assert not any(e["event"] in {"moss-speech-start", "moss-media-start"} for e in trace), trace
                return reply, turn, payload

            async with waiting_for_announcement("第一首") as first:
                reply, turn, _ = await control("停止音乐", first)
            assert "已取消待播放" in reply and turn["transport"]["event"] == "queue_cancelled"
            report["cases"].append({"reply":reply, "event":turn["transport"]["event"]})

            await send(page, server, "第二首")
            old = (await server.state())["turns"][-1]["id"]
            previous = page.locator(f'[data-audio-turn="{old}"]')
            await wait_event(old, "moss-media-start")
            # 新首歌会释放旧音源。恢复音源后等原生播放成功，再测试双目标。
            async with waiting_for_announcement("第一首") as second:
                await asyncio.wait_for(previous.evaluate("""async a=>{
                  const {restoreMediaSource} = await import('/static/agent-transport.js');
                  restoreMediaSource(a);
                  await a.play();
                }"""), 1)
                assert await previous.evaluate("a=>!a.paused")
                reply, turn, payload = await control("暂停音乐", second)
            assert payload["player"]["turn_id"] == old and turn["transport"]["cancelled_queue"]
            assert turn["transport"]["event"] == "paused" and "已取消待播放" in reply and "已暂停" in reply
            assert await previous.evaluate("a=>a.paused")
            report["cases"].append({"reply":reply, "event":turn["transport"]["event"]})

            sibling = await context.new_page()
            sibling.set_default_timeout(15000)
            await sibling.goto(path, wait_until="domcontentloaded")
            await sibling.get_by_test_id("composer").wait_for()
            await page.bring_to_front()
            async with waiting_for_announcement("第一首") as third:
                ordinary = asyncio.create_task(send(page, server, "记住：我喜欢纯音乐"))
                body = await asyncio.wait_for(captured.get(), 6)
                assert body["queued_player"]["turn_id"] == third
                await voice(False)
                await sibling.bring_to_front()
                assert "当前页面没有" in await send(sibling, server, "停止音乐")
                await page.bring_to_front()
                sibling_done.set()
                # 另一页可能先提交一轮，用正在发送的消息内容找到本轮记录。
                await ordinary
            memory = page.get_by_test_id("turn").filter(has_text="记住：我喜欢纯音乐").last
            assert "已记住" in await memory.get_by_test_id("final").inner_text()
            await wait_event(third, "moss-media-start")
            await sibling.close()
            report["other_page_kept_queue"] = True
            report["ordinary_message_preserved_queue"] = True

            for text, variant in (("第一首", "full"), ("第二首", "short")):
                await voice(True)
                await send(page, server, text)
                playing = (await server.state())["turns"][-1]["id"]
                await wait_event(playing, "moss-media-start")
                async with asyncio.timeout(3):
                    while True:
                        media_turn = next(t for t in (await server.state())["turns"] if t["id"] == playing)
                        if (media_turn.get("browser_playback") or {}).get("event") == "started":
                            break
                        await asyncio.sleep(.025)
                before_stop = await page.evaluate("id=>mediaTrace.filter(e=>e.id===id)", playing)
                assert [e["event"] for e in before_stop] == ["moss-speech-start", "moss-media-start"], before_stop
                assert before_stop[0]["shortCue"] == (variant == "short"), before_stop
                stopping = asyncio.create_task(send(page, server, "停止音乐"))
                body = await asyncio.wait_for(captured.get(), 6)
                await voice(False)
                reply = await stopping
                turn = (await server.state())["turns"][-1]
                trace = await page.evaluate("id=>mediaTrace.filter(e=>e.id===id)", playing)
                assert len(trace) >= 3 and trace[2]["event"] == "moss-speech-stop", trace
                assert body.get("queued_player") is None and body["player"]["turn_id"] == playing, body
                assert body["player"]["state"] == "playing", body
                assert turn["transport"]["event"] == "stopped" and "已停止" in reply, turn
                assert not turn["transport"]["cancelled_queue"], turn
                stopped = await page.locator(f'[data-audio-turn="{playing}"]').evaluate("a=>a.paused && a.currentTime <= .1")
                assert stopped
                report[f"{variant}_announcement_live_stop"] = {"reply":reply, "event":turn["transport"]["event"], "trace":trace}

            async def during_native_start(text, item, command):
                intercepted, release = asyncio.Event(), asyncio.Event()
                url = "**/media/" + item["media_id"]

                async def held_bytes(route):
                    intercepted.set()
                    await asyncio.wait_for(release.wait(), 12)
                    await route.continue_()

                await page.route(url, held_bytes)
                try:
                    await send(page, server, text)
                    pending = (await server.state())["turns"][-1]["id"]
                    await asyncio.wait_for(intercepted.wait(), 5)
                    await page.wait_for_function(
                        "id=>nativePlay.some(e=>e.id===id&&e.result==='pending')", arg=pending, polling=50)
                    node = page.locator(f'[data-audio-turn="{pending}"]')
                    assert await node.evaluate("a=>!a.paused && a.readyState < 3")
                    sending = asyncio.create_task(send(page, server, command))
                    body = await asyncio.wait_for(captured.get(), 6)
                    assert body["queued_player"] == {"turn_id":pending, "media_id":item["media_id"]}, body
                    assert await node.evaluate("a=>a.paused")
                    # Release the media connection while the command response is still delayed.
                    release.set()
                    reply = await sending
                    return pending, node, reply
                finally:
                    release.set()
                    await page.unroute(url, held_bytes)

            items = old_list["media"]["items"]
            pending, node, reply = await during_native_start("第三首", items[2], "停止音乐")
            state = await server.state()
            stopped = state["turns"][-1]
            track = next(t for t in state["turns"] if t["id"] == pending)
            assert stopped["transport"]["event"] == "queue_cancelled" and "已取消待播放" in reply, stopped
            assert await node.evaluate("a=>a.paused && a.currentTime <= .1")
            assert not track.get("browser_playback"), track
            attempts = await page.evaluate("id=>nativePlay.filter(e=>e.id===id).map(e=>e.result)", pending)
            assert attempts == ["AbortError"], attempts
            report["native_pending_cancel"] = {"reply":reply, "attempts":attempts}

            pending, node, reply = await during_native_start("第四首", items[3], "记住：我喜欢听现场版")
            assert "已记住" in reply, reply
            await wait_event(pending, "moss-media-start")
            assert await node.evaluate("a=>!a.paused")
            attempts = await page.evaluate("id=>nativePlay.filter(e=>e.id===id).map(e=>e.result)", pending)
            assert attempts == ["AbortError", "started"], attempts
            report["native_pending_release"] = {"reply":reply, "attempts":attempts}

            await page.reload(wait_until="domcontentloaded")
            await page.get_by_test_id("turn").first.wait_for()
            assert await page.locator(".media-audio").evaluate_all("nodes=>nodes.length&&nodes.every(a=>a.paused)")
            assert not report["errors"]
            report.update(passed=True, muted=True, control_request_delay_ms=1400)
            await browser.close()
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
