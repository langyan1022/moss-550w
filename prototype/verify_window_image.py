import argparse
import asyncio
import base64
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
import httpx2
from PIL import Image
from playwright.async_api import async_playwright
from pydantic import SecretStr

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import Profile, SPECS, allowed
from serve import application
from verify_context_reply import free_port
from verify_memory import send


TITLE = "MOSS visual QA 7019"
COMMAND = "看一下这个窗口的画面"
QUESTION = "蓝色圆形在黄色方形的什么方向？有几个绿色圆形？请用一句话回答。"
ANSWER = "蓝色圆形在黄色方形的右上方，有三个绿色圆形。"
BEFORE = "这条测试消息的独立代号是 VISION_HISTORY_7019。请简单确认。"
AFTER = "用一句话解释纸为什么能折出痕迹。"
UNSENT = "VISION_UNSENT_8223"
ENDPOINT = "/api/context/image-question"
ZERO = {"total": 0, "by_provider": {}, "complete": True}
EXPECTED = {"total": 1, "by_provider": {"aliyun": 1}, "complete": True}

FIXTURE = """<!doctype html><meta charset="utf-8"><title>MOSS visual QA 7019</title>
<style>html,body{margin:0;overflow:hidden}svg{display:block;width:100vw;height:100vh}</style>
<svg viewBox="0 0 1000 1000" preserveAspectRatio="none" role="img">
  <rect width="1000" height="1000" fill="#fafafa"/>
  <rect x="150" y="570" width="200" height="200" fill="#ffd33d"/>
  <circle cx="680" cy="260" r="105" fill="#1558e8"/>
  <circle cx="620" cy="815" r="50" fill="#13a754"/>
  <circle cx="750" cy="815" r="50" fill="#13a754"/>
  <circle cx="880" cy="815" r="50" fill="#13a754"/>
</svg>"""

CAPTURE_AUDIT = """localStorage.setItem('moss.voice.enabled','off');
window.captureQA=[];
const originalCapture=navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getDisplayMedia=async options=>{
  const stream=await originalCapture(options);
  const track=stream.getVideoTracks()[0];
  captureQA.push({track,surface:track.getSettings().displaySurface,options,
                  trackCount:stream.getTracks().length});
  return stream;
};"""


def fake_profiles():
    return {key: Profile(key, label, url, model, SecretStr("qa-no-network"))
            for key, (label, _, url, model) in SPECS.items()}


def synthetic_reply(request, body, reply):
    result = {"id": "qa-visual", "created": 0, "model": body["model"]}
    if body.get("stream"):
        result.update(object="chat.completion.chunk", choices=[{
            "index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": "stop"}])
        return httpx2.Response(200, request=request, headers={"Content-Type": "text/event-stream"},
                              content=("data: " + json.dumps(result, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode("utf-8"))
    result.update(object="chat.completion", choices=[{
        "index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}])
    return httpx2.Response(200, request=request, json=result)


def inspect_picture(contents):
    text = [item["text"] for item in contents if item["type"] == "text"]
    assert len(text) == 1 and QUESTION in text[0] and "VISION_" not in text[0]
    (image,) = (item["image_url"]["url"] for item in contents if item["type"] == "image_url")
    assert len(contents) == 2 and image.startswith("data:image/png;base64,")
    encoded = image.partition(",")[2]
    raw = base64.b64decode(encoded, validate=True)
    with Image.open(BytesIO(raw), formats=("PNG",)) as frame:
        rgb = frame.convert("RGB")
        width, height = rgb.size
        assert 150 <= min(width, height) <= max(width, height) <= 4096
        marks = []
        for x, y, expected in ((.25, .67, (255, 211, 61)), (.68, .26, (21, 88, 232)),
                               (.62, .815, (19, 167, 84)), (.75, .815, (19, 167, 84)), (.88, .815, (19, 167, 84))):
            pixel = rgb.getpixel((round(width * x), round(height * y)))
            assert max(abs(a - b) for a, b in zip(pixel, expected)) <= 40, (x, y, pixel, expected)
            marks.append(pixel)
    return {"width": width, "height": height, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "sampled_colors": marks}, encoded


def valid_png(size=(128, 128)):
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def capture_state(page):
    return page.evaluate("""() => captureQA.map(({track,surface,options,trackCount})=>
      ({phase:track.readyState,surface,trackCount,audio:options.audio}))""")


async def run(live):
    folder = ROOT / "runs" / datetime.now().strftime("window-image-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed": False, "mode": "live-bailian" if live else "intercepted-sdk",
              "paid_requests": 0, "ordinary_checks": [], "provider_attempts": [], "rejections": {},
              "unexpected_provider_attempts": [],
              "browser_external_attempts": [], "browser_image_posts": 0, "local_ocr_requests": 0}
    allowed_questions = []
    transmitted = []
    private_replies = []
    original_transport = httpx2.AsyncHTTPTransport.handle_async_request

    async def check_request(transport, request):
        assert request.method == "POST" and request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        messages = body["messages"]
        content = messages[-1]["content"]
        if isinstance(content, list):
            assert allowed_questions == [QUESTION] and request.url.scheme == "https"
            assert allowed("aliyun", str(request.url).removesuffix("/chat/completions"))
            assert [item["role"] for item in messages] == ["system", "user"] and not body.get("tools")
            picture, encoded = inspect_picture(content)
            allowed_questions.pop()
            transmitted.append(encoded)
            report["provider_attempts"].append({"provider": "aliyun", "model": body["model"], "host": request.url.host,
                "picture": picture, "question": QUESTION, "history": 0, "tools": 0})
            if live:
                report["paid_requests"] += 1
                return await original_transport(transport, request)
            return synthetic_reply(request, body, ANSWER)
        assert content in (BEFORE, AFTER) and request.url.host == "api.deepseek.com"
        canonical = json.dumps(messages, ensure_ascii=False)
        assert all(value not in canonical for value in (COMMAND, QUESTION, ANSWER, UNSENT, "data:image", "image_url"))
        if content == AFTER:
            assert BEFORE in canonical and private_replies and all(reply not in canonical for reply in private_replies)
        report["ordinary_checks"].append({"prompt": content, "entries": len(messages), "image_history": False})
        return synthetic_reply(request, body, "已收到验收问题。")

    async def audited_transport(transport, request):
        try:
            return await check_request(transport, request)
        except (AssertionError, KeyError, ValueError):
            report["unexpected_provider_attempts"].append({"host": request.url.host, "path": request.url.path})
            raise

    env = {"MOSS_KEV_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0", "MOSS_MEDIA_DB": str(catalog)}
    fake_config = patch("core.profiles", new=fake_profiles) if not live else patch.dict(os.environ, {})
    with patch.dict(os.environ, env), fake_config, patch.object(httpx2.AsyncHTTPTransport, "handle_async_request", audited_transport):
        server = BrowserServer(folder, free_port())
        moss = Moss(server.data, "http://127.0.0.1:9/v1", "unused")
        runner = None
        try:
            await moss.select_model("deepseek")
            app = application(moss, lambda: "standby", server.data, server.port, "isolated", asyncio.Event())

            async def fixture(_):
                return web.Response(text=FIXTURE, content_type="text/html")

            app.router.add_get("/qa-window-image", fixture)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", server.port).start()
            assert (await server.state())["vision_model"]["available"], "本轮需要可用的百炼视觉配置"
            async with async_playwright() as pw:
                edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
                browser = await pw.chromium.launch(executable_path=str(edge), headless=True, args=[
                    "--mute-audio", "--disable-gpu", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                    "--auto-select-tab-capture-source-by-title=" + TITLE, "--enable-usermedia-screen-capturing"])
                try:
                    await confirm_muted_edge(browser)
                    context = await browser.new_context(viewport={"width": 1000, "height": 1000}, reduced_motion="reduce")

                    async def keep_local(route):
                        target = urlsplit(route.request.url)
                        if target.path == "/api/context/ocr":
                            report["local_ocr_requests"] += 1
                        if target.path == ENDPOINT and route.request.method == "POST":
                            report["browser_image_posts"] += 1
                        if target.scheme in {"http", "https"} and target.netloc != urlsplit(server.url).netloc:
                            report["browser_external_attempts"].append(target.netloc)
                            await route.abort()
                        else:
                            await route.continue_()

                    await context.route("**/*", keep_local)
                    sheet = await context.new_page()
                    await sheet.goto(server.url + "/qa-window-image")
                    assert await sheet.title() == TITLE
                    page = await context.new_page()
                    await page.add_init_script(CAPTURE_AUDIT)
                    address = server.url + "/?chat=1&scene=off"
                    await page.goto(address, wait_until="domcontentloaded")
                    await page.locator(".connection.online").wait_for()
                    await send(page, server, BEFORE)
                    ordinary = (await server.state())["turns"][0]
                    await send(page, server, COMMAND)
                    origin = (await server.state())["turns"][-1]
                    assert origin["context_action"] == "screen_image" and origin["cloud_requests"] == ZERO
                    row = page.get_by_test_id("turn").last
                    composer = page.get_by_test_id("composer")
                    await composer.fill(UNSENT)
                    await row.get_by_test_id("context-action").click()
                    preview = row.get_by_test_id("window-image-preview")
                    await preview.wait_for()
                    expected_capture = {"phase": "ended", "surface": "browser", "trackCount": 1, "audio": False}
                    assert await capture_state(page) == [expected_capture]
                    ask = row.get_by_role("button", name="就这张图提问", exact=True)
                    editor = row.get_by_role("textbox", name="针对窗口画面的问题", exact=True)
                    await ask.click()
                    await editor.fill(QUESTION)
                    await row.get_by_role("button", name=re.compile(r"^取消(?:提问)?$")).click()
                    await editor.wait_for(state="hidden")
                    assert not report["provider_attempts"] and not report["browser_image_posts"]
                    assert len((await server.state())["turns"]) == 2

                    data = {"chat_id": origin["chat_id"], "turn_id": origin["id"], "model_id": "aliyun",
                            "text": QUESTION, "image": valid_png()}
                    for label, changes in (("wrong_provider", {"model_id": "deepseek"}), ("wrong_origin", {"turn_id": "0" * 32}),
                                           ("not_a_preview", {"turn_id": ordinary["id"]}),
                                           ("invalid_png", {"image": base64.b64encode(b"pretend this is a png").decode("ascii")}),
                                           ("oversized_pixels", {"image": valid_png((4097, 1))})):
                        status, _ = await server.post(ENDPOINT, {**data, **changes})
                        assert status in {400, 409, 415, 422}, (label, status)
                        report["rejections"][label] = status
                    assert not report["provider_attempts"] and len((await server.state())["turns"]) == 2
                    if not await preview.is_visible():
                        await row.get_by_test_id("context-action").click()
                        await preview.wait_for()
                    captures = await capture_state(page)
                    assert 1 <= len(captures) <= 2 and all(capture == expected_capture for capture in captures), captures
                    report["capture"] = captures
                    await ask.click()
                    await editor.fill(QUESTION)
                    await row.screenshot(path=folder / "窗口画面预览.png", animations="disabled")
                    allowed_questions.append(QUESTION)
                    started = perf_counter()
                    submit = row.get_by_role("button", name="发送到阿里云百炼", exact=True)
                    await submit.evaluate("""button => button.addEventListener('click', () => {
                      const start = performance.now();
                      const observer = new MutationObserver(() => {
                        const turn = document.querySelectorAll('[data-testid="turn"]')[2];
                        const text = turn?.querySelector('[data-testid="partial-reply"], [data-testid="final"]');
                        if (text?.textContent.trim()) {
                          window.visualFirst = performance.now() - start;
                          observer.disconnect();
                        }
                      });
                      observer.observe(document.querySelector('#turns'), {subtree:true,childList:true,characterData:true,attributes:true});
                    }, {once:true,capture:true})""")
                    await submit.click()
                    answer_row = page.get_by_test_id("turn").nth(2)
                    await answer_row.locator('[data-testid="final"], .turn-error').first.wait_for(timeout=90000)
                    elapsed = round((perf_counter() - started) * 1000, 1)
                    state = await server.state()
                    turn = state["turns"][-1]
                    assert state["model_id"] == "deepseek" and turn["source"] == "window_image" and turn["status"] == "replied"
                    assert turn["cloud_requests"] == EXPECTED and len(report["provider_attempts"]) == 1 and not allowed_questions
                    assert report["provider_attempts"][0]["model"] == state["vision_model"]["model"]
                    assert turn["image_context"]["turn_id"] == origin["id"]
                    assert all(isinstance(turn["image_context"][axis], int) and turn["image_context"][axis] > 0 for axis in ("width", "height"))
                    assert re.search(r"右.{0,8}上|上.{0,8}右", turn["reply"]), turn["reply"]
                    assert re.search(r"[三3]\s*个?\s*绿色|绿色.{0,18}[三3]", turn["reply"]), turn["reply"]
                    assert await composer.input_value() == UNSENT
                    first = await page.evaluate("window.visualFirst")
                    assert isinstance(first, (int, float)) and 0 <= first <= elapsed
                    await answer_row.screenshot(path=folder / "窗口画面答复.png", animations="disabled")
                    private_replies.append(turn["reply"])
                    report.update(reply=turn["reply"], turn_id=turn["id"], cloud_requests=turn["cloud_requests"],
                                  visible_first_text_ms=round(first, 1), visible_complete_ms=elapsed,
                                  composer_preserved=True, selected_model=state["model_id"])
                    assert not state["memories"] and not list(moss.drafts.iterdir())
                    durable = json.dumps(state, ensure_ascii=False) + "\n".join(row[0] for row in moss.db.execute("SELECT doc FROM turns"))
                    assert all(value not in durable for value in (*transmitted, "data:image", "image_url", UNSENT))
                    await page.reload(wait_until="domcontentloaded")
                    await page.get_by_test_id("turn").nth(2).get_by_test_id("final").wait_for()
                    assert await page.get_by_test_id("window-image-preview").count() == 0
                    report["preview_cleared_on_reload"] = True
                    await send(page, server, AFTER)
                    assert len(report["ordinary_checks"]) == 2 and len(report["provider_attempts"]) == 1
                    status, switched = await server.post("/api/new-chat", {})
                    assert status == 200 and switched["chat_id"] != origin["chat_id"]
                    old_status, _ = await server.post(ENDPOINT, data)
                    other_status, _ = await server.post(ENDPOINT, {**data, "chat_id": switched["chat_id"]})
                    assert old_status in {400, 409} and other_status in {400, 409}
                    assert not (await server.state())["turns"] and len(report["provider_attempts"]) == 1
                    report["rejections"].update(stale_chat=old_status, cross_chat_image=other_status)
                    assert not report["local_ocr_requests"] and not report["browser_external_attempts"]
                    assert not report["unexpected_provider_attempts"] and report["browser_image_posts"] == 1
                    assert report["paid_requests"] == int(live)
                    report["passed"] = True
                finally:
                    await browser.close()
        finally:
            if runner is not None:
                await runner.cleanup()
            await moss.close()
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Send the synthetic chart to BaiLian once through its HTTPS endpoint.")
    asyncio.run(run(parser.parse_args().live))
