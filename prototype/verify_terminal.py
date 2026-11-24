import argparse
import asyncio
import audioop
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT
from verify_memory import send


def synthetic_audio(folder):
    path = folder / "control.wav"
    subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "voice/speech_control.ps1"), str(path)], check=True)
    with wave.open(str(path), "rb") as input_file:
        pcm, _ = audioop.ratecv(input_file.readframes(input_file.getnframes()), 2, 1, input_file.getframerate(), 44100, None)
    target = folder / "edge-microphone.wav"
    with wave.open(str(target), "wb") as output:
        output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        output.writeframes(bytes(44100) + pcm + bytes(44100 * 5))
    return target


async def run(args):
    folder = ROOT / "runs" / datetime.now().strftime("terminal-%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    print(folder, flush=True)
    fixture = synthetic_audio(folder) if args.voice else None
    server = BrowserServer(folder, 18334)
    report = {"passed": False, "page_errors": [], "requests": [], "http_errors": [], "models": []}
    await server.start()
    try:
        async with async_playwright() as p:
            edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
            flags = ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"]
            if fixture:
                flags += ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                          "--use-file-for-fake-audio-capture=" + str(fixture) + "%noloop"]
            browser = await p.chromium.launch(executable_path=str(edge), headless=True, args=flags)
            context = await browser.new_context(viewport={"width":1440,"height":900}, permissions=["microphone"] if fixture else [])
            page = await context.new_page()
            if fixture:
                await page.add_init_script("""const originalFetch = window.fetch;
                  window.fetch = async function(input, init) {
                    if (input === '/api/transcribe' && init.body instanceof Blob) {
                      const data = await init.body.arrayBuffer();
                      window.capturedWave = {header: Array.from(new Uint8Array(data.slice(0, 64))), size: data.byteLength};
                    }
                    return originalFetch.call(window, input, init);
                  };""")
            page.set_default_timeout(90000)
            page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
            page.on("request", lambda r: report["requests"].append(r.url) if r.url.startswith("http") else None)
            page.on("response", lambda r: report["http_errors"].append([r.status,r.url]) if r.status >= 400 else None)
            await page.goto(server.url, wait_until="domcontentloaded")
            await page.wait_for_function("document.querySelector('#opening-film').currentTime > 0.05")
            report["opening"] = await page.locator("#opening-film").evaluate("(film) => ({src:film.currentSrc, current_time:film.currentTime, duration:film.duration, muted:film.muted})")
            await page.get_by_role("button", name="跳过开场").click()
            await page.locator("#history-toggle").click()
            await page.locator("#moss-model[data-ready=true]").wait_for(timeout=110000)
            await page.locator("#close-chat").click()
            await page.screenshot(path=str(folder / "550w-stage.png"))
            await page.locator('input[value="optics"]').check(force=True)
            await page.wait_for_function("document.querySelector('#camera-dial').getAttribute('aria-valuenow')==='3'")
            await page.locator("#reset-view").click()
            await page.locator("#history-toggle").click()
            await page.get_by_test_id("memories").click()
            await page.get_by_test_id("memory-input").fill("测试界面的颜色为蓝色。")
            await page.get_by_test_id("memory-add").click()
            await page.get_by_test_id("memory-item").wait_for()
            await page.get_by_test_id("memory-remove").click()
            await page.get_by_test_id("memory-close").click()
            await page.locator(".preferences summary").click()
            selector = page.get_by_test_id("model-select")
            disclosure = page.get_by_test_id("cloud-context")
            await page.wait_for_function("!document.querySelector('#agent-model').disabled")
            available = await selector.locator("option:enabled").evaluate_all("(options)=>options.map(o=>o.value)")
            report["available"] = available
            await page.screenshot(path=str(folder / "models.png"))
            async def select(key):
                if not await selector.is_visible():
                    await page.locator(".preferences summary").click()
                async with page.expect_response(lambda res: res.url.endswith("/api/select-model")) as selected:
                    await selector.select_option(key)
                response = await selected.value
                body = await response.json()
                assert response.ok and body["model_id"] == key
                if key in {"deepseek", "aliyun", "kimi"}:
                    await disclosure.wait_for(state="visible")
                    assert "全部条目" in await disclosure.inner_text()
                else:
                    await disclosure.wait_for(state="hidden")
                await page.locator(".preferences summary").click()
            await page.locator(".preferences summary").click()
            if "deepseek" in available:
                await select("deepseek")
                await select("local")
                report["cloud_disclosure"] = {"visible_when_cloud":True, "hidden_when_local":True}
            if args.providers:
                for key in ("deepseek", "aliyun", "kimi"):
                    assert key in available
                    await select(key)
                    prompt = f"这是界面验收，逐字回复 OK{key.upper()}。"
                    reply = await send(page, server, prompt)
                    assert "OK" + key.upper() in reply.upper(), reply
                    report["models"].append({"id": key, "reply": reply})
                await select("deepseek")
                exact = "终端界面验收草稿。"
                await page.get_by_test_id("composer").fill(f"保存草稿：{exact}")
                await page.get_by_test_id("send").click()
                await page.get_by_test_id("approve").last.wait_for()
                pending = (await server.state())["turns"][-1]
                await select("kimi")
                await server.stop()
                await server.start()
                await page.locator(".connection.online").wait_for()
                restored = (await server.state())["turns"][-1]
                assert restored["call_id"] == pending["call_id"] and restored["status"] == "pending"
                await page.get_by_test_id("approve").last.click()
                await page.get_by_test_id("receipt").last.wait_for()
                receipt_turn = (await server.state())["turns"][-1]
                assert receipt_turn["receipt"]["text"] == exact
                await page.get_by_test_id("turn").last.get_by_test_id("final").wait_for()
                report["resumption"] = {"id": restored["id"], "call_id": restored["call_id"],
                                        "receipt": receipt_turn["receipt"], "approval_model": receipt_turn.get("approval_model")}
                await select("local")
            if fixture:
                async with page.expect_request(lambda req: req.url.endswith("/api/transcribe"), timeout=60000) as captured:
                    async with page.expect_response(lambda res: res.url.endswith("/api/transcribe"), timeout=150000) as asr:
                        await page.locator("#mic").click()
                    response = await asr.value
                request = await captured.value
                actual = await page.evaluate("window.capturedWave")
                assert request.headers["content-type"] == "audio/wav"
                with wave.open(BytesIO(bytes(actual["header"])), "rb") as audio:
                    shape = [audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()]
                assert actual["size"] == 44 + shape[3] * 2
                result = await response.json()
                assert shape[:3] == [16000,1,2] and response.ok and "媒体库" in result.get("text", ""), (shape, result)
                await page.locator('[data-testid="turn"]').filter(has_text=result["text"]).get_by_test_id("final").wait_for()
                voice_turn = (await server.state())["turns"][-1]
                assert voice_turn["source"] == "media"
                report["voice"] = {"wav": shape, "asr": result["text"], "reply": voice_turn["reply"]}
            await page.set_viewport_size({"width":390,"height":844})
            await page.screenshot(path=str(folder / "mobile.png"))
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not report["page_errors"] and not report["http_errors"], (report["page_errors"], report["http_errors"])
            assert all(urlsplit(url).netloc == urlsplit(server.url).netloc for url in report["requests"])
            await browser.close()
        report["passed"] = True
    finally:
        await server.stop()
        report["requests"] = sorted(set(report["requests"]))
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", action="store_true")
    parser.add_argument("--voice", action="store_true")
    asyncio.run(run(parser.parse_args()))
