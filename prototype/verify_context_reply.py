import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
import re
import socket
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from testkit import BrowserServer, ROOT, confirm_muted_edge, open_browser, safe_free_port as free_port
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS
from serve import application


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("context-reply-%Y%m%d-%H%M%S-%f")
    calls = []
    posted = []
    chosen = "演练编号 6342，我的意思是下周再说。"
    external = False
    async def audit(request):
        assert request.url.host == "api.deepseek.com" and request.url.scheme == "https"
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["chat"]["messages"] == [{"from":"her","text":chosen}] and not payload["goal"]
        assert b"PRIVATE" not in request.content and "这是另外一份未发送草稿" not in request.content.decode("utf-8")
        calls.append(payload)
        if not external or len(posted) >= 2:
            raise ValueError("隔离回归阻止了这次出站")
        posted.append(True)

    os.environ["MOSS_KEV_ENABLED"] = "0"
    port = free_port()
    server = BrowserServer(folder, port)
    moss = Moss(folder / "data", "http://127.0.0.1:9/v1", "unused")
    await moss.select_model("deepseek")
    moss.model_client()._client.event_hooks["request"].append(audit)
    app = application(moss, lambda:"standby", folder/"data", port, "isolated", asyncio.Event())
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    report = {"passed":False, "cases":[], "method":"隔离静音 Edge，测试窗口、真实 Windows OCR。仅 OCR 编辑后的虚构来信可提交到真实 DeepSeek，一次起草、一次核对。剪贴板验证在真实 HTTP 请求出站前阻断。"}
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script("""localStorage.setItem('moss.voice.enabled','off');
                  Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:async()=>{
                    const c=document.createElement('canvas'); c.width=880;c.height=170;
                    const x=c.getContext('2d');
                    function draw(){x.fillStyle='white';x.fillRect(0,0,c.width,c.height);
                        x.fillStyle='black';x.font='70px Arial';x.fillText('PRIVATE MOSS 6342',26,110);}
                    draw(); const stream=c.captureStream(10);setTimeout(draw,70);setTimeout(draw,170);return stream;
                  }});
                  Object.defineProperty(navigator.clipboard,'readText',{value:async()=>{
                    throw new DOMException('','NotAllowedError');
                  }});""")
                await page.goto(server.url + "/?chat=1&scene=off")
                await page.get_by_test_id("composer").wait_for()
                async def open_tool(question):
                    await page.get_by_test_id("composer").fill(question)
                    await page.get_by_test_id("send").click()
                    row = page.get_by_test_id("turn").last
                    await row.get_by_test_id("context-action").click()
                    return row

                for action, question in [("screen_text","识别当前窗口的文字"), ("clipboard","读取剪贴板")]:
                    before = len(calls)
                    row = await open_tool(question)
                    if action == "screen_text":
                        await row.get_by_test_id("context-output").wait_for(timeout=45000)
                        raw = await row.get_by_test_id("context-output").inner_text()
                        assert "PRIVATE" in raw and "6342" in raw, raw
                    else:
                        raw = "PRIVATE 剪贴板只在页面显示；我的意思是请处理这份来信。"
                        await row.get_by_role("textbox",name="粘贴剪贴板文字").fill(raw)
                    await page.get_by_test_id("composer").fill("这是另外一份未发送草稿")
                    await row.get_by_role("button",name="用这段文字拟回复").click()
                    editor = row.get_by_role("textbox", name="对方来信")
                    assert await editor.input_value() == raw
                    await row.get_by_role("button",name="取消拟回复").click()
                    assert not await editor.count()
                    await row.get_by_role("button",name="用这段文字拟回复").click()
                    await editor.fill(chosen)
                    state = await server.state()
                    origin = state["turns"][-1]
                    assert len(calls) == before and "PRIVATE" not in json.dumps(state)
                    assert origin["context_action"] == action
                    external = action == "screen_text"
                    paid = len(posted)
                    await row.get_by_role("button",name="生成回复").click()
                    async with asyncio.timeout(70):
                        while True:
                            state = await server.state()
                            if len(state["turns"]) > 0 and state["turns"][-1]["user"].startswith("帮我回复") and state["turns"][-1]["status"] != "thinking":
                                break
                            await asyncio.sleep(.05)
                    turn = state["turns"][-1]
                    assert turn["source"] == "jev" and turn["reply_context"] == {"turn_id":origin["id"],"action":action}
                    if external:
                        assert turn["status"] == "replied" and turn["jev"]["candidate_count"] >= 1 and turn["reply"]
                        await page.get_by_test_id("copy-reply").wait_for()
                        assert len(posted)-paid == 2 and len(calls)-before == 2
                    else:
                        assert turn["status"] == "error" and len(calls)-before == 1 and len(posted) == paid
                    assert await page.get_by_test_id("composer").input_value() == "这是另外一份未发送草稿"
                    assert "PRIVATE" not in json.dumps(calls, ensure_ascii=False)
                    report["cases"].append({"source":action, "origin_id":origin["id"], "read_in_browser":raw,
                        "model_calls_before_submit":0, "provider_attempts_after_submit":len(calls)-before,
                        "external_requests":len(posted)-paid, "model":moss.model_name,
                        "composer_preserved":True, "unselected_text_sent":False, "status":turn["status"], "reply":turn["reply"]})
                old_chat = state["chat_id"]
                status, new = await server.post("/api/new-chat", {})
                assert status == 200 and new["chat_id"] != old_chat
                status, error = await server.post("/api/message",{"text":"帮我回复：“收到。”","chat_id":new["chat_id"],
                                                                  "reply_context_turn":origin["id"]})
                assert status == 409 and not (await server.state())["turns"]
                report["cross_chat_rejected"] = error
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        await moss.close()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report["passed"], flush=True)


async def verify_goal():
    folder = ROOT / "runs" / datetime.now().strftime("context-reply-goal-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    server = BrowserServer(folder, free_port())
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    model_url = server.url + "/v1"
    fixtures = {
        "screen_text": ("识别当前窗口的文字", "同事：周四方便开会吗？\n同事：改期请说清“几点开始”。",
                        "语气委婉，明确说“周四不行”。\n改到周五下午三点。", "周四我不方便，改到周五下午三点可以吗？"),
        "clipboard": ("读取剪贴板", "朋友：我会晚到十分钟。\n朋友：先到的话替我点“咖啡”。",
                      "简短一点，只说我会在东门等。", "我会在东门等你"),
    }
    unseen = "UNSELECTED PRIVATE 84512"
    unrelated_draft = "这是另外一份未发送草稿"
    model_calls, requests, outside, page_errors = [], [], [], []
    replay_responses = []
    report = {"passed": False, "cases": [], "model_calls": model_calls, "requests": requests,
              "outside_requests": outside, "page_errors": page_errors, "viewport": [375, 812]}

    async def model_probe(request):
        body = await request.json()
        assert body["model"] == "fixture-deepseek" and request.headers["Authorization"] == "Bearer fixture-only"
        payload = json.loads(body["messages"][-1]["content"])
        model_calls.append(payload)
        incoming = payload["chat"]["messages"][0]["text"]
        candidate = next(item[3] for item in fixtures.values() if item[1] == incoming)
        result = ({"valid": {key: True for key in payload["choices"]}} if "choices" in payload
                  else {"candidates": [candidate]})
        return web.json_response({"id":"local-fixture", "object":"chat.completion", "created":0,
                                  "model":body["model"], "choices":[{"index":0, "finish_reason":"stop", "message":{
                                      "role":"assistant", "content":json.dumps(result, ensure_ascii=False)}}]})

    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0", "MOSS_KEV_ENABLED": "0"}
    ocr_text = fixtures["screen_text"][1] + "\n" + unseen
    profiles = {key: Profile(key, label, model_url, "fixture-deepseek" if key == "deepseek" else model,
                             SecretStr("fixture-only" if key == "deepseek" else ""))
                for key, (label, _, __, model) in SPECS.items()}
    try:
        with patch.dict(os.environ, environment), patch("core.profiles", return_value=profiles):
            moss = Moss(server.data, model_url, "fixture-local", prefer_cloud=True)
            app = application(moss, lambda: "standby", server.data, server.port, "fixture", asyncio.Event())
            app.router.add_post("/v1/chat/completions", model_probe)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", server.port).start()
                async with async_playwright() as playwright:
                    browser = await open_browser(playwright)
                    try:
                        await confirm_muted_edge(browser)
                        page = await browser.new_page(viewport={"width":375,"height":812}, reduced_motion="reduce")
                        page.on("pageerror", lambda error: page_errors.append(str(error)))

                        async def browser_fixture(route):
                            target = urlsplit(route.request.url)
                            if target.scheme in {"http", "https"} and not route.request.url.startswith(server.url + "/"):
                                outside.append(route.request.url)
                                await route.abort()
                            elif target.path == "/api/context/ocr":
                                assert route.request.post_data_buffer.startswith(b"\x89PNG\r\n\x1a\n")
                                await route.fulfill(status=200, json={"text":ocr_text})
                            elif target.path == "/api/message" and route.request.post_data_json.get("reply_context_turn"):
                                requests.append(route.request.post_data_json)
                                if len(requests) == 1:
                                    await route.fulfill(status=503, json={"error":"隔离测试提交失败"})
                                elif len(requests) in {2, 3}:
                                    response = await route.fetch()
                                    assert response.status == 200, response.status
                                    snapshot = await response.json()
                                    replay_responses.append(snapshot["turns"][-1]["id"])
                                    if len(requests) == 2:
                                        await route.fulfill(status=502, json={"error":"隔离测试已接受后响应丢失"})
                                    else:
                                        await route.fulfill(response=response)
                                else:
                                    await route.continue_()
                            else:
                                await route.continue_()

                        await page.route("**/*", browser_fixture)
                        await page.add_init_script("""localStorage.setItem('moss.voice.enabled','off');
                          Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:async()=>{
                            const canvas=document.createElement('canvas');canvas.width=640;canvas.height=180;
                            const ctx=canvas.getContext('2d');ctx.fillRect(0,0,640,180);
                            const stream=canvas.captureStream(15);
                            setTimeout(()=>ctx.fillRect(0,0,1,1),60);
                            return stream;
                          }});
                          Object.defineProperty(navigator.clipboard,'readText',{value:async()=>{
                            throw new DOMException('','NotAllowedError');
                          }});""")
                        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                        await page.locator(".connection.online").wait_for(timeout=15000)
                        composer = page.get_by_test_id("composer")
                        for action, (question, incoming, goal, candidate) in fixtures.items():
                            start = len(model_calls)
                            await composer.fill(question)
                            await page.get_by_test_id("send").click()
                            row = page.get_by_test_id("turn").last
                            await row.get_by_test_id("context-action").click()
                            origin = (await server.state())["turns"][-1]
                            assert origin["context_action"] == action
                            row = page.locator(f'[data-turn-id="{origin["id"]}"]')
                            if action == "screen_text":
                                output = row.get_by_test_id("context-output")
                                await expect(output).to_have_text(ocr_text)
                                await output.evaluate("""(node,length)=>{
                                  const range=new Range();range.setStart(node.firstChild,0);range.setEnd(node.firstChild,length);
                                  const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
                                }""", len(incoming))
                            else:
                                clipboard = row.get_by_role("textbox", name="粘贴剪贴板文字")
                                await clipboard.fill(incoming + "\n" + unseen)
                                await clipboard.evaluate("(node,length)=>node.setSelectionRange(0,length)", len(incoming))
                            await row.get_by_role("button", name="用这段文字拟回复").click()
                            excerpt = row.get_by_role("textbox", name="对方来信")
                            requirements = row.get_by_role("textbox", name="回复要求（可选）")
                            submit = row.get_by_role("button", name="生成回复")
                            await expect(excerpt).to_have_value(incoming)
                            await expect(requirements).to_have_value("")
                            await composer.fill(unrelated_draft)
                            if action == "screen_text":
                                await requirements.fill("a" * 501)
                                await expect(submit).to_be_disabled()
                                await requirements.fill("🙂" * 376)
                                await expect(submit).to_be_disabled()
                                await requirements.fill("中" * 500)
                                await expect(submit).to_be_enabled()
                            await requirements.fill(goal)
                            expected = {"text": f"帮我回复：“{incoming}”", "reply_context_turn":origin["id"],
                                        "reply_context_goal":goal}
                            assert len(model_calls) == start
                            if action == "screen_text":
                                await submit.scroll_into_view_if_needed()
                                boxes = [await control.bounding_box() for control in (excerpt, requirements, submit)]
                                assert all(box and box["x"] >= 0 and box["x"] + box["width"] <= 376 for box in boxes), boxes
                                assert boxes[0]["y"] < boxes[1]["y"] < boxes[2]["y"], boxes
                                screenshot = folder / "goal-editor-375x812.png"
                                await row.locator(".context-widget").screenshot(path=screenshot, animations="disabled")
                                report.update(editor_geometry=boxes, editor_screenshot=str(screenshot))
                                initial_excerpt = incoming + "\n这行仅用于模拟提交前失败。"
                                await excerpt.fill(initial_excerpt)
                                await submit.click()
                                await expect(page.locator("#error")).to_contain_text("隔离测试提交失败")
                                await expect(submit).to_be_enabled()
                                await expect(excerpt).to_have_value(initial_excerpt)
                                await expect(requirements).to_have_value(goal)
                                assert len(model_calls) == start
                                assert {key:requests[-1][key] for key in expected} == {
                                    **expected, "text": f"帮我回复：“{initial_excerpt}”"}
                                report["submit_failure_preserved_both_fields"] = True

                                await excerpt.fill(incoming)
                                await submit.click()
                                await expect(page.locator("#error")).to_contain_text("隔离测试已接受后响应丢失")
                                await expect(submit).to_be_enabled()
                                await expect(excerpt).to_have_value(incoming)
                                await expect(requirements).to_have_value(goal)
                                accepted_id, = replay_responses
                                accepted = page.locator(f'[data-turn-id="{accepted_id}"]')
                                await accepted.get_by_test_id("copy-reply").wait_for(timeout=10000)
                                assert len(model_calls) == start + 2
                            await submit.click()
                            await expect(excerpt).to_have_count(0)
                            result = page.get_by_test_id("turn").last
                            await result.get_by_test_id("copy-reply").wait_for(timeout=10000)
                            turns = (await server.state())["turns"]
                            turn = turns[-1]
                            assert turn["status"] == "replied" and turn["source"] == "jev" and turn["reply"] == candidate, turn
                            assert turn["reply_context"] == {"turn_id":origin["id"], "action":action, "goal":goal}, turn
                            assert {key:requests[-1][key] for key in expected} == expected
                            if action == "screen_text":
                                initial, accepted_key, retry = (packet["reply_context_request_id"] for packet in requests)
                                assert all(re.fullmatch(r"[0-9a-f]{32}", key) for key in (initial, accepted_key, retry))
                                assert initial != accepted_key == retry, requests
                                assert {key:requests[1][key] for key in expected} == expected
                                source_turns = [item["id"] for item in turns
                                                if (item.get("reply_context") or {}).get("turn_id") == origin["id"]]
                                assert replay_responses == [accepted_id, accepted_id] and source_turns == [accepted_id]
                                report["retry_after_lost_response"] = {"accepted_turn_id":accepted_id,
                                    "same_turn_returned":True, "changed_excerpt_rotated_request_id":True,
                                    "both_fields_preserved":True, "provider_calls":len(model_calls)-start,
                                    "source_turns":len(source_turns)}
                            draft, check = model_calls[start:]
                            assert "choices" not in draft and "choices" in check
                            for payload in (draft, check):
                                assert payload["chat"]["messages"] == [{"from":"her","text":incoming}], payload
                                assert payload["goal"] == goal, payload
                            await expect(composer).to_have_value(unrelated_draft)
                            report["cases"].append({"source":action,"reply":candidate,"turn_id":turn["id"],"context":turn["reply_context"]})

                        await page.reload(wait_until="domcontentloaded")
                        for case in report["cases"]:
                            row = page.locator(f'[data-turn-id="{case["turn_id"]}"]')
                            await expect(row.get_by_test_id("copy-reply")).to_be_visible()
                            await expect(row.locator(".user-message")).to_contain_text(
                                "回复要求 · " + case["context"]["goal"])
                        restored = {turn["id"]: turn.get("reply_context") for turn in (await server.state())["turns"]}
                        assert all(restored[case["turn_id"]] == case["context"] for case in report["cases"])
                        assert len(model_calls) == 4 and len(requests) == 4 and not outside and not page_errors, report

                        legacy_request = {"chat_id":origin["chat_id"], "text":expected["text"],
                                          "reply_context_turn":origin["id"]}
                        for rejected_goal in ("a" * 501, "🙂" * 376):
                            status, _ = await server.post("/api/message", {**legacy_request, "reply_context_goal":rejected_goal})
                            assert 400 <= status < 500, status
                        assert len(model_calls) == 4
                        status, snapshot = await server.post("/api/message", legacy_request)
                        assert status == 200
                        legacy_id = snapshot["turns"][-1]["id"]
                        row = page.locator(f'[data-turn-id="{legacy_id}"]')
                        await row.get_by_test_id("copy-reply").wait_for(timeout=10000)
                        legacy = (await server.state())["turns"][-1]
                        assert legacy["id"] == legacy_id and legacy["status"] == "replied"
                        assert legacy["reply_context"] == {"turn_id":origin["id"], "action":"clipboard"}, legacy
                        await expect(row.locator(".user-message")).to_have_text(legacy_request["text"])
                        assert len(model_calls) == 6 and all(not payload["goal"] for payload in model_calls[-2:]), model_calls
                        assert all(unseen not in json.dumps(call, ensure_ascii=False) and unrelated_draft not in
                                   json.dumps(call, ensure_ascii=False) for call in model_calls)
                        report.update(reload_retained_sources=True, reload_user_bubbles_show_goals=True,
                                      direct_api_rejected_overlong_goals=True, legacy_omits_goal=True, passed=True)
                    finally:
                        await browser.close()
            finally:
                await runner.cleanup()
                await moss.close()
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal-fixture", action="store_true", help="用本机模型夹具验证选中来信和独立回复要求")
    args = parser.parse_args()
    asyncio.run(verify_goal() if args.goal_fixture else run())
