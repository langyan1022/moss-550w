import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import secrets
import socket

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, open_browser


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def unknown(reply, value):
    return (value not in reply and not re.search(r"\b[MT]-[0-9A-F]{8}\b", reply, re.I)
            and any(word in reply for word in ("未知", "不知道", "不清楚", "没有", "未保存", "未找到")))


async def send(page, server, prompt):
    count = len((await server.state())["turns"])
    await page.get_by_test_id("composer").fill(prompt)
    await page.get_by_test_id("send").click()
    row = page.get_by_test_id("turn").nth(count)
    await row.locator('[data-testid="final"], [data-testid="approve"], .turn-error').first.wait_for()
    if await row.get_by_test_id("approve").count():
        raise RuntimeError("只读问答意外产生待确认草稿：" + prompt)
    if await row.locator(".turn-error").count():
        raise RuntimeError(await row.locator(".turn-error").inner_text())
    return await row.get_by_test_id("final").inner_text()


async def new_chat(page, server):
    old = (await server.state())["chat_id"]
    await page.get_by_test_id("new-chat").click()
    await page.wait_for_function("() => document.querySelectorAll('[data-testid=turn]').length === 0")
    latest = await server.state()
    require(latest["chat_id"] != old and latest["turns"] == [], "新对话应为空，并持有独立 ID")
    return latest["chat_id"]


async def run(folder):
    folder.mkdir(parents=True, exist_ok=False)
    server = BrowserServer(folder, _free_port())
    code = "M-" + secrets.token_hex(4).upper()
    temp = "T-" + secrets.token_hex(4).upper()
    other = "纸舟-" + secrets.token_hex(2).upper()
    fact = "我的项目校验码是 " + code
    extra = "我的助手测试昵称是 " + other
    question = "我的项目校验码是什么？如果没有找到已保存的校验码，只回答“未知”。"
    result = {"code": code, "second_value": other, "temporary_code": temp, "events": [], "passed": False}
    def record(name, **fields):
        result["events"].append({"name": name, **fields})
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name, flush=True)

    await server.start()
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            page = await browser.new_page(viewport={"width": 1280, "height": 860}, locale="zh-CN")
            page.set_default_timeout(480000)
            try:
                await page.goto(server.url + "?chat=1")
                initial = await server.state()
                require(not initial["memories"] and not initial["turns"], "测试应该使用空的会话库")
                first_pid = server.pid
                reply = await send(page, server, f"这一轮的临时代号是 {temp}，只回复收到。")
                require(not (await server.state())["memories"], "普通消息不应自动创建长期记忆")
                record("ordinary_chat", chat_id=initial["chat_id"], pid=first_pid, reply=reply)
                reply = await send(page, server, "记住：" + fact)
                state = await server.state()
                require(len(state["memories"]) == 1 and state["memories"][0]["text"] == fact, "应明确保存第一条记忆")
                ident = state["memories"][0]["id"]
                await send(page, server, "记住：" + fact)
                require(len((await server.state())["memories"]) == 1, "相同记忆不应重复")
                record("explicit_memory_and_deduplication", id=ident, reply=reply)

                await page.get_by_test_id("memories").click()
                await page.get_by_test_id("memory-input").fill(extra)
                await page.get_by_test_id("memory-add").click()
                await page.wait_for_function("() => document.querySelectorAll('[data-testid=memory-item]').length===2")
                await page.screenshot(path=folder / "memory-list.png")
                await page.get_by_test_id("memory-close").click()
                require({m["text"] for m in (await server.state())["memories"]} == {fact, extra}, "记忆面板保存失败")
                record("manage_in_browser", stored=2)
                decoys = [f"办公柜第{i:02}层收纳蓝色文件夹、标签纸和打印纸。" for i in range(1, 21)]
                for note in decoys:
                    status, _ = await server.post("/api/remember", {"text": note})
                    require(status == 200, "较大的记忆库应能保存新条目")
                stored = (await server.state())["memories"]
                require(len(stored) == 22, "应在同一数据库保留22条事实")
                record("populate_beyond_prompt_budget", rows=len(stored),
                       total_utf8_bytes=sum(len(r["text"].encode()) for r in stored))

                chat_b = await new_chat(page, server)
                reply = await send(page, server, question)
                require(code in reply and temp not in reply, "新话题未取回正确校验码")
                selected = (await server.state())["turns"][-1].get("memory_ids", [])
                require(ident in selected and len(selected) <= 12, "早期条目应在本轮实际送入模型的记录中")
                record("recall_in_new_chat", chat_id=chat_b, question=question, reply=reply, selected=selected)
                await new_chat(page, server)
                ask_temp = "我在以前对话里随口提到的临时代号是什么？没有依据时只回答“未知”。"
                reply = await send(page, server, ask_temp)
                require(unknown(reply, temp), "另一个话题不应继承未保存的临时代号")
                record("temporary_chat_value_isolated", question=ask_temp, reply=reply)

                await server.stop(hard=True)
                await server.start()
                require(server.pid != first_pid, "需要真正更换进程")
                await page.goto(server.url + "?chat=1")
                chat_c = await new_chat(page, server)
                ask = "我保存的助手测试昵称是什么？请按原文回答。"
                reply = await send(page, server, ask)
                require(other in reply, "新进程未取回第二条记忆")
                record("recall_after_restart", chat_id=chat_c, pid=server.pid, question=ask, reply=reply)
                await page.get_by_test_id("memories").click()
                item = page.locator(f'[data-memory-id="{ident}"]')
                await item.get_by_test_id("memory-remove").click()
                await item.wait_for(state="detached")
                await page.get_by_test_id("memory-close").click()
                require({m["text"] for m in (await server.state())["memories"]} == {extra, *decoys}, "应仅删除指定记忆")
                record("forget_one", kept=extra)

                await server.stop(hard=True)
                await server.start()
                await page.goto(server.url + "?chat=1")
                chat_d = await new_chat(page, server)
                reply = await send(page, server, question)
                require(unknown(reply, code), "删除后新话题应明确回答未知")
                selected = (await server.state())["turns"][-1].get("memory_ids", [])
                require(ident not in selected, "本轮上下文不应包含已删除的记忆")
                record("forget_survives_second_restart", chat_id=chat_d, pid=server.pid, question=question,
                       reply=reply, selected=selected)
                reply = await send(page, server, ask)
                require(other in reply, "删除第一条后第二条应仍然可用")
                record("other_fact_retained", reply=reply)
                chat_e = await new_chat(page, server)
                draft_prompt = "把长期记忆里的助手测试昵称原样保存为本地草稿，正文只写昵称。"
                await page.get_by_test_id("composer").fill(draft_prompt)
                await page.get_by_test_id("send").click()
                turn = page.get_by_test_id("turn").last
                await turn.get_by_test_id("approve").wait_for()
                draft = (await server.state())["turns"][-1]
                require(draft["preview"] == other and not list((server.data / "drafts").glob("*.txt")),
                        "应该仅预览长期记忆中的昵称")
                await turn.get_by_test_id("approve").click()
                await turn.get_by_test_id("final").wait_for()
                done = (await server.state())["turns"][-1]
                require((server.data / "drafts" / done["receipt"]["filename"]).read_text(encoding="utf-8") == other,
                        "使用长期记忆的工具应该写入已确认的准确正文")
                record("remembered_value_saved_after_approval", chat_id=chat_e, prompt=draft_prompt,
                       preview=draft["preview"], receipt=done["receipt"], reply=done["reply"])
                status, old = await server.post("/api/switch-chat", {"id": initial["chat_id"]})
                require(status == 200 and any(code in turn["user"] for turn in old["turns"]),
                        "应可返回原对话查阅原来输入的文字")
                result["passed"] = True
                record("archived_chat_accessible", total_chats=len(old["chats"]))
            finally:
                await browser.close()
    finally:
        await server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / datetime.now().strftime("memory-%Y%m%d-%H%M%S"))
    asyncio.run(run(parser.parse_args().out))
