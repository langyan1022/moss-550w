import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from time import perf_counter
from urllib.request import urlopen

from core import Moss


async def main(source, chat_control):
    folder = Path(__file__).resolve().parent / "runs" / datetime.now().strftime("music-guess-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    if source:
        old = json.loads(source.read_text(encoding="utf-8"))
    else:
        with urlopen("http://127.0.0.1:18221/api/state", timeout=10) as conn:
            turns = json.load(conn)["turns"]
        i = next(i for i, t in enumerate(turns) if t["user"] == "你猜" and "无法猜" in (t["reply"] or ""))
        old = turns[i-2:i+1]
        assert [t["user"] for t in old] == ["我想听刀郎的歌", "第二首", "你猜"]
        assert "告诉我歌名" in old[1]["reply"] and old[2]["source"] == "native"
        for t in old:
            for key in ("native_result", "native_receipts", "browser_playback", "_memory_context"):
                t.pop(key, None)
    (folder / "snapshot.json").write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    moss = Moss(folder / "data", "http://127.0.0.1:18222/v1", "moss-chat-qwen35-4b")
    report = {"passed": False, "events": []}
    def restore(chosen):
        with moss.db:
            for record in chosen:
                item = dict(record, chat_id=moss.chat_id)
                moss.db.execute("INSERT INTO turns(id,doc,chat_id) VALUES(?,?,?)",
                                (item["id"], json.dumps(item, ensure_ascii=False), moss.chat_id))
    async def ask(question):
        start = perf_counter()
        ident = await moss.message(question)
        await moss.job
        t = moss._get(ident)
        assert t["status"] == "replied", t["error"]
        m = t.get("media") or {}
        report["events"].append({"input": question, "reply": t["reply"], "source": t.get("source") or "model",
                                 "selected": (m.get("selected") or {}).get("media_id"),
                                 "suggested": (m.get("suggested") or {}).get("media_id"),
                                 "autoplay": m.get("autoplay", False), "model": t["model_id"],
                                 "elapsed_ms": round((perf_counter() - start) * 1000, 1)})
        return t
    try:
        restore(old[:2])
        await moss.select_model(old[2]["model_id"])
        tracks = old[0]["media"]["items"]
        first, second = tracks[:2]
        t = await ask("你猜")
        assert t["media"]["suggested"]["media_id"] == second["media_id"] and second["title"] in t["reply"]
        assert not t["media"]["autoplay"] and not t["media"].get("playback")
        assert t["media"]["selected"]["media_id"] == first["media_id"]
        accepted = await ask("就这首")
        assert accepted["media"]["selected"]["media_id"] == second["media_id"]
        assert accepted["media"]["autoplay"] and accepted["media"]["play_mode"] == "track"
        await moss.new_chat()
        await moss.select_model("local")
        listing = await ask("搜索本地音乐 刀郎")
        suggestion = await ask("你猜")
        assert first["title"] in suggestion["reply"] and "推荐" in suggestion["reply"]
        assert suggestion["media"]["suggested"]["media_id"] == listing["media"]["items"][0]["media_id"]
        assert not suggestion["media"]["autoplay"]
        if chat_control:
            ordinary = await ask("来猜数字。从一、二、三里选一个，请问我想选哪个数字。")
            assert not ordinary.get("media") and not ordinary.get("source")
            guess = await ask("你猜")
            assert not guess.get("source") and not guess.get("media") and not guess.get("native_receipts")
            assert "歌曲" not in guess["reply"] and "歌名" not in guess["reply"]
        report["passed"] = True
    finally:
        await moss.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--chat-control", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.snapshot, args.chat_control))
