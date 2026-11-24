import asyncio
from datetime import datetime
import json
import secrets

from browser_lab import BrowserServer, ROOT


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("agent-providers-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18330)
    transcript = {"passed": False, "turns": []}
    code = "M-" + secrets.token_hex(3).upper()

    async def change(action, **body):
        status, state = await server.post("/api/" + action, body)
        if status != 200:
            raise RuntimeError(f"{action} 返回 {status}，{state.get('error')}")
        return state

    async def turn(provider, prompt, expected):
        state = await change("message", text=prompt)
        ident = state["turns"][-1]["id"]
        async with asyncio.timeout(180):
            while True:
                row = (await server.state())["turns"][-1]
                if row["status"] in {expected, "error", "pending", "replied"}:
                    break
                await asyncio.sleep(.15)
        transcript["turns"].append({k: row.get(k) for k in ("id", "model_id", "model", "status", "reply",
                                                             "error", "call_id", "preview", "memory_ids")})
        assert row["id"] == ident and row["status"] == expected and row["model_id"] == provider
        return row

    await server.start()
    try:
        await change("remember", text="我的测试代号是 " + code)
        for provider in ("deepseek", "aliyun", "kimi"):
            await change("select-model", id=provider)
            await change("new-chat")
            answer = await turn(provider, "我的测试代号是什么？只写代号。", "replied")
            assert code in answer["reply"]
        for provider in ("kimi", "aliyun", "deepseek"):
            await change("select-model", id=provider)
            await change("new-chat")
            draft = await turn(provider, "帮我保存一份本地草稿，内容逐字写：独立连通测试", "pending")
            assert draft["preview"] == "独立连通测试"
            if provider != "deepseek":
                await change("cancel", turn_id=draft["id"])
        selected = await change("select-model", id="kimi")
        assert selected["turns"][-1]["status"] == "pending"
        await server.stop(hard=True)
        await server.start()
        view = await server.state()
        assert view["model_id"] == "kimi" and view["turns"][-1]["call_id"] == draft["call_id"]
        await change("approve", turn_id=draft["id"])
        async with asyncio.timeout(180):
            while True:
                confirmed = (await server.state())["turns"][-1]
                if confirmed["status"] not in {"saving", "thinking"}:
                    break
                await asyncio.sleep(.2)
        transcript["after_restart"] = {k: confirmed.get(k) for k in ("id", "call_id", "status", "approval_model", "receipt", "reply", "error")}
        assert confirmed["receipt"]["text"] == draft["preview"] and confirmed["status"] == "replied"
        assert len(list((server.data / "drafts").glob("*.txt"))) == 1
        transcript["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
