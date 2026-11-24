import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from statistics import median

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from reply_length import ReplyLength, reply_length
from serve import application
from verify_context_reply import free_port


CASES = {
    "short": "同事临时替我换了班。用一句话自然地道个谢，不要加称呼。",
    "medium": "我有30分钟，想先散步再练琴。给我一个三步时间安排，每步写明几分钟。",
    "long": "用180到220字说明第一次整理书桌怎么做。按清空、分类、放回三个步骤写，再举一个收纳收据的例子。",
}
LENGTH_CASES = {
    "long": CASES["long"],
    "upper": "用不超过90字说明怎样给租来的自行车打气，要写到胎侧标注、检查气嘴和打气后复查。",
    "approximate": "用约100字介绍怎样安排两小时的周末阅读。包含准备、阅读和简短回顾，给出每部分时长。",
}
LENGTH_BOUNDS = {"long":(180,220), "upper":(1,90), "approximate":(85,115)}


OBSERVE = """
if (location.protocol==='http:') localStorage.setItem('moss.voice.enabled', 'off');
window.turnTiming = null;
document.addEventListener('click', event => {
  if (!event.target.closest('[data-testid=send]')) return;
  turnTiming={click:performance.now(), prior:document.querySelectorAll('[data-testid=turn]').length,
    user:document.querySelector('[data-testid=composer]').value.trim(), firstState:null, finalState:null,
    firstVisible:null, finalVisible:null, firstText:null, finalText:null, partials:new Set(), error:null};
}, true);
window.addEventListener('moss-agent-state', event => {
  const row = turnTiming;
  const latest = event.detail.turns.at(-1);
  if (!row || event.detail.turns.length!==row.prior+1 || latest?.user!==row.user) return;
  if (latest.status==='error') row.error=latest.error||'本轮失败';
  if ((latest.partial_reply||latest.reply)?.trim()) row.firstState??=performance.now();
  if (latest.status==='replied') row.finalState??=performance.now();
});
let scheduled=false;
function readVisible() {
  scheduled=false;
  const row=turnTiming;
  if (!row || row.finalVisible) return;
  const turns=document.querySelectorAll('[data-testid=turn]');
  if (turns.length!==row.prior+1) return;
  const reply=turns[row.prior].querySelector('[data-testid=partial-reply],[data-testid=final]');
  if(!reply || !reply.checkVisibility()) return;
  const rect=reply.getBoundingClientRect();
  const pane=document.querySelector('#conversation,#log');
  const view=pane?.getBoundingClientRect() || {top:0,bottom:innerHeight};
  if(rect.height<=0 || rect.width<=0 || rect.bottom<=Math.max(0,view.top) ||
     rect.top>=Math.min(innerHeight,view.bottom)) return;
  const text=reply.innerText.trim();
  if(!text) return;
  const tick=performance.now();
  if(row.firstVisible===null) {row.firstVisible=tick;row.firstText=text}
  if(reply.dataset.testid==='partial-reply') row.partials.add(text);
  else {row.finalVisible=tick;row.finalText=text}
}
function scheduleRead(){
  if(scheduled || !turnTiming || turnTiming.finalVisible) return;
  scheduled=true;
  requestAnimationFrame(readVisible);
}
new MutationObserver(scheduleRead).observe(document,{childList:true,subtree:true,characterData:true,
  attributes:true,attributeFilter:['data-testid']});
document.addEventListener('scroll', scheduleRead, true);
"""


async def measure(page, moss, mode, label, prompt, repetition, requests):
    before = len(requests)
    await page.get_by_test_id("composer").fill(prompt)
    await page.get_by_test_id("send").click()
    await page.wait_for_function("()=>Boolean(turnTiming?.finalVisible||turnTiming?.error)", timeout=60000)
    trace = await page.evaluate("()=>({...turnTiming,partials:turnTiming.partials.size})")
    assert not trace["error"], trace["error"]
    turn = moss.state()["turns"][-1]
    assert turn["user"] == prompt and turn["reply"].strip() == trace["finalText"], turn
    assert turn["model_id"] == "deepseek" and turn["status"] == "replied" and turn["source"] == "holo"
    assert turn["cloud_requests"]["total"] == len(requests)-before == 1
    assert not turn.get("memory_ids") and not turn.get("holo_tools")
    return {"page":mode,"case":label,"repetition":repetition,"previous_turns":trace["prior"],
            "first_visible_ms":round(trace["firstVisible"]-trace["click"],1),
            "final_visible_ms":round(trace["finalVisible"]-trace["click"],1),
            "first_state_ms": round(trace["firstState"]-trace["click"],1),
            "final_state_ms": round(trace["finalState"]-trace["click"],1),
            "first_render_ms":round(trace["firstVisible"]-trace["firstState"],1),
            "visible_partial_updates":trace["partials"],"first_text":trace["firstText"],"reply":trace["finalText"]}


async def opened(browser, url, report):
    context = await browser.new_context(viewport={"width":1360,"height":900}, reduced_motion="reduce")
    await context.add_init_script(OBSERVE)
    page = await context.new_page()
    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    page.on("request", lambda req: report["nonlocal_page_requests"].append(req.url)
            if req.url.startswith(("http:", "https:")) and not req.url.startswith(url+"/") else None)
    await page.goto(url+"/?chat=1", wait_until="domcontentloaded")
    await page.locator(".connection.online").wait_for()
    await page.get_by_test_id("composer").wait_for()
    return context, page


def check_length_parsing():
    cases = (
        ("用180到220字说明。", ReplyLength(180,220)),
        ("把这段话压缩到一百八十—二百二十字。", ReplyLength(180,220)),
        ("写一篇简介，正文字数：90～120 字。", ReplyLength(90,120)),
        ("用不超过90字写介绍。", ReplyLength(None,90)),
        ("写一封邮件，至少80字，全文不超过100字。", ReplyLength(80,100)),
        ("少于100字", ReplyLength(None,99)),
        ("把这段话改成正好90字。", ReplyLength(90,90)),
        ("写200字左右。", ReplyLength(200,200,True)),
        ("用约100字总结：“这篇示例写到600字左右。”", ReplyLength(100,100,True)),
        ("把下段话压缩到80字。原文：用300字另写。", ReplyLength(80,80,True)),
        ("写一段文字，每段不超过80字。", None),
        ("翻译：“用200字写一段简介。”", None),
        ("这篇文章有200字吗？", None),
        ("说明一下《用200字写简介》的书名。", None),
        ("我用了200字描述背景，帮我起个标题。", None),
        ("写一段说明，至少120字，不超过80字。", None),
    )
    for text, expected in cases:
        assert reply_length(text)==expected, (text,reply_length(text),expected)
    example = LENGTH_CASES["long"]
    assert "分 4 项" in reply_length(example).guidance(example)
    two_examples = "用180到220字介绍三个步骤，再举两个例子。"
    assert "分 5 项" in reply_length(two_examples).guidance(two_examples)
    return len(cases)


async def run(lengths, repetitions, only):
    cases = LENGTH_CASES if lengths else CASES
    if only:
        cases = {name:cases[name] for name in only}
    outbound_limit = 2*repetitions*len(cases)
    kind = "chat-page-length" if lengths else "chat-page-latency"
    folder = ROOT/"runs"/datetime.now().strftime(kind+"-%Y%m%d-%H%M%S-%f")
    data = folder/"data"
    data.mkdir(parents=True)
    print(folder, flush=True)
    os.environ.update(MOSS_WINDOWS_REMINDERS="0",MOSS_MEM0_ENABLED="0",MOSS_SPEECH_PREPARE="0",MOSS_KEV_ENABLED="0",
                      MOSS_MEDIA_DB=str(folder/"media.sqlite3"))
    outbound = []
    report = {"passed":False,"prompts":cases,"outbound_limit":outbound_limit,"cases":[],"outbound":outbound,
              "page_errors":[],"nonlocal_page_requests":[],
              "model_manifest_sha256":hashlib.sha256((ROOT/"model-release.json").read_bytes()).hexdigest(),
              "method":"静音 Edge 真实 550W 页面，语音关闭。点击发送到首个/完整文字进入对话可视区域的动画帧。"}
    if lengths:
        report["parsing_checks"] = check_length_parsing()
    moss = Moss(data,"http://127.0.0.1:9/v1","unused",prefer_cloud=False)
    await moss.select_model("deepseek")
    client = moss.model_client()._client
    active_prompt = None

    async def audit(request):
        assert len(outbound)<outbound_limit and active_prompt, "已达到本轮付费请求上限"
        assert request.method=="POST" and request.url.scheme=="https" and request.url.host=="api.deepseek.com"
        body = json.loads(request.content)
        messages = body["messages"]
        assert body.get("stream") is True and messages[-1] == {"role":"user","content":active_prompt}
        assert all(row["content"] in cases.values() or row["content"]=="你好。" for row in messages if row["role"]=="user")
        if lengths:
            assert reply_length(active_prompt).guidance(active_prompt) in messages[0]["content"]
        assert all(row["role"] in {"user","assistant","system"} for row in messages)
        outbound.append({"case":active_prompt,"model":body["model"],"messages":len(messages),
                         "tools":len(body.get("tools") or [])})

    client.event_hooks["request"].append(audit)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    report["model"] = moss.model_name
    runner = web.AppRunner(application(moss,lambda:"standby",data,port,"isolated",asyncio.Event()),access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner,"127.0.0.1",port).start()
        async with async_playwright() as pw, aiohttp.ClientSession() as http:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                context, page = await opened(browser,url,report)
                try:
                    await page.get_by_test_id("composer").fill("你好。")
                    await page.get_by_test_id("send").click()
                    await page.wait_for_function("()=>Boolean(turnTiming?.finalVisible)", timeout=20000)
                    assert not outbound and moss.state()["turns"][-1]["reply"] == "你好。"
                    if not any("/moss-render" in worker.url for worker in page.workers):
                        await page.wait_for_event("worker",predicate=lambda worker:"/moss-render" in worker.url,timeout=30000)
                finally:
                    await context.close()
                for repetition in range(1,repetitions+1):
                    labels = tuple(cases) if repetition==1 else tuple(reversed(cases))
                    for mode in (("cold","continuous") if repetition==1 else ("continuous","cold")):
                        for start in (labels if mode=="cold" else (None,)):
                            async with http.post(url+"/api/new-chat",json={}) as response:
                                assert response.status==200 and not (await response.json())["turns"]
                            context,page = await opened(browser,url,report)
                            try:
                                if mode=="continuous":
                                    await page.get_by_test_id("composer").fill("你好。")
                                    await page.get_by_test_id("send").click()
                                    await page.get_by_test_id("final").wait_for()
                                    assert moss.state()["turns"][-1]["cloud_requests"]["total"] == 0
                                for label in (labels if mode=="continuous" else (start,)):
                                    active_prompt = cases[label]
                                    row = await measure(page,moss,mode,label,active_prompt,repetition,outbound)
                                    if lengths:
                                        low, high = LENGTH_BOUNDS[label]
                                        count = sum(not c.isspace() for c in row["reply"])
                                        row.update(visible_characters=count,length_range=[low,high],length_ok=low<=count<=high)
                                    report["cases"].append(row)
                                    print(mode,label,row["first_visible_ms"],row["final_visible_ms"],flush=True)
                                    active_prompt = None
                                workers = [worker.url for worker in page.workers]
                                assert any("/moss-render" in worker for worker in workers), workers
                            finally:
                                await context.close()
            finally:
                await browser.close()
        assert len(outbound)==len(report["cases"])==outbound_limit
        assert not report["page_errors"] and not report["nonlocal_page_requests"]
        report["medians_ms"] = {
            mode:{column:median(row[column] for row in report["cases"] if row["page"]==mode)
                  for column in ("first_visible_ms","final_visible_ms","first_render_ms")}
            for mode in ("cold","continuous")
        }
        report["passed"] = True
    finally:
        await runner.cleanup()
        await moss.close()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder,report["passed"],flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reply-length", action="store_true")
    parser.add_argument("--repetitions", choices=(1,2), default=2, type=int)
    parser.add_argument("--parser-only", action="store_true")
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    if args.parser_only:
        print(check_length_parsing(), "个字数解析用例通过")
    else:
        known = LENGTH_CASES if args.reply_length else CASES
        if args.case and set(args.case)-known.keys():
            parser.error("当前测试组没有指定用例")
        asyncio.run(run(args.reply_length,args.repetitions,args.case))
