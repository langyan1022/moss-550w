"""Verify dual-session cancel, network-fault recovery, slow stdio MCP cleanup, and draft safety under CPU load."""

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from statistics import median
import sys
import time
from time import perf_counter
from unittest.mock import patch

import aiohttp
from aiohttp import web
import psutil
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import Moss
from local_media.library import MediaLibrary
from mcp_configured import MCP_DATA
from model_catalog import Profile
from serve import application
from verify_context_reply import free_port


def _cpu_burn_worker(stop_event):
    """Pure-Python CPU contention worker (runs in separate process to bypass GIL)."""
    acc = 1
    while not stop_event.is_set():
        for i in range(1, 40000):
            acc = (acc * 1103515245 + i) & 0x7FFFFFFF


@asynccontextmanager
async def cpu_load(workers: int):
    if workers <= 0:
        yield {"workers": 0, "cpu_samples": []}
        return
    ctx = multiprocessing.get_context("spawn")
    stop_event = ctx.Event()
    procs = [ctx.Process(target=_cpu_burn_worker, args=(stop_event,), daemon=True) for _ in range(workers)]
    for p in procs:
        p.start()
    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.35)
    samples = []
    sampling = True

    async def monitor():
        while sampling:
            samples.append(psutil.cpu_percent(interval=None))
            await asyncio.sleep(0.25)

    task = asyncio.create_task(monitor())
    try:
        yield {"workers": workers, "cpu_samples": samples}
    finally:
        sampling = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        stop_event.set()
        for p in procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)


def run_fixture_mcp(journal_path: Path):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("moss_loaded_fault_fixture")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read_only, structured_output=True)
    async def read_delayed() -> str:
        with journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tool": "read_delayed", "pid": os.getpid(), "at": time.time()}) + "\n")
        await asyncio.sleep(25)
        return "这条晚到结果不可显示。"

    server.run(transport="stdio")


def write_mcp_config(data_dir: Path, journal: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "servers": {
            "慢查询": {
                "command": "project-python",
                "args": [str(Path(__file__).resolve()), "--fixture-server", str(journal)],
                "server_name": "moss_loaded_fault_fixture",
                "tools": {"read_delayed": "延时读取"},
                "timeout_seconds": 25,
            }
        }
    }
    (data_dir / "mcp-servers.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


@asynccontextmanager
async def fault_model_server():
    """Loopback OpenAI-compatible server with deterministic fault injection & request logging."""
    state = {
        "requests": [],
        "retry_429_count": 0,
        "stall_opened": asyncio.Event(),
        "late_frame_sent": asyncio.Event(),
        "late_frame_gate": asyncio.Event(),
    }

    async def completions(request):
        body = await request.json()
        messages = body.get("messages") or []
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        entry = {"user": last_user, "stream": bool(body.get("stream")), "at": perf_counter()}
        state["requests"].append(entry)

        # Case: Draft preview (non-streaming Pydantic AI tool call)
        if "保存一份本地草稿" in last_user:
            entry["mode"] = "draft_tool"
            payload = {
                "id": "chatcmpl-draft-1",
                "object": "chat.completion",
                "created": 1700000000,
                "model": body.get("model", "deepseek-flash"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_loaded_draft_01",
                            "type": "function",
                            "function": {
                                "name": "save_draft",
                                "arguments": json.dumps({"text": "高负载故障恢复草稿核对清单"}, ensure_ascii=False),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }
            return web.json_response(payload)

        # Case: 429 retry once -> succeed on 2nd attempt
        if "限流重试测试" in last_user:
            state["retry_429_count"] += 1
            if state["retry_429_count"] == 1:
                entry["mode"] = "429_retry_first"
                return web.json_response(
                    {"error": {"message": "rate limited"}},
                    status=429,
                    headers={"Retry-After": "1"},
                )
            entry["mode"] = "429_retry_second_ok"
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
            await stream.prepare(request)
            c = {"id": "r2", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "限流后重试成功。"}, "finish_reason": "stop"}]}
            await stream.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
            return stream

        # Case: 429 cancel mid-backoff (must only receive 1 request!)
        if "限流取消测试" in last_user:
            entry["mode"] = "429_cancel_backoff"
            return web.json_response(
                {"error": {"message": "rate limited"}},
                status=429,
                headers={"Retry-After": "2"},
            )

        # Case: Truncated SSE stream (partial chunk, then abrupt EOF without finish_reason)
        if "截断流测试" in last_user:
            entry["mode"] = "truncated_sse"
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
            await stream.prepare(request)
            c = {"id": "trunc", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "这一句只说到一半"}, "finish_reason": None}]}
            await stream.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode("utf-8"))
            await asyncio.sleep(0.05)
            # Close cleanly without finish_reason or [DONE] -> simulates server truncation
            await stream.write_eof()
            return stream

        # Case: Stalled SSE stream + late frame attempted after cancel
        if "停顿迟到帧测试" in last_user:
            entry["mode"] = "stalled_late_frame"
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
            await stream.prepare(request)
            c1 = {"id": "stall1", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                  "choices": [{"index": 0, "delta": {"role": "assistant", "content": "正在处理第一半"}, "finish_reason": None}]}
            await stream.write(f"data: {json.dumps(c1, ensure_ascii=False)}\n\n".encode("utf-8"))
            state["stall_opened"].set()
            try:
                await asyncio.wait_for(state["late_frame_gate"].wait(), timeout=10)
                c2 = {"id": "stall2", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                      "choices": [{"index": 0, "delta": {"content": "迟到的尾巴绝不能出现"}, "finish_reason": "stop"}]}
                await stream.write(f"data: {json.dumps(c2, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
                state["late_frame_sent"].set()
            except (ConnectionResetError, aiohttp.ClientConnectionError, asyncio.CancelledError, TimeoutError):
                pass
            return stream

        # Default normal recovery response
        entry["mode"] = "ok"
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await stream.prepare(request)
        c = {"id": "ok", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"恢复正常：{last_user}"}, "finish_reason": "stop"}]}
        await stream.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
        return stream

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        yield f"http://127.0.0.1:{port}/v1", state
    finally:
        await runner.cleanup()


@asynccontextmanager
async def isolated_instance(data_dir: Path, cloud_url: str, *, mcp_journal: Path | None = None):
    port = free_port()
    stopped = asyncio.Event()
    media_db = data_dir / "media.sqlite3"
    data_dir.mkdir(parents=True, exist_ok=True)
    MediaLibrary(media_db)
    if mcp_journal is not None:
        write_mcp_config(data_dir, mcp_journal)

    ds = Profile("deepseek", "DeepSeek", cloud_url, "deepseek-flash", SecretStr("isolated-fault-key"))
    ali = Profile("aliyun", "阿里云百炼", cloud_url, "qwen4-max", SecretStr(""), "未配置")
    kimi = Profile("kimi", "Kimi", cloud_url, "kimi-k2.6", SecretStr(""), "未配置")
    env = {
        "MOSS_MEDIA_DB": str(media_db),
        "MOSS_PROVIDER_DIR": str(data_dir / "providers"),
        "LOCALAPPDATA": str(data_dir / "profile"),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "0",
        "MOSS_SPEECH_STANDBY": "0",
    }
    async with AsyncExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch("core.profiles", return_value={"deepseek": ds, "aliyun": ali, "kimi": kimi}))
        if mcp_journal is not None:
            stack.callback(MCP_DATA.reset, MCP_DATA.set(data_dir))
        moss = Moss(data_dir, "http://127.0.0.1:1/v1", "local-unused", prefer_cloud=True)
        stack.push_async_callback(moss.close)
        app = application(moss, lambda: "standby", data_dir, port, "fault-qa", stopped, speech_standby_seconds=0)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        def _stop():
            stopped.set()
            moss._notify()
        stack.callback(_stop)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        yield f"http://127.0.0.1:{port}", moss


async def wait_turn(client: aiohttp.ClientSession, base: str, turn_id: str, statuses: set[str], *, timeout=15.0):
    deadline = perf_counter() + timeout
    while perf_counter() < deadline:
        async with client.get(base + "/api/state") as resp:
            st = await resp.json()
        turn = next((t for t in st["turns"] if t["id"] == turn_id), None)
        if turn and turn["status"] in statuses:
            return turn
        await asyncio.sleep(0.03)
    raise TimeoutError(f"Turn {turn_id} did not reach {statuses}")


async def sse_cancel_and_immediate_next(client: aiohttp.ClientSession, base: str, turn_id: str, next_text: str):
    """Subscribe to real /api/events SSE; send /api/cancel; the instant SSE emits cancelled, POST next_text."""
    sse_cancelled_at = None
    next_status = None
    next_body = None
    cancel_http_ms = None
    next_http_ms = None
    sse_ready = asyncio.Event()

    async def listen_sse():
        nonlocal sse_cancelled_at, next_status, next_body, next_http_ms
        async with client.get(base + "/api/events", timeout=aiohttp.ClientTimeout(total=30)) as resp:
            event_type = None
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_type == "state":
                    payload = json.loads(line.split(":", 1)[1].strip())
                    sse_ready.set()
                    t = next((row for row in payload.get("turns", []) if row["id"] == turn_id), None)
                    if t and t["status"] == "cancelled":
                        sse_cancelled_at = perf_counter()
                        t0 = perf_counter()
                        async with client.post(base + "/api/message", json={"text": next_text}) as r2:
                            next_status = r2.status
                            next_body = await r2.json()
                        next_http_ms = round((perf_counter() - t0) * 1000, 1)
                        return

    sse_task = asyncio.create_task(listen_sse())
    await asyncio.wait_for(sse_ready.wait(), timeout=5)
    t_cancel = perf_counter()
    async with client.post(base + "/api/cancel", json={"turn_id": turn_id}) as r_cancel:
        cancel_status = r_cancel.status
        await r_cancel.json()
    cancel_http_ms = round((perf_counter() - t_cancel) * 1000, 1)
    await asyncio.wait_for(sse_task, timeout=10)
    return {
        "cancel_status": cancel_status,
        "cancel_http_ms": cancel_http_ms,
        "sse_cancel_to_next_ms": next_http_ms,
        "next_status": next_status,
        "next_error": next_body.get("error") if isinstance(next_body, dict) else None,
        "next_turn_id": next_body["turns"][-1]["id"] if isinstance(next_body, dict) and next_body.get("turns") else None,
    }


async def wait_journal_pid(journal: Path, prev_lines: int, timeout=10.0):
    deadline = perf_counter() + timeout
    while perf_counter() < deadline:
        if journal.is_file():
            lines = [ln for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if len(lines) > prev_lines:
                return json.loads(lines[-1])["pid"], len(lines)
        await asyncio.sleep(0.05)
    raise TimeoutError("MCP child PID did not appear in journal")


async def run_pass(out_dir: Path, label: str, workers: int):
    pass_dir = out_dir / label
    pass_dir.mkdir(parents=True, exist_ok=True)
    journal_b = pass_dir / "mcp-journal.jsonl"
    result = {"label": label, "cpu_workers": workers, "checks": {}, "passed": False}

    async with cpu_load(workers) as load_info:
        async with fault_model_server() as (cloud_url, model_state):
            async with isolated_instance(pass_dir / "inst_a", cloud_url) as (base_a, moss_a), \
                       isolated_instance(pass_dir / "inst_b", cloud_url, mcp_journal=journal_b) as (base_b, moss_b):
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as client:

                    # --- Concurrent Scenario 1: Session A 429 retry + Session B slow MCP F42 SSE-cancel race ---
                    async def session_a_429_retry():
                        before_reqs = len(model_state["requests"])
                        t0 = perf_counter()
                        async with client.post(base_a + "/api/message", json={"text": "请做一次限流重试测试"}) as r:
                            assert r.status == 200
                            tid = (await r.json())["turns"][-1]["id"]
                        turn = await wait_turn(client, base_a, tid, {"replied", "error"})
                        elapsed_ms = round((perf_counter() - t0) * 1000, 1)
                        server_reqs = len(model_state["requests"]) - before_reqs
                        counted = turn.get("cloud_requests", {}).get("total")
                        ok = (turn["status"] == "replied" and "重试成功" in (turn.get("reply") or "")
                              and server_reqs == 2 and counted == 2)
                        return {"passed": ok, "elapsed_ms": elapsed_ms, "server_reqs": server_reqs,
                                "counted": counted, "status": turn["status"]}

                    async def session_b_mcp_sse_cancel():
                        # Trigger slow stdio MCP on Instance B
                        async with client.post(base_b + "/api/message", json={"text": "用 MCP 慢查询调用 延时读取"}) as r:
                            assert r.status == 200
                            tid = (await r.json())["turns"][-1]["id"]
                        pid, _ = await wait_journal_pid(journal_b, 0)
                        race = await sse_cancel_and_immediate_next(client, base_b, tid, "现在几点")
                        await asyncio.sleep(0.15)
                        pid_alive = psutil.pid_exists(pid)
                        next_ok = race["next_status"] == 200
                        next_turn = None
                        if next_ok and race["next_turn_id"]:
                            next_turn = await wait_turn(client, base_b, race["next_turn_id"], {"replied", "error"})
                        ok = (race["cancel_status"] == 200 and next_ok and not pid_alive
                              and next_turn is not None and next_turn["status"] == "replied")
                        return {"passed": ok, "child_pid": pid, "child_alive_after_cancel": pid_alive, **race}

                    r_a1, r_b1 = await asyncio.gather(session_a_429_retry(), session_b_mcp_sse_cancel())
                    result["checks"]["a_429_retry_outbound_match"] = r_a1
                    result["checks"]["b_f42_mcp_sse_cancel_immediate_next"] = r_b1

                    # --- Concurrent Scenario 2: Session A 429 cancel mid-backoff + Session B compound overview+MCP cancel ---
                    async def session_a_429_cancel():
                        before_reqs = len(model_state["requests"])
                        async with client.post(base_a + "/api/message", json={"text": "请做一次限流取消测试"}) as r:
                            assert r.status == 200
                            tid = (await r.json())["turns"][-1]["id"]
                        # Wait until 1st 429 has been sent and client is in 2s backoff
                        deadline = perf_counter() + 5
                        while len(model_state["requests"]) == before_reqs and perf_counter() < deadline:
                            await asyncio.sleep(0.02)
                        await asyncio.sleep(0.12)
                        race = await sse_cancel_and_immediate_next(client, base_a, tid, "限流取消后立即恢复")
                        await asyncio.sleep(0.3)
                        t_prev = await wait_turn(client, base_a, tid, {"cancelled", "replied", "error"})
                        t_next = await wait_turn(client, base_a, race["next_turn_id"], {"replied", "error"}) if race["next_turn_id"] else None
                        reqs_for_cancel = [req for req in model_state["requests"][before_reqs:] if "限流取消测试" in req["user"]]
                        counted_cancel = t_prev.get("cloud_requests", {}).get("total")
                        ok = (len(reqs_for_cancel) == 1 and counted_cancel == 1
                              and t_prev["status"] == "cancelled" and race["next_status"] == 200
                              and t_next is not None and t_next["status"] == "replied")
                        return {"passed": ok, "cancel_server_reqs": len(reqs_for_cancel),
                                "cancel_counted": counted_cancel, **race}

                    async def session_b_compound_mcp_cancel():
                        prev_lines = len([ln for ln in journal_b.read_text(encoding="utf-8").splitlines() if ln.strip()]) if journal_b.is_file() else 0
                        async with client.post(base_b + "/api/message",
                                               json={"text": "现在几点，顺便用 MCP 慢查询调用 延时读取"}) as r:
                            assert r.status == 200
                            tid = (await r.json())["turns"][-1]["id"]
                        pid, _ = await wait_journal_pid(journal_b, prev_lines)
                        race = await sse_cancel_and_immediate_next(client, base_b, tid, "现在几点")
                        await asyncio.sleep(0.15)
                        pid_alive = psutil.pid_exists(pid)
                        next_turn = await wait_turn(client, base_b, race["next_turn_id"], {"replied", "error"}) if race["next_status"] == 200 and race["next_turn_id"] else None
                        ok = (race["cancel_status"] == 200 and race["next_status"] == 200 and not pid_alive
                              and next_turn is not None and next_turn["status"] == "replied")
                        return {"passed": ok, "child_pid": pid, "child_alive_after_cancel": pid_alive, **race}

                    r_a2, r_b2 = await asyncio.gather(session_a_429_cancel(), session_b_compound_mcp_cancel())
                    result["checks"]["a_429_cancel_mid_backoff"] = r_a2
                    result["checks"]["b_f42_compound_overview_mcp_cancel"] = r_b2

                    # --- Scenario 3: Session A Truncated SSE + Immediate Recovery ---
                    async with client.post(base_a + "/api/message", json={"text": "请做一次截断流测试"}) as r:
                        assert r.status == 200
                        tid_trunc = (await r.json())["turns"][-1]["id"]
                    t_trunc = await wait_turn(client, base_a, tid_trunc, {"error", "replied"})
                    t0_rec = perf_counter()
                    async with client.post(base_a + "/api/message", json={"text": "截断后立即恢复"}) as r:
                        rec_status = r.status
                        tid_rec = (await r.json())["turns"][-1]["id"]
                    t_rec = await wait_turn(client, base_a, tid_rec, {"replied", "error"})
                    result["checks"]["a_truncated_sse_recovery"] = {
                        "passed": bool(t_trunc["status"] == "error" and "流中断" in (t_trunc.get("error") or "")
                                       and rec_status == 200 and t_rec["status"] == "replied"),
                        "trunc_status": t_trunc["status"],
                        "trunc_error": t_trunc.get("error"),
                        "recovery_status": rec_status,
                        "recovery_ms": round((perf_counter() - t0_rec) * 1000, 1),
                    }

                    # --- Scenario 4: Session A Stalled SSE + Cancel Before Late Frame + Recovery ---
                    model_state["stall_opened"].clear()
                    model_state["late_frame_gate"].clear()
                    model_state["late_frame_sent"].clear()
                    async with client.post(base_a + "/api/message", json={"text": "请做一次停顿迟到帧测试"}) as r:
                        assert r.status == 200
                        tid_stall = (await r.json())["turns"][-1]["id"]
                    await asyncio.wait_for(model_state["stall_opened"].wait(), timeout=8)
                    race_stall = await sse_cancel_and_immediate_next(client, base_a, tid_stall, "停顿取消后恢复")
                    model_state["late_frame_gate"].set()
                    await asyncio.sleep(0.15)
                    t_stall = await wait_turn(client, base_a, tid_stall, {"cancelled", "replied", "error"})
                    t_stall_next = await wait_turn(client, base_a, race_stall["next_turn_id"], {"replied", "error"}) if race_stall["next_turn_id"] else None
                    result["checks"]["a_stalled_cancel_before_late_frame"] = {
                        "passed": bool(t_stall["status"] == "cancelled"
                                       and "迟到的尾巴" not in (t_stall.get("reply") or "")
                                       and "迟到的尾巴" not in (t_stall.get("partial_reply") or "")
                                       and race_stall["next_status"] == 200
                                       and t_stall_next is not None and t_stall_next["status"] == "replied"),
                        "stall_status": t_stall["status"],
                        **race_stall,
                    }

                    # --- Scenario 5: Session A Pending Pydantic Draft Preserved & Verified Under Load ---
                    async with client.post(base_a + "/api/message",
                                           json={"text": "帮我保存一份本地草稿，内容逐字写：高负载故障恢复草稿核对清单"}) as r:
                        assert r.status == 200
                        tid_draft = (await r.json())["turns"][-1]["id"]
                    t_draft = await wait_turn(client, base_a, tid_draft, {"pending", "error"})
                    t0_app = perf_counter()
                    async with client.post(base_a + "/api/approve", json={"turn_id": tid_draft}) as r:
                        app_status = r.status
                    t_saved = await wait_turn(client, base_a, tid_draft, {"replied", "saved", "error"})
                    approve_ms = round((perf_counter() - t0_app) * 1000, 1)
                    receipt = t_saved.get("receipt") or {}
                    expected_sha = hashlib.sha256("高负载故障恢复草稿核对清单".encode("utf-8")).hexdigest()
                    # Duplicate approve must be idempotent
                    async with client.post(base_a + "/api/approve", json={"turn_id": tid_draft}) as r_dup:
                        dup_status = r_dup.status
                    draft_files = list((pass_dir / "inst_a/drafts").glob("*.txt"))
                    result["checks"]["a_draft_approve_verified"] = {
                        "passed": bool(t_draft["status"] == "pending" and app_status == 200 and dup_status == 200
                                       and receipt.get("sha256") == expected_sha and len(draft_files) == 1),
                        "approve_ms": approve_ms,
                        "draft_file_count": len(draft_files),
                        "sha_match": receipt.get("sha256") == expected_sha,
                    }

                    # --- Outbound Cloud Request Count Reconciliation ---
                    async with client.get(base_a + "/api/state") as r:
                        final_state_a = await r.json()
                    total_counted = sum((t.get("cloud_requests") or {}).get("total", 0) for t in final_state_a["turns"])
                    total_server = len(model_state["requests"])
                    result["checks"]["a_outbound_count_reconciliation"] = {
                        "passed": total_counted == total_server,
                        "total_counted": total_counted,
                        "total_server": total_server,
                    }

        samples = load_info["cpu_samples"]
        result["cpu_stats"] = {
            "samples": len(samples),
            "median_percent": round(median(samples), 1) if samples else None,
            "peak_percent": round(max(samples), 1) if samples else None,
        }
        result["passed"] = all(c["passed"] for c in result["checks"].values())
    return result


async def run_all(args):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = ROOT / "runs" / f"loaded-fault-recovery-{args.tag}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"tag": args.tag, "started_at": datetime.now().isoformat(timespec="seconds"), "passes": {}}

    print("[fault-qa] running idle pass...", flush=True)
    report["passes"]["idle"] = await run_pass(out_dir, "idle", 0)
    if not args.idle_only:
        print(f"[fault-qa] running loaded pass ({args.workers} CPU workers)...", flush=True)
        report["passes"]["loaded"] = await run_pass(out_dir, "loaded", args.workers)
    report["passed"] = all(p["passed"] for p in report["passes"].values())

    result_path = out_dir / "result.json"
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result_path, "passed=", report["passed"], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-server", type=Path)
    parser.add_argument("--tag", default="run")
    parser.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    parser.add_argument("--idle-only", action="store_true")
    args = parser.parse_args()
    if args.fixture_server:
        run_fixture_mcp(args.fixture_server)
        return
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
