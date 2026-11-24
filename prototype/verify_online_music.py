"""Offline unit + HTTP + SSRF + RBAC verification suite for fused online music integration."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import Moss
from local_media.library import MediaLibrary
from online_music import OnlineMusicHub, _validate_public_url, parse_online_music_request
import serve as moss_serve


def _fake_public_dns(host, port, *args, **kwargs):
    low = (host or "").lower()
    if low in {"127.0.0.1", "localhost", "::1"}:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]
    if low.endswith(".internal") or low == "intranet":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.99", port or 443))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]


def test_intent_parser():
    p1 = parse_online_music_request("在 Audius 放一首 piano")
    assert p1 and p1["provider"] == "audius" and p1["play"] is True and p1["online_only"] is True
    assert "piano" in p1["query"]

    p2 = parse_online_music_request("来个华语音乐电台")
    assert p2 and p2["provider"] == "radiobrowser" and p2["explicit_radio"] is True and p2["play"] is True

    p3 = parse_online_music_request("在 Internet Archive 只搜一首古筝曲")
    assert p3 and p3["provider"] == "archive" and p3["play"] is False

    p4 = parse_online_music_request("在 Navidrome 听稻香")
    assert p4 and p4["provider"] == "navidrome" and "稻香" in p4["query"]

    assert parse_online_music_request("别联网放歌") is None
    assert parse_online_music_request("只查本地歌曲") is None
    assert parse_online_music_request("怎么在电台听歌") is None
    assert parse_online_music_request("打开系统通知广播") is None
    assert parse_online_music_request("在线搜今天国际金价") is None
    assert parse_online_music_request("联网搜新闻") is None
    print("  [OK] intent_parser")


def test_ssrf_and_secrets():
    with patch("online_music.socket.getaddrinfo", side_effect=_fake_public_dns):
        # Blocked private/loopback/scheme URLs
        for bad, allow_http in [
            ("http://127.0.0.1/a.mp3", False),
            ("https://127.0.0.1/a.mp3", False),
            ("https://10.0.0.5/a.mp3", False),
            ("https://169.254.169.254/latest/meta-data", False),
            ("https://100.64.0.1/a.mp3", False),
            ("https://secret.internal/a.mp3", False),
            ("http://api.audius.co/v1/tracks/1/stream", False),
            ("ftp://api.audius.co/file.mp3", True),
        ]:
            raised = False
            try:
                _validate_public_url(bad, allow_http=allow_http)
            except ValueError:
                raised = True
            assert raised, f"SSRF guard failed to block {bad}"

        assert _validate_public_url("https://api.audius.co/v1/tracks/1/stream", allow_http=False).startswith("https://")
        assert _validate_public_url("http://stream.radiobrowser.example/live.mp3", allow_http=True).startswith("http://")

    with tempfile.TemporaryDirectory(prefix="moss-om-sec-") as td:
        hub = OnlineMusicHub(Path(td))
        saved = hub.save_config({
            "navidrome": {
                "enabled": True,
                "url": "http://192.168.50.10:4533",
                "username": "alice",
                "password": "s3cr3t_nav_pw",
            },
            "jamendo": {
                "enabled": True,
                "client_id": "jam_key_1234",
            },
        })
        assert saved["navidrome"]["has_password"] is True
        assert saved["jamendo"]["has_client_id"] is True
        assert "password" not in saved["navidrome"]
        assert "client_id" not in saved["jamendo"]

        raw_text = (Path(td) / "online-music.json").read_text(encoding="utf-8")
        assert "s3cr3t_nav_pw" not in raw_text
        assert "jam_key_1234" not in raw_text
        assert "password_enc" in raw_text

        # Changing Navidrome host without entering a new password clears the old credential and rejects
        host_changed_rejected = False
        try:
            hub.save_config({"navidrome": {"enabled": True, "url": "http://192.168.50.99:4533", "username": "alice"}})
        except ValueError:
            host_changed_rejected = True
        assert host_changed_rejected

        # Changing Navidrome username without entering a new password clears the old credential and rejects
        hub2 = OnlineMusicHub(Path(td) / "user-change")
        hub2.save_config({"navidrome": {"enabled": True, "url": "http://192.168.50.10:4533", "username": "alice", "password": "pw1"}})
        user_changed_rejected = False
        try:
            hub2.save_config({"navidrome": {"enabled": True, "url": "http://192.168.50.10:4533", "username": "bob"}})
        except ValueError:
            user_changed_rejected = True
        assert user_changed_rejected
    print("  [OK] ssrf_and_secrets")


async def test_providers_lyrics_and_core():
    calls: list[str] = []

    async def mock_fetcher(url: str):
        calls.append(url)
        parts = urlsplit(url)
        if "search3.view" in parts.path or "getRandomSongs.view" in parts.path:
            return {
                "subsonic-response": {
                    "status": "ok",
                    "searchResult3": {"song": [{"id": "nd1", "title": "Private Track", "artist": "Owner", "duration": 200}]},
                    "randomSongs": {"song": [{"id": "nd1", "title": "Private Track", "artist": "Owner", "duration": 200}]},
                }
            }
        if "api.audius.co" in parts.netloc:
            return {
                "data": [
                    {"id": "a1", "title": "Morning Piano", "user": {"name": "Lofi Artist"}, "duration": 185, "is_streamable": True},
                    {"id": "a2", "title": "Private Track", "user": {"name": "Owner"}, "duration": 200, "is_streamable": True},
                ]
            }
        if "archive.org/advancedsearch.php" in url:
            return {"response": {"docs": [{"identifier": "arc_item_1", "title": "Archive Guzheng", "creator": "Folk Ensemble"}]}}
        if "archive.org/metadata/arc_item_1" in url:
            return {"files": [{"name": "guzheng.mp3", "format": "VBR MP3", "length": "240"}]}
        if "api.jamendo.com" in parts.netloc:
            return {
                "results": [
                    {"id": "j1", "name": "Jamendo Dawn", "artist_name": "CC Band", "duration": 170, "audio": "https://mp3d.jamendo.com/track/1"}
                ]
            }
        if "radio-browser.info" in parts.netloc:
            return [
                {
                    "stationuuid": "rb1",
                    "name": "Chinese Music FM",
                    "country": "China",
                    "language": "chinese",
                    "url_resolved": "http://live.radiobrowser.example/chinese.mp3",
                    "codec": "MP3",
                    "bitrate": 128,
                    "lastcheckok": 1,
                }
            ]
        if "lrclib.net" in parts.netloc:
            return [
                {
                    "trackName": "Morning Piano",
                    "syncedLyrics": "[00:01.00]First line\n[00:05.50]Second line",
                }
            ]
        return {}

    prev_iso = os.environ.pop("MOSS_ISOLATED_FUI", None)
    prev_off = os.environ.pop("MOSS_OFFLINE_ONLY", None)
    prev_mdb = os.environ.get("MOSS_MEDIA_DB")

    try:
        with tempfile.TemporaryDirectory(prefix="moss-om-core-", ignore_cleanup_errors=True) as td:
            td_path = Path(td)
            empty_mdb = td_path / "empty-media.sqlite3"
            MediaLibrary(empty_mdb)
            os.environ["MOSS_MEDIA_DB"] = str(empty_mdb)

            with patch("online_music.socket.getaddrinfo", side_effect=_fake_public_dns):
                hub = OnlineMusicHub(td_path, fetcher=mock_fetcher)
                hub.save_config({
                    "navidrome": {"enabled": True, "url": "http://192.168.50.10:4533", "username": "u", "password": "pw"},
                    "jamendo": {"enabled": True, "client_id": "jam_test_key"},
                })

                # Multi-source merge + deduplication (Audius a2 duplicates Navidrome "Private Track|Owner")
                res = await hub.search_tracks("piano", limit=6)
                items = res["items"]
                titles = [it["title"] for it in items]
                assert "Private Track" in titles
                assert "Morning Piano" in titles
                assert "Archive Guzheng" in titles
                assert "Jamendo Dawn" in titles
                assert titles.count("Private Track") == 1, f"Duplicate track was not deduped: {titles}"

                # Blocked filter & exclude_media_id filter
                blocked_id = next(it["media_id"] for it in items if it["title"] == "Morning Piano")
                filtered = await hub.search_tracks("piano", limit=6, blocked_ids={blocked_id})
                assert all(it["media_id"] != blocked_id for it in filtered["items"])

                # Radio Browser explicit routing & stream target policy
                radio_res = await hub.search_tracks("华语电台", limit=4, explicit_radio=True)
                assert len(radio_res["items"]) == 1 and radio_res["items"][0]["live"] is True
                rt = hub.resolve_stream_target(radio_res["items"][0]["media_id"])
                assert rt["allow_http"] is True and rt["live"] is True

                nd_item = next(it for it in items if it["provider"] == "navidrome")
                nt = hub.resolve_stream_target(nd_item["media_id"])
                assert nt["allow_internal"] is True and "stream.view" in nt["url"]

                # LRCLIB synced LRC parsing & SQLite caching
                lrc_calls_before = sum(1 for c in calls if "lrclib.net" in c)
                lrc1 = await hub.get_lyrics(blocked_id, title="Morning Piano", artist="Lofi Artist")
                lrc2 = await hub.get_lyrics(blocked_id, title="Morning Piano", artist="Lofi Artist")
                lrc_calls_after = sum(1 for c in calls if "lrclib.net" in c)
                assert lrc1["has_lrc"] and lrc1["synced"] and len(lrc1["lines"]) == 2
                assert lrc1["lines"][0]["text"] == "First line"
                assert lrc2["lines"] == lrc1["lines"]
                assert lrc_calls_after == lrc_calls_before + 1, "LRCLIB result was not cached in SQLite"

                # Isolation enforcement
                os.environ["MOSS_ISOLATED_FUI"] = "1"
                iso_before = len(calls)
                iso_res = await hub.search_tracks("piano", limit=4)
                assert hub.is_isolated() is True and hub.is_active() is False
                assert iso_res["items"] == [] and len(calls) == iso_before

                # Core fallback routing (isolated OFF vs ON vs "只查本地")
                os.environ.pop("MOSS_ISOLATED_FUI", None)
                moss = Moss(td_path / "moss-data", "http://127.0.0.1:19999", "qwen3")
                try:
                    moss.online_music = hub
                    turn = {"id": "t1", "chat_id": moss.chat_id, "user": "来首钢琴曲"}

                    calls_before_local_only = len(calls)
                    reply_loc, view_loc = await moss.resolve_media_execution(
                        {"action": "listen", "kind": "audio", "query": "piano"}, None, turn, user_text="只查本地钢琴曲"
                    )
                    assert not view_loc.get("items") and len(calls) == calls_before_local_only

                    reply_fb, view_fb = await moss.resolve_media_execution(
                        {"action": "listen", "kind": "audio", "query": "piano"}, None, turn, user_text="来首钢琴曲"
                    )
                    assert view_fb["autoplay"] is True and view_fb["play_mode"] == "track"
                    assert len(view_fb["items"]) >= 3 and view_fb["selected"]["source_kind"] == "online"

                    # Follow-up "下一首" and "上一首" (offset) on online playlist context
                    reply_nxt, view_nxt = await moss.resolve_media_execution(
                        {"action": "select", "cue_navigation": "next", "play": True}, view_fb, turn, user_text="下一首"
                    )
                    assert view_nxt["selected"]["media_id"] == view_fb["items"][1]["media_id"]
                    assert "已切换到" in reply_nxt

                    reply_prv, view_prv = await moss.resolve_media_execution(
                        {"action": "select", "offset": -1, "play": True}, view_nxt, turn, user_text="上一首"
                    )
                    assert view_prv["selected"]["media_id"] == view_fb["items"][0]["media_id"]
                    assert "已切换到" in reply_prv

                    # Isolation check on Moss.resolve_media_execution
                    os.environ["MOSS_ISOLATED_FUI"] = "1"
                    reply_iso, view_iso = await moss.resolve_media_execution(
                        {"action": "listen", "kind": "audio", "query": ""}, None, turn, user_text="先来首歌吧"
                    )
                    assert not view_iso.get("items")
                finally:
                    await moss.close()
    finally:
        if prev_iso is not None:
            os.environ["MOSS_ISOLATED_FUI"] = prev_iso
        else:
            os.environ.pop("MOSS_ISOLATED_FUI", None)
        if prev_off is not None:
            os.environ["MOSS_OFFLINE_ONLY"] = prev_off
        else:
            os.environ.pop("MOSS_OFFLINE_ONLY", None)
        if prev_mdb is not None:
            os.environ["MOSS_MEDIA_DB"] = prev_mdb
        else:
            os.environ.pop("MOSS_MEDIA_DB", None)
    print("  [OK] providers_lyrics_and_core")


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_http_proxy_rbac_and_range():
    sample_audio = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    async def handle_ping(request):
        q = parse_qs(request.query_string)
        if q.get("u") == ["alice"] and q.get("t") and q.get("s"):
            return web.json_response({"subsonic-response": {"status": "ok", "type": "navidrome", "serverVersion": "0.53.0"}})
        return web.json_response({"subsonic-response": {"status": "failed", "error": {"message": "bad auth"}}})

    async def handle_stream(request):
        rng = request.headers.get("Range")
        if rng == "bytes=0-9":
            return web.Response(
                body=sample_audio[:10],
                status=206,
                headers={
                    "Content-Type": "audio/mpeg",
                    "Content-Range": f"bytes 0-9/{len(sample_audio)}",
                    "Accept-Ranges": "bytes",
                },
            )
        return web.Response(
            body=sample_audio,
            status=200,
            headers={"Content-Type": "audio/mpeg", "Accept-Ranges": "bytes"},
        )

    up_app = web.Application()
    up_app.add_routes([
        web.get("/rest/ping.view", handle_ping),
        web.get("/rest/stream.view", handle_stream),
    ])
    up_runner = web.AppRunner(up_app)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    prev_iso = os.environ.pop("MOSS_ISOLATED_FUI", None)
    prev_off = os.environ.pop("MOSS_OFFLINE_ONLY", None)
    prev_sp = os.environ.get("MOSS_SPEECH_PREPARE")
    prev_mdb = os.environ.get("MOSS_MEDIA_DB")
    os.environ["MOSS_SPEECH_PREPARE"] = "0"

    with tempfile.TemporaryDirectory(prefix="moss-om-http-", ignore_cleanup_errors=True) as td:
        td_path = Path(td)
        empty_mdb = td_path / "empty-media.sqlite3"
        MediaLibrary(empty_mdb)
        os.environ["MOSS_MEDIA_DB"] = str(empty_mdb)

        app_port = _pick_free_port()
        moss = Moss(td_path / "moss-data", "http://127.0.0.1:19999", "qwen3")
        stopped = asyncio.Event()
        app = moss_serve.application(
            moss,
            lambda: "standby",
            td_path / "moss-data",
            app_port,
            "test-token",
            stopped,
            fui_public=ROOT / "fui" / "public",
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", app_port)
        await site.start()
        base = f"http://127.0.0.1:{app_port}"

        try:
            # 1. C-level user cannot read or mutate /api/online-music/config or /api/online-music/ping
            same_origin = {"Origin": base}
            jar_c = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(cookie_jar=jar_c, headers=same_origin) as cli_c:
                r = await cli_c.post(f"{base}/api/auth/verify", json={"code": "TEST"})
                assert r.status == 200, f"auth verify TEST failed: {r.status} {await r.text()}"
                r_get_cfg_c = await cli_c.get(f"{base}/api/online-music/config")
                assert r_get_cfg_c.status == 403, f"expected 403 GET config for C, got {r_get_cfg_c.status}"
                r_get_prov_c = await cli_c.get(f"{base}/api/console-settings/providers")
                assert r_get_prov_c.status == 403, f"expected 403 GET providers for C, got {r_get_prov_c.status}"
                r_get_lib_c = await cli_c.get(f"{base}/api/console-settings/library")
                assert r_get_lib_c.status == 403, f"expected 403 GET library for C, got {r_get_lib_c.status}"
                r_cfg_c = await cli_c.post(f"{base}/api/online-music/config", json={"enabled": False})
                assert r_cfg_c.status == 403, f"expected 403 for C, got {r_cfg_c.status}"
                r_ping_c = await cli_c.post(f"{base}/api/online-music/ping", json={"target": "navidrome"})
                assert r_ping_c.status == 403, f"expected 403 ping for C, got {r_ping_c.status}"

            # 2. CS admin saves Navidrome pointing to mock upstream, pings & streams with HTTP Range
            jar_cs = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(cookie_jar=jar_cs, headers=same_origin) as cli_cs:
                r = await cli_cs.post(f"{base}/api/auth/verify", json={"code": "MOSS"})
                assert r.status == 200, f"auth verify MOSS failed: {r.status} {await r.text()}"

                r_save = await cli_cs.post(
                    f"{base}/api/online-music/config",
                    json={
                        "enabled": True,
                        "navidrome": {
                            "enabled": True,
                            "url": f"http://127.0.0.1:{up_port}",
                            "username": "alice",
                            "password": "alice_secret_pw",
                        },
                    },
                )
                assert r_save.status == 200
                cfg_data = await r_save.json()
                assert cfg_data["navidrome"]["has_password"] is True
                assert "password" not in cfg_data["navidrome"]

                r_ping = await cli_cs.post(f"{base}/api/online-music/ping", json={"target": "navidrome"})
                assert r_ping.status == 200
                ping_data = await r_ping.json()
                assert ping_data["ok"] is True and ping_data["type"] == "navidrome"

                nd_cached = moss.online_music._cache_track({
                    "media_id": "ol_nd_test1",
                    "provider": "navidrome",
                    "external_id": "nd1",
                    "title": "Range Test Song",
                    "artist": "Alice",
                    "live": False,
                    "raw_url": "navidrome://nd1",
                })
                r_full = await cli_cs.get(f"{base}{nd_cached['url']}")
                assert r_full.status == 200 and (await r_full.read()) == sample_audio

                r_rng = await cli_cs.get(f"{base}{nd_cached['url']}", headers={"Range": "bytes=0-9"})
                assert r_rng.status == 206
                assert r_rng.headers.get("Content-Range") == f"bytes 0-9/{len(sample_audio)}"
                assert (await r_rng.read()) == sample_audio[:10]

                # Poisoned public-provider cache entry targeting loopback MUST be rejected by SSRF guard
                poisoned = moss.online_music._cache_track({
                    "media_id": "ol_aud_ssrf1",
                    "provider": "audius",
                    "external_id": "bad1",
                    "title": "Poison",
                    "artist": "Eve",
                    "live": False,
                    "raw_url": f"http://127.0.0.1:{up_port}/rest/stream.view",
                })
                with patch("online_music.socket.getaddrinfo", side_effect=_fake_public_dns):
                    r_ssrf = await cli_cs.get(f"{base}{poisoned['url']}")
                    assert r_ssrf.status in (400, 502)

                # Lyrics endpoint fallback cleanly checks title/artist query
                async def lrc_fetch(_u):
                    return [{"syncedLyrics": "[00:02.00]Lyrics Line OK"}]

                moss.online_music._fetcher = lrc_fetch
                r_lrc = await cli_cs.get(f"{base}/api/media/ol_nd_test1/lyrics?title=Range%20Test%20Song&artist=Alice")
                assert r_lrc.status == 200
                lrc_data = await r_lrc.json()
                assert lrc_data["has_lrc"] is True and lrc_data["lines"][0]["text"] == "Lyrics Line OK"
        finally:
            stopped.set()
            await runner.cleanup()
            await moss.close()
            await up_runner.cleanup()
            if prev_iso is not None:
                os.environ["MOSS_ISOLATED_FUI"] = prev_iso
            else:
                os.environ.pop("MOSS_ISOLATED_FUI", None)
            if prev_off is not None:
                os.environ["MOSS_OFFLINE_ONLY"] = prev_off
            else:
                os.environ.pop("MOSS_OFFLINE_ONLY", None)
            if prev_sp is not None:
                os.environ["MOSS_SPEECH_PREPARE"] = prev_sp
            else:
                os.environ.pop("MOSS_SPEECH_PREPARE", None)
            if prev_mdb is not None:
                os.environ["MOSS_MEDIA_DB"] = prev_mdb
            else:
                os.environ.pop("MOSS_MEDIA_DB", None)
    print("  [OK] http_proxy_rbac_and_range")


async def main():
    test_intent_parser()
    test_ssrf_and_secrets()
    await test_providers_lyrics_and_core()
    await test_http_proxy_rbac_and_range()
    print("verify_online_music PASS (all 4 suites OK)")


if __name__ == "__main__":
    asyncio.run(main())
