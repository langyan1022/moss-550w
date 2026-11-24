import asyncio
from datetime import datetime
import json
from time import monotonic

from browser_lab import BrowserServer, ROOT


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("holo-followup-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18578)
    report = {"passed": False, "cases": []}

    async def say(text):
        start = monotonic()
        code, state = await server.post("/api/message", {"text": text})
        assert code == 200, state.get("error")
        turn = state["turns"][-1]
        async with asyncio.timeout(120):
            while turn["status"] == "thinking":
                await asyncio.sleep(.1)
                turn = (await server.state())["turns"][-1]
        assert turn["status"] == "replied", turn.get("error")
        report["cases"].append({"prompt": text, "reply": turn["reply"], "source": turn.get("source"),
                                "selector": (turn.get("jev") or {}).get("selector"),
                                "ms": round((monotonic() - start) * 1000)})
        return turn

    try:
        await server.start()
        assert (await server.post("/api/select-model", {"id": "deepseek"}))[0] == 200
        empty = await say("帮我回复同事")
        assert empty["jev"]["selector"] == "awaiting_message" and not empty.get("holo_tools")
        assert "取消" in (await say("取消"))["reply"]
        await say("帮我回复这条消息")
        await server.stop()
        await server.start()
        actual = await say("“周四有空吗？” 我想说周四不行，改到周五下午三点")
        assert actual["source"] == "jev" and actual["jev"]["candidate_count"] > 0
        assert "周五" in actual["reply"] and any(x in actual["reply"] for x in ("三点", "3点", "15:00"))
        remembered = await say("记住：我给练琴用的计时器取名叫青杏七号")
        assert remembered["source"] == "memory"
        assert (await server.post("/api/new-chat", {}))[0] == 200
        recalled = await say("我给练琴用的计时器取了什么名字？")
        assert recalled["source"] == "holo" and "青杏七号" in recalled["reply"] and recalled.get("memory_ids")
        explanation = await say("什么是草稿？")
        assert explanation["source"] == "holo" and not explanation.get("call_id")
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"])


if __name__ == "__main__":
    asyncio.run(main())
