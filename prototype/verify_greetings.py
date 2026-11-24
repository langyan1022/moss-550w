import asyncio
from datetime import datetime
import json
import os
import shutil
from time import perf_counter

from browser_lab import ROOT
from everyday import everyday_reply
from local_media.library import MediaLibrary
from verify_configured_mcp import isolated


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("greetings-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    music = folder / "music" / "测试歌手"
    music.mkdir(parents=True)
    for title in ("你好", "晚安"):
        shutil.copyfile(ROOT / "voice/lines/here.wav", music / f"{title}.wav")
    catalog = MediaLibrary(folder / "catalog.sqlite3")
    catalog.configure([{"id": "greetings", "label": "隔离同名曲", "kind": "local", "path": str(music.parent)}])
    catalog.scan()
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_KEV_ENABLED="0", MOSS_MEM0_ENABLED="0",
                      MOSS_MEDIA_DB=str(catalog.db_path), MOSS_PROVIDER_DIR=str(folder / "providers"))
    report = {"passed": False, "model_connections": 0, "greetings": [], "songs": [], "excluded": []}
    server = isolated(folder)

    async def record_connection(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(text):
        start = perf_counter()
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200, state
        turn = state["turns"][-1]
        async with asyncio.timeout(8):
            while turn["status"] == "thinking":
                await asyncio.sleep(.02)
                turn = (await server.state())["turns"][-1]
        assert turn["status"] == "replied", turn
        assert turn["cloud_requests"] == {"total": 0, "by_provider": {}, "complete": True}, turn
        return turn, round((perf_counter() - start) * 1000, 1)

    async with await asyncio.start_server(record_connection, "127.0.0.1", 0) as gate:
        server.model_url = f"http://127.0.0.1:{gate.sockets[0].getsockname()[1]}/v1"
        try:
            await server.start()
            for text, expected in (
                ("你好", "你好。"), ("ＭＯＳＳ，你好呀！", "你好。"), ("您好，莫斯。", "您好。"),
                ("早！", "早。"), ("早安。", "早安。"), ("莫斯，早上好。", "早上好。"),
                ("中午好", "中午好。"), ("下午好啊", "下午好。"), ("晚上好", "晚上好。"), ("晚安", "晚安。"),
            ):
                turn, duration = await ask(text)
                assert turn["source"] == "smalltalk" and turn["reply"] == expected and not turn.get("media"), turn
                report["greetings"].append({"input": text, "reply": expected, "text_receipt_ms": duration})
            for text, title in (("播放《晚安》", "晚安"), ("你好", "你好"), ("晚安", "晚安"), ("《你好》", "你好")):
                turn, _ = await ask(text)
                media = turn["media"]
                assert media["autoplay"] and media["selected"]["title"] == title and not media["online_calls"], turn
                report["songs"].append({"input": text, "title": title, "media_id": media["selected"]["media_id"]})
            status, _ = await server.post("/api/new-chat", {})
            assert status == 200
            turn, _ = await ask("晚安")
            assert turn["source"] == "smalltalk" and turn["reply"] == "晚安。" and not turn.get("media")
            report["new_chat_greeting"] = turn["reply"]
            for text in ("你好，帮我放歌", "早上好，查一下今天的天气", "晚安是什么意思？", "把你好翻成英文",
                         "我想听《晚安》", "别说晚安", "我今天醒得早", "你好坏", "说声你好", "你好吗？"):
                assert everyday_reply(text) is None, text
                report["excluded"].append(text)
            assert report["model_connections"] == 0
            report["passed"] = True
        finally:
            await server.stop()
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
