import argparse
import asyncio
from datetime import datetime
import json
import os
import secrets
from pathlib import Path
import subprocess

from playwright.async_api import async_playwright

from testkit import BrowserServer, ROOT, open_browser, safe_free_port
from verify_memory import new_chat, send


async def run(folder, url, profile):
    server = BrowserServer(folder, safe_free_port())
    code = "G-" + secrets.token_hex(3).upper()
    facts = ["坐高铁时，我的订座代号是 " + code, "我更喜欢红茶。", "周二上午开例会。", "冬天喜欢穿蓝色外套。"]
    report = {"passed": False, "profile": profile, "expected": code, "pids": [], "events": []}
    try:
        await server.start()
        report["pids"].append(server.pid)
        for fact in facts:
            status, view = await server.post("/api/remember", {"text": fact})
            assert status == 200 and view["memory_backend"] == "sqlite"
        original = {item["text"]: item["id"] for item in view["memories"]}
        await server.stop()
        server.memory_url = url
        await server.start()
        report["pids"].append(server.pid)
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            page = await browser.new_page()
            page.set_default_timeout(480000)
            await page.goto(server.url + "?chat=1")
            view = await server.state()
            assert view["memory_backend"] == "mem0" and original == {m["text"]: m["id"] for m in view["memories"]}
            query = "买火车票时我的预订代号是什么？如果没有记录，只回答未知。"
            reply = await send(page, server, query)
            turn = (await server.state())["turns"][-1]
            report["events"].append({"step": "sqlite_to_mem0", "query": query, "reply": reply,
                                     "expected_id": original[facts[0]], "memory_ids": turn.get("memory_ids", [])})
            assert code in reply and original[facts[0]] in turn["memory_ids"]
            await page.get_by_test_id("memories").click()
            assert await page.locator("#memory-backend").inner_text() == "检索服务：本机 Mem0"
            await page.get_by_test_id("memory-close").click()
            await server.stop()
            # 只让这个测试进程连接到没有监听的端口，保留共享服务。
            server.memory_url = "http://127.0.0.1:18379"
            await server.start()
            report["pids"].append(server.pid)
            status, state = await server.post("/api/forget", {"id": original[facts[0]]})
            view = await server.state()
            report["events"].append({"step": "delete_while_index_offline", "http": status, "index_error": state.get("memory_index_error")})
            assert status == 200 and view["memory_index_error"] and len(view["memories"]) == len(facts) - 1
            await server.stop()
            server.memory_url = url
            await server.start()
            report["pids"].append(server.pid)
            await page.goto(server.url + "?chat=1")
            await new_chat(page, server)
            reply = await send(page, server, query)
            view = await server.state()
            ids = view["turns"][-1].get("memory_ids", [])
            report["events"].append({"step": "index_reconnected", "reply": reply, "memory_ids": ids})
            assert code not in reply and original[facts[0]] not in ids and not view["memory_index_error"]
            assert any(word in reply for word in ("未知", "不知道", "没有记录", "未保存", "未找到"))
            await browser.close()
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18337)
    parser.add_argument("--embedding-port", type=int, default=18336)
    parser.add_argument("--profile", choices=["nomic", "bge"], default="nomic")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / datetime.now().strftime("memory-service-%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    script = ROOT.parent / "alternatives/mem0/service.ps1"
    command = ["powershell", "-NoProfile", "-File", str(script)]
    params = ["-Profile", args.profile, "-Port", str(args.port), "-EmbeddingPort", str(args.embedding_port),
              "-DataDirectory", str((args.out / "index").resolve())]
    service_env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    subprocess.run([*command, "start", *params], check=True, env=service_env)
    try:
        asyncio.run(run(args.out, f"http://127.0.0.1:{args.port}", args.profile))
    finally:
        subprocess.run([*command, "stop", *params], check=True)
