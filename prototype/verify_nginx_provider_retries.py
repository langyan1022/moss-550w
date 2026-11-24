import argparse
import asyncio
from contextlib import AsyncExitStack, closing
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
from time import monotonic, perf_counter

import aiohttp
from aiohttp import web
from pydantic import SecretStr

from browser_lab import BrowserServer, ROOT
from core import Moss
from model_catalog import allowed
from serve import application


QUESTION = "用一句话解释为什么雨后会出现彩虹。"
SAMPLE = "周一核对书目"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def nginx_conf(port, relay, *, window_only=False):
    locations = []
    zones = []
    limits = (("short429", 1), ("long429", 30), ("cancel429", 2), ("twice429", 0))
    if window_only:
        limits = (("windowcontrol", 2), ("windowcancel", 2), ("windowclose", 2))
    for name, delay in limits:
        zones.append(f"limit_req_zone $server_addr zone={name}:1m rate=1r/s;")
        guard = f"limit_req zone={name}; limit_req_status 429;"
        locations.append(f"location = /prime/{name} {{ {guard} alias html/prime.txt; }}")
        locations.append(f"location = /{name}/chat/completions {{ {guard} add_header Retry-After {delay} always; "
                         f"proxy_pass http://127.0.0.1:{relay}; }}")
    return """
daemon off;
master_process off;
error_log logs/error.log error;
pid logs/nginx.pid;
events { worker_connections 64; }
http {
    log_format audit '$request_method $uri $status $limit_req_status $limit_conn_status';
    access_log logs/access.log audit;
    limit_conn_zone $server_addr zone=connections:1m;
    sendfile off;
    """ + "\n".join(zones) + f"""
    server {{ listen 127.0.0.1:{port}; server_name localhost;
        location = /ready {{ alias html/prime.txt; }}
        location = /hold {{ alias html/hold.bin; limit_conn connections 1; limit_rate 1k; }}
        location = /short503/chat/completions {{ limit_conn connections 1; limit_conn_status 503;
            add_header Retry-After 1 always; proxy_pass http://127.0.0.1:{relay}; }}
        """ + "\n".join(locations) + "\n}\n}\n"


def copy_pending(source, data):
    data.mkdir(parents=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        rows = src.execute("SELECT doc FROM turns").fetchall()
        assert len(rows) == 1
        row = json.loads(rows[0][0])
        assert row["status"] == "pending" and row["preview"] == SAMPLE and row["model_id"] == "deepseek"
        assert not src.execute("SELECT 1 FROM memories LIMIT 1").fetchone()
        with closing(sqlite3.connect(data / "session.sqlite3")) as dst:
            src.backup(dst)
    return row["id"]


async def window_retry(case, root, base, http):
    data = root / case
    api_port = free_port()
    endpoint = BrowserServer(root, api_port)
    stopped, limited = asyncio.Event(), asyncio.Event()
    statuses = []
    provider = {"windowcontrol":"aliyun", "windowcancel":"deepseek", "windowclose":"kimi"}[case]
    async with AsyncExitStack() as stack:
        moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
        stack.push_async_callback(moss.close)
        moss.cloud[provider] = replace(moss.cloud[provider], url=f"{base}/{case}", secret=SecretStr("qa-only"))
        await moss.select_model(provider)

        async def observe(response):
            assert str(response.request.url) == f"{base}/{case}/chat/completions"
            statuses.append(response.status_code)
            if response.status_code == 429:
                assert response.headers["Retry-After"] == "2"
                limited.set()

        moss.model_client()._client.event_hooks["response"].append(observe)
        app = application(moss, lambda: "standby", data, api_port, "window-qa", stopped)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", api_port).start()
        chat = (await endpoint.state())["chat_id"]
        status, _ = await endpoint.post("/api/message", {"chat_id":chat, "text":"读取窗口文字"})
        assert status == 200
        await moss.job
        origin = (await endpoint.state())["turns"][-1]
        assert origin["status"] == "replied" and origin["context_action"] == "screen_text"
        assert origin["cloud_requests"] == {"complete":True, "total":0, "by_provider":{}}
        selected = {"turn_id":origin["id"], "model_id":provider, "excerpt":"Public library: Room 27."}
        async with http.get(f"{base}/prime/{case}") as prime:
            assert prime.status == 200 and await prime.text() == "ready"
        status, state = await endpoint.post("/api/message", {
            "chat_id":chat, "text":"图书馆在几号房间？", "window_context":selected})
        assert status == 200
        turn = state["turns"][-1]
        assert turn["source"] == "window_question" and turn["status"] == "thinking"
        job = moss.job
        await asyncio.wait_for(limited.wait(), 5)
        await asyncio.sleep(.10)
        assert statuses == [429] and not job.done()
        assert (await endpoint.state())["turns"][-1]["cloud_requests"] == {
            "complete": True, "total": 1, "by_provider": {provider: 1}}
        start = perf_counter()
        result = {"turn_id":turn["id"], "statuses":statuses}
        if case == "windowclose":
            status, _ = await endpoint.post("/api/shutdown", {"token":"window-qa"})
            assert status == 200
            await asyncio.wait_for(stopped.wait(), 2)
        else:
            if case == "windowcancel":
                status, state = await endpoint.post("/api/cancel", {"turn_id":turn["id"]})
                assert status == 200 and state["turns"][-1]["status"] == "cancelled"
            await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), 6)
            if case == "windowcancel":
                status, state = await endpoint.post("/api/message", {"chat_id":chat, "text":"查看我的便签"})
                follow = state["turns"][-1]
                assert status == 200 and follow["status"] == "replied" and follow["source"] == "notes", follow
                assert follow["cloud_requests"] == origin["cloud_requests"]
                result["next_local_turn"] = follow["id"]
    result.update(job_finished=job.done(), job_cancelled=job.cancelled(),
                  elapsed_ms=round((perf_counter() - start) * 1000, 1))
    await asyncio.sleep(max(0, 2.15 - (perf_counter() - start)))
    reopened = Moss(data, "http://127.0.0.1:9/v1", "unused")
    try:
        restored = next(entry for entry in reopened.state()["turns"] if entry["id"] == turn["id"])
        result["cloud_requests_after_restart"] = restored["cloud_requests"]
        assert restored["cloud_requests"] == {"complete":True, "total":len(statuses),
                                               "by_provider":{provider:len(statuses)}}
    finally:
        await reopened.close()
    return result


async def run(args):
    root = ROOT / "runs" / datetime.now().strftime("nginx-provider-%Y%m%d-%H%M%S-%f")
    for directory in ("logs", "html", "temp"):
        (root / directory).mkdir(parents=True)
    (root / "html/prime.txt").write_text("ready", encoding="utf-8")
    (root / "html/hold.bin").write_bytes(bytes(128 * 1024))
    report = {"passed":False, "quota_source":"local NGINX limit_req and limit_conn", "cases":{}, "cloud_outbound":[]}
    exe = args.nginx.resolve(strict=True)
    report["nginx"] = subprocess.run([str(exe), "-v"], capture_output=True, encoding="utf-8",
                                      check=True, creationflags=subprocess.CREATE_NO_WINDOW).stderr.strip()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=40)) as http:
        official = None
        async def upstream(req):
            case = req.match_info["case"]
            payload = await req.json()
            assert req.headers.get("Authorization") == "Bearer qa-only"
            if (case not in {"short429", "short503"} or len(report["cloud_outbound"]) >= 2
                    or any(row["case"] == case for row in report["cloud_outbound"])):
                raise web.HTTPForbidden()
            assert official and allowed("deepseek", official.url) and official.secret.get_secret_value()
            assert payload["model"] == official.model
            assert (QUESTION if case == "short429" else SAMPLE) in json.dumps(payload["messages"], ensure_ascii=False)
            entry = {"case":case, "official_host":"api.deepseek.com"}
            report["cloud_outbound"].append(entry)
            payload["max_tokens"] = min(160, payload.get("max_tokens", 160))
            async with http.post(official.url + "/chat/completions", json=payload, allow_redirects=False,
                                 headers={"Authorization": "Bearer " + official.secret.get_secret_value()}) as response:
                entry["status"] = response.status
                return web.Response(status=response.status, body=await response.read(),
                                    headers={"Content-Type": response.headers["Content-Type"]})

        origin = web.Application()
        if args.turn_counts:
            from verify_cloud_request_counts import protocol_receipt
            origin.router.add_post("/short429/chat/completions", protocol_receipt)
        elif args.allow_paid:
            origin.router.add_post("/{case}/chat/completions", upstream)
        else:
            # 重试通过限流后，静态文件服务对 POST 返回 HTTP 405。
            origin.router.add_static("/", root / "html")
        relay = web.AppRunner(origin, access_log=None)
        await relay.setup()
        site = web.TCPSite(relay, "127.0.0.1", 0)
        await site.start()
        relay_port = relay.addresses[0][1]
        (root / "nginx.conf").write_text(nginx_conf(port, relay_port, window_only=args.window_only), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(str(exe), "-p", str(root) + "/", "-c", "nginx.conf",
                    cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            async with asyncio.timeout(10):
                while True:
                    if proc.returncode is not None:
                        raise RuntimeError((root / "logs/error.log").read_text(encoding="utf-8"))
                    try:
                        async with http.get(base + "/ready") as r:
                            if r.status == 200 and await r.text() == "ready":
                                break
                    except aiohttp.ClientConnectionError:
                        pass
                    await asyncio.sleep(.05)

            provider_cases = ("long429", "cancel429", "twice429", "short429", "short503")
            if args.turn_counts:
                from verify_cloud_request_counts import verify_counts
                report["turn_counts"] = await verify_counts(root, base, http, args.pending)
                provider_cases = ()
            elif args.window_only:
                for case in ("windowcontrol", "windowcancel", "windowclose"):
                    report["cases"][case] = await window_retry(case, root, base, http)
                provider_cases = ()
            for case in provider_cases:
                draft = case == "short503"
                data = root / case
                key = copy_pending(args.pending, data) if draft else None
                moss = Moss(data, "http://127.0.0.1:9/v1", "unused", draft_model_reply=draft)
                attempts, statuses, commits = [], [], []
                first_response = asyncio.Event()
                held = None
                try:
                    provider = args.approval_provider if draft else "deepseek"
                    official = moss.cloud[provider]
                    previous = moss.state()["turns"][0]["cloud_requests"] if draft else None
                    if args.allow_paid:
                        assert official.public()["available"], "DeepSeek 已配置凭据不可用"
                    if provider in moss.clients:
                        await moss.clients.pop(provider).close()
                    moss.cloud[provider] = replace(official, url=base + "/" + case, secret=SecretStr("qa-only"))
                    await moss.select_model(provider)
                    sdk = moss.model_client()
                    moss.agent = moss._agent_for(provider)
                    async def before(request):
                        nonlocal held
                        if not attempts:
                            if draft:
                                held = await http.get(base + "/hold")
                                assert held.status == 200 and await held.content.read(1) == bytes(1)
                            else:
                                async with http.get(base + "/prime/" + case) as prime:
                                    assert prime.status == 200 and await prime.text() == "ready"
                        attempts.append({"elapsed_s": round(monotonic()-start,3),
                                         "body_sha256":hashlib.sha256(request.content).hexdigest()})
                        if draft:
                            row = moss._get(key)
                            receipt = row["receipt"]
                            path = moss.drafts / receipt["filename"]
                            assert path.read_text(encoding="utf-8") == SAMPLE
                            meta = path.stat()
                            commits.append((row["call_id"], receipt["sha256"], meta.st_ino, meta.st_mtime_ns))
                    async def after(response):
                        statuses.append(response.status_code)
                        if held and response.status_code == 503:
                            held.close()
                        first_response.set()
                    sdk._client.event_hooks["request"].append(before)
                    sdk._client.event_hooks["response"].append(after)
                    start = monotonic()
                    if draft:
                        await moss.approve(key)
                    else:
                        key = await moss.message(QUESTION)
                    job = moss.job
                    if case == "cancel429":
                        await asyncio.wait_for(first_response.wait(), timeout=10)
                        await moss.cancel(key)
                    await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), timeout=50)
                    if case == "cancel429":
                        await asyncio.sleep(2.1)
                    row = moss._get(key)
                    expected = {"long429":[429], "cancel429":[429], "twice429":[429,429],
                                "short429":[429,200 if args.allow_paid else 405],
                                "short503":[503,200 if args.allow_paid else 405]}[case]
                    record = report["cases"][case] = {"statuses":statuses, "attempts":attempts,
                            "turn_status":row["status"], "message":row.get("error"), "reply":row.get("reply")}
                    by_provider = dict(previous["by_provider"]) if previous else {}
                    by_provider[provider] = by_provider.get(provider, 0) + len(expected)
                    usage = moss.state()["turns"][-1]["cloud_requests"]
                    assert usage == {"complete":previous["complete"] if previous else not draft,
                                     "total":sum(by_provider.values()), "by_provider":by_provider}, usage
                    record["cloud_requests"] = usage
                    assert statuses == expected and len(attempts) == len(expected), record
                    assert len({attempt["body_sha256"] for attempt in attempts}) == 1, record
                    if case in {"short429","short503"}:
                        assert attempts[1]["elapsed_s"] - attempts[0]["elapsed_s"] >= .95
                    if case == "cancel429":
                        assert row["status"] == "cancelled", record
                    elif case in {"long429","twice429"}:
                        assert row["status"] == "error" and "429" in row["error"], record
                    elif args.allow_paid:
                        assert row["reply"] and row["status"] == "replied", record
                        if not draft:
                            assert "虹" in row["reply"] or "光" in row["reply"], record
                    if draft:
                        assert len(commits) == 2 and commits[0] == commits[1] and len(list(moss.drafts.iterdir())) == 1
                        await moss.approve(key)
                        assert len(attempts) == 2
                        await moss.close()
                        moss = Moss(data, "http://127.0.0.1:9/v1", "unused", draft_model_reply=True)
                        await moss.approve(key)
                        recovered = moss._get(key)
                        assert not moss.job and recovered["receipt"] == row["receipt"]
                        assert moss.state()["turns"][-1]["cloud_requests"] == usage
                        record.update(previous_cloud_requests=previous, proposal_provider=row["model_id"],
                                      approval_provider=provider, restarted_turn_id=key)
                        path = moss.drafts / recovered["receipt"]["filename"]
                        assert len(list(moss.drafts.iterdir())) == 1 and path.stat().st_mtime_ns == commits[0][3]
                        record["saved"] = {"files_after_restart_and_retry":1, "sha256":commits[0][1],
                                           "same_call_id_file_id_and_mtime":True}
                finally:
                    if held:
                        held.close()
                    await moss.close()
            assert len(report["cloud_outbound"]) == (2 if args.allow_paid else 0)
            access = (root / "logs/access.log").read_text(encoding="utf-8")
            for case, entry in report["cases"].items():
                lines = [line for line in access.splitlines() if line.startswith("POST /" + case + "/chat/completions ")]
                assert [int(line.split()[2]) for line in lines] == entry["statuses"], lines
                assert "REJECTED" in lines[0], lines
                entry["nginx_access"] = lines
            if args.window_only:
                assert report["cases"]["windowcontrol"]["statuses"] == [429, 405], report["cases"]
                for case in ("windowcancel", "windowclose"):
                    assert report["cases"][case]["statuses"] == [429], report["cases"]
                    assert report["cases"][case]["job_cancelled"]
            report["passed"] = True
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            await relay.cleanup()
            (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(root, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nginx", type=Path, required=True)
    parser.add_argument("--pending", type=Path, help="只含指定虚构草稿的既有 DeepSeek QA 快照")
    parser.add_argument("--window-only", action="store_true", help="仅在回环地址验证窗口问答取消及关闭服务")
    parser.add_argument("--turn-counts", action="store_true", help="核对本机实际 HTTP POST 与每轮请求次数")
    parser.add_argument("--approval-provider", choices=("deepseek", "aliyun", "kimi"), default="deepseek",
                        help="回环测试中处理草稿确认的供应方")
    parser.add_argument("--allow-paid", action="store_true", help="仅允许两条隔离测试输入转发到已配置的官方 DeepSeek")
    args = parser.parse_args()
    if (args.window_only or args.turn_counts) and args.allow_paid:
        parser.error("这组验证仅使用本机 NGINX。")
    if args.window_only and args.turn_counts:
        parser.error("请单独运行窗口问答或每轮请求计数。")
    if args.allow_paid and args.approval_provider != "deepseek":
        parser.error("跨供应方的确认测试仅使用本机 NGINX。")
    if not args.window_only and not args.pending:
        parser.error("草稿测试需要 --pending。")
    asyncio.run(run(args))
