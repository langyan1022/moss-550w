import argparse
import asyncio
from datetime import datetime
import json
import re
import subprocess
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send
from verify_speech_continuity import MEASURE


async def run():
    directory = ROOT / "runs" / datetime.now().strftime("verse-chat-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(directory, 18635, model_url="http://127.0.0.1:9/v1")
    report = {"passed":False, "cases":[]}
    try:
        await server.start()
        assert (await server.post("/api/select-model", {"id":"deepseek"}))[0] == 200
        for question, lines in [
            ("请朗诵王之涣的《登鹳雀楼》。", ["白日依山尽","黄河入海流","欲穷千里目","更上一层楼"]),
            ("换一首七言的，读李白的《早发白帝城》。", ["朝辞白帝彩云间","千里江陵一日还","两岸猿声啼不住","轻舟已过万重山"]),
        ]:
            status, state = await server.post("/api/message", {"text":question})
            assert status == 200
            async with asyncio.timeout(65):
                while (turn := (await server.state())["turns"][-1])["status"] == "thinking":
                    await asyncio.sleep(.06)
            assert turn["id"] == state["turns"][-1]["id"]
            reply = turn["reply"] or ""
            row = {"user":question, "reply":reply, "source":turn.get("source"), "error":turn.get("error")}
            report["cases"].append(row)
            assert turn["status"] == "replied" and all(line in reply for line in lines), row
            script = """import fs from 'node:fs'; import {speechDelivery,speechSegments} from './static/speech-delivery.js';
                const turn=JSON.parse(fs.readFileSync(0,'utf8')), voice=speechDelivery(turn);
                console.log(JSON.stringify({category:voice.category, segments:speechSegments(voice.text,voice.category)}));"""
            cmd = subprocess.run(["node","--input-type=module","-e",script], cwd=ROOT, text=True, encoding="utf-8",
                                 input=json.dumps(turn,ensure_ascii=False), capture_output=True, check=True)
            row.update(json.loads(cmd.stdout))
            assert row["category"] == "verse", row
            recited = "".join(part["spoken"] for part in row["segments"])
            assert all(re.sub(r"\s+", "", recited).count(line) == 1 for line in lines)
        report["passed"] = True
    finally:
        await server.stop()
        directory.mkdir(exist_ok=True)
        (directory/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(directory,report["passed"])


async def verify_ci_dialogue(excerpt):
    directory = ROOT / "runs" / datetime.now().strftime("verse-ci-dialogue-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(directory, 18639, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "cases": [], "page_errors": []}
    if excerpt:
        opening = "请朗诵辛弃疾《破阵子·为陈同甫赋壮词以寄之》的前两句，只读这两句。"
        lines = ["醉里挑灯看剑", "梦回吹角连营"]
    else:
        opening = "念首李清照的《如梦令·昨夜雨疏风骤》。"
        lines = ["昨夜雨疏风骤", "浓睡不消残酒", "试问卷帘人", "却道海棠依旧", "知否", "知否", "应是绿肥红瘦"]
    try:
        await server.start()
        assert (await server.post("/api/select-model", {"id": "deepseek"}))[0] == 200
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                await page.add_init_script(MEASURE)
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                for prompt, category in ((opening, "verse"),
                                         ("再轻柔一点，念一遍。", "verse-warm")):
                    sent = perf_counter()
                    reply = await send(page, server, prompt)
                    row = {"user": prompt, "reply": reply, "category": category,
                           "submit_to_visible_text_ms": round(1000 * (perf_counter() - sent))}
                    report["cases"].append(row)
                    normalized = re.sub(r"[^\u3400-\u9fff]", "", reply)
                    assert "".join(lines) in normalized, row
                    if excerpt:
                        assert normalized == "".join(lines), "模型多读了诗词节选以外的内容"
                    if category == "verse-warm":
                        assert reply == report["cases"][0]["reply"], "重读改变了原来的正文"
                    await page.wait_for_function(
                        "n => speechTest.events.filter(e=>e.type==='end'||e.type==='error').length>=n",
                        arg=len(report["cases"]), timeout=180000)
                    events = await page.evaluate("speechTest.events")
                    turn = (await server.state())["turns"][-1]
                    own = [e for e in events if e.get("id") == turn["id"]]
                    starts = [e for e in own if e["type"] == "start"]
                    assert own[-1]["type"] == "end" and starts, own
                    assert all(e["category"] == category for e in starts), starts
                    spoken = re.sub(r"[^\u3400-\u9fff]", "", "".join(e["text"] for e in starts))
                    assert "".join(lines) in spoken, spoken
                    row.update(clips=[{"caption":e["text"], "seconds":e["seconds"]} for e in starts],
                               source=turn.get("source"), speech_pending_to_audio_ms=round(starts[0]["ms"]-own[0]["ms"]))
                assert not report["page_errors"]
                report["passed"] = True
            finally:
                await browser.close()
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        await server.stop()
        directory.mkdir(exist_ok=True)
        (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(directory, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ci-dialogue", action="store_true")
    mode.add_argument("--excerpt-dialogue", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify_ci_dialogue(args.excerpt_dialogue) if args.ci_dialogue or args.excerpt_dialogue else run())
