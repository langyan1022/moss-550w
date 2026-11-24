import asyncio
from datetime import datetime
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from verify_memory import send


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("text-speech-%Y%m%d-%H%M%S")
    server = BrowserServer(folder,18357,model_url="http://127.0.0.1:18278/v1")
    await server.start()
    report = {"passed":False,"speech_requests":[],"responses":[],"page_errors":[]}
    try:
        async with async_playwright() as p:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            browser = await p.chromium.launch(executable_path=str(edge),headless=True,
                                             args=["--mute-audio","--autoplay-policy=document-user-activation-required"])
            await confirm_muted_edge()
            page = await browser.new_page(viewport={"width":1440,"height":900},reduced_motion="reduce")
            page.set_default_timeout(180000)
            page.on("pageerror", lambda e:report["page_errors"].append(str(e)))
            page.on("request", lambda r:report["speech_requests"].append(r.post_data_json["text"]) if r.url.endswith("/api/speak") else None)
            page.on("response", lambda r:report["responses"].append(r.status) if r.url.endswith("/api/speak") else None)
            await page.add_init_script("""window.speechEvents=[];
              for(const ev of ['moss-speech-start','moss-speech-end'])
                window.addEventListener(ev, e=>speechEvents.push({type:ev,t:performance.now(),...e.detail}));""")
            await page.goto(server.url + "?chat=1")
            prompt = "列出媒体库"
            first = await send(page,server,prompt)
            first_id = (await server.state())["turns"][-1]["id"]
            await page.wait_for_function("""id=>speechEvents.some(e=>e.type==='moss-speech-start' && e.id===id) &&
                     speechEvents.some(e=>e.type==='moss-speech-end' && e.id===id)""",arg=first_id)
            before = await page.evaluate("speechEvents")
            assert before[0]["seconds"] > 0 and before[-1]["t"] > before[0]["t"]
            assert report["speech_requests"] and all(s==200 for s in report["responses"])
            n = len(report["speech_requests"])
            await page.locator(".preferences summary").click()
            check = page.get_by_role("checkbox",name="MOSS 语音播报")
            assert await check.is_checked()
            await check.uncheck()
            await page.locator(".preferences summary").click()
            second = await send(page,server,prompt)
            second_id = (await server.state())["turns"][-1]["id"]
            assert first == second and second_id != first_id
            await page.wait_for_timeout(2500)
            assert before == await page.evaluate("speechEvents") and len(report["speech_requests"]) == n
            await page.reload()
            await page.locator(".preferences summary").click()
            assert not await check.is_checked()
            report.update(passed=not report["page_errors"],edge=browser.version,prompt=prompt,reply=first,
                          spoken_turn=first_id,events=before,manual_speech_off_turn=second_id,
                          muted_synthesis_requests=len(report["speech_requests"])-n,setting_restored_after_reload=True)
            await browser.close()
        assert report["passed"]
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder)


if __name__=="__main__":
    asyncio.run(run())
