import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
import traceback

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, confirm_muted_edge, open_browser


ROOT = Path(__file__).resolve().parent


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Case:
    def __init__(self, server, page, folder, result):
        self.server = server
        self.page = page
        self.folder = folder
        self.result = result

    async def open(self):
        await self.page.goto(self.server.url + "?chat=1", wait_until="domcontentloaded")
        await self.page.locator(".connection.online").wait_for()

    async def say(self, text):
        before = await self.page.get_by_test_id("turn").count()
        await self.page.get_by_test_id("composer").fill(text)
        await self.page.get_by_test_id("send").click()
        article = self.page.get_by_test_id("turn").nth(before)
        await article.wait_for()
        return await article.get_attribute("data-turn-id")

    def article(self, turn_id):
        return self.page.locator(f'article[data-turn-id="{turn_id}"]')

    async def wait_status(self, turn_id, expected):
        await self.page.wait_for_function(
            """([id, expected]) => {
                const status = document.querySelector('[data-turn-id="' + id + '"]')?.dataset.status;
                return status === expected || ['replied', 'cancelled', 'saved', 'error'].includes(status);
            }""", arg=[turn_id, expected], polling=35)
        actual = await self.article(turn_id).get_attribute("data-status")
        require(actual == expected, f"期望页面状态 {expected}，实际 {actual}")

    async def state(self, turn_id):
        snapshot = await self.server.state()
        return next(t for t in snapshot["turns"] if t["id"] == turn_id)

    async def capture(self, name, turn_id=None):
        if turn_id:
            self.result.setdefault("snapshots", {})[name] = await self.state(turn_id)
        await self.page.screenshot(path=self.folder / f"{name}.png")

    async def draft(self, text):
        turn_id = await self.say("帮我保存一份本地草稿，内容逐字写：" + text)
        await self.wait_status(turn_id, "pending")
        current = await self.state(turn_id)
        preview = await self.article(turn_id).get_by_test_id("preview").locator(".card-body").inner_text()
        require(current["call_id"] and preview == current["preview"] == text, f"预览与请求不符 {current}")
        self.no_files()
        self.result.update(turn_id=turn_id, call_id=current["call_id"], text=text, preview=preview)
        return turn_id

    async def button(self, turn_id, name):
        await self.article(turn_id).get_by_test_id(name).click()

    async def command(self, text):
        await self.page.get_by_test_id("composer").fill(text)
        await self.page.get_by_test_id("composer").press("Enter")

    def files(self):
        return sorted((self.server.data / "drafts").iterdir())

    def no_files(self):
        require(not self.files(), f"目录里发现了文件 {self.files()}")

    async def check_file(self, turn_id):
        current = await self.state(turn_id)
        receipt = current["receipt"]
        if current["status"] == "replied" and not self.server.draft_model_reply:
            require(current.get("reply_source") == "receipt" and current.get("approval_model") is None,
                    "普通草稿确认应由本地文件回执生成答复")
        expected = self.result["text"].encode("utf-8")
        path = self.server.data / "drafts" / receipt["filename"]
        require(self.files() == [path], f"应只有一份草稿 {self.files()}")
        require(path.read_bytes() == expected, "文件字节与已确认文本不一致")
        require(receipt["sha256"] == hashlib.sha256(expected).hexdigest(), "文件回执摘要不一致")
        card = self.article(turn_id).get_by_test_id("receipt")
        require(await card.is_visible(), "页面上没有文件回执")
        require(await card.locator(".card-body").inner_text() == self.result["text"], "页面回执正文不符")
        async with self.page.expect_popup() as opened:
            await card.get_by_role("link", name="在新标签页打开已保存的草稿").click()
        popup = await opened.value
        try:
            await popup.wait_for_load_state("domcontentloaded")
            require(await popup.locator("body").inner_text() == self.result["text"], "打开草稿后正文不符")
        finally:
            await popup.close()
        self.result.update(filename=path.name, sha256=receipt["sha256"], file_count=1)
        return path

    async def restart(self, hard, reload=True):
        original = self.server.pid
        await self.server.stop(hard=hard)
        await self.server.start()
        require(self.server.pid != original, "重启实验需要不同的进程")
        self.result.setdefault("restarts", []).append(
            {"before_pid": original, "after_pid": self.server.pid, "forced": hard, "reload_page": reload})
        if reload:
            await self.open()

    async def writing(self, turn_id):
        await self.button(turn_id, "approve")
        async with asyncio.timeout(15):
            while True:
                current = await self.state(turn_id)
                if current["tool_started_at"]:
                    require(current["status"] == "saving" and not current["receipt"], "未捕获写入前的窗口")
                    self.result["tool_started_at"] = current["tool_started_at"]
                    self.no_files()
                    return
                await asyncio.sleep(.025)

    async def receipt_before_reply(self, turn_id):
        await self.button(turn_id, "approve")
        article = self.article(turn_id)
        await article.get_by_test_id("receipt").wait_for()
        require(await article.get_attribute("data-status") == "saving", "显示回执时回复已经结束")
        require(await article.get_by_test_id("final").count() == 0, "最终回复先于测试操作")
        self.result["receipt_displayed_before_final"] = True


async def layout(case):
    await case.page.locator("#dialogue").get_by_role("heading", name="对话记录").wait_for()
    await case.capture("desktop")
    await case.page.set_viewport_size({"width": 390, "height": 844})
    await case.capture("mobile")
    require(await case.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "手机页面横向溢出")
    await case.page.get_by_test_id("memories").click()
    await case.page.get_by_test_id("memory-input").wait_for()
    await case.capture("mobile-memory-panel")
    await case.page.get_by_test_id("memory-close").click()
    await case.page.get_by_test_id("history").click()
    await case.page.get_by_test_id("history-close").click()
    await case.page.get_by_role("button", name="试写一份草稿").click()
    text = await case.page.get_by_test_id("composer").input_value()
    require("明天核对清单" in text, "示例草稿没有填入输入框")
    require(await case.page.get_by_test_id("send").is_enabled(), "输入草稿后未启用发送按钮")
    require(not (await case.server.state())["turns"], "点击示例时不应直接发送消息")
    case.result["mobile_width"] = 390
    await case.restart(hard=False, reload=False)
    await case.page.locator(".connection.online").wait_for()
    require(await case.page.get_by_test_id("composer").input_value() == text, "服务重启后未发送的输入丢失")


async def chat_history(case):
    word = "MOSS-" + secrets.token_hex(3).upper()
    first = await case.say(f"请记住本次校验码 {word}。现在只回答“记住了”，不要写文件。")
    await case.wait_status(first, "replied")
    require((await case.state(first))["receipt"] is None, "普通聊天产生了工具回执")
    second = await case.say("我刚才给你的完整校验码是什么？请只输出校验码。")
    await case.wait_status(second, "replied")
    answer = await case.article(second).get_by_test_id("final").inner_text()
    require(word in answer, f"模型未从上轮找回校验码 {answer}")
    case.no_files()
    case.result.update(code=word, answer=answer, first_id=first, second_id=second)
    await case.capture("chat-history", second)


async def memory_actions(case):
    initial = await case.server.state()
    old_chat = initial["chat_id"]
    page = case.page
    text = "页面测试：" + secrets.token_hex(3)
    await page.get_by_test_id("memories").click()
    await page.get_by_test_id("memory-input").fill(text)
    await page.get_by_test_id("memory-add").click()
    item = page.get_by_test_id("memory-item").filter(has_text=text)
    await item.wait_for()
    key = await item.get_attribute("data-memory-id")
    require((await case.server.state())["memories"][0]["id"] == key, "保存的记忆与界面不符")
    await case.capture("memory-created")
    await page.get_by_test_id("memory-close").click()
    async with page.expect_response(case.server.url + "/api/new-chat"):
        await page.get_by_test_id("new-chat").click()
    snapshot = await case.server.state()
    require(snapshot["chat_id"] != old_chat and len(snapshot["chats"]) == 2, "新对话未建立")
    await page.get_by_test_id("history").click()
    history_item = page.locator(f'button[data-chat-id="{old_chat}"]')
    await history_item.click()
    await page.locator("#history-panel").wait_for(state="hidden")
    require((await case.server.state())["chat_id"] == old_chat, "未能切换回原对话")
    await page.get_by_test_id("memories").click()
    await item.get_by_test_id("memory-remove").click()
    await item.wait_for(state="detached")
    require((await case.server.state())["memories"] == [], "移除后数据库仍有记忆")
    await page.get_by_test_id("memory-close").click()
    case.result.update(memory_id=key, original_chat=old_chat, new_chat=snapshot["chat_id"], model_calls=0)


async def cancel_pending(case):
    turn = await case.draft("取消这条草稿")
    await case.command("取消")
    await case.wait_status(turn, "cancelled")
    await case.restart(hard=False)
    await case.wait_status(turn, "cancelled")
    status, response = await case.server.post("/api/approve", {"turn_id": turn})
    require(status == 409, f"已取消任务的旧确认应被拒绝 {status} {response}")
    await asyncio.sleep(3.25)
    case.no_files()
    case.result.update(stale_approval_http=status, observation_s=3.25, file_count=0)
    await case.capture("cancelled-after-restart", turn)


async def restart_pending(case):
    turn = await case.draft("周五核对会议清单")
    await case.capture("before-restart", turn)
    await case.restart(hard=False)
    await case.wait_status(turn, "pending")
    current = await case.state(turn)
    require(current["call_id"] == case.result["call_id"], "待确认调用在重启后发生变化")
    case.no_files()
    await case.page.evaluate("""id => {
        const composer = document.querySelector('[data-testid="composer"]');
        composer.addEventListener('keydown', event => {
            if (event.key !== 'Enter') return;
            const start = performance.now();
            const samples = window.draftTiming = {};
            const observer = new MutationObserver(() => {
                const article = document.querySelector('[data-turn-id="' + id + '"]');
                for (const name of ['receipt', 'final']) {
                    const card = article?.querySelector('[data-testid="' + name + '"]');
                    if (!(name in samples) && card?.getClientRects().length) {
                        samples[name] = Math.round(performance.now() - start);
                    }
                }
                if ('final' in samples && 'receipt' in samples) observer.disconnect();
            });
            observer.observe(document.body, {subtree: true, childList: true, attributes: true});
        }, {once: true, capture: true});
    }""", turn)
    await case.command("确认保存")
    await case.page.wait_for_function(
        """id => document.querySelector('[data-turn-id="' + id + '"]').dataset.status !== 'pending'""",
        arg=turn, polling=25)
    status, _ = await case.server.post("/api/approve", {"turn_id": turn})
    require(status == 200, "重复确认没有返回现有结果")
    await case.wait_status(turn, "replied")
    times = await case.page.evaluate("window.draftTiming")
    require(times["receipt"] <= times["final"], "页面回执出现晚于最终回复")
    case.result["visible_ms_from_confirm"] = {**times, "lead": times["final"] - times["receipt"]}
    path = await case.check_file(turn)
    before = path.stat()
    status, _ = await case.server.post("/api/approve", {"turn_id": turn})
    after = path.stat()
    require(status == 200 and (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns),
            "结束后再次确认改变了已有文件")
    case.result["repeat_approval_http"] = status
    await case.capture("resumed", turn)


async def cancel_writing(case):
    turn = await case.draft("稍后核对库存")
    await case.writing(turn)
    await case.button(turn, "cancel")
    await case.wait_status(turn, "cancelled")
    await asyncio.sleep(3.25)
    case.no_files()
    current = await case.state(turn)
    require(current["receipt"] is None and current["reply"] is None, "取消后出现了成功结果")
    case.result.update(observation_s=3.25, file_count=0)
    await case.capture("cancelled-during-tool", turn)


async def crash_writing(case):
    turn = await case.draft("重启后继续核对清单")
    await case.writing(turn)
    await case.restart(hard=True, reload=False)
    await case.wait_status(turn, "pending")
    current = await case.state(turn)
    require(current["call_id"] == case.result["call_id"] and current["approved_at"] is None,
            "重启后的待确认调用不匹配")
    await asyncio.sleep(3.25)
    case.no_files()
    await case.capture("reconnected-pending", turn)
    await case.button(turn, "approve")
    await case.wait_status(turn, "replied")
    await case.check_file(turn)
    await case.capture("reconfirmed", turn)


async def cancel_after_receipt(case):
    turn = await case.draft("已核对周报")
    await case.receipt_before_reply(turn)
    await case.button(turn, "cancel")
    await case.wait_status(turn, "saved")
    await asyncio.sleep(3.25)
    require((await case.state(turn))["reply"] is None, "停止后出现了最终回复")
    await case.check_file(turn)
    await case.capture("receipt-kept", turn)


async def crash_after_receipt(case):
    turn = await case.draft("已记录本次议题")
    await case.receipt_before_reply(turn)
    await case.restart(hard=True, reload=False)
    await case.wait_status(turn, "saved")
    current = await case.state(turn)
    require(current["call_id"] == case.result["call_id"] and current["reply"] is None, "重启后的已保存状态不符")
    await case.check_file(turn)
    await case.capture("reconnected-saved", turn)


CASES = [layout, memory_actions, chat_history, cancel_pending, restart_pending, cancel_writing,
         crash_writing, cancel_after_receipt, crash_after_receipt]


async def run(args):
    label = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = ROOT / "runs" / f"browser-flows-{label}"
    folder.mkdir(parents=True)
    summary = {"run": folder.name, "model": args.model, "model_url": args.model_url, "cases": []}
    print(folder, flush=True)
    async with async_playwright() as playwright:
        browser = await open_browser(playwright)
        try:
            await confirm_muted_edge(browser)
            for test in CASES:
                if args.cases and test.__name__ not in args.cases:
                    continue
                out = folder / test.__name__
                out.mkdir()
                delay = 3 if test in (cancel_writing, crash_writing) else 0
                model_reply = test in (cancel_after_receipt, crash_after_receipt)
                server = BrowserServer(out, args.port, write_delay=delay, model_url=args.model_url,
                                       model=args.model, draft_model_reply=model_reply)
                result = {"name": test.__name__, "write_delay_s": delay, "draft_model_reply": model_reply,
                          "passed": False, "page_errors": []}
                context = await browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
                await context.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                page = await context.new_page()
                page.set_default_timeout(300000)
                page.on("pageerror", lambda exc: result["page_errors"].append(str(exc)))
                case = Case(server, page, out, result)
                try:
                    await server.start()
                    result["initial_pid"] = server.pid
                    if args.provider:
                        status, _ = await server.post("/api/select-model", {"id": args.provider})
                        state = await server.state()
                        require(status == 200 and state["model_id"] == args.provider, "模型切换失败")
                        result.update(provider=state["model_id"], engine=state["agent_mode"])
                    await case.open()
                    await test(case)
                    require(not result["page_errors"], f"页面脚本错误 {result['page_errors']}")
                    result["passed"] = True
                except Exception:
                    result["error"] = traceback.format_exc()
                    await page.screenshot(path=out / "failure.png")
                finally:
                    await context.close()
                    await server.stop()
                    write_json(out / "result.json", result)
                    summary["cases"].append({"name": test.__name__, "passed": result["passed"]})
                    write_json(folder / "summary.json", summary)
                    print(test.__name__, result["passed"], flush=True)
        finally:
            await browser.close()
    summary["passed"] = all(case["passed"] for case in summary["cases"])
    write_json(folder / "summary.json", summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18335)
    parser.add_argument("--model-url", default="http://127.0.0.1:18332/v1")
    parser.add_argument("--model", default="moss-chat-qwen35-4b")
    parser.add_argument("--provider", help="测试页面选择的模型服务")
    parser.add_argument("--cases", nargs="+", choices=[case.__name__ for case in CASES])
    raise SystemExit(asyncio.run(run(parser.parse_args())))
