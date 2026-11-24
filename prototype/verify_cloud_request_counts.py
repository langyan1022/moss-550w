import asyncio
from contextlib import AsyncExitStack, closing
from dataclasses import replace
import hashlib
import ipaddress
import json
import os
import sqlite3
import sys

from aiohttp import web
from pydantic import SecretStr

from browser_lab import BrowserServer, ROOT
from core import Moss
from local_media.library import MediaLibrary
from mcp_configured import MCP_DATA
from model_catalog import SPECS
from serve import application
from verify_nginx_provider_retries import QUESTION, SAMPLE, copy_pending, free_port


async def protocol_receipt(request):
    raw = await request.read()
    payload = json.loads(raw)
    assert payload["stream"] and payload["messages"][-1]["content"] == QUESTION
    assert request.headers.get("Authorization") == "Bearer qa-local-only"
    digest = hashlib.sha256(raw).hexdigest()
    content = "本机协议服务已收到请求，正文 SHA256 为 " + digest
    event = {"id":"qa-" + digest[:12], "object":"chat.completion.chunk", "created":0, "model":payload["model"],
             "choices":[{"index":0, "delta":{"role":"assistant","content":content}, "finish_reason":"stop"}]}
    return web.Response(text="data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n\n",
                        content_type="text/event-stream")


def snapshot_old_completed(folder):
    source = ROOT / "runs/nginx-provider-20261120-210232-001781/long429/session.sqlite3"
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
        rows = old.execute("SELECT doc FROM turns").fetchall()
        assert len(rows) == 1 and not old.execute("SELECT 1 FROM memories LIMIT 1").fetchone()
        turn = json.loads(rows[0][0])
        assert turn["user"] == QUESTION and turn["status"] == "error"
        assert "cloud_requests" not in {row[1] for row in old.execute("PRAGMA table_info(turns)")}
        with closing(sqlite3.connect(folder / "session.sqlite3")) as copy:
            old.backup(copy)
    return turn["id"]


async def verify_counts(root, base, http, pending):
    data = root / "tracked/data"
    old_id = copy_pending(pending, data)
    legacy = root / "unmeasured/data"
    untouched_id = snapshot_old_completed(legacy)
    catalog = root / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    prefixes = tuple(prefix + "_" for _, keys, _, _ in SPECS.values() for prefix in keys)
    for name in tuple(os.environ):
        if name.startswith(prefixes):
            del os.environ[name]
    os.environ.update(LOCALAPPDATA=str(root / "local-profile"), MOSS_PROVIDER_DIR=str(root / "providers"),
                      MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0")
    refused = []

    def guard_network(event, args):
        if event == "socket.connect":
            host = args[1][0]
        elif event == "socket.getaddrinfo":
            host = args[0]
        else:
            return
        if host is None or host == "localhost":
            return
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            refused.append(event)
            raise RuntimeError("隔离验收只允许回环连接。")

    sys.addaudithook(guard_network)
    result = {"scope":"实际本机 NGINX 配额，成功回合是本机 HTTP 服务对请求正文的 SHA256 回执。",
              "tracked_data":str(data), "untouched_history_data":str(legacy), "cases":{}}
    routes = {"deepseek":"long429", "aliyun":"cancel429", "kimi":"short429"}
    ledger = root / "logs/access.log"

    def posts():
        if not ledger.exists():
            return []
        return [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.startswith("POST ")]

    def check(turn, total, by_provider, complete=True):
        usage = turn["cloud_requests"]
        assert usage == {"total":total,"by_provider":by_provider,"complete":complete}, (turn["id"], usage)
        return usage

    known = {}
    async with AsyncExitStack() as stack:
        moss = Moss(data, "http://127.0.0.1:9/v1", "qa", draft_model_reply=True)
        stack.push_async_callback(moss.close)
        stack.callback(MCP_DATA.reset, MCP_DATA.set(data))
        for client in moss.clients.values():
            await client.close()
        moss.clients.clear()
        for key, route in routes.items():
            moss.cloud[key] = replace(moss.cloud[key], url=base + "/" + route, secret=SecretStr("qa-local-only"))
        moss.agent = moss._agent_for(moss.model_id)
        endpoint = BrowserServer(root / "tracked", free_port())
        runner = web.AppRunner(application(moss, lambda:"standby", data, endpoint.port, "qa", asyncio.Event()), access_log=None)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", endpoint.port).start()
        test_pid = os.getpid()
        state = await endpoint.state()
        chat = state["chat_id"]
        assert state["turns"][0]["id"] == old_id and state["turns"][0]["cloud_requests"] is None
        assert state["model_id"] == "deepseek" and not posts()
        result["tracked_chat"] = chat
        digests = {}

        def prime_first(provider):
            route = routes[provider]
            attempts = digests[provider] = []

            async def prime(request):
                assert str(request.url) == f"{base}/{route}/chat/completions"
                if not attempts:
                    async with http.get(base + "/prime/" + route) as response:
                        assert response.status == 200 and await response.text() == "ready"
                attempts.append(hashlib.sha256(request.content).hexdigest())

            moss.model_client()._client.event_hooks["request"].append(prime)

        async def post(path, body):
            status, state = await endpoint.post(path, body)
            assert status == 200, (path, status, state)
            return state["turns"][-1]

        async def settled():
            async with asyncio.timeout(10):
                while True:
                    turn = (await endpoint.state())["turns"][-1]
                    if turn["status"] not in {"thinking","saving"}:
                        return turn
                    await asyncio.sleep(.02)

        prime_first("deepseek")
        await post("/api/approve", {"turn_id":old_id})
        old = await settled()
        assert old["status"] == "saved" and old["receipt"]["text"] == SAMPLE
        known[old_id] = check(old, 1, {"deepseek":1}, False)
        result["cases"]["old_pending"] = {"id":old_id,"before":None,"after":known[old_id]}
        before_local = posts()
        local = await post("/api/message", {"chat_id":chat,"text":"查看我的便签"})
        assert local["source"] == "notes" and local["status"] == "replied"
        known[local["id"]] = check(local, 0, {})
        assert posts() == before_local
        result["cases"]["local"] = {"id":local["id"],"usage":known[local["id"]]}

        await post("/api/select-model", {"id":"aliyun"})
        prime_first("aliyun")
        same = {"chat_id":chat,"text":QUESTION}
        initial = await post("/api/message", same)
        async with asyncio.timeout(5):
            while not any("/cancel429/" in line for line in posts()):
                await asyncio.sleep(.01)
        in_flight = (await endpoint.state())["turns"][-1]
        assert in_flight["id"] == initial["id"] and in_flight["status"] == "thinking"
        check(in_flight, 1, {"aliyun":1})
        status, _ = await endpoint.post("/api/message", same)
        assert status == 409
        now = await endpoint.state()
        assert len(now["turns"]) == 3 and now["turns"][-1]["id"] == initial["id"]
        cancelled = await post("/api/cancel", {"turn_id":initial["id"]})
        assert cancelled["status"] == "cancelled"
        known[initial["id"]] = check(cancelled, 1, {"aliyun":1})
        await asyncio.sleep(2.15)
        assert len(posts()) == 2
        result["cases"]["cancelled_duplicate"] = {"id":initial["id"], "second_http":status,
                                                 "same_turn_count":len(now["turns"]), "usage":known[initial["id"]]}

        await post("/api/select-model", {"id":"kimi"})
        prime_first("kimi")
        await post("/api/message", same)
        retry = await settled()
        assert retry["status"] == "replied" and retry["reply"].startswith("本机协议服务已收到请求"), retry
        assert len(digests["kimi"]) == 2 and len(set(digests["kimi"])) == 1
        assert retry["reply"].endswith(digests["kimi"][0])
        known[retry["id"]] = check(retry, 2, {"kimi":2})
        result["cases"]["retry"] = {"id":retry["id"], "usage":known[retry["id"]], "reply":retry["reply"]}
        audit = posts()
        expected = [("long429",429),("cancel429",429),("short429",429),("short429",200)]
        assert [(line.split()[1].split("/")[1],int(line.split()[2])) for line in audit] == expected, audit
        result["gateway_posts"] = audit
        result["request_body_sha256"] = digests

    for folder, expected_id in ((root / "tracked", None),(root / "unmeasured", untouched_id)):
        cold = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
        try:
            await cold.start()
            assert cold.pid != test_pid
            state = await cold.state()
            values = {turn["id"]:turn["cloud_requests"] for turn in state["turns"]}
            if expected_id is None:
                assert state["chat_id"] == chat and values == known, values
                result["cold_restart"] = {"old_pid":test_pid,"new_pid":cold.pid,"turns":values}
            else:
                assert values == {expected_id:None}, values
                result["untouched_history"] = {"id":expected_id,"chat":state["chat_id"],"usage":None}
        finally:
            await cold.stop()
    assert not refused and posts() == audit
    result["outside_loopback_attempts"] = refused
    return result
