import asyncio
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import shutil
from time import perf_counter

import aiohttp
import av
from PIL import Image, ImageDraw, ImageFont

from browser_lab import ROOT
from context_tools import recognize_capture
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated
from verify_media_stream import share


DURATION = 15
RATE = 2
SIZE = (640, 240)
FONT = Path(os.environ["WINDIR"]) / "Fonts/msyhbd.ttc"


def make_video(path, cards):
    font = ImageFont.truetype(str(FONT), 68)
    with av.open(str(path), mode="w") as output:
        stream = output.add_stream("libx264", rate=RATE)
        stream.width, stream.height = SIZE
        stream.pix_fmt = "yuv420p"
        stream.thread_count = 1
        stream.options = {"preset":"ultrafast", "crf":"15", "x264-params":"keyint=10:min-keyint=10:scenecut=0"}
        for tick in range(DURATION * RATE):
            line = next((word for start, end, word in cards if start <= tick / RATE < end), "")
            image = Image.new("RGB", SIZE, "white")
            if line:
                ImageDraw.Draw(image).text((320, 120), line, font=font, fill="black", anchor="mm")
            output.mux(stream.encode(av.VideoFrame.from_image(image)))
        output.mux(stream.encode())
    with av.open(str(path)) as actual:
        assert [stream.type for stream in actual.streams] == ["video"]
        assert actual.duration / av.time_base == DURATION
        assert sum(1 for _ in actual.decode(video=0)) == DURATION * RATE


async def check_single_capture(path):
    with av.open(str(path)) as video:
        frame = next(frame for frame in video.decode(video=0) if frame.time == 2)
        image = BytesIO()
        frame.to_image().save(image, format="PNG")
    started = perf_counter()
    text = await recognize_capture(image.getvalue())
    assert "北京天气" in "".join(text.split()), text
    return {"frame_s":2, "recognized":text, "ms":round((perf_counter() - started) * 1000)}


async def ask(server, report, path, body):
    began = perf_counter()
    code, state = await server.post(path, body)
    assert code == 200, (code, state)
    turn = state["turns"][-1]
    turn_id = turn["id"]
    async with asyncio.timeout(60):
        while turn["status"] == "thinking":
            await asyncio.sleep(.05)
            turn = (await server.state())["turns"][-1]
            assert turn["id"] == turn_id
    media = turn.get("media") or {}
    report["cases"].append({"path":path, "request":body, "reply":turn.get("reply"), "error":turn.get("error"),
                            "status":turn["status"], "source":turn.get("source"), "media":media,
                            "cloud_requests":turn["cloud_requests"], "ms":round((perf_counter() - began) * 1000)})
    assert turn["status"] == "replied" and turn["source"] == "media", report["cases"][-1]
    assert turn["cloud_requests"] == {"complete":True, "total":0, "by_provider":{}}
    assert media.get("online_calls") == 0
    return turn


async def search(server, report, title, word, start=0):
    offset = f"从{start}秒开始" if start else ""
    text = f"查《{title}》{offset}画面里的文字“{word}”"
    turn = await ask(server, report, "/api/message", {"text":text})
    media = turn["media"]
    section = media["frame_search"]
    end = min(start + 12, DURATION)
    assert (section["start_s"], section["end_s"], section["duration_s"]) == (start, end, DURATION), section
    assert (section["interval_s"], section["sampled"]) == (2, len(range(start, end, 2))), section
    assert not media.get("autoplay")
    return turn


def check_hits(turn, word, source, starts):
    items = turn["media"]["items"]
    assert [item["subtitle"]["start_s"] for item in items] == starts, items
    assert len({item["media_id"] for item in items}) == 1
    for item in items:
        cue = item["subtitle"]
        assert item["source"] == source and item["kind"] == "video"
        assert cue["kind"] == "frame_text" and cue["start_s"] < cue["end_s"] <= min(DURATION, cue["start_s"] + 2)
        assert all(word in "".join(cue[key].split()) for key in ("text", "excerpt"))
    return items


async def check_playback(server, report, item, path):
    turn = await ask(server, report, "/api/message", {"text":"播放第 2 个"})
    media = turn["media"]
    assert media["autoplay"] and media["play_mode"] == "video" and media["selected"] == item
    start, end = (item["subtitle"][key] for key in ("start_s", "end_s"))
    assert (media["playback"]["clip_start_s"], media["playback"]["clip_s"]) == (start, end - start)
    async with aiohttp.ClientSession() as client:
        async with client.get(f"{server.url}/media/{item['media_id']}") as response:
            assert response.status == 200
            content = await response.read()
    assert content == path.read_bytes()
    with av.open(BytesIO(content)) as video:
        stream = video.streams.video[0]
        video.seek(int(start / stream.time_base), stream=stream)
        frame = next(frame for frame in video.decode(stream) if frame.time >= start)
        assert frame.time == start
        image = frame.to_image().convert("L")
        assert sum(image.histogram()[:64]) > 1000
    report["http_video"] = {"source":item["source"], "sha256":hashlib.sha256(content).hexdigest(),
                            "requested_start_s":start, "decoded_frame_s":frame.time, "end_s":end,
                            "bytes":len(content)}


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("video-frame-text-%Y%m%d-%H%M%S-%f")
    local, remote = folder / "local", folder / "remote"
    local.mkdir(parents=True)
    remote.mkdir()
    print(folder, flush=True)
    disk = local / "山河记录.mp4"
    nas = remote / "远岸记录.mp4"
    disk_cards = [(2, 4, "北京天气"), (8, 10, "北京天气"), (13, 15, "晚间新闻")]
    nas_cards = [(4, 6, "今日开放")]
    report = {"passed":False, "cases":[], "model_connections":0,
              "fixtures":{"disk":disk_cards, "nas":nas_cards, "duration_s":DURATION,
                          "streams":["video"], "rate":RATE, "font":str(FONT)}}

    async def count_connection(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        make_video(disk, disk_cards)
        make_video(nas, nas_cards)
        report["single_capture"] = await check_single_capture(disk)
        shutil.copyfile(nas, remote / disk.name)
        catalog = folder / "index.sqlite3"
        os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                          LOCALAPPDATA=str(folder / "local-profile"), MOSS_WINDOWS_REMINDERS="0",
                          MOSS_MEM0_ENABLED="0", MOSS_SAMPLE_USER="moss_frame_qa",
                          MOSS_SAMPLE_PASSWORD=secrets.token_urlsafe(18))
        for _, prefixes, _, _ in SPECS.values():
            for prefix in prefixes:
                os.environ[prefix + "_BASE_URL"] = "http://127.0.0.1:9/v1"
        library = MediaLibrary(catalog)
        with share(remote, folder) as smb:
            library.configure([{"id":"disk", "kind":"local", "path":str(local)},
                               {"id":"nas", "kind":"nas", "path":r"\\127.0.0.1\MEDIA",
                                "smb":{"port":smb["port"], "username_env":"MOSS_SAMPLE_USER",
                                       "password_env":"MOSS_SAMPLE_PASSWORD"}}])
            assert all(row["state"] == "ready" for row in library.scan())
            assert all(not library.search_subtitles(word)["items"] for word in ("北京天气", "晚间新闻", "今日开放"))
            async with await asyncio.start_server(count_connection, "127.0.0.1", 0) as gate:
                server = isolated(folder / "app")
                server.model_url = f"http://127.0.0.1:{gate.sockets[0].getsockname()[1]}/v1"
                await server.start()
                try:
                    first = await search(server, report, disk.stem, "北京天气")
                    _, second = check_hits(first, "北京天气", "disk", [2, 8])
                    assert first["media"]["consulted_sources"] == ["disk"]
                    await check_playback(server, report, second, disk)
                    miss = await search(server, report, disk.stem, "晚间新闻")
                    assert not miss["media"]["items"] and not miss["media"].get("selected")
                    assert any(term in miss["reply"] for term in ("没有", "没找到", "未", "无匹配"))
                    assert "12" in miss["reply"], miss["reply"]
                    later = await search(server, report, disk.stem, "晚间新闻", start=5)
                    check_hits(later, "晚间新闻", "disk", [13])
                    shared = await search(server, report, nas.stem, "今日开放")
                    check_hits(shared, "今日开放", "nas", [4])
                    assert shared["media"]["consulted_sources"] == ["disk", "nas"]
                    beyond = await search(server, report, disk.stem, "北京天气", start=20)
                    assert not beyond["media"]["items"] and "超出" in beyond["reply"]
                    replacement = folder / "replacement.mp4"
                    make_video(replacement, [(0, DURATION, "内容更新")])
                    replacement.replace(disk)
                    stale = await ask(server, report, "/api/media-select",
                                      {"chat_id":first["chat_id"], "turn_id":first["id"],
                                       "media_id":second["media_id"], "index":2})
                    assert not stale["media"].get("autoplay")
                    assert any(term in stale["reply"] for term in ("重新", "更新", "变化", "不可用", "失效")), stale["reply"]
                    damaged = local / "缺失片尾.mp4"
                    with nas.open("rb") as intact:
                        damaged.write_bytes(intact.read(128))
                    library.scan()
                    assert library.search(damaged.stem, kind="video")["items"][0]["title"] == damaged.stem
                    invalid = await ask(server, report, "/api/message",
                                        {"text":f"查《{damaged.stem}》画面里的文字“今日开放”"})
                    assert "查询未完成" in invalid["reply"] and "没有识别到" not in invalid["reply"], invalid["reply"]
                    assert not invalid["media"]["items"] and not invalid["media"].get("autoplay")
                    assert report["model_connections"] == 0
                finally:
                    await server.stop()
        report["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
