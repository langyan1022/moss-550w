import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from statistics import median

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send


MEASURE = """
window.speechTest = {clips:[], events:[], requests:[], clock:null};
const originalFetch = window.fetch;
window.fetch = function(url, options) {
  if(url==='/api/speak') speechTest.requests.push({text:JSON.parse(options.body).text, ms:performance.now()});
  return originalFetch.apply(this, arguments);
};
const originalStart = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(when=0, offset=0, length) {
  const buffer=this.buffer, rate=buffer.sampleRate, pcm=buffer.getChannelData(0);
  const block=Math.ceil(rate*.01), threshold=.004;
  let first=null, last=null, quiet=0, longestInternal=0;
  for(let i=0; i<pcm.length; i+=block) {
    let sum=0, end=Math.min(i+block,pcm.length);
    for(let n=i;n<end;n++) sum+=pcm[n]*pcm[n];
    if(Math.sqrt(sum/(end-i))>threshold){
      if(first===null) first=i/rate; else longestInternal=Math.max(longestInternal,quiet);
      last=end/rate;quiet=0;
    } else if(first!==null) quiet+=(end-i)/rate;
  }
  const start=Math.max(when,this.context.currentTime);
  const span=length ?? buffer.duration-offset;
  speechTest.clips.push({start, end:start+span, first:first===null?null:start+first-offset,
    last:last===null?null:start+last-offset, max_internal_silence_ms:Math.round(1000*longestInternal),
    duration:span, scheduled:performance.now()});
  speechTest.clock=this.context;
  return originalStart.apply(this,arguments);
};
for(const type of ['start','end','stop','error','pending']) window.addEventListener('moss-speech-'+type,e=>{
  speechTest.events.push({type,...e.detail,ms:performance.now()});
});
"""
TEXT = "北京今天局部多云，清晨出门的人，记得加一件外套。下午想去公园走走吗？如果风大，就等明天再去吧。"


async def main(args):
    folder = ROOT / "runs" / datetime.now().strftime(f"speech-continuity-{args.label}-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    server = BrowserServer(folder, 18631, model_url="http://127.0.0.1:9/v1")
    report = {"passed":False, "label":args.label, "page_errors":[]}
    try:
        if args.port is None:
            await server.start()
        else:
            server.url = f"http://127.0.0.1:{args.port}"
            if args.port == 18221 or (await server.state())["turns"]:
                raise RuntimeError("复测需要空的隔离会话")
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script(MEASURE)
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                speech = []
                page.on("response", lambda response: speech.append(response) if response.url.endswith("/api/speak") else None)
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                await send(page, server, "记住：" + TEXT)
                await page.wait_for_function("speechTest.events.some(e=>e.type==='end'||e.type==='error')", timeout=210000)
                rows = await page.evaluate("({clips:speechTest.clips, events:speechTest.events, requests:speechTest.requests})")
                report.update(rows)
                assert rows["events"][-1]["type"] == "end" and not report["page_errors"], rows["events"]
                assert len(speech) == len(rows["clips"]) == len(rows["requests"]) >= 1
                for index, response in enumerate(speech, 1):
                    assert response.status == 200
                    (folder/f"line-{index}.wav").write_bytes(await response.body())
                clips = rows["clips"]
                gaps = [round(1000*(b["start"]-a["end"]),1) for a,b in zip(clips,clips[1:])]
                audible = [round(1000*(b["first"]-a["last"]),1) for a,b in zip(clips,clips[1:])]
                report.update(gaps_ms=gaps, median_gap_ms=median(gaps) if gaps else None, audible_gaps_ms=audible)
                if args.check:
                    assert len(clips) == 1 and clips[0]["max_internal_silence_ms"] < 1500, clips
                    await send(page, server, "记住：" + TEXT + "\n\n" + "天气晴朗的时候，树影会沿着河岸慢慢移动。")
                    await page.wait_for_function("speechTest.events.filter(e=>e.type==='end').length===2", timeout=150000)
                    long = await page.evaluate("({clips:speechTest.clips.slice(1), requests:speechTest.requests.slice(1)})")
                    assert len(long["clips"]) == len(long["requests"]) >= 2, long
                    assert all(req["ms"] < prior["scheduled"] + 1000*prior["duration"]
                               for req,prior in zip(long["requests"][1:],long["clips"])), long
                    report["long_run"] = long
                    await page.get_by_role("button",name="重播播报").click()
                    await page.wait_for_function("speechTest.clips.length>" + str(1+len(long["clips"])))
                    await page.locator("#interrupt").click()
                    n = await page.evaluate("speechTest.clips.length")
                    await page.wait_for_timeout(1800)
                    assert await page.evaluate("speechTest.clips.length") == n
                    assert not await page.evaluate("mossSpeech.active()")
                    report["interrupt_silent"] = True
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        if args.port is None:
            await server.stop()
        folder.mkdir(parents=True, exist_ok=True)
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report.get("gaps_ms"), report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=("before","after"), required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--port", type=int, help="连接已启动的本机隔离测试实例")
    asyncio.run(main(parser.parse_args()))
