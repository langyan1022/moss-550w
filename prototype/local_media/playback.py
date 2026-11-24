import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from time import monotonic, sleep, time

from .library import DEFAULT_DB as DEFAULT_LIBRARY, MediaLibrary


HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "playback.sqlite3"
ACTIONS = ("discover_preview", "play_last", "set_library", "search_and_play", "cancel", "ignore", "clear_selection", "state")


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS contexts (
            session TEXT PRIMARY KEY, artist TEXT NOT NULL DEFAULT '',
            media_id TEXT, root TEXT);
        CREATE TABLE IF NOT EXISTS commands (
            session TEXT NOT NULL, command_id TEXT NOT NULL, action TEXT NOT NULL,
            status TEXT NOT NULL, result TEXT NOT NULL, expires REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(session,command_id));
    """)
    return con


def put(con, session, command, action, status, fields, expires=0):
    result = dict(fields, session=session, command_id=command, action=action, status=status)
    con.execute("""INSERT INTO commands VALUES(?,?,?,?,?,?)
                   ON CONFLICT(session,command_id) DO UPDATE SET
                   status=excluded.status,result=excluded.result,expires=excluded.expires""",
                (session, command, action, status, encode(result), expires))
    return result


def library_at(path):
    if not Path(path).is_file():
        raise ValueError("媒体索引尚未创建")
    return MediaLibrary(path)


def _find_audio(library, artist, title, root):
    if not artist and not title:
        return None, []
    result = library.search(" ".join(filter(None, (artist, title))), kind="audio", limit=50)
    for item in result["items"]:
        if not item.get("available", True) or item["kind"] != "audio":
            continue
        path = Path(item["path"])
        if root and not within_folder(path, root):
            continue
        searchable = (str(item.get("artist") or "") + " " + str(path)).casefold()
        if artist and artist.casefold() not in searchable:
            continue
        if title and title.casefold() != str(item["title"]).casefold() and title.casefold() != path.stem.casefold():
            continue
        return item, []
    return None, result["unavailable_sources"]


def select_media(library, artist, title, root):
    return _find_audio(library, artist, title, root)[0]


def media_fields(item):
    return {k: item.get(k) for k in ("media_id", "path", "title", "artist", "kind", "source", "source_kind")}


def within_folder(path, folder):
    return Path(os.path.abspath(path)).is_relative_to(Path(os.path.abspath(folder)))


def current_state(con, session):
    context = con.execute("SELECT * FROM contexts WHERE session=?", (session,)).fetchone()
    recent = con.execute("SELECT result FROM commands WHERE session=? ORDER BY rowid DESC LIMIT 12", (session,))
    return {"session": session, "context": dict(context) if context else None,
            "recent": [json.loads(row[0]) for row in recent]}


def run_clip(con, session, command, action, item, seconds, library):
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    import pygame
    import soundfile

    fields = media_fields(item)
    try:
        with library.open_media(item["media_id"]) as stream, soundfile.SoundFile(stream) as source:
            rate, channels = source.samplerate, source.channels
            offset = 30 * rate if len(source) > (30 + seconds) * rate else 0
            source.seek(offset)
            pcm = bytes(source.buffer_read(round(seconds * rate), dtype="int16"))
            fields.update(source_duration_s=round(len(source) / rate, 6), clip_start_s=offset / rate,
                          source_format=source.format, sample_rate=rate, channels=channels)
        frames = len(pcm) // (2 * channels)
        if frames == 0:
            raise ValueError("音轨没有可解码的音频帧")
        pygame.mixer.init(frequency=rate, size=-16, channels=channels, buffer=1024, allowedchanges=0)
        clip = pygame.mixer.Sound(buffer=pcm)
        fields.update(decoded_bytes=len(pcm), pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                      pcm_nonzero=any(pcm), clip_s=round(frames / rate, 6), sink="SDL dummy")
        with con:
            con.execute("BEGIN IMMEDIATE")
            status = con.execute("SELECT status FROM commands WHERE session=? AND command_id=?",
                                 (session, command)).fetchone()[0]
            if status == "stopping":
                return put(con, session, command, action, "cancelled", dict(fields, play_started=False))
            if status != "starting":
                raise ValueError("播放请求已过期")
            channel = clip.play()
            if channel is None or not channel.get_busy():
                raise RuntimeError("测试播放器没有开始消费音频")
            start = monotonic()
            fields.update(play_started=True, player_pid=os.getpid(), submitted_frames=frames)
            put(con, session, command, action, "playing", fields, time() + seconds + 20)

        stopped = False
        while channel.get_busy():
            status = con.execute("SELECT status FROM commands WHERE session=? AND command_id=?",
                                 (session, command)).fetchone()[0]
            if status == "stopping":
                channel.stop()
                stopped = True
                break
            if monotonic() - start > seconds + 10:
                raise TimeoutError("静音播放器没有在时限内结束")
            sleep(0.025)
        fields.update(playback_elapsed_s=round(monotonic() - start, 6), channel_finished=not channel.get_busy())
        with con:
            return put(con, session, command, action, "cancelled" if stopped else "played", fields)
    except Exception as exc:
        with con:
            return put(con, session, command, action, "error", dict(fields, error=f"{type(exc).__name__}: {exc}"))
    finally:
        pygame.mixer.quit()


def apply(action, *, session, command_id=None, db=DEFAULT_DB, library_db=DEFAULT_LIBRARY,
          artist=None, title=None, media_id=None, root=None, seconds=3.0, target=None, text=None):
    if action not in ACTIONS or not session or (action != "state" and not command_id):
        raise ValueError("需要有效的动作、会话 ID 和命令 ID")
    if not 0.25 <= seconds <= 30:
        raise ValueError("测试片段为 0.25 至 30 秒")
    with closing(connect(db)) as con:
        with con:
            con.execute("BEGIN IMMEDIATE")
            expired = con.execute("SELECT * FROM commands WHERE session=? AND expires>0 AND expires<?",
                                  (session, time())).fetchall()
            for row in expired:
                put(con, session, row["command_id"], row["action"], "interrupted",
                    dict(json.loads(row["result"]), reason="播放器失联，已停止自动重试"))
            if action == "state":
                return current_state(con, session)
            previous = con.execute("SELECT action,result FROM commands WHERE session=? AND command_id=?",
                                   (session, command_id)).fetchone()
            if previous:
                if previous["action"] != action:
                    raise ValueError("该命令 ID 已绑定其他动作")
                return dict(json.loads(previous["result"]), repeated=True)
            con.execute("INSERT OR IGNORE INTO contexts(session) VALUES(?)", (session,))
            ctx = con.execute("SELECT * FROM contexts WHERE session=?", (session,)).fetchone()

            def done(status, **fields):
                return put(con, session, command_id, action, status, fields)

            if action == "clear_selection":
                con.execute("UPDATE contexts SET media_id=NULL WHERE session=?", (session,))
                return done("selection_cleared")
            active = con.execute("""SELECT * FROM commands WHERE session=?
                                   AND status IN ('starting','playing','stopping')""", (session,)).fetchone()
            if action == "ignore":
                if text and text.strip(" \t\n\r.。…"):
                    raise ValueError("仅忽略空白或句点输入")
                return done("ignored", received_text=text or "", media_id=ctx["media_id"])
            if action == "cancel":
                if not active or (target and active["command_id"] != target):
                    return done("nothing_playing", target=target)
                put(con, session, active["command_id"], active["action"], "stopping",
                    json.loads(active["result"]), active["expires"])
                return done("stop_requested", target=active["command_id"])
            if active:
                return done("busy", target=active["command_id"])
            if action == "set_library":
                if not root or not Path(root).is_absolute():
                    return done("unavailable", root=root)
                library = library_at(library_db)
                if not any(within_folder(root, src["path"]) for src in library.sources()):
                    return done("unavailable", root=root)
                folder = os.path.abspath(root)
                con.execute("UPDATE contexts SET root=? WHERE session=?", (folder, session))
                return done("library_selected", root=folder, artist=ctx["artist"])
            library = library_at(library_db)
            if action == "play_last":
                if not ctx["media_id"]:
                    return done("unavailable", media_id=None)
                selected = library.route("", selected=ctx["media_id"])
                item = next(iter(selected["items"]), None)
                if not item or not item.get("available", True) or item["kind"] != "audio":
                    return done("unavailable", media_id=ctx["media_id"], reason=selected.get("reason"))
                if ctx["root"] and not within_folder(item["path"], ctx["root"]):
                    return done("unavailable", media_id=ctx["media_id"], root=ctx["root"])
            else:
                artist = artist or ctx["artist"]
                if media_id and action == "discover_preview":
                    selected = library.route("", selected=media_id)
                    if not selected["items"]:
                        return done("unavailable", media_id=media_id, reason=selected["reason"])
                    item = selected["items"][0]
                    if item and (item["kind"] != "audio" or not item.get("available", True)
                                 or (ctx["root"] and not within_folder(item["path"], ctx["root"]))):
                        item = None
                else:
                    item, problems = _find_audio(library, artist, title, ctx["root"])
                    if not item and problems:
                        return done("unavailable", artist=artist, title=title, unavailable_sources=problems)
                if not item:
                    con.execute("UPDATE contexts SET artist=?,media_id=NULL WHERE session=?", (artist, session))
                    return done("not_found", artist=artist, title=title, root=ctx["root"])
                con.execute("UPDATE contexts SET artist=?,media_id=? WHERE session=?",
                            (artist or item.get("artist") or "", item["media_id"], session))
                if action == "discover_preview":
                    return done("preview_available", **media_fields(item))
            put(con, session, command_id, action, "starting", media_fields(item), time() + seconds + 30)
        return run_clip(con, session, command_id, action, item, seconds, library)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--session", required=True)
    parser.add_argument("--command-id")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--library-db", type=Path, default=DEFAULT_LIBRARY)
    for parameter in ("artist", "title", "media-id", "root", "target", "text"):
        parser.add_argument("--" + parameter)
    parser.add_argument("--seconds", type=float, default=3)
    args = vars(parser.parse_args())
    try:
        print(encode(apply(**args)))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(encode({"status": "error", "error": str(exc)}))
        raise SystemExit(1)
