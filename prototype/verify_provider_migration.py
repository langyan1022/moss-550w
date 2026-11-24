import asyncio
import json
import sqlite3
from datetime import datetime

from browser_lab import BrowserServer, ROOT


async def wait_turn(server, status):
    async with asyncio.timeout(150):
        while True:
            turn = (await server.state())["turns"][-1]
            if turn["status"] == status:
                return turn
            if turn["status"] == "error":
                raise RuntimeError(turn["error"])
            await asyncio.sleep(.04)


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("provider-migration-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18499)
    report = {"passed": False}
    try:
        await server.start()
        content = "周六下午复核音箱位置"
        status, _ = await server.post("/api/message", {"text": "请保存草稿，内容逐字写：" + content})
        assert status == 200
        pending = await wait_turn(server, "pending")
        assert pending["preview"] == content and not list((server.data / "drafts").glob("*"))
        for old in ("hiroute-auto", "hiroute-fixed"):
            await server.stop()
            with sqlite3.connect(server.data / "session.sqlite3") as db:
                db.execute("UPDATE model_selection SET provider=? WHERE id=1", (old,))
            await server.start()
            state = await server.state()
            row = state["turns"][-1]
            assert state["model_id"] == "local" and state["agent_mode"] == "holojarvis"
            assert row["id"] == pending["id"] and row["status"] == "pending" and row["call_id"] == pending["call_id"]
            status, _ = await server.post("/api/select-model", {"id": old})
            assert status == 409
        for _ in range(2):
            status, _ = await server.post("/api/approve", {"turn_id": pending["id"]})
            assert status == 200
        final = await wait_turn(server, "replied")
        files = list((server.data / "drafts").glob("*"))
        assert len(files) == 1 and files[0].read_text(encoding="utf-8") == content
        assert final["receipt"]["text"] == content
        report.update(passed=True, files=len(files), receipt_ms=final["receipt_ms"],
                      final_ms=final["reply_ms"], receipt_lead_ms=round(final["reply_ms"] - final["receipt_ms"], 2))
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report)


if __name__ == "__main__":
    asyncio.run(main())
