import asyncio
from datetime import datetime
import json

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_artist_request import OBSERVER


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("first-music-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18746)
    result = {}
    await server.start()
    try:
        assert (await server.post("/api/select-model", {"id": "deepseek"}))[0] == 200
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(viewport={"width":1440,"height":900},locale="zh-CN",timezone_id="Asia/Shanghai")
                await page.add_init_script(OBSERVER)
                await page.add_init_script("""window.firstMusic={};
                    document.addEventListener('input',e=>{if(e.target.id==='message')firstMusic.input=performance.now()},true);
                    document.addEventListener('pointerdown',e=>{if(e.target.closest('[data-testid=send]'))firstMusic.pointer=performance.now()},true);
                    document.addEventListener('submit',()=>firstMusic.submit=performance.now(),true);""")
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                await page.evaluate("""()=>{firstMusic.fillStart=performance.now();firstMusic.openAtFill=document.querySelector('main').dataset.opening||null}""")
                await page.get_by_test_id("composer").fill("我想听刘德华的歌")
                await page.get_by_test_id("send").click()
                await page.get_by_test_id("final").first.wait_for()
                await page.wait_for_function("attempts[0]?.music.length&&attempts[0]?.speech.length",timeout=60000)
                result = await page.evaluate("""()=>{
                    const s=firstMusic, a=attempts[0], net=performance.getEntriesByType('resource').find(x=>x.name.endsWith('/api/message'));
                    return {introAtFill:s.openAtFill,readyAt:Math.round(s.fillStart),
                      fillToInput:Math.round(s.input-s.fillStart),inputToClick:Math.round(s.pointer-s.input),
                      fillToSubmit:Math.round(s.submit-s.fillStart),
                      submitToHttp:Math.round(net.requestStart-s.submit),httpToFirstByte:Math.round(net.responseStart-net.requestStart),
                      submitToChoices:a.visible_ms,submitToReply:a.reply_ms,submitToSpeech:a.speech[0].ms,
                      submitToMusic:a.music[0].ms,fillToReply:Math.round(s.submit-s.fillStart+a.reply_ms)} }""")
                result["source"] = (await server.state())["turns"][0]["source"]
                assert result["source"] == "media"
                assert result["introAtFill"] is None
                await page.close()
                home = await browser.new_page(viewport={"width":1440,"height":900})
                await home.goto(server.url, wait_until="domcontentloaded")
                await home.wait_for_function("document.querySelector('#opening-film').currentTime > .05")
                await home.get_by_role("button", name="跳过开场").click()
                await home.locator("#history-toggle").click()
                await home.get_by_test_id("composer").wait_for()
                result["home_intro_and_entry"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),encoding="utf-8")
        print(folder, result)


if __name__ == "__main__":
    asyncio.run(main())
