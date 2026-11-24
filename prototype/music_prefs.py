"""Music preferences (favorites & blocklist), sleep timer parsing, and local .lrc lyrics parser (v5.5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sqlite3


class MusicPrefsStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS music_prefs (
                media_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('favorite', 'blocked')),
                title TEXT NOT NULL DEFAULT '',
                artist TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )"""
        )
        self.db.commit()

    def set_status(self, media_id: str, status: str, *, title: str = "", artist: str = "") -> dict:
        mid = (media_id or "").strip()
        if not mid:
            raise ValueError("缺少媒体编号")
        now_s = datetime.now().isoformat(timespec="seconds")
        if status in {"none", "clear", ""}:
            self.db.execute("DELETE FROM music_prefs WHERE media_id=?", (mid,))
            self.db.commit()
            return {"media_id": mid, "status": "none"}
        if status not in {"favorite", "blocked"}:
            raise ValueError("状态仅支持 favorite、blocked 或 clear")
        self.db.execute(
            "INSERT INTO music_prefs (media_id, status, title, artist, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(media_id) DO UPDATE SET status=excluded.status, title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE music_prefs.title END, "
            "artist=CASE WHEN excluded.artist<>'' THEN excluded.artist ELSE music_prefs.artist END, updated_at=excluded.updated_at",
            (mid, status, (title or "")[:120], (artist or "")[:80], now_s),
        )
        self.db.commit()
        return {"media_id": mid, "status": status, "title": title, "artist": artist, "updated_at": now_s}

    def get_status(self, media_id: str) -> str:
        row = self.db.execute("SELECT status FROM music_prefs WHERE media_id=?", (media_id,)).fetchone()
        return row[0] if row else "none"

    def list_by_status(self, status: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT media_id, status, title, artist, updated_at FROM music_prefs WHERE status=? ORDER BY updated_at DESC",
            (status,),
        ).fetchall()
        return [{"media_id": r[0], "status": r[1], "title": r[2], "artist": r[3], "updated_at": r[4]} for r in rows]

    def blocked_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT media_id FROM music_prefs WHERE status='blocked'").fetchall()}

    def snapshot(self) -> dict:
        favs = self.list_by_status("favorite")
        blocked = self.list_by_status("blocked")
        return {
            "favorites": favs,
            "blocked": blocked,
            "favorite_ids": [x["media_id"] for x in favs],
            "blocked_ids": [x["media_id"] for x in blocked],
        }


_LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")


def parse_lrc_text(text: str) -> list[dict]:
    """Parse timed [MM:SS.xx] LRC lines into sorted [{time, text}] list."""
    lines: list[dict] = []
    for raw_line in (text or "").splitlines():
        stamps = list(_LRC_TIME_RE.finditer(raw_line))
        if not stamps:
            continue
        lyric = _LRC_TIME_RE.sub("", raw_line).strip()
        if not lyric:
            continue
        for m in stamps:
            mins = int(m.group(1))
            secs = int(m.group(2))
            frac = m.group(3) or "0"
            ms = int(frac.ljust(3, "0")[:3])
            t = round(mins * 60 + secs + ms / 1000.0, 3)
            lines.append({"time": t, "text": lyric})
    lines.sort(key=lambda x: x["time"])
    return lines


def load_lrc_for_media_path(audio_path: str | Path) -> list[dict]:
    """Locate sibling `<stem>.lrc` next to the local audio file and return sorted [{time, text}]."""
    if not audio_path:
        return []
    p = Path(audio_path)
    lrc = p.with_suffix(".lrc")
    if not lrc.is_file():
        return []
    raw = lrc.read_bytes()
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    return parse_lrc_text(text)


_ZH_NUM_MAP = {
    "半": 0.5, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十五": 15, "二十": 20, "三十": 30,
    "四十五": 45, "六十": 60, "九十": 90,
}


def parse_sleep_timer_minutes(text: str) -> int | None:
    """Parse Chinese/English music sleep timer commands like '30分钟后停歌', '半小时后关闭音乐', '取消睡眠定时' -> 0."""
    s = (text or "").strip()
    if re.search(r"(?:取消|关闭|解除).*(?:睡眠定时|定时停[歌播]|停歌定时)", s):
        return 0
    if not re.search(r"(?:停[歌播止]|关[闭掉]?(?:音乐|播[放歌])|睡眠定时|定时停)", s):
        return None
    m = re.search(r"(\d+(?:\.\d+)?|半|十五|二十|三十|四十五|六十|九十|[一两二三四五六七八九十])\s*(?:个?小时|个?钟头|分钟|分|min)", s, re.I)
    if not m:
        return None
    raw_num = m.group(1)
    unit = m.group(0)
    val = float(raw_num) if re.fullmatch(r"\d+(?:\.\d+)?", raw_num) else float(_ZH_NUM_MAP.get(raw_num, 0))
    if val <= 0:
        return None
    mins = round(val * 60) if any(u in unit for u in ("小时", "钟头")) else round(val)
    return max(1, min(240, mins))
