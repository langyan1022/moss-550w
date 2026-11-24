"""用独立 SQLite 服务检查实际回给用户的记忆原文与 ID。允许未覆盖的改写如实计为漏检。"""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
from time import perf_counter

from aiohttp import web
from browser_lab import BrowserServer, ROOT


async def verify_prompt_boundary(server, stored):
    requests = []

    async def reject(request):
        requests.append((request.path, await request.json()))
        return web.json_response({"error": "隔离记录端未启用推理"}, status=503)

    app = web.Application()
    app.router.add_post("/{rest:.*}", reject)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        await server.stop()
        server.memory_url = "http://127.0.0.1:" + str(runner.addresses[0][1])
        server.model_url = server.memory_url + "/v1"
        await server.start()
        checks = []
        for query, expect in [("我记下的 NX-2040 是什么？", "archive"), ("我的 NX-2041 是什么？", None),
                              ("请给我科普一下自行车内胎的制造原理。", None)]:
            assert (await server.post("/api/new-chat", {}))[0] == 200
            start = len(requests)
            code, state = await server.post("/api/message", {"text": query})
            assert code == 200
            async with asyncio.timeout(10):
                while state["turns"][-1]["status"] == "thinking":
                    await asyncio.sleep(.025)
                    state = await server.state()
            sent = requests[start:]
            messages = [body["messages"] for path, body in sent if path.endswith("/chat/completions")]
            assert messages, (query, state["turns"][-1])
            prompt = json.dumps(messages[-1], ensure_ascii=False)
            present = [label for label, text in stored.items() if text in prompt]
            index_calls = sum(path.endswith("/retrieve") for path, _ in sent)
            assert present == ([expect] if expect else []) and index_calls == 0, (query, present, index_calls)
            checks.append({"query": query, "sent_facts": present, "index_requests": index_calls})
        return checks
    finally:
        await runner.cleanup()


async def run():
    args = argparse.ArgumentParser()
    args.add_argument("--baseline", action="store_true", help="记录当前表现，用于有变更前的比较")
    args.add_argument("--supplement", type=Path, help="单独重建库，给由其他人封存的新题计分")
    options = args.parse_args()
    fixture = options.supplement or ROOT / "verify_memory_holdout_cases.json"
    inputs = json.loads(fixture.read_text(encoding="utf-8"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = ROOT / "runs" / f"memory-holdout-{stamp}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = BrowserServer(folder, port, model_url="http://127.0.0.1:1/v1")
    report = {"fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(), "cases": [], "pids": []}

    async def chat(text):
        status, view = await server.post("/api/message", {"text": text})
        assert status == 200, (text, view.get("error"))
        turn = view["turns"][-1]
        assert turn["status"] == "replied" and turn["source"] == "memory", turn
        return turn

    async def recall(query):
        return await chat("查记忆：" + query)

    await server.start()
    try:
        report["pids"].append(server.pid)
        assert not (await server.state())["memories"]
        keys = {}
        for label, fact in inputs["facts"].items():
            turn = await chat("记住：" + fact)
            keys[turn["memory_id"]] = label
        assert len(keys) == len(inputs["facts"])
        for kind, cases in inputs["sets"].items():
            for query, expected in cases:
                start = perf_counter()
                turn = await recall(query)
                labels = [keys[key] for key in turn["memory_ids"]]
                assert all(inputs["facts"][label] in turn["reply"] for label in labels)
                report["cases"].append({"kind": kind, "query": query, "expected": expected, "actual": labels,
                                        "elapsed_ms": round(1000 * (perf_counter() - start), 2)})
        if not options.supplement:
            target = next(key for key, label in keys.items() if label == "project")
            revised = "我手上代号 NX-205 的项目是屋顶照明。"
            edit = await chat("修改记忆：" + target + " → " + revised)
            assert edit["memory_id"] == target
            await server.stop()
            await server.start()
            report["pids"].append(server.pid)
            old = await recall("我手上编号 NX-204 是做什么的？")
            current = await recall("我手上编号 NX-205 是做什么的？")
            await chat("撤回记忆：" + target)
            await server.stop()
            await server.start()
            report["pids"].append(server.pid)
            gone = await recall("我手上编号 NX-205 是做什么的？")
            other = await recall("我手上编号 NX-2040 是做什么的？")
            report["integrity"] = {"corrected_old_absent": not old["memory_ids"], "updated_value_present":
                                   current["memory_ids"] == [target] and revised in current["reply"],
                                   "removed_absent": not gone["memory_ids"],
                                   "neighbor_preserved": [keys[key] for key in other["memory_ids"]] == ["archive"]}
            if not options.baseline:
                report["automatic_prompt_checks"] = await verify_prompt_boundary(server, inputs["facts"])
                report["pids"].append(server.pid)
    finally:
        await server.stop()
    report["summary"] = {}
    for group in inputs["sets"]:
        rows = [r for r in report["cases"] if r["kind"] == group]
        report["summary"][group] = {"exact": sum(r["expected"] == r["actual"] for r in rows), "total": len(rows),
                                    "irrelevant_facts_returned": sum(len(set(r["actual"]) - set(r["expected"])) for r in rows)}
    artifact = folder / "result.json"
    artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(artifact)
    print(json.dumps(report["summary"], ensure_ascii=False), report.get("integrity", {}))
    if not (options.baseline or options.supplement):
        assert all(report["integrity"].values())
        assert all(r["expected"] == r["actual"] for r in report["cases"] if r["kind"] != "rephrase")
        assert not any(set(r["actual"]) - set(r["expected"]) for r in report["cases"])
        assert report["summary"]["rephrase"]["exact"] >= 7


if __name__ == "__main__":
    asyncio.run(run())
