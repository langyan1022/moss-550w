import asyncio
from datetime import datetime
import json

from browser_lab import BrowserServer, ROOT


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("agent-media-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18343)
    record = {"passed": False, "turns": []}
    await server.start()
    try:
        for prompt in ("曲名里有西海两个字，我的收藏里有哪些音轨版本？给我列出来。",
                       "刚才选中的这首歌，文件格式和采样率分别是什么？读一下实际文件。"):
            status, data = await server.post("/api/message", {"text": prompt})
            assert status == 200, data.get("error")
            async with asyncio.timeout(420):
                while True:
                    turn = (await server.state())["turns"][-1]
                    if turn["status"] != "thinking":
                        break
                    await asyncio.sleep(.25)
            media = turn.get("media", {})
            record["turns"].append({"user": prompt, "model": turn["model"], "reply": turn["reply"],
                                    "actions": turn.get("media_actions", []), "error": turn["error"],
                                    "online_calls": media.get("online_calls"), "autoplay": media.get("autoplay"),
                                    "selected": (media.get("selected") or {}).get("media_id"),
                                    "metadata": media.get("metadata"), "count": len(media.get("items", []))})
            assert turn["status"] == "replied" and media.get("online_calls") == 0 and not media.get("autoplay")
        first, second = record["turns"]
        assert "search" in first["actions"] and first["count"] >= 2 and "西海" in first["reply"]
        assert first["selected"] == second["selected"] and "inspect" in second["actions"]
        info = second["metadata"]
        assert info["status"] == "ok" and info["sample_rate"] > 0
        assert (str(info["sample_rate"]) in second["reply"].replace(",", "")
                or str(info["sample_rate"] / 1000).removesuffix(".0") in second["reply"])
        record["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder)


if __name__ == "__main__":
    asyncio.run(main())
