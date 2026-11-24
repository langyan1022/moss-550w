import argparse
import asyncio
from contextlib import AsyncExitStack, closing
from datetime import datetime
import json
import sqlite3

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from serve import engine_process, local_port
from verify_flows import Case


class LocalModelLink:
    def __init__(self, upstream, http):
        self.upstream = upstream
        self.http = http
        self.events = []
        self.arm(None)

    def arm(self, phase, call_id=None):
        self.phase = phase
        self.call_id = call_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = False

    async def complete(self, request):
        body = await request.read()
        messages = json.loads(body)["messages"]
        last = messages[-1]
        event = {"phase": self.phase, "role": last["role"], "tool_call_id": last.get("tool_call_id")}
        self.events.append(event)
        matches = (self.phase == "before" or
                   (self.phase == "after" and last.get("tool_call_id") == self.call_id and last["role"] == "tool"))
        if matches and self.interrupted:
            event["closed_retry"] = True
            request.transport.abort()
            return web.Response()
        async with self.http.post(self.upstream + "/chat/completions", data=body,
                                  headers={"Content-Type": "application/json"}) as response:
            response.raise_for_status()
            event["upstream_http"] = response.status
            if matches:
                self.entered.set()
                await asyncio.wait_for(self.release.wait(), timeout=40)
                self.interrupted = True
                event["connection_aborted"] = True
                request.transport.abort()
                return web.Response()
            data = await response.read()
            event["forwarded_bytes"] = len(data)
            return web.Response(body=data, content_type=response.content_type)


async def verify(server, link, page, folder, report, offline):
    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
    page.set_default_timeout(120000)
    case = Case(server, page, folder, report)
    await case.open()
    assert (await server.state())["model_id"] == "local"

    link.arm("before")
    before = await case.say("帮我保存一份本地草稿，内容逐字写：周一核对目录")
    await asyncio.wait_for(link.entered.wait(), 100)
    try:
        assert not (await case.state(before))["receipt"]
        case.no_files()
    finally:
        link.release.set()
    await case.wait_status(before, "error")
    case.no_files()
    before_turn = await case.state(before)
    assert not before_turn.get("call_id") and not before_turn["receipt"]
    report["before"] = {"status": before_turn["status"], "file_count": len(case.files()),
                        "screen": await case.article(before).inner_text()}
    assert before_turn["error"] in report["before"]["screen"]
    assert "模型连接中断" in before_turn["error"] and "预览尚未生成" in before_turn["error"]
    before_actions = {}
    for action in ("approve", "cancel"):
        code, _ = await server.post("/api/" + action, {"turn_id": before})
        before_actions[action] = code
    report["before"]["stale_actions"] = before_actions
    assert before_actions == {"approve": 409, "cancel": 409}
    case.no_files()
    await case.restart(hard=False)
    await case.wait_status(before, "error")
    case.no_files()

    link.arm(None)
    after = await case.draft("周三核对三份材料")
    # Run both approval paths from the same real deferred Qwen tool call.
    await server.stop()
    offline.data.mkdir(parents=True)
    with closing(sqlite3.connect(server.data / "session.sqlite3")) as source:
        with closing(sqlite3.connect(offline.data / "session.sqlite3")) as target:
            source.backup(target)
    report["default_offline_confirm"] = {key: report[key] for key in ("turn_id", "call_id", "text", "preview")}
    await server.start()
    await case.open()
    await case.wait_status(after, "pending")
    link.arm("after", report["call_id"])
    await case.button(after, "approve")
    await asyncio.wait_for(link.entered.wait(), 100)
    try:
        await case.article(after).get_by_test_id("receipt").wait_for()
        committed = await case.state(after)
        assert committed["status"] == "saving" and committed["receipt"] and not committed["reply"]
        await case.check_file(after)
        report["at_disconnect"] = {"turn_id": after, "call_id": committed["call_id"],
                                   "status": committed["status"], "receipt": committed["receipt"]}
    finally:
        link.release.set()
    await case.wait_status(after, "saved")
    saved = await case.state(after)
    assert saved["receipt"] == committed["receipt"]
    report["after"] = {"status": saved["status"], "error": saved.get("error"),
                       "screen": await case.article(after).inner_text()}
    assert "模型回复中断" in saved["error"] and saved["error"] in report["after"]["screen"]
    await page.reload(wait_until="domcontentloaded")
    await case.wait_status(after, "saved")
    await case.check_file(after)
    await case.restart(hard=False)
    await case.wait_status(after, "saved")
    await case.check_file(after)
    assert (await case.state(after))["call_id"] == committed["call_id"]
    calls = len(link.events)
    for action in ("approve", "cancel"):
        status, _ = await server.post("/api/" + action, {"turn_id": after})
        report["after"][action] = status
        assert status == 200
    await case.check_file(after)
    assert len(link.events) == calls
    report["after"]["after_restart"] = await case.article(after).inner_text()
    await page.screenshot(path=folder / "after-restart.png")

    link.arm(None)
    recovered = await case.say("只回复：模型已恢复。")
    await case.wait_status(recovered, "replied")
    reply = (await case.state(recovered))["reply"]
    assert "模型已恢复" in reply
    report["recovered"] = reply
    assert len(case.files()) == 1 and (await case.state(after))["receipt"] == committed["receipt"]


async def verify_default(server, link, page, folder, report, model_processes):
    case = Case(server, page, folder, report)
    await case.open()
    assert (await server.state())["model_id"] == "local"
    turn_id = report["turn_id"]
    await case.wait_status(turn_id, "pending")
    pending = await case.state(turn_id)
    assert pending["call_id"] == report["call_id"] and pending["preview"] == report["text"]
    assert await case.article(turn_id).get_by_test_id("preview").locator(".card-body").inner_text() == report["text"]
    case.no_files()
    # Leave the relay listening to count even a failed contact attempt by the assistant.
    await model_processes.aclose()
    try:
        async with link.http.get(link.upstream.removesuffix("/v1") + "/health") as health:
            raise AssertionError(f"隔离模型端口仍响应 {health.status}")
    except aiohttp.ClientConnectorError:
        report["model_health_at_confirm"] = "connection refused"
    model_calls = len(link.events)
    await case.button(turn_id, "approve")
    await case.wait_status(turn_id, "replied")
    turn = await case.state(turn_id)
    assert turn["reply_source"] == "receipt" and turn["receipt"] and not turn["error"]
    await case.check_file(turn_id)
    await page.reload(wait_until="domcontentloaded")
    await case.wait_status(turn_id, "replied")
    await case.check_file(turn_id)
    await case.restart(hard=False)
    await case.wait_status(turn_id, "replied")
    for action in ("approve", "cancel"):
        status, _ = await server.post("/api/" + action, {"turn_id": turn_id})
        report[action] = status
        assert status == 200
    await case.check_file(turn_id)
    assert len(link.events) == model_calls
    recovered = await case.state(turn_id)
    assert recovered["receipt"] == turn["receipt"] and recovered["call_id"] == turn["call_id"]
    assert recovered["reply"] == turn["reply"] and recovered["reply_source"] == "receipt"
    report.update(reply=turn["reply"], reply_source=turn["reply_source"], model_requests_after_confirm=len(link.events) - model_calls,
                  receipt_ms=turn["receipt_ms"], reply_ms=turn["reply_ms"],
                  status=recovered["status"], screen=await case.article(turn_id).inner_text())
    await page.screenshot(path=folder / "offline-confirm-restarted.png")


async def main(args):
    for port in (args.port, args.engine_port, args.link_port):
        local_port(port)
    folder = ROOT / "runs" / datetime.now().strftime("draft-model-loss-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    report = {"passed": False, "engine": "local Qwen CPU", "paid_model_calls": 0}
    model_url = f"http://127.0.0.1:{args.link_port}/v1"
    server = BrowserServer(folder, args.port, model_url=model_url, draft_model_reply=True)
    offline_folder = folder / "offline-confirm"
    offline = BrowserServer(offline_folder, args.port, model_url=model_url)
    try:
        async with AsyncExitStack() as model_processes:
            upstream, _, _ = await model_processes.enter_async_context(engine_process(args.engine_port, folder, False))
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=150)) as http:
                link = LocalModelLink(upstream, http)
                report["model_requests"] = link.events
                app = web.Application()
                app.router.add_post("/v1/chat/completions", link.complete)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                try:
                    await web.TCPSite(runner, "127.0.0.1", args.link_port).start()
                    await server.start()
                    async with async_playwright() as pw:
                        browser = await open_browser(pw)
                        try:
                            await confirm_muted_edge(browser)
                            page = await browser.new_page(reduced_motion="reduce")
                            await verify(server, link, page, folder, report, offline)
                            await server.stop()
                            await offline.start()
                            await verify_default(offline, link, page, offline_folder,
                                                 report["default_offline_confirm"], model_processes)
                        finally:
                            await browser.close()
                finally:
                    link.release.set()
                    await offline.stop()
                    await server.stop()
                    await runner.cleanup()
        report["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--engine-port", type=int, default=18766)
    parser.add_argument("--link-port", type=int, default=18767)
    asyncio.run(main(parser.parse_args()))
