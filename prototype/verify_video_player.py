import asyncio
from datetime import datetime
import json
import os
import shutil
import subprocess
import sys
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send


async def photograph(page, folder, name):
    await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=90000)
    panel = page.get_by_test_id("media-projection")
    video = panel.locator(".projection-player > video")
    await video.wait_for()
    if not await video.evaluate("v=>v.paused"):
        await panel.get_by_role("button", name="暂停", exact=True).click()
    seek = panel.get_by_role("slider", name="播放进度")
    await seek.press("Home")
    await seek.press("End")
    for _ in range(17):
        await seek.press("ArrowLeft")
    await page.wait_for_function("""()=>{const v=document.querySelector('#tool-player video');
        return v&&!v.seeking&&v.readyState>=2&&v.currentTime>1&&v.currentTime<v.duration-.2}""")
    await page.screenshot(path=str(folder / (name + ".png")))
    return await panel.evaluate("""p=>{
      const visible = e=>{const r=e.getBoundingClientRect(), x=r.x+r.width/2,y=r.y+r.height/2;
        return {width:Math.round(r.width),height:Math.round(r.height),
          hit:r.width>0&&r.height>0&&e.contains(document.elementFromPoint(x,y))}};
      return Object.fromEntries(['video','.player-toggle','.player-seek','.player-mute','.player-fullscreen','#tools-close']
        .map(s=>[s,visible(p.querySelector(s))]));
    }""")


async def verify():
    folder = ROOT / "runs" / ("video-player-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    catalog = folder / "catalog"
    catalog.mkdir(parents=True)
    sample = catalog / "测试航行片段.mp4"
    shutil.copyfile(ROOT / "static/conversation-assets/moss-opening.mp4", sample)
    artist = catalog / "测试乐手"
    artist.mkdir()
    with wave.open(str(artist / "测试航行音乐.wav"), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", ""))
        stream.writeframes(bytes(8000 * 2 * 30))
    database = folder / "library.sqlite3"
    config = folder / "sources.json"
    config.write_text(json.dumps([{"id":"video-test", "label":"隔离媒体库", "kind":"local", "path":str(catalog)}],
                                 ensure_ascii=False), encoding="utf-8")
    subprocess.run([sys.executable, "-X", "utf8", "-m", "local_media.media_cli", "scan",
                    "--config", str(config), "--db", str(database)], cwd=ROOT,
                   check=True, capture_output=True, encoding="utf-8", errors="replace")
    server = BrowserServer(folder, 18472)
    previous = os.environ.get("MOSS_MEDIA_DB")
    os.environ["MOSS_MEDIA_DB"] = str(database)
    try:
        await server.start()
    finally:
        if previous is None:
            os.environ.pop("MOSS_MEDIA_DB")
        else:
            os.environ["MOSS_MEDIA_DB"] = previous
    report = {"passed":False}
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            await confirm_muted_edge(browser)
            page = await browser.new_page(viewport={"width":1440, "height":900}, reduced_motion="reduce")
            page.set_default_timeout(15000)
            await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
            await page.locator('#moss-model[data-ready="true"]').wait_for(timeout=90000)
            answer = await send(page, server, "搜索本地视频 测试航行片段")
            assert "测试航行片段" in answer, answer
            found = (await server.state())["turns"][-1]["media"]
            assert found["online_calls"] == 0 and len(found["items"]) == 1
            video_id = found["items"][0]["media_id"]
            response = await page.request.get(server.url + "/media/" + video_id, headers={"Range":"bytes=0-127"})
            with sample.open("rb") as source:
                assert response.status == 206 and await response.body() == source.read(128)
            assert response.headers["content-type"].split(";")[0] == "video/mp4"
            panel = page.get_by_test_id("media-projection")
            if not await panel.is_visible():
                await page.get_by_test_id("open-media").last.click()
            await panel.get_by_role("button", name="测试航行片段", exact=False).first.click()
            video = panel.locator(".projection-player > video")
            await video.wait_for()
            await page.wait_for_function("""()=>{const v=document.querySelector('#tool-player video');
                return v&&!v.paused&&v.currentTime>.12&&v.videoWidth>0&&v.readyState>=2}""")
            assert await video.evaluate("(v,id)=>v.currentSrc.endsWith('/media/'+id)",video_id)
            metrics = await video.evaluate("v=>({width:v.videoWidth,height:v.videoHeight,duration:v.duration})")
            async with asyncio.timeout(6):
                while not any((t.get("browser_playback") or {}).get("event") == "started"
                              and (t.get("browser_playback") or {}).get("media_id") == video_id
                              for t in (await server.state())["turns"]):
                    await asyncio.sleep(.05)
            await panel.get_by_role("button", name="暂停", exact=True).click()
            assert await video.evaluate("v=>v.paused")
            report["desktop"] = await photograph(page, folder, "video-desktop")
            assert all(box["hit"] for box in report["desktop"].values()), report["desktop"]
            await panel.get_by_role("slider", name="播放进度").press("End")
            await panel.get_by_role("slider", name="播放进度").press("Home")
            await page.wait_for_function("document.querySelector('#tool-player video').currentTime<.2")
            await panel.get_by_role("button", name="静音", exact=True).click()
            assert await video.evaluate("v=>v.muted")
            await panel.get_by_role("button", name="取消静音", exact=True).click()
            assert not await video.evaluate("v=>v.muted")
            await panel.get_by_role("button", name="全屏播放", exact=True).click()
            await page.wait_for_function("document.fullscreenElement?.contains(document.querySelector('#tool-player video'))")
            await panel.get_by_role("button", name="退出全屏", exact=True).click()
            await page.wait_for_function("!document.fullscreenElement")
            await video.click()
            await page.wait_for_function("!document.querySelector('#tool-player video').paused")
            await page.locator("#tools-close").click()
            await panel.wait_for(state="hidden")
            assert await page.evaluate("[...document.querySelectorAll('#tool-player video')].every(v=>v.paused)")
            await send(page, server, "我想听测试乐手的歌")
            audio = page.locator("audio.media-audio").last
            await page.wait_for_function("[...document.querySelectorAll('audio.media-audio')].some(a=>!a.paused&&a.currentTime>.1)")
            await send(page, server, "播放视频 测试航行片段")
            await page.wait_for_function("""()=>{const v=document.querySelector('#tool-player video');
                return v&&!v.paused&&v.currentTime>.12&&v.videoWidth>0}""")
            assert await audio.evaluate("a=>a.paused")
            previous_video = await panel.locator("video").element_handle()
            await send(page, server, "我想听测试乐手的歌")
            await page.wait_for_function("[...document.querySelectorAll('audio.media-audio')].some(a=>!a.paused&&a.currentTime>.1)")
            assert await previous_video.evaluate("v=>v.paused")
            intercepted = []
            async def intercept(route):
                intercepted.append(route.request.url)
                await route.abort()
            await page.route("https://**/*", intercept)
            watch = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
            await send(page, server, "打开视频 " + watch)
            official_choice = panel.get_by_role("button", name="www.bilibili.com 视频")
            await official_choice.wait_for()
            assert await panel.locator("iframe").count() == 0 and not intercepted
            await official_choice.click()
            await panel.locator("iframe.player-window").wait_for()
            await page.wait_for_function("document.querySelector('#tool-player iframe')?.title.includes('哔哩哔哩')")
            async with asyncio.timeout(3):
                while not intercepted:
                    await asyncio.sleep(.05)
            assert len(intercepted) == 1 and intercepted[0].startswith("https://player.bilibili.com/player.html?")
            assert not await page.evaluate("[...document.querySelectorAll('.media-audio')].some(e=>!e.paused)")
            await page.locator("#tools-close").click()
            await panel.locator("iframe").wait_for(state="detached")
            await send(page, server, "打开视频 https://example.org/watch/123")
            await panel.get_by_role("button", name="example.org 视频").click()
            assert await panel.locator("iframe").count() == 0
            assert await panel.get_by_role("link", name="打开片源").get_attribute("href") == "https://example.org/watch/123"
            assert len(intercepted) == 1
            external = await page.evaluate("""async()=>{
              const {mossLegacyVideo:player} = await import('/static/agent-video.js');
              const watch='https://www.bilibili.com/video/BV1xx411c7mD?p=2';
              const official=player.createExternal({url:watch,title:'样片'}).root.querySelector('iframe');
              const unknown=player.createExternal({url:'https://example.org/watch/123',title:'样片'}).root;
              return {supported:player.canEmbed(watch), official:official?.src, fullscreen:official?.allowFullscreen,
                fallback:unknown.querySelector('a')?.href, iframe:!!unknown.querySelector('iframe')};
            }""")
            assert external == {"supported":True, "official":"https://player.bilibili.com/player.html?bvid=BV1xx411c7mD&p=2&autoplay=1",
                                "fullscreen":True, "fallback":"https://example.org/watch/123", "iframe":False}
            await page.close()
            mobile = await browser.new_page(viewport={"width":375, "height":812}, is_mobile=True,
                                            has_touch=True, device_scale_factor=1, reduced_motion="reduce")
            await mobile.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            await mobile.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
            await mobile.locator('#moss-model[data-ready="true"]').wait_for(timeout=90000)
            await mobile.get_by_test_id("composer").fill("播放视频 测试航行片段")
            await mobile.get_by_test_id("send").tap()
            await mobile.wait_for_function("""()=>{const v=document.querySelector('#tool-player video');
                return v&&!v.paused&&v.currentTime>.1&&v.videoWidth>0}""")
            await mobile.wait_for_function("document.querySelector('main').dataset.mobileWorkspace === 'media'")
            assert not await mobile.locator("#dialogue").is_visible()
            report["mobile"] = await photograph(mobile, folder, "video-mobile")
            assert all(box["hit"] for box in report["mobile"].values()), report["mobile"]
            frame = report["mobile"]["video"]
            assert abs(frame["width"] / frame["height"] - 16 / 9) < .04, frame
            mobile_panel = mobile.get_by_test_id("media-projection")
            await mobile_panel.get_by_role("button", name="全屏播放", exact=True).tap()
            await mobile.wait_for_function("document.fullscreenElement?.contains(document.querySelector('#tool-player video'))")
            await mobile_panel.get_by_role("button", name="退出全屏", exact=True).tap()
            await mobile.wait_for_function("!document.fullscreenElement")
            await mobile_panel.get_by_role("button", name="播放", exact=True).tap()
            await mobile.wait_for_function("!document.querySelector('#tool-player video').paused")
            await mobile.locator("#tools-close").tap()
            await mobile_panel.wait_for(state="hidden")
            await mobile.wait_for_function("document.querySelector('#tool-player video').paused")
            await mobile.locator("#dialogue").wait_for()
            await mobile.locator("#workspace-media-return").tap()
            await mobile_panel.wait_for()
            assert await mobile_panel.locator("video").evaluate("v=>v.paused && v.currentTime > 1")
            report.update(passed=True, muted_browser=True, range_status=response.status, video=metrics,
                          music_to_video=True, video_to_music=True, click_only_iframe=intercepted, external=external)
            await browser.close()
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await server.stop()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(verify())
