"""MOSS 550W Fused Online Open-Source Music & Synced Lyrics Engine.

Supports:
  1. Zero-key open catalogs:
     - Audius (api.audius.co) — indie, pop, lofi, electronic & global tracks
     - Internet Archive (archive.org) — classical, guzheng/folk, jazz, live & ambient audio
     - Radio Browser (api.radio-browser.info) — Chinese & international live radio streams
  2. Optional / Self-hosted protocols:
     - OpenSubsonic / Navidrome (/rest/ping.view, search3.view, getRandomSongs.view, stream.view)
     - Jamendo v3 API (Creative Commons music, requires user-supplied free client_id)
  3. Open-source synced lyrics:
     - LRCLIB (lrclib.net) — automatic fallback LRC / plain lyrics cached in SQLite

Security & isolation:
  - DPAPI-encrypted storage for Navidrome password and Jamendo client_id (matching console_settings).
  - Strict public-IP SSRF guard on all public URLs & redirect hops (allows explicit Navidrome origin only).
  - Hermanetic offline mode respected when MOSS_ISOLATED_FUI=1 or MOSS_OFFLINE_ONLY=1.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlencode, urljoin, urlsplit

import aiohttp

from music_prefs import parse_lrc_text
from secret_store import protect_secret as _seal_secret, unprotect_secret as _unseal_secret


DEFAULT_SOURCE_ORDER = ["navidrome", "audius", "archive", "jamendo", "radiobrowser"]

PROVIDER_LABELS = {
    "navidrome": "Navidrome",
    "audius": "Audius",
    "archive": "Internet Archive",
    "jamendo": "Jamendo",
    "radiobrowser": "Radio Browser",
}

PROVIDER_SHORT = {
    "navidrome": "nd",
    "audius": "aud",
    "archive": "arc",
    "jamendo": "jam",
    "radiobrowser": "rb",
}

RADIO_HINT_RE = re.compile(
    r"(?:音乐|歌曲|中文|华语|怀旧|爵士|欧美|流行|粤语|轻音乐|古典)?(?:电台|广播电台|在线广播|收音机|\b(?:fm|radio)\b)",
    re.IGNORECASE,
)
ONLINE_EXPLICIT_RE = re.compile(
    r"(?:在线(?:搜|找|查|听|放|播)|网上(?:搜|找|听|放|播)|联网(?:搜|找|听|放|播)|公网(?:搜|找|听)|开源曲库|"
    r"\b(?:audius|internet\s*archive|jamendo|navidrome|opensubsonic|subsonic|radio\s*browser)\b)",
    re.IGNORECASE,
)
MUSIC_DOMAIN_RE = re.compile(
    r"(?:歌(?:曲|单)?|音乐|曲(?:子|目|库)|歌手|专辑|音频|钢琴|古筝|琵琶|二胡|笛子|小提琴|吉他|爵士|古典|纯音乐|轻音乐|助眠|国风|流行|民谣|摇滚|电子|电台|广播电台|收音机|"
    r"\b(?:music|songs?|tracks?|audio|lofi|jazz|piano|classical|folk|rock|fm|radio)\b)",
    re.IGNORECASE,
)


def parse_online_music_request(text: str) -> dict | None:
    """Detect explicit online music, self-hosted Navidrome, or live radio playback requests."""
    raw = (text or "").strip()
    if not raw or len(raw) > 160:
        return None
    if re.search(r"别(?:放|播|听|联网)|不要(?:放|播|听|联网)|不用(?:放|播|联网)|只(?:查|搜|看|听|放)(?:本地|本机)|(?:播放|放|听)(?:本地|本机)音乐", raw):
        return None
    if re.search(r"能否|能不能|可不可以|怎么|如何|为什么|介绍|解释|有哪些|新闻|热搜|金价|股价|房价|天气|气温|教程|攻略", raw):
        return None

    provider = None
    low = raw.lower()
    has_music_ctx = bool(MUSIC_DOMAIN_RE.search(raw))
    if "navidrome" in low or "subsonic" in low or "私有曲库" in raw or "私人曲库" in raw:
        provider = "navidrome"
    elif "audius" in low:
        provider = "audius"
    elif "internet archive" in low or ("archive" in low and has_music_ctx):
        provider = "archive"
    elif "jamendo" in low:
        provider = "jamendo"
    elif RADIO_HINT_RE.search(raw):
        provider = "radiobrowser"

    explicit_online = bool(provider or (ONLINE_EXPLICIT_RE.search(raw) and (has_music_ctx or "开源曲库" in raw)))
    if not explicit_online:
        return None

    # Exclude unrelated "广播通知/广播电视" non-music nouns unless paired with play/listen intent
    if provider == "radiobrowser" and not re.search(r"听|放|播|来|开|换|找|搜|music|jazz|lofi|流行|华语|中文|粤语|怀旧|古典", raw, re.I):
        return None

    has_play_verb = bool(re.search(r"放|播|听|来|开|唱|换", raw))
    search_only = bool(re.search(r"只(?:看|查|搜|列)|(?:搜(?:索|一下)?|查找|检索|找(?:一下)?|查(?:一下)?|列出?)", raw)) and not has_play_verb
    play = not search_only and not re.search(r"只(?:看|查|搜|列)", raw)

    cleaned = re.sub(
        r"^(?:请(?:你|您)?|帮我|麻烦(?:你)?|给我|我想|我要|想|要|试着|去|再|只|换[成个到]?|切[到成]?)[\s，,]*",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:在|从|用)?\s*(?:在线|网上|联网|公网)?\s*"
        r"(?:audius|internet\s*archive|archive|jamendo|navidrome|opensubsonic|subsonic|radio\s*browser|开源曲库|私有曲库|私人曲库)?\s*"
        r"(?:上|里|中)?\s*"
        r"(?:的)?\s*"
        r"(?:只?(?:搜索|检索|查找|搜一下|找一下|播放|打开|开启|收听|听一下|来一首|来首|放一首|放首|来个|放个|来|放|播|搜|找|听))?\s*"
        r"(?:一?[首手个支])?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?:的)?(?:歌曲|音乐|歌|电台|广播|收音机|曲子)$", "", cleaned.strip("。！!？?《》\"' ")).strip()
    if "的" in cleaned and not cleaned.endswith("的"):
        artist_part, _, title_part = cleaned.partition("的")
        if 1 <= len(artist_part.strip()) <= 24 and 1 <= len(title_part.strip()) <= 40:
            cleaned = f"{title_part.strip()} {artist_part.strip()}"
    return {
        "action": "listen" if play else "search",
        "kind": "audio",
        "query": cleaned,
        "online": True,
        "online_only": True,
        "explicit_radio": provider == "radiobrowser",
        "provider": provider,
        "play": play,
        "mode": "track",
    }

# Helpful Chinese -> English query hints for Internet Archive / Audius acoustic catalogs
_ZH_TO_EN_HINTS: tuple[tuple[str, str], ...] = (
    ("古筝", "guzheng"),
    ("琵琶", "pipa"),
    ("二胡", "erhu"),
    ("笛子", "bamboo flute"),
    ("国风", "chinese traditional"),
    ("民族", "chinese folk"),
    ("钢琴", "piano"),
    ("小提琴", "violin"),
    ("大提琴", "cello"),
    ("古典", "classical"),
    ("爵士", "jazz"),
    ("轻音乐", "instrumental"),
    ("纯音乐", "instrumental ambient"),
    ("助眠", "ambient sleep calm"),
    ("白噪音", "rain nature ambient"),
    ("吉他", "acoustic guitar"),
    ("摇滚", "rock"),
    ("电子", "electronic"),
    ("民谣", "folk acoustic"),
    ("流行", "pop"),
)


def _protect_secret(scope: str, plain: str) -> str:
    """Encrypt secret via shared cross-platform secret_store (DPAPI on Windows, Fernet fallback)."""
    return _seal_secret(plain, entropy=f"MOSS-550W\0online-music\0{scope}", description="MOSS-OnlineMusic", flags=1)


def _unprotect_secret(scope: str, cipher_b64: str) -> str:
    """Decrypt stored secret cleanly, returning empty string on corruption."""
    return _unseal_secret(cipher_b64, entropy=f"MOSS-550W\0online-music\0{scope}", flags=0)


def _media_id(provider: str, external_id: str) -> str:
    short = PROVIDER_SHORT.get(provider, "ol")
    digest = hashlib.sha256(f"{provider}:{external_id}".encode("utf-8")).hexdigest()[:20]
    return f"ol_{short}_{digest}"


def _is_truly_public_ip(ip_val) -> bool:
    return bool(ip_val.is_global and not ip_val.is_multicast)


def _host_is_public(hostname: str) -> bool:
    """Reject loopback, private, link-local, CGNAT, multicast, or unresolved hostnames."""
    if not hostname:
        return False
    clean = hostname.strip("[]")
    try:
        return _is_truly_public_ip(ip_address(clean))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(clean, None, proto=socket.IPPROTO_TCP)
    except OSError:
        try:
            infos = socket.getaddrinfo(clean, None)
        except OSError:
            return False
    if not infos:
        return False
    for _, _, _, _, sockaddr in infos:
        addr_str = sockaddr[0].split("%", 1)[0]
        try:
            ip_val = ip_address(addr_str)
        except ValueError:
            return False
        if not _is_truly_public_ip(ip_val):
            return False
    return True


def _validate_public_url(url: str, *, allow_http: bool = False) -> str:
    parsed = urlsplit(str(url or "").strip())
    schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("不支持的非公开媒体地址")
    if not _host_is_public(parsed.hostname):
        raise ValueError("拦截非公网媒体地址以防止内网探测")
    return url


async def resolve_public_url(url: str, *, allow_http: bool = False) -> tuple[str, str, str]:
    """Validate URL asynchronously and return (url, hostname, pinned_public_ip) to block DNS rebinding."""
    parsed = urlsplit(str(url or "").strip())
    schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("不支持的非公开媒体地址")
    clean = parsed.hostname.strip("[]")
    try:
        ip_val = ip_address(clean)
        if not _is_truly_public_ip(ip_val):
            raise ValueError("拦截非公网媒体地址以防止内网探测")
        return url, parsed.hostname, clean
    except ValueError as exc:
        if "非公网" in str(exc):
            raise
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(clean, None, proto=socket.IPPROTO_TCP)
    except OSError:
        try:
            infos = await loop.getaddrinfo(clean, None)
        except OSError:
            raise ValueError("无法解析上游音频主机地址") from None
    if not infos:
        raise ValueError("拦截非公网媒体地址以防止内网探测")
    pinned_ip = ""
    for _, _, _, _, sockaddr in infos:
        addr_str = sockaddr[0].split("%", 1)[0]
        try:
            ip_val = ip_address(addr_str)
        except ValueError:
            raise ValueError("拦截非公网媒体地址以防止内网探测") from None
        if not _is_truly_public_ip(ip_val):
            raise ValueError("拦截非公网媒体地址以防止内网探测")
        if not pinned_ip:
            pinned_ip = addr_str
    return url, parsed.hostname, pinned_ip


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Pin one validated DNS resolution result inside aiohttp to stop DNS-rebinding TOCTOU."""

    def __init__(self, expected_host: str, resolved_ip: str):
        self._expected_host = (expected_host or "").strip("[]").lower()
        self._resolved_ip = resolved_ip

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        if host.strip("[]").lower() != self._expected_host:
            raise OSError(f"Refusing unpinned DNS resolution for {host}")
        addr_family = socket.AF_INET6 if ":" in self._resolved_ip else socket.AF_INET
        return [{
            "hostname": host,
            "host": self._resolved_ip,
            "port": port,
            "family": addr_family,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST,
        }]

    async def close(self) -> None:
        return None


def make_pinned_connector(hostname: str, resolved_ip: str) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, resolved_ip))


def _validate_navidrome_url(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise ValueError("OpenSubsonic / Navidrome 地址须为有效 http:// 或 https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("服务地址请不要包含内嵌密码、查询参数或片段")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _parse_lrc_text(lrc_text: str) -> list[dict]:
    return parse_lrc_text(lrc_text)


def _translate_hints(query: str) -> str:
    q = (query or "").strip()
    hints = [en for zh, en in _ZH_TO_EN_HINTS if zh in q]
    ascii_parts = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9 '&._-]*", q))
    combined = " ".join(part for part in (ascii_parts, *hints) if part).strip()
    return combined or q


class OnlineMusicHub:
    """Thread-safe online open-source music catalog, proxy resolver & lyrics cache."""

    def __init__(self, data_dir: Path, *, fetcher=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "online-music.json"
        self.cache_db_path = self.data_dir / "online_music_cache.sqlite3"
        self._fetcher = fetcher  # Optional injected async JSON/bytes callable for offline unit tests
        self._init_db()

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.cache_db_path, timeout=5)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS online_tracks (
                    media_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    duration_s REAL,
                    live INTEGER NOT NULL DEFAULT 0,
                    stream_url TEXT NOT NULL,
                    license TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS online_lyrics_cache (
                    cache_key TEXT PRIMARY KEY,
                    synced INTEGER NOT NULL DEFAULT 1,
                    lines_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'lrclib',
                    updated_at TEXT NOT NULL
                )"""
            )

    def _default_raw_config(self) -> dict:
        return {
            "enabled": True,
            "fallback_when_empty": True,
            "fallback_when_offline": True,
            "audius_enabled": True,
            "archive_enabled": True,
            "radiobrowser_enabled": True,
            "lyrics_lrclib": True,
            "navidrome": {"enabled": False, "url": "", "username": "", "password_enc": ""},
            "jamendo": {"enabled": False, "client_id_enc": ""},
            "source_order": list(DEFAULT_SOURCE_ORDER),
        }

    def _load_raw_config(self) -> dict:
        base = self._default_raw_config()
        if not self.config_path.is_file():
            return base
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return base
        if not isinstance(loaded, dict):
            return base
        for key in (
            "enabled",
            "fallback_when_empty",
            "fallback_when_offline",
            "audius_enabled",
            "archive_enabled",
            "radiobrowser_enabled",
            "lyrics_lrclib",
        ):
            if key in loaded:
                base[key] = bool(loaded[key])
        if isinstance(loaded.get("navidrome"), dict):
            nav = loaded["navidrome"]
            base["navidrome"] = {
                "enabled": bool(nav.get("enabled", False)),
                "url": str(nav.get("url") or "").strip(),
                "username": str(nav.get("username") or "").strip(),
                "password_enc": str(nav.get("password_enc") or ""),
            }
        if isinstance(loaded.get("jamendo"), dict):
            jam = loaded["jamendo"]
            base["jamendo"] = {
                "enabled": bool(jam.get("enabled", False)),
                "client_id_enc": str(jam.get("client_id_enc") or ""),
            }
        if isinstance(loaded.get("source_order"), list):
            clean = [s for s in loaded["source_order"] if s in DEFAULT_SOURCE_ORDER]
            for s in DEFAULT_SOURCE_ORDER:
                if s not in clean:
                    clean.append(s)
            base["source_order"] = clean
        return base

    def is_isolated(self) -> bool:
        return (
            os.environ.get("MOSS_ISOLATED_FUI") == "1"
            or os.environ.get("MOSS_OFFLINE_ONLY") == "1"
        )

    def is_active(self) -> bool:
        if self.is_isolated():
            return False
        return bool(self._load_raw_config().get("enabled", True))

    def public_config(self) -> dict:
        cfg = self._load_raw_config()
        nav = cfg["navidrome"]
        jam = cfg["jamendo"]
        return {
            "enabled": cfg["enabled"],
            "isolated": self.is_isolated(),
            "fallback_when_empty": cfg["fallback_when_empty"],
            "fallback_when_offline": cfg["fallback_when_offline"],
            "audius_enabled": cfg["audius_enabled"],
            "archive_enabled": cfg["archive_enabled"],
            "radiobrowser_enabled": cfg["radiobrowser_enabled"],
            "lyrics_lrclib": cfg["lyrics_lrclib"],
            "navidrome": {
                "enabled": nav["enabled"],
                "url": nav["url"],
                "username": nav["username"],
                "has_password": bool(nav["password_enc"]),
            },
            "jamendo": {
                "enabled": jam["enabled"],
                "has_client_id": bool(jam["client_id_enc"]),
            },
            "source_order": cfg["source_order"],
        }

    def save_config(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("配置必须是 JSON 对象")
        cfg = self._load_raw_config()
        for key in (
            "enabled",
            "fallback_when_empty",
            "fallback_when_offline",
            "audius_enabled",
            "archive_enabled",
            "radiobrowser_enabled",
            "lyrics_lrclib",
        ):
            if key in patch:
                cfg[key] = bool(patch[key])

        if isinstance(patch.get("navidrome"), dict):
            np = patch["navidrome"]
            cur_nav = cfg["navidrome"]
            new_url = _validate_navidrome_url(np["url"]) if "url" in np else cur_nav["url"]
            new_user = str(np.get("username", cur_nav["username"]) or "").strip()[:80]
            new_en = bool(np.get("enabled", cur_nav["enabled"]))
            pw_enc = cur_nav["password_enc"]
            if np.get("clear_password"):
                pw_enc = ""
            elif isinstance(np.get("password"), str) and np["password"]:
                pw_enc = _protect_secret("navidrome", np["password"][:256])
            elif (
                (new_url and cur_nav["url"] and urlsplit(new_url).netloc != urlsplit(cur_nav["url"]).netloc)
                or (new_user and cur_nav["username"] and new_user != cur_nav["username"])
            ):
                pw_enc = ""
            if new_en and (not new_url or not new_user or not pw_enc):
                raise ValueError("启用 Navidrome / OpenSubsonic 需要填写服务地址、用户名和密码。")
            cfg["navidrome"] = {
                "enabled": new_en,
                "url": new_url,
                "username": new_user,
                "password_enc": pw_enc,
            }

        if isinstance(patch.get("jamendo"), dict):
            jp = patch["jamendo"]
            cur_jam = cfg["jamendo"]
            new_en = bool(jp.get("enabled", cur_jam["enabled"]))
            cid_enc = cur_jam["client_id_enc"]
            if jp.get("clear_client_id"):
                cid_enc = ""
            elif isinstance(jp.get("client_id"), str) and jp["client_id"].strip():
                cid = jp["client_id"].strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", cid):
                    raise ValueError("Jamendo Client ID 格式无效")
                cid_enc = _protect_secret("jamendo", cid)
            if new_en and not cid_enc:
                raise ValueError("启用 Jamendo 曲库需要填写免费的 Jamendo Client ID。")
            cfg["jamendo"] = {"enabled": new_en, "client_id_enc": cid_enc}

        if isinstance(patch.get("source_order"), list):
            clean = [s for s in patch["source_order"] if s in DEFAULT_SOURCE_ORDER]
            for s in DEFAULT_SOURCE_ORDER:
                if s not in clean:
                    clean.append(s)
            cfg["source_order"] = clean

        staged = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", dir=self.config_path.parent, delete=False
            ) as fh:
                staged = Path(fh.name)
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(staged, self.config_path)
        finally:
            if staged:
                staged.unlink(missing_ok=True)
        return self.public_config()

    async def _http_get_json(self, url: str, *, allow_internal: bool = False, timeout_s: float = 6.5):
        if self._fetcher is not None:
            return await self._fetcher(url)
        cur_url = url
        headers = {"User-Agent": "MOSS-550W/5.5 (local companion music player)"}
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        for _ in range(4):
            connector = None
            if not allow_internal:
                cur_url, host, pinned_ip = await resolve_public_url(cur_url, allow_http=False)
                connector = make_pinned_connector(host, pinned_ip)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(cur_url, headers=headers, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location") or ""
                        nxt = urljoin(cur_url, loc)
                        if allow_internal and urlsplit(nxt).netloc != urlsplit(cur_url).netloc:
                            raise ValueError("拒绝跳转到配置以外的服务主机")
                        cur_url = nxt
                        continue
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        raise ValueError("重定向次数过多")

    def _cache_track(self, track: dict) -> dict:
        mid = track["media_id"]
        now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.cache_db_path, timeout=5)) as db, db:
            db.execute(
                """INSERT INTO online_tracks(
                       media_id, provider, external_id, title, artist, album,
                       duration_s, live, stream_url, license, meta_json, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(media_id) DO UPDATE SET
                       title=excluded.title, artist=excluded.artist, album=excluded.album,
                       duration_s=excluded.duration_s, live=excluded.live, stream_url=excluded.stream_url,
                       license=excluded.license, meta_json=excluded.meta_json, updated_at=excluded.updated_at""",
                (
                    mid,
                    track["provider"],
                    track["external_id"],
                    track["title"],
                    track["artist"],
                    track.get("album", ""),
                    track.get("duration_s"),
                    1 if track.get("live") else 0,
                    track["raw_url"],
                    track.get("license", ""),
                    json.dumps(track.get("meta", {}), ensure_ascii=False),
                    now_s,
                ),
            )
            # Prune cache cleanly above 600 rows
            db.execute(
                """DELETE FROM online_tracks WHERE media_id IN (
                       SELECT media_id FROM online_tracks ORDER BY updated_at DESC LIMIT -1 OFFSET 600
                   )"""
            )
        out = {
            "media_id": mid,
            "title": track["title"],
            "artist": track["artist"],
            "album": track.get("album", ""),
            "kind": "audio",
            "source": f"online-{track['provider']}",
            "source_kind": "online",
            "source_label": PROVIDER_LABELS.get(track["provider"], track["provider"]),
            "provider": track["provider"],
            "live": bool(track.get("live")),
            "duration_s": track.get("duration_s"),
            "license": track.get("license", ""),
            "url": f"/api/media/online-stream/{mid}",
            "available": True,
        }
        if (track.get("meta") or {}).get("cover_ref"):
            out["cover_url"] = f"/api/media/online-cover/{mid}"
        return out

    def get_cached_track(self, media_id: str) -> dict | None:
        if not media_id:
            return None
        with closing(sqlite3.connect(self.cache_db_path, timeout=5)) as db:
            row = db.execute(
                """SELECT media_id, provider, external_id, title, artist, album,
                          duration_s, live, stream_url, license, meta_json
                   FROM online_tracks WHERE media_id=?""",
                (media_id,),
            ).fetchone()
        if not row:
            return None
        meta = json.loads(row[10] or "{}")
        out = {
            "media_id": row[0],
            "provider": row[1],
            "external_id": row[2],
            "title": row[3],
            "artist": row[4],
            "album": row[5],
            "duration_s": row[6],
            "live": bool(row[7]),
            "raw_url": row[8],
            "license": row[9],
            "meta": meta,
        }
        if meta.get("cover_ref"):
            out["cover_url"] = f"/api/media/online-cover/{row[0]}"
        return out

    def _navidrome_params(self, cfg_nav: dict, extra: dict | None = None) -> tuple[str, dict]:
        pw = _unprotect_secret("navidrome", cfg_nav.get("password_enc") or "")
        if not cfg_nav.get("url") or not cfg_nav.get("username") or not pw:
            raise ValueError("Navidrome 凭据未配置或无法解密")
        salt = secrets.token_hex(6)
        token = hashlib.md5((pw + salt).encode("utf-8")).hexdigest()
        params = {
            "u": cfg_nav["username"],
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "MOSS550W",
            "f": "json",
            **(extra or {}),
        }
        return cfg_nav["url"].rstrip("/"), params

    async def ping_navidrome(self, candidate: dict | None = None) -> dict:
        cfg = self._load_raw_config()["navidrome"]
        url = _validate_navidrome_url(candidate.get("url", cfg["url"]) if candidate else cfg["url"])
        user = str((candidate.get("username") if candidate else None) or cfg["username"] or "").strip()
        raw_pw = (candidate.get("password") if candidate else None) or ""
        same_target = (
            bool(cfg.get("url"))
            and urlsplit(url).netloc == urlsplit(cfg["url"]).netloc
            and user == (cfg.get("username") or "")
        )
        pw = raw_pw if raw_pw else (_unprotect_secret("navidrome", cfg.get("password_enc") or "") if same_target else "")
        if not url or not user or not pw:
            raise ValueError("请填写完整的 Navidrome 地址、用户名与密码。")
        salt = secrets.token_hex(6)
        token = hashlib.md5((pw + salt).encode("utf-8")).hexdigest()
        params = {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "MOSS550W", "f": "json"}
        doc = await self._http_get_json(f"{url.rstrip('/')}/rest/ping.view?{urlencode(params)}", allow_internal=True, timeout_s=6.0)
        sub = doc.get("subsonic-response") or {}
        if sub.get("status") != "ok":
            err = (sub.get("error") or {}).get("message") or "认证或服务异常"
            raise ValueError(f"Navidrome 返回错误：{err}")
        return {"ok": True, "type": sub.get("type") or "subsonic", "version": sub.get("serverVersion") or sub.get("version") or ""}

    async def ping_jamendo(self, candidate_id: str | None = None) -> dict:
        cfg_jam = self._load_raw_config()["jamendo"]
        cid = str(candidate_id or "").strip() or _unprotect_secret("jamendo", cfg_jam.get("client_id_enc") or "")
        if not cid:
            return {"ok": False, "configured": False, "detail": ""}
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", cid):
            return {"ok": False, "configured": True, "error": True, "detail": "Jamendo Client ID 格式无效"}
        params = {"client_id": cid, "format": "json", "audioformat": "mp32", "limit": 1}
        doc = await self._http_get_json(f"https://api.jamendo.com/v3.0/tracks/?{urlencode(params)}", timeout_s=5.5)
        hdr = (doc.get("headers") or {}) if isinstance(doc, dict) else {}
        if hdr.get("status") == "failed":
            msg = str(hdr.get("error_message") or f"code {hdr.get('code')}").strip()
            return {"ok": False, "configured": True, "error": True, "detail": f"Jamendo 校验失败：{msg}"}
        results = doc.get("results") or [] if isinstance(doc, dict) else []
        if not results:
            return {"ok": False, "configured": True, "error": True, "detail": "Jamendo 返回空结果"}
        sample = str(results[0].get("name") or "OK").strip()[:40]
        return {"ok": True, "configured": True, "detail": f"Jamendo OK ({sample})"}

    def _navidrome_song_to_track(self, s: dict) -> dict | None:
        sid = str(s.get("id") or "").strip()
        if not sid:
            return None
        meta: dict = {"suffix": str(s.get("suffix") or "").lower()}
        cover_art = str(s.get("coverArt") or "").strip()
        if cover_art:
            meta["cover_ref"] = cover_art
        return {
            "media_id": _media_id("navidrome", sid),
            "provider": "navidrome",
            "external_id": sid,
            "title": str(s.get("title") or "Untitled").strip()[:140],
            "artist": str(s.get("artist") or "").strip()[:100],
            "album": str(s.get("album") or "").strip()[:100],
            "duration_s": float(s["duration"]) if s.get("duration") else None,
            "live": False,
            "raw_url": f"navidrome://{sid}",
            "license": "private-server",
            "meta": meta,
        }

    async def _search_navidrome(self, query: str, limit: int, cfg_nav: dict) -> list[dict]:
        if not cfg_nav.get("enabled") or not cfg_nav.get("url"):
            return []
        try:
            q = (query or "").strip()
            if q:
                base, params = self._navidrome_params(cfg_nav, {"query": q, "songCount": limit, "artistCount": 0, "albumCount": 0})
                doc = await self._http_get_json(f"{base}/rest/search3.view?{urlencode(params)}", allow_internal=True)
                songs = ((doc.get("subsonic-response") or {}).get("searchResult3") or {}).get("song") or []
            else:
                base, params = self._navidrome_params(cfg_nav, {"size": limit})
                doc = await self._http_get_json(f"{base}/rest/getRandomSongs.view?{urlencode(params)}", allow_internal=True)
                songs = ((doc.get("subsonic-response") or {}).get("randomSongs") or {}).get("song") or []
        except Exception:
            return []
        out = []
        for s in songs[:limit]:
            item = self._navidrome_song_to_track(s)
            if item:
                out.append(item)
        return out

    async def _search_audius(self, query: str, limit: int) -> list[dict]:
        q = (query or "").strip()
        if q:
            url = f"https://api.audius.co/v1/tracks/search?{urlencode({'query': q, 'limit': limit + 4})}"
        else:
            url = f"https://api.audius.co/v1/tracks/trending?{urlencode({'limit': limit + 4})}"
        doc = None
        try:
            doc = await self._http_get_json(url, timeout_s=5.0)
        except Exception:
            doc = None
        tracks = (doc or {}).get("data") or []
        if not tracks and q:
            hint = _translate_hints(q)
            if hint and hint != q:
                try:
                    doc2 = await self._http_get_json(
                        f"https://api.audius.co/v1/tracks/search?{urlencode({'query': hint, 'limit': limit + 4})}",
                        timeout_s=5.0,
                    )
                    tracks = (doc2 or {}).get("data") or []
                except Exception:
                    tracks = []
        out = []
        for tr in tracks:
            tid = str(tr.get("id") or "").strip()
            if not tid:
                continue
            if tr.get("is_streamable") is False or (tr.get("access") or {}).get("stream") is False:
                continue
            artist_name = str((tr.get("user") or {}).get("name") or "").strip()
            art = tr.get("artwork") if isinstance(tr.get("artwork"), dict) else {}
            art_url = str(art.get("480x480") or art.get("150x150") or "").strip()
            meta = {}
            if art_url.startswith("https://"):
                meta["cover_ref"] = art_url
            out.append({
                "media_id": _media_id("audius", tid),
                "provider": "audius",
                "external_id": tid,
                "title": str(tr.get("title") or "Audius Track").strip()[:140],
                "artist": artist_name[:100],
                "album": str(tr.get("genre") or "").strip()[:80],
                "duration_s": float(tr["duration"]) if tr.get("duration") else None,
                "live": False,
                "raw_url": f"https://api.audius.co/v1/tracks/{quote(tid, safe='')}/stream",
                "license": "open-audius",
                "meta": meta,
            })
            if len(out) >= limit:
                break
        return out

    async def _search_archive(self, query: str, limit: int) -> list[dict]:
        term = _translate_hints(query) if query and query.strip() else "instrumental classical piano"
        safe_q = re.sub(r'["()\\/:]+', " ", term).strip()
        if not safe_q:
            safe_q = "instrumental music"
        lucene = f"mediatype:(audio) AND ({safe_q}) AND format:(MP3 OR OGG OR VBR MP3)"
        url = "https://archive.org/advancedsearch.php?" + urlencode(
            {
                "q": lucene,
                "fl[]": ["identifier", "title", "creator", "downloads"],
                "sort[]": "downloads desc",
                "rows": min(8, limit + 4),
                "page": 1,
                "output": "json",
            },
            doseq=True,
        )
        try:
            doc = await self._http_get_json(url, timeout_s=5.0)
        except Exception:
            return []
        docs = ((doc.get("response") or {}).get("docs")) or []
        if not docs:
            return []

        async def resolve_item(item: dict) -> dict | None:
            ident = str(item.get("identifier") or "").strip()
            if not ident or not re.fullmatch(r"[A-Za-z0-9._-]+", ident):
                return None
            try:
                meta = await self._http_get_json(f"https://archive.org/metadata/{quote(ident, safe='')}", timeout_s=4.5)
            except Exception:
                return None
            files = meta.get("files") or []
            audio_file = next(
                (
                    f for f in files
                    if str(f.get("name") or "").lower().endswith((".mp3", ".ogg", ".m4a"))
                    and f.get("source") in (None, "original", "derivative")
                ),
                None,
            )
            if not audio_file:
                return None
            fname = str(audio_file["name"])
            raw_url = f"https://archive.org/download/{quote(ident, safe='')}/{quote(fname, safe='/')}"
            creator = item.get("creator")
            if isinstance(creator, list):
                creator = ", ".join(str(x) for x in creator[:2])
            dur = None
            try:
                if audio_file.get("length"):
                    dur = float(audio_file["length"])
            except (TypeError, ValueError):
                dur = None
            return {
                "media_id": _media_id("archive", ident),
                "provider": "archive",
                "external_id": ident,
                "title": str(item.get("title") or Path(fname).stem).strip()[:140],
                "artist": str(creator or "Internet Archive").strip()[:100],
                "album": "",
                "duration_s": dur,
                "live": False,
                "raw_url": raw_url,
                "license": "public-domain / cc",
            }

        resolved = await asyncio.gather(*(resolve_item(d) for d in docs[: min(5, limit + 2)]), return_exceptions=True)
        return [r for r in resolved if isinstance(r, dict)][:limit]

    async def _search_radiobrowser(self, query: str, limit: int) -> list[dict]:
        q = (query or "").strip()
        params: dict[str, str | int] = {
            "hidebroken": "true",
            "order": "votes",
            "reverse": "true",
            "limit": limit + 6,
        }
        if re.search(r"粤语|香港|港台", q):
            params["language"] = "cantonese"
        elif not q or re.search(r"华语|中文|国语|普通话|怀旧|中国|音乐台|金曲", q):
            params["language"] = "chinese"
            params["tag"] = "music"
        elif re.search(r"爵士|jazz", q, re.I):
            params["tag"] = "jazz"
        elif re.search(r"古典|classical", q, re.I):
            params["tag"] = "classical"
        elif re.search(r"lofi|lo-fi|chill|轻音乐|纯音乐|学习", q, re.I):
            params["tag"] = "lofi"
        elif re.search(r"摇滚|rock", q, re.I):
            params["tag"] = "rock"
        elif re.search(r"pop|流行|英文", q, re.I):
            params["tag"] = "pop"
        else:
            params["name"] = re.sub(r"(?:在线|网络|电台|广播|收音机|\bfm\b|\bradio\b)", "", q, flags=re.I).strip() or "music"

        stations = None
        for host in ("https://all.api.radio-browser.info", "https://de1.api.radio-browser.info"):
            try:
                stations = await self._http_get_json(f"{host}/json/stations/search?{urlencode(params)}", timeout_s=5.0)
                if isinstance(stations, list) and stations:
                    break
            except Exception:
                continue
        if not isinstance(stations, list):
            return []

        out = []
        for st in stations:
            if not isinstance(st, dict) or st.get("lastcheckok") == 0:
                continue
            codec = str(st.get("codec") or "").upper()
            if codec and codec not in ("MP3", "AAC", "AAC+", "OGG", "OPUS"):
                continue
            stream_url = str(st.get("url_resolved") or st.get("url") or "").strip()
            try:
                _validate_public_url(stream_url, allow_http=True)
            except ValueError:
                continue
            uuid = str(st.get("stationuuid") or stream_url)
            country = str(st.get("countrycode") or st.get("country") or "").strip()
            tags = str(st.get("tags") or "").strip()
            artist_note = " · ".join(p for p in ("LIVE", country, tags[:40]) if p)
            out.append({
                "media_id": _media_id("radiobrowser", uuid),
                "provider": "radiobrowser",
                "external_id": uuid,
                "title": str(st.get("name") or "Live Radio").strip()[:120],
                "artist": artist_note[:100] or "Radio Browser",
                "album": "",
                "duration_s": None,
                "live": True,
                "raw_url": stream_url,
                "license": "public-radio",
            })
            if len(out) >= limit:
                break
        return out

    async def _search_jamendo(self, query: str, limit: int, cfg_jam: dict) -> list[dict]:
        cid = _unprotect_secret("jamendo", cfg_jam.get("client_id_enc") or "")
        if not cfg_jam.get("enabled") or not cid:
            return []
        q = _translate_hints(query or "")
        params = {
            "client_id": cid,
            "format": "json",
            "audioformat": "mp32",
            "limit": limit + 2,
            "order": "popularity_total",
        }
        if q:
            params["search"] = q
        try:
            doc = await self._http_get_json(f"https://api.jamendo.com/v3.0/tracks/?{urlencode(params)}", timeout_s=5.5)
        except Exception:
            return []
        out = []
        for tr in doc.get("results") or []:
            tid = str(tr.get("id") or "")
            audio_url = str(tr.get("audio") or "").strip()
            if not tid or not audio_url:
                continue
            try:
                _validate_public_url(audio_url, allow_http=False)
            except ValueError:
                continue
            art_url = str(tr.get("album_image") or tr.get("image") or "").strip()
            meta = {"cover_ref": art_url} if art_url.startswith("https://") else {}
            out.append({
                "media_id": _media_id("jamendo", tid),
                "provider": "jamendo",
                "external_id": tid,
                "title": str(tr.get("name") or "Jamendo Track").strip()[:140],
                "artist": str(tr.get("artist_name") or "").strip()[:100],
                "album": str(tr.get("album_name") or "").strip()[:100],
                "duration_s": float(tr["duration"]) if tr.get("duration") else None,
                "live": False,
                "raw_url": audio_url,
                "license": str(tr.get("license_ccurl") or "CC"),
                "meta": meta,
            })
            if len(out) >= limit:
                break
        return out

    async def search_tracks(
        self,
        query: str = "",
        *,
        limit: int = 6,
        explicit_radio: bool = False,
        preferred_provider: str | None = None,
        exclude_providers: set[str] | None = None,
        blocked_ids: set[str] | None = None,
        exclude_media_id: str | None = None,
    ) -> dict:
        """Query enabled online sources concurrently, deduplicate, cache and rank playable tracks."""
        cfg = self._load_raw_config()
        if self.is_isolated() or not cfg.get("enabled", True):
            return {"items": [], "providers_consulted": []}

        blocked = blocked_ids or set()
        excluded = exclude_providers or set()
        q = (query or "").strip()
        is_radio = explicit_radio or bool(RADIO_HINT_RE.search(q))

        enabled_map = {
            "navidrome": bool(cfg["navidrome"].get("enabled") and cfg["navidrome"].get("url")),
            "audius": bool(cfg.get("audius_enabled", True)),
            "archive": bool(cfg.get("archive_enabled", True)),
            "jamendo": bool(cfg["jamendo"].get("enabled") and cfg["jamendo"].get("client_id_enc")),
            "radiobrowser": bool(cfg.get("radiobrowser_enabled", True)),
        }

        if preferred_provider and preferred_provider in DEFAULT_SOURCE_ORDER:
            targets = [preferred_provider] if (enabled_map.get(preferred_provider) and preferred_provider not in excluded) else []
        elif is_radio:
            targets = ["radiobrowser"] if (enabled_map["radiobrowser"] and "radiobrowser" not in excluded) else []
        else:
            targets = [p for p in cfg["source_order"] if enabled_map.get(p) and p != "radiobrowser" and p not in excluded]

        async def query_one(name: str) -> list[dict]:
            if name == "navidrome":
                return await self._search_navidrome(q, limit, cfg["navidrome"])
            if name == "audius":
                return await self._search_audius(q, limit)
            if name == "archive":
                return await self._search_archive(q, limit)
            if name == "jamendo":
                return await self._search_jamendo(q, limit, cfg["jamendo"])
            if name == "radiobrowser":
                return await self._search_radiobrowser(q, limit)
            return []

        raw_batches: list[list[dict]] = []
        consulted: list[str] = []
        if targets:
            consulted.extend(targets)
            gathered = await asyncio.gather(*(query_one(name) for name in targets), return_exceptions=True)
            for batch in gathered:
                if isinstance(batch, list):
                    raw_batches.append(batch)

        seen_keys: set[tuple[str, str]] = set()
        merged: list[dict] = []
        for batch in raw_batches:
            for item in batch:
                mid = item["media_id"]
                if mid in blocked or (exclude_media_id and mid == exclude_media_id):
                    continue
                dedup_key = (item["title"].casefold(), item["artist"].casefold())
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                merged.append(self._cache_track(item))
                if len(merged) >= limit:
                    break
            if len(merged) >= limit:
                break

        # If on-demand search produced nothing and Radio Browser is enabled, offer Chinese/genre live radio cleanly.
        if not merged and not preferred_provider and not is_radio and enabled_map.get("radiobrowser"):
            consulted.append("radiobrowser")
            for item in await self._search_radiobrowser(q, limit):
                mid = item["media_id"]
                if mid not in blocked and mid != exclude_media_id:
                    merged.append(self._cache_track(item))
                if len(merged) >= limit:
                    break

        return {"items": merged, "providers_consulted": consulted}

    def resolve_stream_target(self, media_id: str) -> dict:
        """Resolve server-side upstream URL & SSRF policy for /api/media/online-stream/{media_id}."""
        cached = self.get_cached_track(media_id)
        if not cached:
            raise FileNotFoundError("未找到在线曲目缓存，请重新搜歌。")
        provider = cached["provider"]
        if provider == "navidrome":
            cfg_nav = self._load_raw_config()["navidrome"]
            base, params = self._navidrome_params(cfg_nav, {"id": cached["external_id"]})
            suffix = str((cached.get("meta") or {}).get("suffix") or "")
            if suffix in ("ape", "wma", "dsf", "dff", "aiff", "aif", "amr"):
                params["format"] = "mp3"
            return {
                "url": f"{base}/rest/stream.view?{urlencode(params)}",
                "allow_internal": True,
                "allowed_netloc": urlsplit(base).netloc,
                "live": False,
                "provider": provider,
            }
        allow_http = provider == "radiobrowser"
        _validate_public_url(cached["raw_url"], allow_http=allow_http)
        return {
            "url": cached["raw_url"],
            "allow_internal": False,
            "allow_http": allow_http,
            "live": bool(cached.get("live")),
            "provider": provider,
        }

    def resolve_cover_target(self, media_id: str) -> dict:
        """Resolve server-side artwork URL & SSRF policy for /api/media/online-cover/{media_id}."""
        cached = self.get_cached_track(media_id)
        if not cached:
            raise FileNotFoundError("未找到曲目缓存。")
        cover_ref = str((cached.get("meta") or {}).get("cover_ref") or "").strip()
        if not cover_ref:
            raise FileNotFoundError("该曲目无封面信息。")
        if cached["provider"] == "navidrome":
            cfg_nav = self._load_raw_config()["navidrome"]
            base, params = self._navidrome_params(cfg_nav, {"id": cover_ref, "size": 256})
            return {
                "url": f"{base}/rest/getCoverArt.view?{urlencode(params)}",
                "allow_internal": True,
                "allowed_netloc": urlsplit(base).netloc,
                "allow_http": False,
            }
        _validate_public_url(cover_ref, allow_http=False)
        return {
            "url": cover_ref,
            "allow_internal": False,
            "allowed_netloc": "",
            "allow_http": False,
        }

    async def browse_navidrome(self, *, kind: str = "playlists", item_id: str = "", limit: int = 18) -> dict:
        """Browse Navidrome playlists or albums and return cached track queues."""
        cfg = self._load_raw_config()
        cfg_nav = cfg["navidrome"]
        if self.is_isolated() or not cfg.get("enabled", True):
            raise ValueError("当前处于离线隔离模式或在线曲库已关闭。")
        if not cfg_nav.get("enabled") or not cfg_nav.get("url"):
            raise ValueError("尚未启用并配置 Navidrome 私有曲库。")
        mode = str(kind or "playlists").strip().lower()
        clean_id = str(item_id or "").strip()[:80]
        max_n = min(40, max(1, int(limit or 18)))

        def _check_sub(doc) -> dict:
            sub = (doc.get("subsonic-response") if isinstance(doc, dict) else None) or {}
            if not isinstance(sub, dict) or sub.get("status") != "ok":
                err = ((sub.get("error") or {}) if isinstance(sub, dict) else {}).get("message") or "Navidrome 查询失败"
                raise ValueError(f"Navidrome 返回错误：{err}")
            return sub

        if mode == "playlists":
            base, params = self._navidrome_params(cfg_nav)
            sub = _check_sub(await self._http_get_json(f"{base}/rest/getPlaylists.view?{urlencode(params)}", allow_internal=True))
            pls = (sub.get("playlists") or {}).get("playlist") or []
            return {
                "kind": "playlists",
                "items": [
                    {
                        "id": str(pl.get("id") or ""),
                        "name": str(pl.get("name") or "Playlist").strip()[:120],
                        "song_count": int(pl.get("songCount") or 0),
                        "duration_s": float(pl["duration"]) if pl.get("duration") else None,
                    }
                    for pl in pls[:max_n] if pl.get("id")
                ],
            }
        if mode == "albums":
            base, params = self._navidrome_params(cfg_nav, {"type": "newest", "size": max_n})
            sub = _check_sub(await self._http_get_json(f"{base}/rest/getAlbumList2.view?{urlencode(params)}", allow_internal=True))
            albums = (sub.get("albumList2") or {}).get("album") or []
            return {
                "kind": "albums",
                "items": [
                    {
                        "id": str(al.get("id") or ""),
                        "name": str(al.get("name") or "Album").strip()[:120],
                        "artist": str(al.get("artist") or "").strip()[:100],
                        "song_count": int(al.get("songCount") or 0),
                        "year": al.get("year"),
                    }
                    for al in albums[:max_n] if al.get("id")
                ],
            }
        if mode == "playlist":
            if not clean_id:
                raise ValueError("缺少歌单 ID。")
            base, params = self._navidrome_params(cfg_nav, {"id": clean_id})
            sub = _check_sub(await self._http_get_json(f"{base}/rest/getPlaylist.view?{urlencode(params)}", allow_internal=True))
            pl = sub.get("playlist") or {}
            tracks = []
            for s in (pl.get("entry") or [])[:max_n]:
                item = self._navidrome_song_to_track(s)
                if item:
                    tracks.append(self._cache_track(item))
            return {
                "kind": "playlist",
                "id": clean_id,
                "name": str(pl.get("name") or "Playlist").strip()[:120],
                "tracks": tracks,
            }
        if mode == "album":
            if not clean_id:
                raise ValueError("缺少专辑 ID。")
            base, params = self._navidrome_params(cfg_nav, {"id": clean_id})
            sub = _check_sub(await self._http_get_json(f"{base}/rest/getAlbum.view?{urlencode(params)}", allow_internal=True))
            al = sub.get("album") or {}
            tracks = []
            for s in (al.get("song") or [])[:max_n]:
                item = self._navidrome_song_to_track(s)
                if item:
                    tracks.append(self._cache_track(item))
            return {
                "kind": "album",
                "id": clean_id,
                "name": str(al.get("name") or "Album").strip()[:120],
                "artist": str(al.get("artist") or "").strip()[:100],
                "tracks": tracks,
            }
        raise ValueError("不支持的浏览类型（仅支持 playlists / albums / playlist / album）。")

    async def scrobble_navidrome(self, media_id: str, *, submission: bool = True) -> dict:
        """Report playback start or completion back to Navidrome via Subsonic scrobble.view."""
        cfg = self._load_raw_config()
        cfg_nav = cfg["navidrome"]
        if self.is_isolated() or not cfg.get("enabled", True) or not cfg_nav.get("enabled"):
            return {"ok": False, "skipped": True}
        cached = self.get_cached_track(media_id)
        if not cached or cached.get("provider") != "navidrome":
            return {"ok": False, "skipped": True}
        try:
            base, params = self._navidrome_params(
                cfg_nav,
                {
                    "id": cached["external_id"],
                    "submission": "true" if submission else "false",
                    "time": int(datetime.now(timezone.utc).timestamp() * 1000),
                },
            )
            doc = await self._http_get_json(f"{base}/rest/scrobble.view?{urlencode(params)}", allow_internal=True, timeout_s=4.0)
            ok = (doc.get("subsonic-response") or {}).get("status") == "ok"
            return {"ok": bool(ok)}
        except Exception:
            return {"ok": False}

    async def get_lyrics(self, media_id: str, *, title: str = "", artist: str = "") -> dict:
        """Fetch synced/plain LRC lyrics via Navidrome OpenSubsonic or LRCLIB with SQLite caching."""
        cfg = self._load_raw_config()
        if self.is_isolated() or not cfg.get("lyrics_lrclib", True):
            return {"lines": [], "has_lrc": False, "source": "disabled"}

        cached_track = self.get_cached_track(media_id)
        raw_title = (title or (cached_track or {}).get("title") or "").strip()
        raw_artist = (artist or (cached_track or {}).get("artist") or "").strip()
        q_title = re.sub(r"^\d{1,3}[\s._-]+", "", raw_title).strip() or raw_title
        q_artist = "" if raw_artist in {"[Unknown Artist]", "Unknown Artist", "未知歌手"} else raw_artist
        if not q_title or (cached_track and cached_track.get("live")):
            return {"lines": [], "has_lrc": False, "source": "none"}

        if cached_track and cached_track.get("provider") == "navidrome" and cfg["navidrome"].get("enabled"):
            try:
                base, params = self._navidrome_params(cfg["navidrome"], {"id": cached_track["external_id"]})
                nd_doc = await self._http_get_json(f"{base}/rest/getLyricsBySongId.view?{urlencode(params)}", allow_internal=True, timeout_s=3.5)
                lrc_list = ((nd_doc.get("subsonic-response") or {}).get("lyricsList") or {}).get("structuredLyrics") or []
                for entry in lrc_list:
                    line_objs = entry.get("line") or []
                    if entry.get("synced") and line_objs:
                        nd_lines = [
                            {"time": round((ln.get("start") or 0) / 1000.0, 3), "text": str(ln.get("value") or "").strip()}
                            for ln in line_objs if str(ln.get("value") or "").strip()
                        ]
                        if nd_lines:
                            return {"lines": nd_lines, "has_lrc": True, "synced": True, "source": "navidrome"}
            except Exception:
                pass

        cache_key = hashlib.sha256(f"{q_title.casefold()}|{q_artist.casefold()}".encode("utf-8")).hexdigest()[:24]
        with closing(sqlite3.connect(self.cache_db_path, timeout=5)) as db:
            row = db.execute(
                "SELECT synced, lines_json, source, updated_at FROM online_lyrics_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row:
            lines = json.loads(row[1] or "[]")
            if lines:
                return {"lines": lines, "has_lrc": True, "synced": bool(row[0]), "source": row[2]}
            # Retry empty misses cleanly after 300s instead of caching permanently
            try:
                age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(row[3])).total_seconds()
                if age_s < 300:
                    return {"lines": [], "has_lrc": False, "synced": False, "source": row[2]}
            except Exception:
                pass

        search_q = " ".join(p for p in (q_title, q_artist) if p)
        try:
            docs = await self._http_get_json(
                f"https://lrclib.net/api/search?{urlencode({'q': search_q})}",
                timeout_s=4.5,
            )
        except Exception:
            return {"lines": [], "has_lrc": False, "source": "lrclib_error"}

        if not isinstance(docs, list):
            docs = []
        best = next((d for d in docs if isinstance(d, dict) and d.get("syncedLyrics")), None)
        lines: list[dict] = []
        synced = False
        if best and best.get("syncedLyrics"):
            lines = _parse_lrc_text(best["syncedLyrics"])
            synced = bool(lines)
        elif docs and isinstance(docs[0], dict) and docs[0].get("plainLyrics"):
            raw_plain = [ln.strip() for ln in str(docs[0]["plainLyrics"]).splitlines() if ln.strip()]
            lines = [{"time": round(i * 4.0, 2), "text": ln} for i, ln in enumerate(raw_plain[:60])]
            synced = False

        now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.cache_db_path, timeout=5)) as db, db:
            db.execute(
                """INSERT INTO online_lyrics_cache(cache_key, synced, lines_json, source, updated_at)
                   VALUES (?, ?, ?, 'lrclib', ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       synced=excluded.synced, lines_json=excluded.lines_json, updated_at=excluded.updated_at""",
                (cache_key, 1 if synced else 0, json.dumps(lines, ensure_ascii=False), now_s),
            )
        return {"lines": lines, "has_lrc": bool(lines), "synced": synced, "source": "lrclib" if lines else "none"}
