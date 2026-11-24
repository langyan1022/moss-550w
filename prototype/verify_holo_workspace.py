import asyncio
from datetime import datetime
import io
import json

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("holo-workspace-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18533)
    report = {"passed": False}
    await server.start()
    try:
        status, _ = await server.post("/api/select-model", {"id": "deepseek"})
        assert status == 200
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                ctx = await browser.new_context(viewport={"width": 390, "height": 830}, reduced_motion="reduce")
                await ctx.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                page = await ctx.new_page()
                await page.goto(server.url + "/api/health")
                await page.evaluate("""url => {
                    document.body.style.margin = '0';
                    const app = document.createElement('iframe');
                    app.name = 'moss';
                    app.allow = "clipboard-read 'none'; clipboard-write 'none'; display-capture 'none'";
                    app.style.cssText = 'width:100vw;height:100vh;border:0;display:block;';
                    app.src = url + '/?chat=1'; document.body.replaceChildren(app);
                }""", server.url)
                embedded = await page.locator("iframe[name=moss]").element_handle()
                frame = await embedded.content_frame()
                assert frame
                await frame.locator("#kernel-label").wait_for()
                assert await frame.locator("#kernel-label").inner_text() == "· HoloJarvis"
                blocked = await frame.evaluate("""() => {
                    const p = document.featurePolicy;
                    return ['clipboard-read','clipboard-write','display-capture'].every(name=>!p.allowsFeature(name));
                }""")
                assert blocked
                report["restricted_device_permissions"] = blocked
                async def send(text):
                    size = len((await server.state())["turns"])
                    await frame.get_by_test_id("composer").fill(text)
                    await frame.get_by_test_id("send").click()
                    row = frame.get_by_test_id("turn").nth(size)
                    await row.get_by_test_id("final").wait_for(timeout=120000)
                    return row, (await server.state())["turns"][-1]

                await frame.get_by_test_id("tool-catalog-open").click()
                panel = frame.locator("#tool-catalog")
                bounds = await panel.bounding_box()
                assert bounds and bounds["width"] <= 390 and bounds["x"] >= 0
                await panel.get_by_role("button", name="查询本机时间").click()
                assert await frame.get_by_test_id("composer").input_value() == "现在几点了？"
                assert not (await server.state())["turns"] and not await panel.is_visible()
                time_row, turn = await send("现在几点了？")
                assert "get_time" in turn.get("holo_tools", []), turn
                assert not await time_row.get_by_test_id("copy-reply").count()
                report["clock"] = {"reply": turn["reply"], "tools": turn["holo_tools"]}
                await send("3秒后提醒我伸个懒腰")
                await frame.get_by_test_id("timer-notice").wait_for(timeout=10000)
                assert "伸个懒腰" in await frame.get_by_test_id("timer-notice").inner_text()
                await frame.get_by_test_id("timer-dismiss").click()
                await frame.get_by_test_id("timer-notice").wait_for(state="hidden")
                assert not (await server.state())["timers"]
                report["timer_due_and_dismissed"] = True

                clip, _ = await send("读取剪贴板")
                await clip.get_by_test_id("context-action").click()
                area = clip.get_by_role("textbox", name="粘贴剪贴板文字")
                await area.fill("local-only-64302")
                assert "local-only-64302" not in json.dumps(await server.state(), ensure_ascii=False)
                report["clipboard_permission_denied_paste"] = True
                screen, _ = await send("识别当前窗口的文字")
                await screen.get_by_role("button", name="选择窗口并识别").click()
                await screen.get_by_text("窗口未共享，可重新选择。").wait_for()
                fixture = Image.new("RGB", (740, 165), "white")
                draw = ImageDraw.Draw(fixture)
                draw.text((26, 24), "MOSS 6342", fill="black", font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 78))
                source = io.BytesIO()
                fixture.save(source, format="PNG")
                ocr = await ctx.request.post(server.url + "/api/context/ocr", data=source.getvalue(),
                                             headers={"Content-Type":"image/png"}, timeout=45000)
                result = await ocr.json()
                assert ocr.ok and "6342" in result["text"], result
                report["controlled_ocr"] = result["text"]

                jev, turn = await send("帮我回同事：明天几点开会？我的意思是下午三点。")
                assert turn.get("source") == "jev" and turn["jev"]["candidate_count"] > 0
                await jev.get_by_test_id("copy-reply").click()
                await jev.get_by_text("文字已选中，可使用系统复制。").wait_for()
                assert await frame.evaluate("()=>String(window.getSelection())") == turn["reply"]
                report["jev"] = {"reply": turn["reply"], "count": turn["jev"]["candidate_count"],
                                 "selection_matches": True}
                await page.set_viewport_size({"width": 1440, "height": 900})
                await frame.get_by_test_id("tool-catalog-open").click()
                assert await panel.is_visible()
                await panel.get_by_role("button", name="关闭工具示例").click()
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report)


if __name__ == "__main__":
    asyncio.run(main())
