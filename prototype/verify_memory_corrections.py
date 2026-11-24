import argparse
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from aiohttp import web

from browser_lab import BrowserServer, ROOT


async def run(archive, port):
    folder = ROOT / "runs" / ("memory-corrections-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    data = folder / "data"
    data.mkdir(parents=True)
    results = {"passed": False, "archive": archive.name, "checks": [], "application_pids": []}

    def record(check, **details):
        results["checks"].append({"check": check, **details})
        (folder / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(check, flush=True)

    report = json.loads((archive / "result.json").read_text(encoding="utf-8"))
    assert report["passed"] and archive.parent.resolve() == (ROOT / "runs").resolve()
    old_value = report["second_value"]
    sample = next(e for e in report["events"] if e["name"] == "recall_after_restart")
    old_file = (archive / "data/session.sqlite3").resolve()
    with sqlite3.connect(old_file.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(data / "session.sqlite3") as target:
        assert len(list(source.execute("PRAGMA table_info(memories)"))) == 3
        source.backup(target)
    requests = []

    async def reject(request):
        requests.append((request.path, await request.json()))
        return web.json_response({"error": "隔离验收只收集请求，不提供模型或索引"}, status=503)

    listener = web.Application()
    listener.router.add_post("/{path:.*}", reject)
    runner = web.AppRunner(listener)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host = "http://127.0.0.1:" + str(runner.addresses[0][1])
    server = BrowserServer(folder, port, model_url=host + "/v1", memory_url=host)

    async def post(verb, body, expected=200):
        code, state = await server.post("/api/" + verb, body)
        assert code == expected, (verb, code, state.get("error"))
        return state

    async def say(text, expected=200):
        return await post("message", {"text": text}, expected)

    async def model_input(text):
        start = len(requests)
        state = await say(text)
        for _ in range(200):
            if state["turns"][-1]["status"] == "error":
                break
            await asyncio.sleep(.05)
            state = await server.state()
        assert state["turns"][-1]["status"] == "error"
        sent = [body for path, body in requests[start:] if path == "/v1/chat/completions"]
        assert sent, state["turns"][-1]
        return json.dumps(sent[-1]["messages"], ensure_ascii=False), state["turns"][-1]

    try:
        await server.start()
        results["application_pids"].append(server.pid)
        initial = await server.state()
        assert initial["model_id"] == "local"
        assert all(m["source"] == "legacy" and m["updated_at"] == m["created_at"] for m in initial["memories"])
        known = next(m for m in initial["memories"] if old_value in m["text"])
        await post("switch-chat", {"id": sample["chat_id"]})
        question = "用一句话解释为什么纸张可以折叠。"
        before, _ = await model_input(question)
        assert old_value in before
        excluded, turn = await model_input("不要参考我的记忆，请简单解释为什么纸张可以折叠。")
        assert old_value not in excluded and not turn.get("memory_ids")
        record("旧库无损升级，真实历史回复在更正前可用于后文", migrated=len(initial["memories"]))

        changed = "我的助手测试昵称是 海雾试验甲"
        state = await say("修改记忆：" + known["text"] + " → " + changed)
        updated = next(m for m in state["memories"] if m["id"] == known["id"])
        assert updated["text"] == changed and updated["source"] == "chat"
        assert updated["source_chat"] == sample["chat_id"] and updated["source_turn"] == state["turns"][-1]["id"]
        assert updated["updated_at"] > updated["created_at"] and state["memory_index_error"]
        for _ in range(24):
            await say("查看记忆")
        after, turn = await model_input(question)
        assert old_value not in after and changed not in after and not turn.get("memory_ids")
        relevant, turn = await model_input("告诉我我的助手测试昵称。")
        assert changed in relevant and old_value not in relevant and known["id"] in turn["memory_ids"]
        await say("撤回记忆：" + known["id"])
        assert known["id"] not in {m["id"] for m in (await server.state())["memories"]}
        record("按原文更正、按 ID 撤回，24 轮后索引故障时回退 SQLite，检查实际模型请求", history_rows=len((await server.state())["turns"]))

        first = await post("new-chat", {})
        rail_chat = first["chat_id"]
        rail = await say("记住：我坐高铁喜欢靠窗，预订标记青杉")
        rail_id = rail["turns"][-1]["memory_id"]
        absent = (await say("乘飞机时，根据我的选座习惯，座位怎么选？"))["turns"][-1]
        assert absent["source"] == "memory" and not absent["memory_ids"] and "没有保存" in absent["reply"]
        air_chat = (await post("new-chat", {}))["chat_id"]
        air = await say("记住：我坐飞机喜欢靠窗")
        air_id = air["turns"][-1]["memory_id"]
        assert rail_id != air_id
        for chat, key, topic in [(rail_chat, rail_id, "高铁"), (air_chat, air_id, "飞机")]:
            await post("switch-chat", {"id": chat})
            revised = await say("以后改成靠过道")
            assert key == revised["turns"][-1]["memory_id"]
            recalled = await say("我的" + topic + "座位偏好是什么？")
            assert recalled["turns"][-1]["memory_ids"] == [key] and "靠过道" in recalled["turns"][-1]["reply"]
            if chat == rail_chat:
                assert "预订标记青杉" in recalled["turns"][-1]["reply"]
                synonym = await say("查记忆：买火车票时应该替我挑哪一侧的位置？")
                assert synonym["turns"][-1]["memory_ids"][0] == key
        await post("new-chat", {})
        ambiguous = await say("以后改成靠窗", expected=409)
        assert "指定" in ambiguous["error"]
        both = (await say("坐高铁和飞机，按我的座位偏好给一个选座建议。"))["turns"][-1]
        assert set(both["memory_ids"]) == {rail_id, air_id} and "高铁座位" in both["reply"] and "飞机座位" in both["reply"]
        assert [m["id"] for m in (await server.state())["memories"] if m["topic"] in ("seat.rail", "seat.air")] == [rail_id, air_id]
        record("同一句更正在高铁和飞机对话分别更新对应偏好，新对话有歧义时不覆盖")

        music = await say("记住：我的工作音乐偏好是钢琴曲")
        music_id = music["turns"][-1]["memory_id"]
        await say("记住：我的睡前音乐偏好是白噪声")
        updated_music = await say("记住：我的工作音乐偏好是室内乐")
        assert updated_music["turns"][-1]["memory_id"] == music_id
        assert sum(m["topic"] == "preference.工作音乐" for m in updated_music["memories"]) == 1
        assert any("白噪声" in m["text"] for m in updated_music["memories"])
        scoped = await say("修改记忆：" + music_id + " -> 工作时我更喜欢弦乐四重奏")
        assert scoped["turns"][-1]["memory_id"] == music_id
        await say("撤回记忆：工作时我更喜欢弦乐四重奏")
        assert not any(m["id"] == music_id for m in (await server.state())["memories"])
        record("非座位偏好替换，保留不同场景，支持按 ID 修改任意原文及按原文撤回")

        expiry = (datetime.now() + timedelta(seconds=3)).replace(microsecond=0)
        short = await post("remember", {"text": "短期试验门牌是墨松（有效至 " + expiry.isoformat(sep=" ") + "）"})
        marker = next(m for m in short["memories"] if "短期试验门牌" in m["text"])
        assert marker["source"] == "panel" and datetime.fromisoformat(marker["expires_at"]).timestamp() == expiry.timestamp()
        recall = await say("查记忆：短期试验门牌")
        assert marker["id"] in recall["turns"][-1]["memory_ids"]
        await asyncio.sleep(max(0, expiry.timestamp() - datetime.now().timestamp()) + .05)
        expired = await say("查记忆：短期试验门牌")
        assert not expired["turns"][-1]["memory_ids"] and marker["id"] not in {m["id"] for m in expired["memories"]}
        await post("remember", {"text": "今日临时偏好（有效至今天）"})
        await post("remember", {"text": "非法期限（有效至 2026-99-99）"}, expected=409)
        record("真实时间跨过截止，面板状态和回忆均排除过期条目", cutoff=marker["expires_at"])

        await server.stop()
        await server.start()
        results["application_pids"].append(server.pid)
        assert len(set(results["application_pids"])) == 2
        after_restart = await post("switch-chat", {"id": sample["chat_id"]})
        assert any(old_value in (row.get("reply") or "") for row in after_restart["turns"])
        final, turn = await model_input("告诉我我的助手测试昵称。")
        assert old_value not in final and changed not in final and known["id"] not in turn.get("memory_ids", [])
        mirror = next(body["items"] for path, body in reversed(requests) if path in ("/v1/sync", "/v1/retrieve"))
        assert {item["id"] for item in mirror} == {item["id"] for item in after_restart["memories"]}
        assert marker["id"] not in {m["id"] for m in after_restart["memories"]}
        assert "靠过道" in (await say("我的高铁座位偏好是什么？"))["turns"][-1]["reply"]
        old_count = len((await server.state())["memories"])
        await model_input("我的普通聊天内容是橙石无忧，请解释这个词。")
        assert len((await server.state())["memories"]) == old_count
        results["passed"] = True
        record("真实进程重启，UI 保留历史，模型请求排除旧值、撤回值和过期值，普通聊天不会写入")
    finally:
        try:
            await server.stop()
        finally:
            await runner.cleanup()
    print(folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=ROOT / "runs/memory-20261120-120855")
    parser.add_argument("--port", type=int, default=18783)
    args = parser.parse_args()
    asyncio.run(run(args.archive, args.port))
