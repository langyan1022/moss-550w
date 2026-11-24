import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
from time import perf_counter
import wave

from aiohttp import web

from browser_lab import BrowserServer, ROOT
from local_media.library import MediaLibrary


def fixture_catalog(folder):
    music = folder / "music"
    artist = music / "测试歌手"
    artist.mkdir(parents=True)
    for title in ("云角", "白帆", "长桥"):
        with wave.open(str(artist / f"{title}.wav"), "wb") as track:
            track.setparams((1, 2, 16000, 0, "NONE", "NONE"))
            track.writeframes(b"\0\0" * 32000)
    for title in ("隔离航行甲", "隔离航行乙"):
        shutil.copyfile(ROOT / "static/conversation-assets/moss-opening.mp4", music / f"{title}.mp4")
    catalog = MediaLibrary(folder / "catalog.sqlite3")
    catalog.configure([{"id": "test", "label": "合成音乐", "kind": "local", "path": str(music)}])
    catalog.scan()
    return catalog.db_path


def age_offer(data, turn_id):
    connection = sqlite3.connect(data / "session.sqlite3")
    try:
        (raw,) = connection.execute("SELECT doc FROM turns WHERE id=?", (turn_id,)).fetchone()
        turn = json.loads(raw)
        earlier = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        turn.update(created_at=earlier, replied_at=earlier)
        with connection:
            connection.execute("UPDATE turns SET doc=? WHERE id=?", (json.dumps(turn, ensure_ascii=False), turn_id))
    finally:
        connection.close()


async def exercise(server, model_requests, report):
    async def request(path, body):
        start = perf_counter()
        before = len(model_requests)
        code, state = await server.post(path, body)
        assert code == 200, (path, body, state)
        turn = state["turns"][-1]
        turn_id = turn["id"]
        async with asyncio.timeout(12):
            while turn["status"] == "thinking":
                await asyncio.sleep(.025)
                turn = (await server.state())["turns"][-1]
        assert turn["id"] == turn_id
        return turn, {
            "text": turn["user"], "status": turn["status"], "source": turn.get("source"),
            "reply": turn.get("reply"), "autoplay_requested": bool((turn.get("media") or {}).get("autoplay")),
            "model_probe_calls": len(model_requests) - before, "wall_ms": round((perf_counter() - start) * 1000, 2),
        }

    async def send(text):
        turn, result = await request("/api/message", {"text": text})
        assert turn["user"] == text
        return turn, result

    async def fresh():
        code, state = await server.post("/api/new-chat", {})
        assert code == 200 and not state["turns"]

    async def offer():
        await fresh()
        listing, _ = await send("只看看测试歌手的歌，不要播放")
        assert len(listing["media"]["items"]) == 3 and not listing["media"]["autoplay"]
        turn, _ = await send("你猜一下")
        assert turn["status"] == "replied" and turn["source"] == "media" and not turn["media"]["autoplay"]
        assert turn["media"]["suggested"]["media_id"]
        return turn

    async def thank(text):
        turn, result = await send(text)
        assert turn["source"] == "smalltalk" and turn["reply"] == "不客气。", result
        assert not result["autoplay_requested"] and result["model_probe_calls"] == 0, result
        report["thanks"].append(result)

    async def picked(name, proposal, text="就它"):
        turn, result = await send(text)
        selected = turn.get("media", {}).get("selected") or {}
        result.update(name=name, target=selected.get("title"))
        assert turn["source"] == "media" and turn["status"] == "replied", result
        assert selected.get("media_id") == proposal["media"]["suggested"]["media_id"], result
        assert result["autoplay_requested"] and result["model_probe_calls"] == 0, result
        assert turn["media"]["online_calls"] == 0
        report["cases"].append(result)

    async def declined(name, text="就它"):
        turn, result = await send(text)
        assert not result["autoplay_requested"] and not turn.get("transport"), result
        assert not (turn.get("media") or {}).get("selected"), result
        report["cases"].append(dict(result, name=name))

    def selected(result, turn, item, *, play):
        data = turn["media"]
        found = data.get("selected") or {}
        result.update(target=found.get("title"), kind=found.get("kind"), mode=data.get("play_mode"))
        assert result["source"] == "media" and result["status"] == "replied", result
        assert found.get("media_id") == item["media_id"], result
        assert result["autoplay_requested"] is play and result["model_probe_calls"] == 0, result
        assert data["online_calls"] == 0, result
        report["references"].append(result)

    async def references():
        await fresh()
        missing, result = await send("不是第二首，播放第三首")
        assert not result["autoplay_requested"] and result["model_probe_calls"] == 0 and not missing["media"].get("selected"), result
        report["references"].append(result)
        video_list, _ = await send("搜索本地视频 隔离航行")
        videos = video_list["media"]["items"]
        assert len(videos) == 2 and all(item["kind"] == "video" for item in videos), videos
        first, result = await send("播放第一个视频")
        selected(result, first, videos[0], play=True)
        for phrase in ("第二首", "播放第二首", "下一首", "试听第二个", "第二首叫什么？"):
            turn, result = await send(phrase)
            selected(result, turn, videos[0], play=False)
            assert not turn["media"]["show_audio"] and result["mode"] is None, result
            assert "视频" in result["reply"], result
        video, result = await send("播放第二个视频")
        selected(result, video, videos[1], play=True)
        assert result["mode"] == "video", result

        audio_list, _ = await send("只看看测试歌手的歌，不要播放")
        songs = audio_list["media"]["items"]
        assert len(songs) == 3 and all(item["kind"] == "audio" for item in songs), songs
        for phrase, index in (("第二首", 1), ("不是第二首，播放第三首", 2),
                              ("上一首", 1), ("别放这首，换下一首", 2),
                              ("先别播放第三首；换成第一首吧", 0)):
            turn, result = await send(phrase)
            selected(result, turn, songs[index], play=True)
            assert result["mode"] == "track", result
        for phrase in ("不是第二首，别换下一首", "不是第二首，不准播放第三首？", "别放这首，不准放第三首", "别放这首，只是问下一首叫什么",
                       "别放这首，换第二首", "别播放第二首，放第2首", "不是第二首，播放第三首吗？",
                       "不是第二首，播放第三首，还是别换"):
            await send("第二首")
            turn, result = await send(phrase)
            assert not result["autoplay_requested"] and not turn.get("transport"), result
            assert result["status"] == "replied" and result["source"] == "media" and result["model_probe_calls"] == 0, result
            report["references"].append(result)

        for listing, item, index in ((video_list, videos[1], 2), (audio_list, songs[2], 3)):
            turn, result = await request("/api/media-select", {"chat_id": listing["chat_id"], "turn_id": listing["id"],
                                                                "media_id": item["media_id"], "index": index})
            selected(result, turn, item, play=True)

    await server.start()
    assert (await server.state())["model_id"] == "local"
    await picked("直接选择对照", await offer())

    proposal = await offer()
    await thank("谢谢")
    await picked("感谢后继续选择", proposal)

    proposal = await offer()
    for text in ("谢谢你！", "MOSS，谢谢。", "多谢"):
        await thank(text)
    await picked("连续三条感谢后继续选择", proposal, "就听这首")

    await offer()
    await thank("谢谢")
    await declined("随口应答不启动播放", "好的")

    await offer()
    await thank("谢谢")
    note, _ = await send("记个便签：下周二核对纸榆电路。")
    assert note["source"] == "notes"
    await thank("谢谢")
    await declined("切到便签后拒绝复用曲目")

    await offer()
    await thank("谢谢")
    cancelled, _ = await send("算了")
    assert cancelled["status"] == "replied" and not (cancelled.get("media") or {}).get("autoplay")
    await thank("谢谢")
    await declined("感谢后取消会结束引用")

    await offer()
    rejected, _ = await send("不用了")
    assert rejected["status"] == "replied" and rejected["media_declined"] and not rejected["media"]["autoplay"]
    await declined("直接拒播后不重开同一首")

    await offer()
    await thank("谢谢")
    rejected, _ = await send("先别播放")
    assert rejected["status"] == "replied" and rejected["media_declined"] and not rejected["media"]["autoplay"]
    await declined("感谢后明确拒播会结束引用")

    await offer()
    rejected, _ = await send("不用了")
    assert rejected["media_declined"] and not rejected["media"]["autoplay"]
    await thank("谢谢")
    await declined("拒播的媒体回合不成为引用来源")

    await offer()
    await thank("谢谢")
    await fresh()
    await declined("新对话不复用引用")

    proposal = await offer()
    await thank("谢谢")
    await server.stop()
    await server.start()
    await picked("近期同一对话重启后可继续", proposal)

    proposal = await offer()
    await thank("谢谢")
    await server.stop()
    age_offer(server.data, proposal["id"])
    await server.start()
    await declined("建议已过期，即使刚说过谢谢也不延长")
    await references()


async def verify():
    model_requests = []
    report = {"passed": False, "cases": [], "thanks": [], "references": [], "external_models": 0}

    async def refuse(request):
        model_requests.append(request.path)
        return web.json_response({"error": {"message": "isolated no-inference probe"}}, status=503)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", refuse)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    model_port = site._server.sockets[0].getsockname()[1]
    try:
        folder = ROOT / "runs" / datetime.now().strftime("media-courtesy-%Y%m%d-%H%M%S-%f")
        os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_KEV_ENABLED="0",
                          MOSS_MEDIA_DB=str(fixture_catalog(folder)))
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        server = BrowserServer(folder / "service", port, model_url=f"http://127.0.0.1:{model_port}/v1",
                               model="offline-probe")
        try:
            await exercise(server, model_requests, report)
            report.update(passed=True, loopback_probe_calls=len(model_requests))
        finally:
            await server.stop()
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await runner.cleanup()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify())
    data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(data, encoding="utf-8")
    print(data)
