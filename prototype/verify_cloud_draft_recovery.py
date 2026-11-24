import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import socket
import sqlite3

import httpx2
from openai import AsyncOpenAI

from browser_lab import ROOT
from core import Moss


async def disconnect(moss, port, attempts):
    profile = moss.cloud["deepseek"]
    async def record(request):
        attempts.append({"method": request.method, "host": request.url.host})
    if client := moss.clients.get("deepseek"):
        await client.close()
    transport = httpx2.AsyncClient(proxy=f"http://127.0.0.1:{port}", trust_env=False,
                                   follow_redirects=False, timeout=4, event_hooks={"request": [record]})
    moss.clients["deepseek"] = AsyncOpenAI(base_url=profile.url, api_key=profile.secret.get_secret_value(),
                                          max_retries=0, http_client=transport)
    moss.agent = moss._agent_for("deepseek")


async def main():
    root = ROOT / "runs" / datetime.now().strftime("deepseek-draft-recovery-%Y%m%d-%H%M%S-%f")
    prompt = "帮我保存一份本地草稿，内容逐字写：周一核对书目"
    def service(folder, **settings):
        return Moss(folder, "http://127.0.0.1:18954/v1", "moss-chat-qwen35-4b", prefer_cloud=True, **settings)

    report = {"passed": False, "network_fault": "isolated client HTTPS proxy connection refused", "cases": {}}
    with socket.socket() as offline:
        offline.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        offline.bind(("127.0.0.1", 0))
        port = offline.getsockname()[1]
        app = service(root / "prepare")
        try:
            await app.select_model("deepseek")
            key = await app.message(prompt)
            await asyncio.wait_for(app.job, timeout=75)
            preview = app._get(key)
            assert preview["model_id"] == "deepseek" and preview["status"] == "pending", preview["error"]
            assert preview["preview"] == "周一核对书目" and preview["call_id"]
        finally:
            await app.close()
        app = service(root / "before")
        try:
            await app.select_model("deepseek")
            attempts = []
            await disconnect(app, port, attempts)
            early = await app.message(prompt)
            await asyncio.wait_for(app.job, timeout=15)
            item = app._get(early)
            assert item["status"] == "error" and not item["receipt"] and not item["call_id"] and attempts
            assert not list(app.drafts.iterdir())
            report["cases"]["before_preview"] = {"status": item["status"], "error": item["error"],
                                                  "files": 0, "egress_attempts": attempts}
        finally:
            await app.close()
        for mode, reply_needed in (("normal_confirm", False), ("post_write_model_reply", True)):
            data = root / mode
            data.mkdir()
            with closing(sqlite3.connect((root / "prepare" / "session.sqlite3").as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(data / "session.sqlite3")) as dest:
                    source.backup(dest)
            attempts = []
            app = service(data, draft_model_reply=reply_needed)
            try:
                await disconnect(app, port, attempts)
                await app.approve(key)
                await asyncio.wait_for(app.job, timeout=15)
                finished = app._get(key)
                expected = preview["preview"].encode("utf-8")
                receipt = finished["receipt"]
                assert (app.drafts / receipt["filename"]).read_bytes() == expected and receipt["sha256"] == hashlib.sha256(expected).hexdigest()
                assert finished["status"] == ("saved" if reply_needed else "replied")
                assert bool(attempts) == reply_needed and bool(finished["error"]) == reply_needed
            finally:
                await app.close()
            app = service(data, draft_model_reply=reply_needed)
            try:
                await disconnect(app, port, attempts)
                await app.approve(key)
                row = app._get(key)
                assert row["call_id"] == preview["call_id"] and row["receipt"] == receipt
                assert len(list(app.drafts.iterdir())) == 1 and not app.job
                report["cases"][mode] = {"status": row["status"], "error": row["error"], "sha256": receipt["sha256"],
                                          "files_after_restart_and_retry": 1, "egress_attempts": attempts}
            finally:
                await app.close()
    report["passed"] = True
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(root, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-paid-preview", required=True, action="store_true", help="用隔离测试文本向 DeepSeek 请求一次真实预览")
    parser.parse_args()
    asyncio.run(main())
