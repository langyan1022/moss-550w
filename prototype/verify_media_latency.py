import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from statistics import median
from time import perf_counter

import aiohttp

from browser_lab import BrowserServer, ROOT
from local_media.library import DEFAULT_DB
from media_chat import classify


async def main(label, count):
    folder = ROOT / "runs" / datetime.now().strftime(f"media-latency-{label}-%Y%m%d-%H%M%S")
    folder.mkdir()
    with closing(sqlite3.connect(DEFAULT_DB.as_uri()+"?mode=ro",uri=True)) as origin:
        with closing(sqlite3.connect(folder/"index.sqlite3")) as copy:
            origin.backup(copy)
    os.environ["MOSS_MEDIA_DB"] = str(folder/"index.sqlite3")
    server = BrowserServer(folder, 18462, model_url="http://127.0.0.1:18278/v1")
    report = {"revision":hashlib.sha256((ROOT/"media_chat.py").read_bytes()).hexdigest(),"model":"local","rounds":[]}
    await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async def ask(text):
                started = perf_counter()
                async with http.post(server.url+"/api/message", json={"text":text}) as response:
                    response.raise_for_status()
                    item = (await response.json())["turns"][-1]
                while item["status"] == "thinking":
                    await asyncio.sleep(.005)
                    async with http.get(server.url+"/api/state") as response:
                        item = (await response.json())["turns"][-1]
                assert item["model_id"] == "local" and item["source"] == "media" and item["status"] == "replied"
                assert item["media"]["online_calls"] == 0 and not item.get("native_receipts")
                if text in {"我想听刘德华的歌","来一手刘德华的歌"}:
                    assert all(f"{i}. {song['title']}" in item["reply"] for i,song in enumerate(item["media"]["items"],1))
                    assert "？" not in item["reply"] and "?" not in item["reply"]
                return item["media"], round(1000*(perf_counter()-started),2)
            for _ in range(count):
                async with http.post(server.url+"/api/new-chat",json={}) as response:
                    response.raise_for_status()
                row = {}
                if label=="after":
                    music,row["artist_listen_ms"] = await ask("我想听刘德华的歌")
                    songs = music["items"]
                    assert len(songs)==6 and all(song["artist"]=="刘德华" and song["artist_source"]=="folder" for song in songs)
                    assert music["selected"]==songs[0] and music["play_mode"]=="track" and music["autoplay"]
                    report["artist_list"] = [{"id":song["media_id"],"title":song["title"],"artist":song["artist"],
                                              "source":song["artist_source"]} for song in songs]
                    details,row["artist_browse_ms"] = await ask("只看看刘德华的歌")
                    assert details["items"]==songs and not details["autoplay"]
                    homophone,row["artist_homophone_ms"] = await ask("来一手刘德华的歌")
                    assert homophone["autoplay"] and homophone["selected"]==songs[0] and homophone["items"]==songs
                    assert classify("来一手好菜",None) is None
                    missing,_ = await ask("我想听没有收录的测试歌手的歌")
                    assert missing["items"]==[] and not missing["autoplay"] and missing["selected"] is None
                music,row["listen_ms"] = await ask("我想听刀郎的歌")
                assert music["autoplay"] and music["selected"]["title"]=="冲动的惩罚" and music["play_mode"]=="track"
                first,row["search_ms"] = await ask("搜索本地音乐 刀郎")
                assert first["items"][1]["title"] == "喀什噶尔胡杨" and not first["autoplay"]
                selected,row["select_ms"] = await ask("第二首")
                item = selected["selected"]
                assert item["media_id"] == first["items"][1]["media_id"] and selected["autoplay"]
                path = Path(item["path"])
                url = server.url+"/media/"+item["media_id"]
                size = path.stat().st_size
                with path.open("rb") as f:
                    for name,method,requested,start,end in [
                        ("head_ms","HEAD",None,0,size-1),("first_ms","GET","bytes=0-127",0,127),
                        ("seek_ms","GET","bytes=65536-66559",65536,66559),
                        ("tail_ms","GET","bytes=-128",size-128,size-1),
                    ]:
                        begin = perf_counter()
                        async with http.request(method,url,headers={"Range":requested} if requested else {}) as response:
                            data = await response.read()
                            row[name] = round(1000*(perf_counter()-begin),2)
                            assert response.status == (206 if requested else 200)
                            assert int(response.headers["Content-Length"]) == end-start+1
                            if requested:
                                assert response.headers["Content-Range"] == f"bytes {start}-{end}/{size}"
                                f.seek(start)
                                assert data == f.read(end-start+1)
                            else:
                                assert data == b""
                    for bad in ("bytes=30-10","bytes=-0","bytes=0-1,3-5",f"bytes={size}-"):
                        async with http.get(url,headers={"Range":bad}) as response:
                            assert response.status == 416 and response.headers["Content-Range"] == f"bytes */{size}"
                    async with http.get(url,headers={"Range":f"bytes={size-2}-{size+40}"}) as response:
                        f.seek(size-2)
                        assert response.status==206 and await response.read()==f.read()
                    async with http.get(url) as response:
                        f.seek(0)
                        assert response.status==200 and await response.content.readexactly(4096)==f.read(4096)
                report["rounds"].append(row)
            for invalid in ("0"*32,"unknown"):
                async with http.get(server.url+"/media/"+invalid) as response:
                    assert response.status == 404
            report["median_ms"] = {k:median(row[k] for row in report["rounds"]) for k in report["rounds"][0]}
            report["passed"] = True
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
        print(folder,report.get("median_ms"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("label",choices=["before","after"])
    parser.add_argument("--count",type=int,default=5)
    args = parser.parse_args()
    asyncio.run(main(args.label,args.count))
