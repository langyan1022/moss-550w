"""Windows NTFS + authenticated loopback SMB. Only disposable files and a unique test share are touched."""
import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4
import wave

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from local_media.library import MediaLibrary
from local_media.playback import apply
from media_chat import MediaChat


def powershell(script, env=None):
    code = "$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=[Text.UTF8Encoding]::new()\n" + script
    command = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                            env={**os.environ, **(env or {})}, capture_output=True, encoding="utf-8",
                            errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


class TestShare:
    def __init__(self, folder):
        self.env = {"MOSS_QA_PATH": str(folder.resolve()), "MOSS_QA_SHARE": "MossQA" + uuid4().hex + "$"}
        self.path = r"\\localhost" + "\\" + self.env["MOSS_QA_SHARE"]

    def publish(self):
        powershell("""
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
New-SmbShare -Name $env:MOSS_QA_SHARE -Path $env:MOSS_QA_PATH -ReadAccess $who -CachingMode None | Out-Null
""", self.env)

    def withdraw(self):
        powershell("""
$share = Get-SmbShare -Name $env:MOSS_QA_SHARE -ErrorAction SilentlyContinue
if ($null -ne $share) {
    if ($share.Path -ne $env:MOSS_QA_PATH) { throw "Unexpected share path" }
    Remove-SmbShare -Name $share.Name -Confirm:$false
}
if (Get-SmbShare -Name $env:MOSS_QA_SHARE -ErrorAction SilentlyContinue) { throw "Test share still present" }
""", self.env)


@contextmanager
def denied_read(path, sid):
    principal = "*" + sid
    def acl(*args):
        subprocess.run(["icacls", str(path), *args], capture_output=True, check=True, timeout=12)
    acl("/deny", principal + ":(RD)")
    try:
        yield
    finally:
        acl("/remove:d", principal)


def write_tracks(folder, prefix, count):
    folder.mkdir()
    for n in range(count):
        with wave.open(str(folder / f"{prefix}{n:02d}.wav"), "wb") as track:
            track.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            track.writeframes((b"\x80\x04\x80\xfb") * 2400)


async def verify(run):
    external = []
    def network_event(event, args):
        if event == "urllib.Request":
            external.append(args[0])
            raise AssertionError("Unexpected Internet search")
    sys.addaudithook(network_event)
    sid = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
    count = 12
    report = {"per_source": count, "renamed": {}, "permissions": {}, "smb": {}}
    with TemporaryDirectory(prefix="assets-", dir=run) as temporary:
        base = Path(temporary)
        local, remote = base / "disk", base / "share"
        write_tracks(local, "本地旧曲", count)
        write_tracks(remote, "共享旧曲", count)
        share = TestShare(remote)
        lib = MediaLibrary(base / "media.sqlite3")
        lib.configure([{"id": "disk", "kind": "local", "path": str(local)},
                       {"id": "share", "kind": "nas", "path": share.path}])
        media = MediaChat(base)
        media.db = lib.db_path
        media.library = lib
        app = web.Application()
        app.router.add_get("/media/{media_id}", media.stream)
        def play(action, **params):
            return apply(action, session="changes", command_id=uuid4().hex, db=media.player_db,
                         library_db=lib.db_path, seconds=.25, **params)
        async def response(action, context=None, **intent):
            return await media.execute(dict(action=action, **intent), context,
                                       {"id": uuid4().hex, "chat_id": "changes"})
        def verify_unavailable(query, key, reason, source):
            hit = lib.route(query, kind="audio", online=True)
            assert hit["status"] == "unavailable" and not hit["online_calls"], hit
            assert (source, reason) in [(x["id"], x["state"]) for x in hit["unavailable_sources"]], hit
            selected = lib.route("", selected=key)
            assert selected["status"] == "selected_unavailable" and selected["reason"] == reason, selected
            chosen = play("discover_preview", media_id=key)
            assert chosen["status"] == "unavailable" and chosen["reason"] == reason, chosen

        try:
            share.publish()
            async with TestClient(TestServer(app)) as client:
                async def fetch(key):
                    async with client.get("/media/" + key, headers={"Range": "bytes=0-11"}) as reply:
                        data = await reply.read()
                        if reply.status == 206:
                            assert len(data) == 12 and data[:4] == b"RIFF" and data[-4:] == b"WAVE", data
                        return reply.status

                first = lib.scan()
                assert all(s["count"] == count and s["state"] == "ready" for s in first), first
                assert lib.route("库中确实不存在的曲名")["status"] == "empty"
                for source, folder, stem in [("disk", local, "本地"), ("share", remote, "共享")]:
                    old = lib.route(stem + "旧曲", limit=100)["items"]
                    assert len(old) == count
                    key = old[0]["media_id"]
                    assert await fetch(key) == 206
                    for path in folder.glob("*.wav"):
                        path.rename(path.with_name(path.name.replace("旧", "新")))
                    verify_unavailable(stem + "旧曲", key, "stale", source)
                    assert await fetch(key) == 404
                    lib.scan()
                    assert lib.route(stem + "旧曲")["status"] == "empty"
                    fresh = lib.route(stem + "新曲", limit=100)["items"]
                    assert len(fresh) == len({item["media_id"] for item in fresh}) == count
                    assert await fetch(fresh[0]["media_id"]) == 206
                    report["renamed"][source] = {"count": len(fresh), "old_stream": 404, "new_stream": 206,
                                                  "before_scan": "stale", "after_scan": "empty"}

                item = lib.route("共享新曲00")["items"][0]
                key = item["media_id"]
                assert play("discover_preview", media_id=key)["status"] == "preview_available"
                share.withdraw()
                verify_unavailable(item["title"], key, "offline", "share")
                assert await fetch(key) == 503
                assert lib.route("本地新曲00")["status"] == "found"
                stopped = play("play_last")
                assert stopped["status"] == "unavailable" and not stopped.get("play_started"), stopped
                assert stopped["media_id"] == key
                state = next(s for s in lib.scan() if s["id"] == "share")
                assert state["state"] == "offline" and state["count"] == count, state
                answer, view = await response("search", query=item["title"])
                assert "当前不可用" in answer and not view["online_calls"], (answer, view)
                share.publish()
                assert lib.route(item["title"])["items"][0]["media_id"] == key
                assert await fetch(key) == 206
                recovered = play("play_last")
                assert recovered["status"] == "played" and recovered["pcm_nonzero"] and recovered["channel_finished"], recovered
                assert all(s["state"] == "ready" for s in lib.scan())
                report["smb"] = {"withdrawn_stream": 503, "retained": state["count"], "restored_stream": 206,
                                 "same_selection": recovered["media_id"] == key, "frames": recovered["submitted_frames"],
                                 "sink": recovered["sink"], "offline_reply": answer}

                for source, folder, stem in [("disk", local, "本地"), ("share", remote, "共享")]:
                    title = stem + "新曲00"
                    item = lib.route(title)["items"][0]
                    key = item["media_id"]
                    with denied_read(folder / (title + ".wav"), sid):
                        assert Path(item["path"]).stat().st_size > 0
                        verify_unavailable(title, key, "denied", source)
                        assert await fetch(key) == 403
                        assert len(lib.route(stem + "新曲", limit=100)["items"]) == count - 1
                        answer, view = await response("select", {"items": [item], "selected": item},
                                                      index=1, play=True, mode="track")
                        assert "读取权限受限" in answer and not view["autoplay"], (answer, view)
                        named, view = await response("named_song", title=title)
                        assert "读取权限受限" in named and not view["autoplay"], (named, view)
                    assert await fetch(key) == 206
                    assert lib.route(title)["items"][0]["media_id"] == key
                    report["permissions"][source] = {"denied_stream": 403, "restored_stream": 206,
                                                     "unaffected": count - 1, "selection_reply": answer}
                    # NTFS 区分枚举目录和读取已知文件。只撤销目录的 ListDirectory，
                    # 不将子文件上仍然有效的读取权也当成丢失。
                    with denied_read(folder, sid):
                        state = next(s for s in lib.scan() if s["id"] == source)
                        assert state["state"] == "denied" and state["count"] == count, state
                        hit = lib.route(title, online=True)
                        assert hit["status"] == "unavailable" and not hit["online_calls"], hit
                        assert any(s["id"] == source and s["state"] == "denied" for s in hit["unavailable_sources"]), hit
                        assert await fetch(key) == 206
                    state = next(s for s in lib.scan() if s["id"] == source)
                    assert state["state"] == "ready" and state["count"] == count, state
                    report["permissions"][source].update(directory_listing="denied", retained=count, known_leaf_stream=206)
                assert not external
        finally:
            share.withdraw()
    report.update(cleaned=not base.exists(), online_calls=len(external), passed=True)
    return report


if __name__ == "__main__":
    run = Path(__file__).resolve().parent / "runs" / ("media-changes-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run.mkdir(parents=True)
    result = asyncio.run(verify(run))
    (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(run / "result.json")
