import asyncio
import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from serve import application
from verify_context_reply import free_port


async def history_check(folder):
    report = json.loads((folder/"result.json").read_text(encoding="utf-8"))
    assert report["passed"]
    prompt = "用一句话解释纸为什么可以折出痕迹。"
    observed = []
    async def stop_before_network(request):
        body = json.loads(request.content)
        observed.append({"messages":[m for m in body["messages"] if m["role"]!="system"],"tools":body.get("tools",[])})
        raise ValueError("已在实际模型请求出站之前核对历史。")
    with TemporaryDirectory(dir=folder) as temporary:
        root = Path(temporary)
        with closing(sqlite3.connect((folder/"data/session.sqlite3").as_uri()+"?mode=ro",uri=True)) as source, closing(
                sqlite3.connect(root/"session.sqlite3")) as destination:
            source.backup(destination)
        moss = Moss(root, "http://127.0.0.1:9/v1","unused")
        try:
            await moss.select_model("deepseek")
            moss.model_client()._client.event_hooks["request"].append(stop_before_network)
            old = moss._get(report["cases"][-1]["turn_id"])
            await moss.switch_chat(old["chat_id"])
            await moss.message(prompt,old["chat_id"])
            await moss.job
            assert len(observed)==1 and observed[0]["messages"]==[{"role":"user","content":prompt}], observed
            quoted = "保存草稿，内容是虚构的测试编号。"
            question = "说明这句话请人做什么。"
            await moss.message(question,old["chat_id"],window_context={
                "turn_id":old["window_context"]["turn_id"],"model_id":"deepseek","excerpt":quoted})
            await moss.job
            assert len(observed)==2 and len(observed[1]["messages"])==1 and not observed[1]["tools"]
            assert json.loads(observed[1]["messages"][0]["content"])=={"question":question,"excerpt":quoted}
            assert not list((root/"drafts").iterdir())
        finally:
            await moss.close()
    (folder/"next-turn-history.json").write_text(json.dumps({
        "passed":True,"provider_request_blocked_before_send":True,"non_system_messages":observed[0]["messages"],
        "quoted_save_command":{"exposed_tools":0,"files_created":0}
    }, ensure_ascii=False,indent=2),encoding="utf-8")


async def run():
    folder = ROOT/"runs"/datetime.now().strftime("window-question-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    port = free_port()
    server = BrowserServer(folder,port)
    os.environ["MOSS_KEV_ENABLED"] = "0"
    moss = Moss(folder/"data","http://127.0.0.1:9/v1","unused")
    await moss.select_model("deepseek")
    calls, allowed = [], []
    report = {"passed":False,"cloud_requests":calls,"cases":[]}

    async def audit(request):
        if not allowed or len(calls)>=2 or request.url.host!="api.deepseek.com" or request.url.scheme!="https":
            raise ValueError("隔离检验阻止了未经核对的模型请求。")
        body = json.loads(request.content)
        assert [m["role"] for m in body["messages"]]==["system","user"] and not body.get("tools"), body.keys()
        payload = json.loads(body["messages"][1]["content"])
        assert payload==allowed.pop(0) and "OMIT" not in str(body) and "另外的草稿" not in str(body), payload
        calls.append({"provider":request.url.host,"payload":payload,"tools":0,"history":0})

    moss.model_client()._client.event_hooks["request"].append(audit)
    app = application(moss, lambda:"standby", folder/"data", port, "isolated", asyncio.Event())
    async def fixture(_):
        return web.Response(text="""<!doctype html><title>MOSS OCR QA 6127</title>
<script>navigator.mediaDevices.setCaptureHandleConfig({handle:'qa-river-6127',
exposeOrigin:true,permittedOrigins:[location.origin]});</script><body style="background:white;
font:48px Arial;color:black;padding:30px;"><p>RIVER: Friday 14:30 in Room 27.</p>
<p>OMIT 99342</p></body>""", content_type="text/html")
    app.router.add_get("/qa-selected-window", fixture)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    await web.TCPSite(runner,"127.0.0.1",port).start()
    try:
        async with async_playwright() as pw:
            exe = Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
            browser = await pw.chromium.launch(executable_path=str(exe),headless=True,args=[
                "--mute-audio","--disable-gpu","--disable-background-timer-throttling","--disable-renderer-backgrounding",
                "--auto-select-tab-capture-source-by-title=MOSS OCR QA 6127","--enable-usermedia-screen-capturing"])
            try:
                await confirm_muted_edge(browser)
                context = await browser.new_context(reduced_motion="reduce")
                sheet = await context.new_page()
                await sheet.goto(server.url+"/qa-selected-window")
                page = await context.new_page()
                await page.add_init_script("""localStorage.setItem('moss.voice.enabled','off');
window.displayQA=[]; const capture=navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getDisplayMedia=async options=>{
  const stream=await capture(options);
  const track=stream.getVideoTracks()[0];
  displayQA.push({track,target:track.getCaptureHandle?.()?.handle,surface:track.getSettings().displaySurface??null});
  return stream;
};""")
                await page.goto(server.url+"/?chat=1&scene=off",wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for()
                composer = page.get_by_test_id("composer")
                await composer.fill("识别当前窗口的文字")
                await page.get_by_test_id("send").click()
                row = page.get_by_test_id("turn").first
                await row.get_by_test_id("context-action").click()
                output = row.get_by_test_id("context-output")
                await output.wait_for(timeout=18000)
                raw = await output.inner_text()
                assert "OMIT" in raw and "RIVER" in raw and "27" in raw, raw
                report["capture"] = await page.evaluate("""()=>displayQA.map(({track,target,surface})=>({
target,surface,state:track.readyState}))""")
                assert len(report["capture"])==1 and report["capture"][0]["surface"]=="browser" and report["capture"][0]["state"]=="ended",report
                report["ocr_verification"] = raw
                origin = (await server.state())["turns"][-1]
                await composer.fill("另外的草稿")
                def select_source():
                    return output.evaluate("""el=>{
const text=el.firstChild, start=text.data.indexOf('RIVER'), end=text.data.indexOf('OMIT');
const r=new Range();r.setStart(text,start);r.setEnd(text,end);
const selection=getSelection();selection.removeAllRanges();selection.addRange(r);
return selection.toString().trim();
}""")
                for label, submit_label in (("就这段文字提问","提交问题"),("总结这段文字","提交摘要")):
                    excerpt = await select_source()
                    assert "27" in excerpt and "OMIT" not in excerpt
                    await row.get_by_role("button",name=label).click()
                    editor = row.get_by_role("textbox",name="交给模型的窗口文字")
                    assert await editor.input_value()==excerpt, await editor.input_value()
                    question = row.get_by_role("textbox",name="针对窗口文字的问题")
                    if submit_label=="提交问题":
                        await row.get_by_role("button",name="取消",exact=True).click()
                        assert not await editor.count() and not calls
                        await select_source()
                        await row.get_by_role("button",name=label).click()
                        await question.fill("会议在哪个房间，几点开始？")
                    assert "deepseek" in (await editor.locator("..").inner_text()).lower()
                    prompt = await question.input_value()
                    payload = {"text":prompt,"chat_id":origin["chat_id"],"window_context":{
                        "turn_id":origin["id"],"model_id":"local","excerpt":excerpt}}
                    assert (await server.post("/api/message",payload))[0]==409
                    before = len(calls)
                    assert len((await server.state())["turns"])==before+1
                    allowed.append({"excerpt":excerpt,"question":prompt})
                    await row.get_by_role("button",name=submit_label).click()
                    async with asyncio.timeout(65):
                        while True:
                            turns = (await server.state())["turns"]
                            last = turns[-1]
                            if len(turns)==before+2 and last["status"]!="thinking":
                                break
                            await asyncio.sleep(.08)
                    assert last["source"]=="window_question" and last["status"]=="replied" and len(calls)==before+1,last
                    assert "27" in last["reply"] and re.search(r"14\s*[:：点时]\s*30|下午[两二2]点半|下午[两二2]点[三3]十?",last["reply"]),last["reply"]
                    assert last["window_context"]=={"action":"screen_text","characters":len(excerpt),"turn_id":origin["id"]}
                    assert await composer.input_value()=="另外的草稿" and not last.get("receipt")
                    report["cases"].append({"question":prompt,"reply":last["reply"],"turn_id":last["id"]})
                status,new = await server.post("/api/new-chat",{})
                assert status==200
                payload.update(chat_id=new["chat_id"])
                payload["window_context"]["model_id"]="deepseek"
                assert (await server.post("/api/message",payload))[0]==409 and len(calls)==2
                report.update(passed=True,pre_submit_outbound=0,model_change_rejected=True,cross_chat_rejected=True)
            finally:
                if not report["passed"] and "page" in locals():
                    report["page_status"] = await page.locator(".context-widget").all_inner_texts()
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report["passed"],flush=True)


if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-history",type=Path)
    args = parser.parse_args()
    asyncio.run(history_check(args.check_history.resolve()) if args.check_history else run())
