import asyncio
from datetime import datetime
import json
import os
from unittest.mock import patch

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send
from verify_playlist_recovery import restore_list
from verify_transport_announcement import cache_only_speech, seed_full_announcement


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("announcement-switch-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18384, model_url="http://127.0.0.1:1/v1")
    old, _ = restore_list(server.data)
    seed_full_announcement(server.data, old["media"]["items"][0])
    with patch.dict(os.environ, MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0"):
        await server.start()
    data = {"passed":False}
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            await confirm_muted_edge()
            page = await browser.new_page(reduced_motion="reduce")
            page.set_default_timeout(75000)
            holds = asyncio.Queue()

            async def held_speech(route):
                await cache_only_speech(route, holds=holds)

            await page.route("**/api/{speak,speak-batch,prepare-speech}", held_speech)
            await page.add_init_script("""window.traces=[];
              for (const name of ['moss-speech-start','moss-speech-end','moss-speech-stop','moss-speech-skipped','moss-media-start'])
                addEventListener(name,e=>traces.push({name,...e.detail,t:performance.now()}));""")
            await page.goto(server.url + "/?chat=1&scene=off")
            intercepted, release = asyncio.Event(), asyncio.Event()
            holds.put_nowait((intercepted, release))
            try:
                await send(page,server,"第一首")
                a = (await server.state())["turns"][-1]["id"]
                await asyncio.wait_for(intercepted.wait(), 3)
                await send(page,server,"2")
            finally:
                release.set()
            b = (await server.state())["turns"][-1]["id"]
            await page.wait_for_function("id=>traces.some(x=>x.turnId===id&&x.name==='moss-media-start')",arg=b)
            recorded = await page.evaluate("traces")
            assert not any(x.get("id")==a and x["name"]=="moss-speech-start" for x in recorded), recorded
            assert [x["turnId"] for x in recorded if x["name"]=="moss-media-start"] == [b], recorded
            actual = await page.locator(f'[data-audio-turn="{b}"]').evaluate("a=>({src:a.currentSrc,seconds:a.duration,paused:a.paused})")
            assert not actual["paused"] and actual["seconds"]>100, actual
            data.update(latest_only=recorded, audio=actual)
            # 通过真实浏览器网络限速复验慢响应；歌曲和报幕仍由本机服务实际提供。
            client = await page.context.new_cdp_session(page)
            await client.send("Network.enable")
            await client.send("Network.emulateNetworkConditions", {"offline":False, "latency":2200,
                              "downloadThroughput":20000000,"uploadThroughput":20000000})
            await send(page,server,"第一首")
            c = (await server.state())["turns"][-1]["id"]
            await page.wait_for_function("""id=>traces.some(x=>x.id===id&&x.name==='moss-speech-skipped')
              &&traces.some(x=>x.turnId===id&&x.name==='moss-media-start')""",arg=c)
            slow = await page.evaluate("id=>traces.filter(x=>x.id===id||x.turnId===id)",c)
            assert not any(x["name"]=="moss-speech-start" for x in slow), slow
            data.update(passed=True, slow_response=slow, throttling_ms=2200)
            await browser.close()
    finally:
        try:
            await server.stop()
        finally:
            (folder/"result.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
            print(folder, data["passed"])


if __name__=="__main__":
    asyncio.run(run())
