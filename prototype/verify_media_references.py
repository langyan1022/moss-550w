import asyncio
from datetime import datetime
import json
from pathlib import Path
from time import perf_counter

from core import Moss
from media_chat import classify, context_summary


async def main():
    root = Path(__file__).resolve().parent
    folder = root / "runs" / datetime.now().strftime("media-references-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    path = folder / "data"
    report = {"passed": False, "turns": []}
    def open_app():
        return Moss(path, "http://127.0.0.1:18222/v1", "moss-chat-qwen35-4b")
    moss = open_app()
    async def finish(text, action):
        begin = perf_counter()
        key = await action
        await moss.job
        turn = moss._get(key)
        assert turn["status"] == "replied" and turn["source"] == "media", (text, turn)
        data = turn["media"]
        report["turns"].append({"input": text, "reply": turn["reply"], "id": key,
                                "selected": (data.get("selected") or {}).get("media_id"),
                                "autoplay": data["autoplay"], "mode": data.get("play_mode"),
                                "elapsed_ms": round((perf_counter() - begin) * 1000, 1)})
        return turn
    async def ask(text):
        return await finish(text, moss.message(text))
    try:
        empty = await ask("第二首")
        assert not empty["media"]["autoplay"] and "还没有" in empty["reply"]
        first = await ask("搜索本地音乐 刀郎")
        items = first["media"]["items"]
        assert len(items) >= 3
        original = moss.chat_id
        for text, index in [("第二首", 1), ("下一首", 2), ("上一首", 1), ("就这首", 1),
                            ("第2个", 1), ("2", 1), ("换第二首", 1), ("我听", 1), ("我听第三首", 2)]:
            got = (await ask(text))["media"]
            assert got["selected"]["media_id"] == items[index]["media_id"] and got["items"] == items
            assert got["autoplay"] and got["play_mode"] == "track" and got["playback"] is None
        for text in ["第99首", "不要播放第二首", "第二首叫什么？", "我刚才选了哪首？", "我刚才听的是哪首？"]:
            turn = await ask(text)
            assert not turn["media"]["autoplay"] and not turn["media"].get("playback")
            assert turn["media"]["selected"]["media_id"] == items[2]["media_id"]
            if text == "第二首叫什么？":
                assert items[1]["title"] in turn["reply"]
            elif "刚才听" in text:
                assert "没有" in turn["reply"] and "回执" in turn["reply"]
        excerpt = (await ask("播放第二首的片段"))["media"]
        assert excerpt["play_mode"] == "excerpt" and excerpt["playback"]["status"] == "played"
        assert excerpt["playback"]["media_id"] == items[1]["media_id"] and excerpt["playback"]["decoded_bytes"] > 0
        for text in ["把第二首保存为草稿", "书单第2本怎么样", "第二首可以播放吗？", "先别播放，我问的是书单第二本"]:
            assert classify(text, excerpt) is None
        assert classify("2", excerpt, focused=False) is None and classify("第2个", None) is None
        assert not context_summary("谢谢", excerpt)
        summary = context_summary("刚才那些版本都是谁的？", excerpt)
        assert items[1]["title"] in summary and "已选序号" in summary and items[1]["path"] not in summary
        await moss.close()
        moss = open_app()
        assert moss.chat_id == original
        assert (await ask("下一首"))["media"]["selected"]["media_id"] == items[2]["media_id"]
        other = await moss.new_chat()
        try:
            await moss.select_media(other, first["id"], items[1]["media_id"])
        except ValueError:
            pass
        else:
            raise AssertionError("不同对话不能共享候选")
        assert (await ask("第二首"))["media"].get("selected") is None
        await moss.switch_chat(original)
        await ask("搜索本地音乐 西海情歌")
        clicked = await finish("历史列表点击", moss.select_media(original, first["id"], items[2]["media_id"]))
        assert clicked["user"] == "播放第 3 首" and clicked["media"]["items"] == items
        assert clicked["media"]["autoplay"] and clicked["media"]["selected"]["media_id"] == items[2]["media_id"]
        report["passed"] = True
    finally:
        await moss.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
