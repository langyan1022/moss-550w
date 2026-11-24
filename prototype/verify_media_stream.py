import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import wave

import smbclient
from mutagen.id3 import TIT2, TPE1
from mutagen.wave import WAVE
from PIL import Image

from local_media.library import MediaLibrary

ROOT = Path(__file__).resolve().parent


def sample(folder, filename, title, artist):
    path = folder / filename
    with wave.open(str(path), "wb") as track:
        track.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        track.writeframes((b"\x80\x04" + b"\x80\xfb") * 8000)
    track = WAVE(path)
    track.add_tags()
    track.tags.add(TIT2(encoding=3, text=title))
    track.tags.add(TPE1(encoding=3, text=artist))
    track.save()


def smb_server(folder, ready):
    from impacket.ntlm import compute_lmhash, compute_nthash
    from impacket.smbserver import SimpleSMBServer

    if os.name == "nt":
        import ctypes

        # Impacket 在只读分支重新设定打开标志，独立服务用二进制模式保留媒体字节。
        if ctypes.CDLL("ucrtbase.dll")._set_fmode(os.O_BINARY):
            raise RuntimeError("无法设置测试共享的二进制文件模式")
    server = SimpleSMBServer(listenAddress="127.0.0.1", listenPort=0)
    server.setSMB2Support(True)
    server.addShare("MEDIA", str(folder), readOnly="yes")
    user, password = os.environ["MOSS_SAMPLE_USER"], os.environ["MOSS_SAMPLE_PASSWORD"]
    server.addCredential(user, 0, compute_lmhash(password), compute_nthash(password))
    host, port = server.getServer().server_address
    assert host == "127.0.0.1" and port > 1024
    ready.write_text(json.dumps({"host": host, "port": port}), encoding="utf-8")
    server.start()


@contextmanager
def share(folder, run):
    ready = run / "smb-instance.json"
    with (run / "smb.log").open("wb") as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(folder), str(ready)],
                                 stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until = time.monotonic() + 15
            while not ready.is_file():
                if child.poll() is not None or time.monotonic() > until:
                    raise RuntimeError("临时 SMB 启动失败，请查看本轮 smb.log")
                time.sleep(.1)
            yield json.loads(ready.read_text(encoding="utf-8"))
        finally:
            smbclient.reset_connection_cache(fail_on_error=False)
            child.terminate()
            child.wait(timeout=15)


def prepare():
    folder = ROOT / "runs" / datetime.now().strftime("http-smb-%Y%m%d-%H%M%S")
    local, remote = folder/"local", folder/"remote"
    local.mkdir(parents=True)
    remote.mkdir()
    sample(local,"同名.wav","同名","同一作者")
    sample(local,"远岸作者访谈.wav","谈远岸作者","另一作者")
    (local/"远岸作者").mkdir()
    sample(local/"远岸作者","翻唱.wav","来自其他歌手","另一作者")
    sample(remote,"同名.wav","同名","同一作者")
    sample(remote,"远岸.wav","远岸海风","远岸作者")
    Image.new("RGB",(7,5),"red").save(local/"红图.png")
    os.environ["MOSS_SAMPLE_USER"] = "moss_media_qa"
    os.environ["MOSS_SAMPLE_PASSWORD"] = secrets.token_urlsafe(24)
    db = folder / "index.sqlite3"
    os.environ["MOSS_MEDIA_DB"] = str(db)
    library = MediaLibrary(db)
    with share(remote,folder) as instance:
        library.configure([{"id":"disk","kind":"local","path":str(local)},
                           {"id":"nas","kind":"nas","path":r"\\127.0.0.1\MEDIA",
                            "smb":{"port":instance["port"],"username_env":"MOSS_SAMPLE_USER",
                                   "password_env":"MOSS_SAMPLE_PASSWORD"}}])
        library.scan()
        subprocess.run([sys.executable,str(Path(__file__).resolve()),"--client",str(folder)],
                       check=True,timeout=150,creationflags=subprocess.CREATE_NO_WINDOW)
    print(folder, flush=True)


async def verify(folder):
    import aiohttp
    from browser_lab import BrowserServer

    server = BrowserServer(folder, 18463, model_url="http://127.0.0.1:18278/v1")
    await server.start()
    result = {"passed":False}
    try:
        async with aiohttp.ClientSession() as http:
            async def ask(text):
                status, state = await server.post("/api/message",{"text":text})
                assert status == 200
                turn = state["turns"][-1]
                while turn["status"] == "thinking":
                    await asyncio.sleep(.02)
                    turn = (await server.state())["turns"][-1]
                assert turn["source"]=="media" and turn["status"]=="replied"
                return turn["media"]

            local = await ask("搜索本地音乐 同名")
            assert local["selected"]["source"] == "disk" and local["consulted_sources"] == ["disk"]
            nas = await ask("我想听远岸作者的歌")
            item = nas["selected"]
            assert nas["autoplay"] and item["source"] == "nas" and not nas["online_calls"]
            assert nas["consulted_sources"] == ["disk","nas"]
            excerpt = await ask("那就放这个片段")
            playback = excerpt["playback"]
            assert playback["status"] == "played" and playback["media_id"] == item["media_id"]
            assert playback["sink"] == "SDL dummy" and playback["pcm_nonzero"] and playback["decoded_bytes"] > 0
            result["smb_excerpt"] = {key: playback[key] for key in ("media_id", "clip_s", "decoded_bytes", "sink")}
            path = folder / "remote/远岸.wav"
            original = path.read_bytes()
            url = server.url + "/media/" + item["media_id"]
            async def read(offset):
                end = offset+143
                async with http.get(url,headers={"Range":f"bytes={offset}-{end}"}) as response:
                    assert response.status==206 and await response.read()==original[offset:end+1]
                    assert response.headers["Content-Range"]==f"bytes {offset}-{end}/{len(original)}"
            await asyncio.gather(*(read(offset) for offset in [0,1024,8192,30000]))
            async with http.get(url) as response:
                assert response.status==200 and await response.read()==original
            async with http.head(url) as response:
                assert response.status==200 and int(response.headers["Content-Length"])==len(original)
            result.update(local_first=local["consulted_sources"],nas_consulted=nas["consulted_sources"],
                          nas_sha256=hashlib.sha256(original).hexdigest(), concurrent_ranges=4, bytes=len(original))
            index = MediaLibrary(folder/"index.sqlite3")
            photo = index.search("红图",kind="image")["items"][0]
            async with http.get(server.url+"/media/"+photo["media_id"]) as response:
                assert response.status==404
            index.configure([{"id":"disk","kind":"local","path":str(folder/"local")}])
            async with http.get(url) as response:
                assert response.status==404
            result.update(passed=True,non_audio_status=404,removed_source_status=404)
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client",type=Path)
    parser.add_argument("--serve", nargs=2, type=Path)
    args = parser.parse_args()
    if args.serve:
        smb_server(*args.serve)
    elif args.client:
        asyncio.run(verify(args.client))
    else:
        prepare()
