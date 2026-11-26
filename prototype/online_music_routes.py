import asyncio
from contextlib import closing
import sqlite3
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

from music_prefs import load_lrc_for_media_path
from online_music import make_pinned_connector, resolve_public_url


def install_online_music_routes(app, moss):
    """Register online music catalog, preferences, lyrics, cover proxy, and audio stream proxy routes."""

    async def music_prefs_handler(request):
        if request.method == "GET":
            return web.json_response(moss.music_prefs.snapshot(), headers={"Cache-Control": "no-store"})
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要 JSON 对象")
            res = moss.music_prefs.set_status(
                str(body.get("media_id") or ""),
                str(body.get("status") or ""),
                title=str(body.get("title") or ""),
                artist=str(body.get("artist") or ""),
            )
            return web.json_response({**res, "prefs": moss.music_prefs.snapshot()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    def _lookup_local_media_sync(db_path, media_id: str) -> tuple[str, str, str]:
        try:
            if db_path.is_file():
                with closing(sqlite3.connect(db_path, timeout=1)) as mdb:
                    row = mdb.execute("SELECT path, title, artist FROM media WHERE media_id=?", (media_id,)).fetchone()
                    if row:
                        return str(row[0] or ""), str(row[1] or ""), str(row[2] or "")
        except Exception:
            pass
        return "", "", ""

    async def media_lyrics(request):
        mid = request.match_info.get("media_id", "")
        if not mid:
            return web.json_response({"media_id": "", "lines": []}, headers={"Cache-Control": "no-store"})
        q_title = str(request.query.get("title") or "").strip()[:120]
        q_artist = str(request.query.get("artist") or "").strip()[:80]
        audio_path = ""
        if not mid.startswith("ol_"):
            audio_path, row_title, row_artist = await asyncio.to_thread(_lookup_local_media_sync, moss.media.db, mid)
            q_title = q_title or row_title
            q_artist = q_artist or row_artist
        lines = await asyncio.to_thread(load_lrc_for_media_path, audio_path) if audio_path else []
        if lines:
            return web.json_response(
                {"media_id": mid, "has_lrc": True, "synced": True, "source": "local", "lines": lines},
                headers={"Cache-Control": "no-store"},
            )
        if moss.online_music.is_active():
            res = await moss.online_music.get_lyrics(mid, title=q_title, artist=q_artist)
            if res.get("lines"):
                return web.json_response(
                    {
                        "media_id": mid,
                        "has_lrc": True,
                        "synced": bool(res.get("synced")),
                        "source": res.get("source") or "lrclib",
                        "lines": res["lines"],
                    },
                    headers={"Cache-Control": "no-store"},
                )
        return web.json_response({"media_id": mid, "has_lrc": False, "lines": []}, headers={"Cache-Control": "no-store"})

    async def _pinned_hop(cur_url: str, *, allow_internal: bool, allow_http: bool):
        if allow_internal:
            return cur_url, None
        resolved_url, host, pinned_ip = await resolve_public_url(cur_url, allow_http=allow_http)
        return resolved_url, make_pinned_connector(host, pinned_ip)

    async def online_music_browse(request):
        if moss.online_music.is_isolated():
            return web.json_response({"error": "当前处于离线隔离模式，在线曲库浏览已禁用。"}, status=503)
        kind = str(request.query.get("kind") or "playlists").strip().lower()
        item_id = str(request.query.get("id") or "").strip()
        try:
            limit = int(request.query.get("limit") or 18)
        except ValueError:
            limit = 18
        try:
            res = await moss.online_music.browse_navidrome(kind=kind, item_id=item_id, limit=limit)
            return web.json_response(res, headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return web.json_response({"error": f"Navidrome 请求失败：{type(exc).__name__}"}, status=502)

    async def online_music_scrobble(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "无效 JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "需要 JSON 对象"}, status=400)
        mid = str(body.get("media_id") or "").strip()[:64]
        submission = bool(body.get("submission", True))
        res = await moss.online_music.scrobble_navidrome(mid, submission=submission)
        return web.json_response(res, headers={"Cache-Control": "no-store"})

    async def online_media_cover(request):
        mid = request.match_info.get("media_id", "")
        if moss.online_music.is_isolated():
            raise web.HTTPServiceUnavailable(text="离线隔离模式已禁用在线封面加载。")
        try:
            target = await asyncio.to_thread(moss.online_music.resolve_cover_target, mid, False)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from None
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None

        allow_internal = bool(target.get("allow_internal"))
        ident = request.get("moss_identity")
        if allow_internal and ident is not None and ident.get("role") != "CS":
            raise web.HTTPForbidden(text="仅 CS 管理员可访问私有曲库封面。")

        cur_url = target["url"]
        allow_http = bool(target.get("allow_http"))
        allowed_netloc = str(target.get("allowed_netloc") or "")
        timeout = aiohttp.ClientTimeout(total=8, connect=5, sock_read=5)
        try:
            for _ in range(4):
                try:
                    cur_url, connector = await _pinned_hop(cur_url, allow_internal=allow_internal, allow_http=allow_http)
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                    async with session.get(cur_url, allow_redirects=False) as upstream:
                        if upstream.status in (301, 302, 303, 307, 308):
                            nxt = urljoin(cur_url, upstream.headers.get("Location") or "")
                            if allow_internal and urlsplit(nxt).netloc != allowed_netloc:
                                raise web.HTTPBadGateway(text="拒绝跳转到配置以外的服务主机")
                            cur_url = nxt
                            continue
                        if upstream.status >= 400:
                            raise web.HTTPNotFound(text=f"封面源返回 {upstream.status}")
                        ctype = (upstream.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                        if not ctype.startswith("image/"):
                            raise web.HTTPBadGateway(text="上游封面不是有效的图片格式")
                        raw = await upstream.content.read(4 * 1024 * 1024 + 1)
                        if len(raw) > 4 * 1024 * 1024:
                            raise web.HTTPBadGateway(text="封面图片超过 4 MiB 限制")
                        return web.Response(
                            body=raw,
                            headers={
                                "Content-Type": ctype,
                                "X-Content-Type-Options": "nosniff",
                                "Cache-Control": "private, max-age=3600",
                            },
                        )
            raise web.HTTPBadGateway(text="封面重定向次数过多")
        except web.HTTPException:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise web.HTTPBadGateway(text="封面加载失败") from None

    async def online_media_stream(request):
        mid = request.match_info.get("media_id", "")
        if moss.online_music.is_isolated():
            raise web.HTTPServiceUnavailable(text="当前处于离线隔离模式，在线音频流已禁用。")
        try:
            target = await asyncio.to_thread(moss.online_music.resolve_stream_target, mid, False)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from None
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None

        allow_internal = bool(target.get("allow_internal"))
        ident = request.get("moss_identity")
        if allow_internal and ident is not None and ident.get("role") != "CS":
            raise web.HTTPForbidden(text="仅 CS 管理员可访问私有曲库音频流。")

        cur_url = target["url"]
        allow_http = bool(target.get("allow_http"))
        allowed_netloc = str(target.get("allowed_netloc") or "")
        is_live = bool(target.get("live"))
        req_headers = {
            "User-Agent": "MOSS-550W/5.5 (local companion music player)",
            "Accept": "audio/*,*/*;q=0.8",
        }
        if not is_live and (rng := request.headers.get("Range")):
            req_headers["Range"] = rng[:80]

        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=60)
        prepared = False
        resp = None
        try:
            for _ in range(6):
                try:
                    cur_url, connector = await _pinned_hop(cur_url, allow_internal=allow_internal, allow_http=allow_http)
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                    async with session.get(cur_url, headers=req_headers, allow_redirects=False) as upstream:
                        if upstream.status in (301, 302, 303, 307, 308):
                            loc = upstream.headers.get("Location") or ""
                            nxt = urljoin(cur_url, loc)
                            if allow_internal and urlsplit(nxt).netloc != allowed_netloc:
                                raise web.HTTPBadGateway(text="拒绝跳转到配置以外的服务主机")
                            cur_url = nxt
                            continue

                        if upstream.status >= 400 and upstream.status != 416:
                            raise web.HTTPBadGateway(text=f"上游音频源返回 {upstream.status}")

                        ctype = (upstream.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                        path_low = urlsplit(cur_url).path.lower()
                        is_playlist = ctype in (
                            "audio/x-mpegurl",
                            "audio/mpegurl",
                            "application/vnd.apple.mpegurl",
                            "audio/x-scpls",
                            "application/pls+xml",
                        ) or path_low.endswith((".m3u", ".pls"))
                        if is_playlist:
                            raw_pls = (await upstream.content.read(65536)).decode("utf-8", errors="ignore")
                            next_url = ""
                            for ln in raw_pls.splitlines():
                                s = ln.strip()
                                if not s or s.startswith("#"):
                                    continue
                                if s.lower().startswith("file") and "=" in s:
                                    s = s.split("=", 1)[1].strip()
                                if s.lower().startswith(("http://", "https://")):
                                    next_url = s
                                    break
                            if not next_url:
                                raise web.HTTPBadGateway(text="电台播放列表中无有效音频地址")
                            if allow_internal and urlsplit(next_url).netloc != allowed_netloc:
                                raise web.HTTPBadGateway(text="拒绝跳转到配置以外的服务主机")
                            cur_url = next_url
                            continue

                        if ctype.startswith(("text/", "application/xhtml", "application/json", "application/xml")):
                            raise web.HTTPBadGateway(text="上游返回的不是有效的音频流")

                        out_headers = {
                            "Content-Type": upstream.headers.get("Content-Type") or "audio/mpeg",
                            "X-Content-Type-Options": "nosniff",
                            "Cache-Control": "private, no-store",
                        }
                        for hdr in ("Content-Length", "Content-Range", "Accept-Ranges"):
                            if val := upstream.headers.get(hdr):
                                out_headers[hdr] = val

                        resp = web.StreamResponse(status=upstream.status, headers=out_headers)
                        await resp.prepare(request)
                        prepared = True
                        if request.method != "HEAD":
                            async for chunk in upstream.content.iter_chunked(65536):
                                if not chunk:
                                    continue
                                try:
                                    await resp.write(chunk)
                                except (ConnectionError, aiohttp.ClientConnectionError, RuntimeError):
                                    break
                        return resp
            raise web.HTTPBadGateway(text="音频流重定向次数过多")
        except web.HTTPException:
            if prepared and resp is not None:
                resp.force_close()
                return resp
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            if prepared and resp is not None:
                resp.force_close()
                return resp
            raise web.HTTPBadGateway(text=f"在线音频流连接中断：{type(exc).__name__}") from None

    app.add_routes([
        web.get("/api/music/prefs", music_prefs_handler),
        web.post("/api/music/prefs", music_prefs_handler),
        web.get("/api/online-music/browse", online_music_browse),
        web.post("/api/online-music/scrobble", online_music_scrobble),
        web.get(r"/api/media/online-cover/{media_id:[A-Za-z0-9_-]{1,64}}", online_media_cover),
        web.get(r"/api/media/online-stream/{media_id:[A-Za-z0-9_-]{1,64}}", online_media_stream),
        web.get(r"/api/media/{media_id:[A-Za-z0-9_-]{1,64}}/lyrics", media_lyrics),
    ])
