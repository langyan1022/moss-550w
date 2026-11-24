from collections import defaultdict
from html import unescape
import ntpath
import re
import unicodedata

import webvtt


SCHEMA = """
CREATE TABLE IF NOT EXISTS subtitle_scans(source TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS subtitle_files(
    id INTEGER PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    rel TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER NOT NULL, UNIQUE(media_id,rel)
);
CREATE TABLE IF NOT EXISTS embedded_subtitle_scans(
    media_id TEXT PRIMARY KEY REFERENCES media(media_id) ON DELETE CASCADE,
    size INTEGER NOT NULL, modified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subtitle_cues(
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES subtitle_files(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL, searchable TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subtitle_cues_file ON subtitle_cues(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS subtitle_fts USING fts5(
    searchable, content='subtitle_cues', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS subtitle_cue_add AFTER INSERT ON subtitle_cues BEGIN
    INSERT INTO subtitle_fts(rowid,searchable) VALUES(new.id,new.searchable);
END;
CREATE TRIGGER IF NOT EXISTS subtitle_cue_drop AFTER DELETE ON subtitle_cues BEGIN
    INSERT INTO subtitle_fts(subtitle_fts,rowid,searchable) VALUES('delete',old.id,old.searchable);
END;
"""
TRACK_TAG = re.compile(r"(?:[a-z]{2,3}(?:-[a-z0-9]{2,8})*|forced|sdh|cc|default|简中|繁中|简体|繁体|中文|中英|双语)", re.I)
EMBEDDED_PREFIX = ":embedded:"
EMBEDDED_FORMATS = {".mp4": "mov", ".m4v": "mov", ".mov": "mov", ".mkv": "matroska", ".webm": "matroska"}
TEXT_CODECS = {"mov_text", "subrip", "srt", "ssa", "ass", "webvtt", "text"}


def search_text(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def excerpt(text, needle, width=160):
    if len(text) <= width:
        return text
    normalized = unicodedata.normalize("NFKC", text)
    offsets = [i for i, char in enumerate(normalized) for letter in char.casefold() if letter.isalnum()]
    position = search_text(normalized).find(needle)
    start = max(0, offsets[position] - width // 3) if 0 <= position < len(offsets) else 0
    start = min(start, max(0, len(normalized) - width))
    return ("…" if start else "") + normalized[start:start + width] + ("…" if start + width < len(normalized) else "")


def read_cues(files, rel):
    ext = ntpath.splitext(rel)[1].lower()[1:]
    with files.open(files.path(rel)) as stream:
        try:
            captions = webvtt.from_buffer(stream, format=ext)
        except UnicodeDecodeError:
            if ext != "srt":
                raise
            stream.seek(0)
            captions = webvtt.from_buffer(stream, format=ext, encoding="gb18030")
    if captions.parse_issues and not captions:
        raise ValueError("没有可解析的字幕时间段")
    for cue in captions:
        start, end = cue.start_in_milliseconds, cue.end_in_milliseconds
        text = " ".join(unescape(cue.text).split())
        plain = search_text(text)
        if plain and 0 <= start < end:
            yield start, end, text, plain


def sync_folder(db, files, videos, sidecars, old, seen, errors):
    by_name = defaultdict(list)
    for rel, media_id, _, _ in videos:
        by_name[ntpath.splitext(ntpath.basename(rel))[0].casefold()].append(media_id)
    for rel, size, modified in sidecars:
        changed = []
        for media_id in matching_videos(rel, by_name):
            key = (media_id, rel)
            seen.add(key)
            prior = old.get(key)
            if prior and (prior["size"], prior["modified"]) == (size, modified):
                continue
            if prior:
                db.execute("DELETE FROM subtitle_files WHERE id=?", (prior["id"],))
            changed.append(media_id)
        if not changed:
            continue
        try:
            cues = list(read_cues(files, rel))
        except (OSError, ValueError) as exc:
            errors.append(f"字幕 {rel}：{exc}")
            continue
        for media_id in changed:
            save_cues(db, media_id, rel, size, modified, cues)


def save_cues(db, media_id, rel, size, modified, cues):
    doc = db.execute("INSERT INTO subtitle_files(media_id,rel,size,modified) VALUES(?,?,?,?)",
                     (media_id, rel, size, modified)).lastrowid
    db.executemany("INSERT INTO subtitle_cues(file_id,start_ms,end_ms,text,searchable) VALUES(?,?,?,?,?)",
                   ((doc, *cue) for cue in cues))


def read_embedded(files, rel, format_name):
    import av
    from av.stream import Discard
    from pysubs2 import SSAEvent

    # 主视频使用已配置来源打开的文件对象。容器中的引用不能打开其他文件或联网。
    with files.open(files.path(rel)) as handle, av.open(
        handle, format=format_name, options={"protocol_whitelist": "pipe", "enable_drefs": "0"},
    ) as video:
        tracks = [stream for stream in video.streams.subtitles if stream.codec_context.name in TEXT_CODECS]
        if not tracks:
            return {}
        for stream in video.streams:
            if stream not in tracks:
                stream.discard = Discard.all
        cues = defaultdict(list)
        origin = (video.start_time or 0) * 1000 / av.time_base
        for packet in video.demux(*tracks):
            if packet.pts is None or not packet.duration or packet.time_base is None:
                continue
            start = max(0, round(packet.pts * packet.time_base * 1000 - origin))
            end = round((packet.pts + packet.duration) * packet.time_base * 1000 - origin)
            if end <= start:
                continue
            for subtitle in packet.decode():
                data = getattr(subtitle, "dialogue", None) or getattr(subtitle, "text", b"")
                raw = data.decode("utf-8", errors="replace")
                event = SSAEvent(text=raw)
                if event.is_drawing:
                    continue
                text = " ".join(unescape(event.plaintext).split())
                if plain := search_text(text):
                    cues[packet.stream.index].append((start, end, text, plain))
        return cues


def sync_embedded(db, files, videos, scans, old_tracks, seen, errors):
    for rel, media_id, size, modified in videos:
        format_name = EMBEDDED_FORMATS.get(ntpath.splitext(rel)[1].casefold())
        if not format_name:
            continue
        prior_tracks = old_tracks.get(media_id, {})
        if scans.get(media_id) == (size, modified):
            seen.update((media_id, name) for name in prior_tracks)
            continue
        try:
            # 完整读取后再替换，失败的读取不会发布半份字幕。
            tracks = read_embedded(files, rel, format_name)
        except (OSError, ValueError) as exc:
            seen.update((media_id, name) for name in prior_tracks)
            errors.append(f"内嵌字幕 {rel}：{exc}")
            continue
        for old_id in prior_tracks.values():
            db.execute("DELETE FROM subtitle_files WHERE id=?", (old_id,))
        for track, cues in tracks.items():
            name = EMBEDDED_PREFIX + str(track)
            save_cues(db, media_id, name, size, modified, cues)
            seen.add((media_id, name))
        db.execute("INSERT OR REPLACE INTO embedded_subtitle_scans VALUES(?,?,?)", (media_id, size, modified))


def matching_videos(rel, by_name):
    stem = ntpath.splitext(ntpath.basename(rel))[0].casefold()
    while stem not in by_name:
        stem, dot, tag = stem.rpartition(".")
        if not dot or not TRACK_TAG.fullmatch(tag):
            return ()
    return by_name[stem]
