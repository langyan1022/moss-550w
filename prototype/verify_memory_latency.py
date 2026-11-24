import asyncio
from datetime import datetime, timedelta
import json
from time import perf_counter

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser


async def run():
    folder = ROOT / "runs" / ("memory-latency-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    probes, received = [], []

    async def delayed_index(request):
        probes.append((request.path, perf_counter()))
        await asyncio.sleep(2.5)
        return web.json_response({"error": "隔离慢索引拒绝请求"}, status=503)

    async def no_model(request):
        received.append((perf_counter(), json.dumps((await request.json())["messages"], ensure_ascii=False)))
        return web.json_response({"error": "验收端口只记录模型输入"}, status=503)

    service = web.Application()
    service.router.add_post("/v1/retrieve", delayed_index)
    service.router.add_post("/v1/sync", delayed_index)
    service.router.add_post("/v1/chat/completions", no_model)
    runner = web.AppRunner(service)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host = f"http://127.0.0.1:{runner.addresses[0][1]}"
    server = BrowserServer(folder, 18786, model_url=host + "/v1")
    report = {"index_delay_s": 2.5, "passed": False}
    async def post(verb, **body):
        status, state = await server.post("/api/" + verb, body)
        assert status == 200, state.get("error")
        return state

    async def lookup():
        count = len(received)
        before = perf_counter()
        state = await post("message", text="说一说我的加密档案代号，用一句话。")
        async with asyncio.timeout(5):
            while state["turns"][-1]["status"] == "thinking":
                await asyncio.sleep(.025)
                state = await server.state()
        assert len(received) == count + 1 and "丁香试验乙" in received[-1][1]
        assert state["turns"][-1]["memory_ids"] == [key] and state["memory_index_error"]
        return round((received[-1][0] - before) * 1000)

    try:
        await server.start()
        state = await post("remember", text="我的加密档案代号是丁香试验乙")
        key = state["memories"][0]["id"]
        await server.stop()
        server.memory_url = host
        await server.start()
        report["first_submit_to_model_ms"] = await lookup()
        report["index_to_sqlite_model_ms"] = round((received[-1][0] - probes[0][1]) * 1000)
        assert report["index_to_sqlite_model_ms"] < 1350
        report["repeat_submit_to_model_ms"] = await lookup()
        assert report["repeat_submit_to_model_ms"] < 750 and [path for path, _ in probes] == ["/v1/retrieve"]
        await server.stop()
        await server.start()
        before = perf_counter()
        state = await post("remember", text="我的工作音乐偏好是极简钢琴")
        report["write_with_slow_sync_ms"] = round((perf_counter() - before) * 1000)
        assert report["write_with_slow_sync_ms"] < 1350 and state["memory_index_error"]
        assert [path for path, _ in probes] == ["/v1/retrieve", "/v1/sync"]
        assert await lookup() < 750 and len(probes) == 2
        await post("message", text="记住：我坐高铁喜欢靠窗")
        await post("message", text="以后改成靠过道")
        await post("new-chat")
        before_model = len(received)
        state = await post("message", text="我要坐高铁，按我的座位偏好给一句选座建议。")
        seat = state["turns"][-1]
        assert seat["source"] == "memory" and seat["reply"] == "高铁座位优先选靠过道的位置。" and len(received) == before_model
        report["corrected_answer"] = seat["reply"]
        report["corrected_answer_model_calls"] = len(received) - before_model

        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page()
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url + "/?chat=1")
                await page.get_by_test_id("memories").click()
                deadline = (datetime.now() + timedelta(seconds=4)).replace(microsecond=0)
                state = await post("remember", text="短时可见的面板记录（有效至 " + deadline.isoformat(sep=" ") + "）")
                item = next(m for m in state["memories"] if m["text"] == "短时可见的面板记录")
                row = page.locator(f'[data-memory-id="{item["id"]}"]')
                await row.wait_for(state="visible", timeout=2500)
                assert "来自记忆面板" in await row.inner_text() and "有效至" in await row.inner_text()
                revision = state["revision"]
                await row.wait_for(state="detached", timeout=7000)
                state = await server.state()
                assert state["revision"] == revision and item["id"] not in [m["id"] for m in state["memories"]]
                assert await page.get_by_test_id("memory-close").is_visible()
                report["panel_expired_without_revision"] = revision
            finally:
                await browser.close()
        report["passed"] = True
    finally:
        try:
            await server.stop()
        finally:
            await runner.cleanup()
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(folder)


if __name__ == "__main__":
    asyncio.run(run())
