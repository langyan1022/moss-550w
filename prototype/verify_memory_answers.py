import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys

from aiohttp import ClientSession
from browser_lab import BrowserServer, ROOT


def assess(folder):
    artifact = folder / "result.json"
    result = json.loads(artifact.read_text(encoding="utf-8"))
    seat, other, removed = result["replies"]
    letter = re.search(r"\b[A-F]\b", seat["reply"])
    result["quality"] = {
        "uses_updated_preference": "过道" in seat["reply"] and bool(seat["selected"]),
        "seat_advice_supported": not letter or ("C" in seat["reply"] and "D" in seat["reply"]),
        "unrelated_answer": not other["selected"] and "折射" in other["reply"],
        "unknown_after_revocation": not removed["selected"] and "没有" in removed["reply"] and "过道" not in removed["reply"],
    }
    if not result["quality"]["seat_advice_supported"]:
        result["quality_note"] = "给出了 A/F 作为靠过道建议。中国铁路公布的常见二等座布局为 A/F 靠窗、C/D 靠过道。"
        result["review_source"] = "https://m.thepaper.cn/newsDetail_forward_30690101"
    result["passed"] = all(result["quality"].values())
    artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result["passed"]


async def run():
    folder = ROOT / "runs" / ("chat-provider-memory-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    server = BrowserServer(folder, 18791)
    result = {"provider": "deepseek", "passed": False, "replies": [], "pids": []}
    async def start(resume=False):
        command = [sys.executable, "-u", str(ROOT / "verify_chat_providers.py"), str(folder), "--serve", "--port",
                   str(server.port), "--paid-cap", "3", "--model-url", "http://127.0.0.1:18799/v1"]
        if resume:
            command.append("--resume-server")
        server.proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
        owner_file = server.data / "instance.json"
        async with asyncio.timeout(45):
            while not owner_file.is_file():
                if server.proc.poll() is not None:
                    raise RuntimeError("隔离应用启动失败")
                await asyncio.sleep(.1)
        owner = json.loads(owner_file.read_text(encoding="utf-8"))
        async with ClientSession() as client:
            async with client.get(server.url + "/api/health") as response:
                health = await response.json()
        assert owner["port"] == server.port and owner["pid"] == health["pid"]
        server.pid = owner["pid"]
        result["pids"].append(server.pid)

    def save():
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    async def post(verb, **body):
        code, state = await server.post("/api/" + verb, body)
        assert code == 200, (verb, state.get("error"))
        return state

    async def ask(text):
        state = await post("message", text=text)
        async with asyncio.timeout(130):
            while state["turns"][-1]["status"] == "thinking":
                await asyncio.sleep(.12)
                state = await server.state()
        turn = state["turns"][-1]
        assert turn["status"] == "replied" and turn["model_id"] == "deepseek" and turn["source"] in ("holo", "memory"), turn
        result["replies"].append({"input": text, "reply": turn["reply"], "selected": turn.get("memory_ids", []),
                                  "model": turn["model"], "chat": turn["chat_id"]})
        save()
        print(turn["reply"], flush=True)
        return result["replies"][-1]

    try:
        await start()
        state = await server.state()
        assert not state["turns"] and not state["memories"]
        state = await post("select-model", id="deepseek")
        assert state["model_id"] == "deepseek"
        await post("message", text="记住：我坐高铁喜欢靠窗")
        state = await post("message", text="以后改成靠过道")
        key = state["turns"][-1]["memory_id"]
        await post("new-chat")
        recalled = await ask("我要坐高铁，按我的座位偏好给一句选座建议。")
        assert recalled["selected"] == [key] and "过道" in recalled["reply"] and "靠窗" not in recalled["reply"]
        original_chat = recalled["chat"]
        await post("new-chat")
        independent = await ask("用一句话解释彩虹为什么会出现。")
        assert not independent["selected"] and not any(t in independent["reply"] for t in ("过道", "高铁", "靠窗"))
        await post("switch-chat", id=original_chat)
        await post("message", text="撤回记忆：" + key)
        await server.stop()
        await start(resume=True)
        assert (await server.state())["chat_id"] == original_chat
        forgotten = await ask("我的高铁选座偏好还有记录吗？有记录就按记录答，没有就说没有记录。一句话。")
        assert not forgotten["selected"] and not any(t in forgotten["reply"] for t in ("过道", "靠窗"))
        assert any(t in forgotten["reply"] for t in ("没有", "未", "不"))
        result["requests"] = json.loads((folder / "paid-limit.json").read_text(encoding="utf-8"))
        assert result["requests"]["allowed"] == 3 and 0 < result["requests"]["outbound"] <= 3 and len(set(result["pids"])) == 2
        save()
        assert assess(folder)
    finally:
        await server.stop()
    print(folder)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--assess":
        print("回答质量通过", assess(Path(sys.argv[2])))
    else:
        asyncio.run(run())
