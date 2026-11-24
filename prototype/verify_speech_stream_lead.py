import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from core import Moss
from serve import application
from verify_context_reply import free_port
from verify_speech_continuity import MEASURE


PASSAGES = (
    "桌上有一张折旧的地图。沿着蓝线往前走，穿过一座小桥，就能看见山坡。",
    "石墙的缝隙长着青苔。太阳照在窗边，屋里的人把门轻轻推开，递来一张手写的便条。",
    "便条请我们沿村口的石阶往上走，经过第一道木栅栏后向左转，再顺着竹林旁的小路慢慢走。",
    "天色渐晚，乌鸫掠过屋檐。我们在屋外等了一会儿，听见河水拍过石头，远处亮起一盏灯。",
    "返程时风停了，衣服上还沾着一点潮气。我们把地图沿原来的折痕收好，约好下次从另一条路出发。",
    "那天回家后，窗台上只有一杯温水。故事记到了这里，剩下的几页仍然留着，等下一次再写。",
)
TEXT = "\n\n".join(PASSAGES)
PROMPT = "逐字复述以下正文，保留每段之间的空行，只输出正文。\n\n" + TEXT

WATCH = """
window.speechLead = {submitted:0, partial:[], final:null};
document.addEventListener('submit', e => {
  if(e.target.id === 'composer') speechLead.submitted=performance.now();
}, true);
window.addEventListener('moss-agent-state', e => {
  const turn=e.detail.turns.at(-1);
  if (!turn || !speechLead.submitted || speechLead.final) return;
  if(turn.status === 'thinking' && turn.partial_reply)
    speechLead.partial.push({ms:performance.now(), length:turn.partial_reply.length});
  if(turn.status === 'replied') speechLead.final={ms:performance.now(), text:turn.reply, id:turn.id};
});
"""
WATCH_CANCEL = """
window.speechCancellation = {clicked:false, aborted:false};
const sendVoiceRequest = window.fetch;
window.fetch = function(url, options) {
  if (url === '/api/speak' && !speechCancellation.clicked) {
    options.signal.addEventListener('abort', () => {speechCancellation.aborted=true;}, {once:true});
    queueMicrotask(() => {
      const button = document.querySelector('#interrupt');
      if (speechLead.final || button.disabled) return;
      speechCancellation.clicked = true;
      button.click();
    });
  }
  return sendVoiceRequest.apply(this, arguments);
};
"""


async def run(args):
    label = "cancel" if args.cancel_before_final else args.expect
    folder = ROOT / "runs" / datetime.now().strftime(f"speech-stream-lead-{label}-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "cloud_requests": [], "page_errors": [], "expected": args.expect}
    data = folder / "data"
    moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
    await moss.select_model("deepseek")

    async def audit(request):
        document = json.loads(request.content)
        assert request.url.host == "api.deepseek.com" and request.url.scheme == "https"
        assert not report["cloud_requests"] and not document.get("tools"), "超出本次云模型测试范围"
        assert document["messages"][1:] == [{"role": "user", "content": PROMPT}]
        report["cloud_requests"].append({"model": document["model"], "tools": 0})

    moss.model_client()._client.event_hooks["request"].append(audit)
    port = free_port()
    runner = web.AppRunner(application(moss, lambda: "standby", data, port, "isolated", asyncio.Event()),
                           access_log=None, shutdown_timeout=1)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script(MEASURE + WATCH + (WATCH_CANCEL if args.cancel_before_final else ""))
                if args.script:
                    await page.route(re.compile(r"/static/agent-speech\.js(?:\?.*)?$"),
                                     lambda route: route.fulfill(path=str(args.script), content_type="text/javascript"))
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                audio = []
                page.on("response", lambda response: audio.append(response) if response.url.endswith("/api/speak") else None)
                await page.goto(f"http://127.0.0.1:{port}/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.get_by_test_id("composer").fill(PROMPT)
                if args.cancel_before_final:
                    async with page.expect_response(lambda response: response.url.endswith("/api/cancel")) as cancelled:
                        await page.get_by_test_id("send").click()
                    response = await cancelled.value
                    assert response.ok, await response.text()
                    await page.wait_for_function("speechCancellation.clicked && speechCancellation.aborted")
                    await page.wait_for_timeout(1000)
                    samples = await page.evaluate("""({cancelled:speechCancellation, speech:speechLead,
                                                       requests:speechTest.requests, events:speechTest.events})""")
                    state = json.loads(await (await page.request.get(f"http://127.0.0.1:{port}/api/state")).text())
                    assert state["turns"][-1]["status"] == "cancelled"
                    assert not samples["speech"]["final"] and not samples["events"] and not report["page_errors"]
                    assert len(samples["requests"]) == len(report["cloud_requests"]) == 1
                    report.update(samples=samples, cancel_before_final=True, passed=True)
                    return
                await page.get_by_test_id("send").click()
                await page.wait_for_function("speechTest.events.some(e=>e.type==='start'||e.type==='error')", timeout=120000)
                samples = await page.evaluate("({speech:speechLead, requests:speechTest.requests, events:speechTest.events, clips:speechTest.clips})")
                report["samples"] = samples
                first = samples["events"][1]
                lead = samples["speech"]
                assert first["type"] == "start" and lead["final"]["text"] == TEXT, samples["events"]
                assert lead["partial"] and not report["page_errors"] and len(report["cloud_requests"]) == 1
                assert samples["requests"][0]["text"] == first["text"] == PASSAGES[0]
                timings = {"first_text_ms": lead["partial"][0]["ms"] - lead["submitted"],
                           "final_text_ms": lead["final"]["ms"] - lead["submitted"],
                           "first_voice_request_ms": samples["requests"][0]["ms"] - lead["submitted"],
                           "starts_playing_ms": first["ms"] - lead["submitted"]}
                report["timings_ms"] = {key: round(value, 2) for key, value in timings.items()}
                lead_ms = round(timings["final_text_ms"] - timings["first_voice_request_ms"], 2)
                report["request_lead_over_final_ms"] = lead_ms
                assert lead_ms <= 10 if args.expect == "baseline" else lead_ms >= 100
                wave = await audio[0].body()
                report["first_wav_sha256"] = hashlib.sha256(wave).hexdigest()
                report["first_wav_engine"] = audio[0].headers.get("x-moss-voice")
                (folder / "first.wav").write_bytes(wave)
                assert samples["clips"][0]["first"] is not None
                await page.locator("#interrupt").click()
                await page.wait_for_function("!mossSpeech.active()")
                await page.wait_for_timeout(650)
                assert await page.evaluate("speechTest.events.filter(e=>e.type==='start').length") == 1
                assert not report["page_errors"]
                report["interrupt"] = True
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", required=True, choices=("baseline", "overlap"))
    parser.add_argument("--script", type=Path)
    parser.add_argument("--cancel-before-final", action="store_true")
    asyncio.run(run(parser.parse_args()))
