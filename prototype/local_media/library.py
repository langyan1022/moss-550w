from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import ntpath
import os
from pathlib import Path
from random import shuffle
import re
import sqlite3
import stat
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from . import subtitles


EXTENSIONS = {
    "audio": "mp3 flac wav wave m4a aac ogg oga opus wma aiff aif ape dsf dff alac amr",
    "video": "mp4 mkv mov webm avi wmv m4v mpeg mpg ts m2ts flv",
    "image": "jpg jpeg png webp gif bmp tif tiff heic heif avif",
}
KINDS = {"." + ext: kind for kind, extensions in EXTENSIONS.items() for ext in extensions.split()}
BROWSER_UNSUPPORTED_AUDIO = frozenset({".dsf", ".dff", ".ape"})
EDITION = re.compile(r"[\s_-]*(?:[（(【\[]\s*(?:live\b|现场|演唱会|伴奏|翻唱|remix\b|remaster|acoustic\b)[^()（）【】\[\]]*[）)】\]]|"
                     r"(?:[-_ ]+)(?:live|现场版|伴奏版|remix|remastered))\s*$", re.I)
TRACK_NUMBER = re.compile(r"^\s*[0-9０-９]{1,3}\s*[.．、)）_\-–—]\s*\S")
ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "media.sqlite3"
DEFAULT_CONFIG = ROOT / "sources.json"


def words(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold(), re.UNICODE)


def compact(text):
    return "".join(words(text))


def song_title_key(title):
    return compact(EDITION.sub("", unicodedata.normalize("NFKC", title or "")))


def audio_metadata_key(title, artist):
    return tuple(" ".join(unicodedata.normalize("NFKC", part).casefold().split()) for part in (title, artist))


def same_song(item, previous):
    title = song_title_key(previous.get("title"))
    return item["media_id"] == previous.get("media_id") or (bool(title) and song_title_key(item["title"]) == title)


def artist_credit(row, source, artist):
    target = unicodedata.normalize("NFKC", artist).strip().casefold()
    tags = unicodedata.normalize("NFKC", row["artist"]).strip().casefold()
    if tags:
        if tags == target or target in re.split(r"\s*(?:[、,;&/]| feat\. | featuring )\s*", tags):
            return "tag"
        return None
    folders = [ntpath.basename(ntpath.normpath(source["path"])), *ntpath.dirname(row["rel"]).replace("/", "\\").split("\\")]
    if any(unicodedata.normalize("NFKC", folder).strip().casefold() == target for folder in folders):
        return "folder"
    return None


def audio_header(stream):
    import mutagen
    stream.seek(0)
    try:
        audio = mutagen.File(stream)
    except mutagen.MutagenError:
        return None
    if audio is None or not audio.info or not getattr(audio.info, "length", 0):
        return None
    info = audio.info
    kind = type(audio).__name__
    rate = getattr(info, "sample_rate", None)
    frames = getattr(info, "total_samples", None)
    if kind == "WAVE" and rate:
        frames = round(info.length * rate)
    return {"format": kind, "duration": info.length, "sample_rate": rate,
            "channels": getattr(info, "channels", None), "frames": frames}


@contextmanager
def transport_errors():
    try:
        yield
    except OSError:
        raise
    except Exception as error:
        if type(error).__module__.startswith(("smbprotocol.", "smbclient.", "spnego.")):
            raise OSError(str(error)) from error
        raise


def access_state(error):
    if getattr(error, "winerror", None) in {53, 64, 67, 121, 1231}:
        return "offline"
    if isinstance(error, PermissionError) or getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}:
        return "denied"
    if isinstance(error, FileNotFoundError) or getattr(error, "errno", None) in {errno.ENOENT, errno.ENOTDIR}:
        return "stale"
    return "offline"


class SourceUnavailableError(OSError):
    pass


def scan_issue(source):
    state = source["state"]
    if state in {"offline","denied"}:
        return {"id":source["id"], "state":"stale", "error":"来源已重新连通，需要更新索引"}
    return {"id":source["id"], "state":state, "error":source.get("error") or "需要更新索引"}


class Files:
    def __init__(self, source):
        self.root = source["path"]
        self.options = source.get("smb")
        self.remote = None
        if self.options:
            import smbclient
            self.remote = smbclient
            host = ntpath.splitdrive(self.root)[0].strip("\\").split("\\")[0]
            self.port = int(self.options["port"])
            if host not in {"127.0.0.1", "localhost"}:
                raise ValueError("自定义 SMB 端口仅用于回环验证。真实 NAS 请使用本机已授权的 UNC 路径或挂载目录")
            with transport_errors():
                smbclient.register_session(host, port=self.port, username=os.environ[self.options["username_env"]],
                                           password=os.environ[self.options["password_env"]], connection_timeout=3)

    def path(self, relative):
        return ntpath.join(self.root, relative) if self.options else os.path.join(self.root, relative)

    def entries(self, path):
        if self.remote:
            with transport_errors():
                return list(self.remote.scandir(path, port=self.port))
        with os.scandir(path) as entries:
            return list(entries)

    def probe(self):
        if self.remote:
            with transport_errors():
                self.remote.stat(self.root, port=self.port)
        else:
            with os.scandir(self.root):
                pass

    def open(self, path):
        if self.remote:
            with transport_errors():
                return self.remote.open_file(path, "rb", port=self.port)
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_relative_to(Path(self.root).resolve(strict=True)):
            raise PermissionError("媒体路径已离开配置的来源")
        return resolved.open("rb")

    def open_indexed(self, path, version):
        if version is None:
            return self.open(path)
        with transport_errors(), ExitStack() as ownership:
            source = ownership.enter_context(self.open(path))
            if self.remote:
                from smbclient._io import SMBFileTransaction, query_info
                from smbprotocol.file_info import FileBasicInformation, FileStandardInformation
                from smbprotocol.structure import DateTimeField

                with SMBFileTransaction(source.raw) as query:
                    query_info(query, FileBasicInformation)
                    query_info(query, FileStandardInformation)
                basic, standard = query.results
                modified = basic["last_write_time"].get_value()
                if isinstance(modified, datetime):
                    modified_ns = int(modified.timestamp() * 1_000_000_000)
                else:
                    modified_ns = (modified - DateTimeField.EPOCH_FILETIME) * 100
                current = standard["end_of_file"].get_value(), modified_ns
            else:
                item = os.fstat(source.fileno())
                current = item.st_size, int(item.st_mtime_ns)
            if current != version:
                raise FileNotFoundError("音轨已变化，请更新媒体索引。")
            ownership.pop_all()
            return source

    def stat_entry(self, entry):
        if self.remote:
            with transport_errors():
                return self.remote.stat(entry.path, follow_symlinks=False, port=self.port)
        return entry.stat(follow_symlinks=False)

    def fingerprint(self, path):
        if self.remote:
            with transport_errors():
                result = self.remote.stat(path, follow_symlinks=False, port=self.port)
        else:
            result = os.stat(path, follow_symlinks=False)
        return result.st_size, int(result.st_mtime_ns)

    def is_file(self, path):
        if self.remote:
            with transport_errors():
                return stat.S_ISREG(self.remote.stat(path, port=self.port).st_mode)
        return os.path.isfile(path)

    def readable(self, path, version=None):
        # Windows 可列目录或读取文件属性，但没有读取媒体数据的权限。
        with self.open_indexed(path, version):
            return True


def subtitle_versions(files, row):
    path = row["path"]
    subtitle = (row["subtitle_size"], row["subtitle_modified"])
    if row["subtitle_rel"].startswith(subtitles.EMBEDDED_PREFIX):
        return ((path, subtitle),)
    return ((path, (row["size"], row["modified"])), (files.path(row["subtitle_rel"]), subtitle))


def subtitle_version(row):
    fields = (row["size"], row["modified"], row["subtitle_rel"], row["subtitle_size"], row["subtitle_modified"])
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def metadata(files, path, kind):
    title, artist = "", ""
    try:
        with transport_errors(), files.open(path) as fp:
            if kind == "image":
                from PIL import Image
                with Image.open(fp) as img:
                    exif = img.getexif()
                    title = img.info.get("Title") or exif.get(270) or ""
                    artist = img.info.get("Author") or exif.get(315) or ""
            else:
                import mutagen
                try:
                    audio = mutagen.File(fp, easy=True)
                except mutagen.MutagenError:
                    return "", ""
                if audio:
                    tags = audio.tags or {}
                    def first(*keys):
                        for key in keys:
                            item = tags.get(key)
                            if item:
                                return str(item[0] if isinstance(item, (list, tuple)) else item)
                        return ""
                    title, artist = first("title", "TIT2", "Title"), first("artist", "TPE1", "Author")
    except (OSError, ValueError):
        pass
    return str(title).strip(), str(artist).strip()


class MediaLibrary:
    def __init__(self, db_path=DEFAULT_DB, *, read_only=False, timeout=30):
        self.db_path = Path(db_path).resolve()
        self.read_only = read_only
        self.timeout = timeout
        if read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError("媒体索引尚未建立")
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL, error TEXT, checked TEXT
                );
                CREATE TABLE IF NOT EXISTS media(
                    media_id TEXT PRIMARY KEY, source TEXT NOT NULL, rel TEXT NOT NULL,
                    path TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, artist TEXT NOT NULL,
                    size INTEGER NOT NULL, modified INTEGER NOT NULL, searchable TEXT NOT NULL,
                    UNIQUE(source,rel)
                );
                CREATE INDEX IF NOT EXISTS media_source_kind ON media(source,kind);
                CREATE TABLE IF NOT EXISTS missing_audio(
                    source TEXT NOT NULL, rel TEXT NOT NULL, media_id TEXT NOT NULL,
                    title_key TEXT NOT NULL, artist_key TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER NOT NULL,
                    PRIMARY KEY(source,rel)
                );
            """)
            db.executescript(subtitles.SCHEMA)

    def connect(self):
        if self.read_only:
            db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout)
        else:
            db = sqlite3.connect(self.db_path, timeout=self.timeout)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def configure(self, sources):
        ids = set()
        for src in sources:
            if not isinstance(src.get("id"), str) or not src["id"].strip() or src["id"] in ids:
                raise ValueError("每个来源需要唯一的非空 ID")
            if src.get("kind") not in {"local", "nas"} or not os.path.isabs(src["path"]):
                raise ValueError("来源需要 local/nas 类型与绝对路径")
            ids.add(src["id"])
        with closing(self.connect()) as db, db:
            for src in sources:
                old = db.execute("SELECT config FROM sources WHERE id=?", (src["id"],)).fetchone()
                value = json.dumps(src, ensure_ascii=False, sort_keys=True)
                if old and json.loads(old[0])["path"] != src["path"]:
                    raise ValueError("同一来源 ID 已绑定其他目录，请为新目录指定不同的 ID")
                db.execute("""INSERT INTO sources(id,config,state) VALUES(?,?,'new')
                              ON CONFLICT(id) DO UPDATE SET config=excluded.config""", (src["id"], value))
            for row in db.execute("SELECT id FROM sources").fetchall():
                if row[0] not in ids:
                    db.execute("DELETE FROM sources WHERE id=?", (row[0],))
                    db.execute("DELETE FROM media WHERE source=?", (row[0],))
                    db.execute("DELETE FROM missing_audio WHERE source=?", (row[0],))

    def sources(self):
        with closing(self.connect()) as db:
            return [{**json.loads(r["config"]), "state": r["state"], "error": r["error"], "checked": r["checked"],
                     "count": r["count"]} for r in db.execute("""
                     SELECT s.*, (SELECT COUNT(*) FROM media m WHERE m.source=s.id) AS count
                     FROM sources s ORDER BY json_extract(config,'$.kind') DESC, id""")]

    def scan(self, source_id=None):
        sources = self.sources()
        if source_id is not None:
            sources = [source for source in sources if source["id"] == source_id]
            if not sources:
                raise ValueError("指定的媒体来源不存在。")
        for source in sources:
            self._scan_one(source)
        return self.sources()

    def _scan_one(self, src):
        key = src["id"]
        with closing(self.connect()) as db, db:
            old = {ntpath.normcase(r["rel"]): r for r in db.execute("SELECT * FROM media WHERE source=?", (key,))}
            missing_audio = {r["rel"]: r for r in db.execute("SELECT * FROM missing_audio WHERE source=?", (key,))}
            old_subs = {(r["media_id"], r["rel"]): r for r in db.execute(
                "SELECT f.* FROM subtitle_files f JOIN media m USING(media_id) WHERE m.source=?", (key,))}
            embedded_scans = {r["media_id"]: (r["size"], r["modified"]) for r in db.execute(
                """SELECT b.* FROM embedded_subtitle_scans b JOIN media m USING(media_id) WHERE m.source=?""", (key,))}
            embedded_tracks = {}
            for pair, row in old_subs.items():
                if pair[1].startswith(subtitles.EMBEDDED_PREFIX):
                    embedded_tracks.setdefault(pair[0], {})[pair[1]] = row["id"]
            seen_subs = set()
            seen, errors = set(), []
            try:
                files = Files(src)
                files.probe()
                folders = [""]
                while folders:
                    rel = folders.pop()
                    try:
                        entries = files.entries(files.path(rel))
                    except OSError as exc:
                        errors.append(str(exc))
                        continue
                    videos, sidecars = [], []
                    for entry in entries:
                        child = os.path.join(rel, entry.name) if rel else entry.name
                        path = files.path(child)
                        try:
                            if entry.is_symlink():
                                continue
                            info = files.stat_entry(entry)
                            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                folders.append(child)
                                continue
                            suffix = ntpath.splitext(entry.name)[1].casefold()
                            if suffix in {".srt", ".vtt"} and entry.is_file(follow_symlinks=False):
                                sidecars.append((child, info.st_size, info.st_mtime_ns))
                                continue
                            kind = KINDS.get(suffix)
                            if not kind or not entry.is_file(follow_symlinks=False):
                                continue
                            name = ntpath.normcase(child)
                            previous = old.get(name)
                            seen.add(name)
                            modified = int(info.st_mtime_ns)
                            identity = previous["media_id"] if previous else hashlib.sha256(
                                (key + "\0" + ntpath.normcase(child)).encode("utf-8")).hexdigest()[:32]
                            if kind == "video":
                                videos.append((child, identity, info.st_size, modified))
                            if previous and (child, info.st_size, modified) == (previous["rel"], previous["size"], previous["modified"]):
                                continue
                            title, artist = metadata(files, path, kind)
                            title = title or ntpath.splitext(entry.name)[0]
                            if kind == "audio":
                                tag_key = audio_metadata_key(title, artist)
                                if not previous:
                                    previous_missing = missing_audio.get(name)
                                    if previous_missing and (*tag_key, info.st_size, modified) == (
                                            previous_missing["title_key"], previous_missing["artist_key"],
                                            previous_missing["size"], previous_missing["modified"]):
                                        identity = previous_missing["media_id"]
                                    else:
                                        identity = uuid4().hex
                                    if previous_missing:
                                        db.execute("DELETE FROM missing_audio WHERE source=? AND rel=?", (key, name))
                                elif tag_key != audio_metadata_key(previous["title"], previous["artist"]):
                                    identity = uuid4().hex
                                    db.execute("UPDATE media SET media_id=? WHERE media_id=?",
                                               (identity, previous["media_id"]))
                            values = (identity, key, child, path, kind, title, artist, info.st_size, modified,
                                      compact(" ".join((title, artist, child))))
                            db.execute("""INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT
                                       DO UPDATE SET rel=excluded.rel,path=excluded.path,title=excluded.title,artist=excluded.artist,
                                       size=excluded.size,modified=excluded.modified,searchable=excluded.searchable""", values)
                        except OSError as exc:
                            errors.append(str(exc))
                    if videos and sidecars:
                        subtitles.sync_folder(db, files, videos, sidecars, old_subs, seen_subs, errors)
                    if videos:
                        with transport_errors():
                            subtitles.sync_embedded(db, files, videos, embedded_scans, embedded_tracks, seen_subs, errors)
                state = "partial" if errors else "ready"
                db.execute("INSERT OR IGNORE INTO subtitle_scans(source) VALUES(?)", (key,))
                if not errors:
                    db.executemany("DELETE FROM subtitle_files WHERE id=?",
                                   ((r["id"],) for pair,r in old_subs.items() if pair not in seen_subs))
                    missing = [(name, row) for name, row in old.items() if name not in seen]
                    db.executemany("INSERT OR REPLACE INTO missing_audio VALUES(?,?,?,?,?,?,?)",
                                   ((key, name, r["media_id"], *audio_metadata_key(r["title"], r["artist"]), r["size"], r["modified"])
                                    for name, r in missing if r["kind"] == "audio"))
                    db.executemany("DELETE FROM media WHERE media_id=?", ((r["media_id"],) for _, r in missing))
            except (OSError, KeyError, ValueError) as exc:
                state, errors = "denied" if access_state(exc) == "denied" else "offline", [str(exc)]
            checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.execute("UPDATE sources SET state=?, error=?, checked=? WHERE id=?",
                       (state, "; ".join(errors[:3]) or None, checked, key))

    def _record(self, row, source):
        item = {**{k: row[k] for k in ("media_id", "path", "title", "artist", "kind", "source")},
                "source_kind": source["kind"], "source_label": source.get("label") or source["id"], "available": True}
        if row["kind"] == "audio":
            item["_audio_index_version"] = [row["size"], row["modified"]]
        return item

    def resolve(self, media_id):
        try:
            item, stream = self.open_record(media_id)
            stream.close()
            return item
        except OSError:
            return None

    def open_record(self, media_id, kinds=None):
        with closing(self.connect()) as db:
            row = db.execute("SELECT m.*,s.config FROM media m JOIN sources s ON s.id=m.source WHERE media_id=?",
                             (media_id,)).fetchone()
        if not row or (kinds is not None and row["kind"] not in kinds):
            raise FileNotFoundError("媒体 ID 不在当前索引")
        source = json.loads(row["config"])
        try:
            files = Files(source)
            version = (row["size"], row["modified"]) if row["kind"] == "audio" else None
            try:
                return self._record(row, source), files.open_indexed(row["path"], version)
            except OSError as error:
                if access_state(error) != "stale":
                    raise
                # 区分已更名的文件与临时撤下的整个共享。
                files.probe()
        except (OSError, ValueError, KeyError) as exc:
            if access_state(exc) == "denied":
                raise PermissionError("没有此媒体的读取权限") from exc
            raise SourceUnavailableError("媒体来源当前不可用") from exc
        raise FileNotFoundError("该文件已移动或移除，请更新媒体索引")

    def open_media(self, media_id):
        return self.open_record(media_id)[1]

    def audio_version(self, media_id):
        with closing(self.connect()) as db:
            row = db.execute("""SELECT m.path,m.size,m.modified,s.config FROM media m JOIN sources s ON s.id=m.source
                                WHERE m.media_id=? AND m.kind='audio'""", (media_id,)).fetchone()
        if not row:
            raise FileNotFoundError("音轨已不在媒体索引中。")
        files = Files(json.loads(row["config"]))
        version = files.fingerprint(row["path"])
        if version != (row["size"], row["modified"]):
            raise ValueError("音轨已变化，请更新媒体索引。")
        return (row["path"], row["config"], *version)

    def video_version(self, media_id):
        with closing(self.connect()) as db:
            row = db.execute("""SELECT m.path,m.size,m.modified,s.config FROM media m JOIN sources s ON s.id=m.source
                                WHERE m.media_id=? AND m.kind='video'""", (media_id,)).fetchone()
        if not row:
            raise FileNotFoundError("视频已不在媒体索引中。")
        files = Files(json.loads(row["config"]))
        version = files.fingerprint(row["path"])
        if version != (row["size"], row["modified"]):
            raise ValueError("视频已变化，请更新媒体索引后重新查找。")
        return list(version)

    def search_subtitles(self, query, limit=6):
        needle = subtitles.search_text(query)
        if not needle or not 1 <= limit <= 100:
            raise ValueError("请提供有效台词，返回条数应在 1 到 100 之间")
        sql = """SELECT m.*, f.rel AS subtitle_rel, f.size AS subtitle_size, f.modified AS subtitle_modified,
                        c.start_ms, c.end_ms, c.text
                 FROM subtitle_cues c JOIN subtitle_files f ON f.id=c.file_id
                 JOIN media m ON m.media_id=f.media_id WHERE m.source=? AND """
        if len(needle) >= 3:
            sql += "c.id IN (SELECT rowid FROM subtitle_fts WHERE subtitle_fts MATCH ?)"
            term = '"' + needle + '"'
        else:
            sql += "instr(c.searchable,?)>0"
            term = needle
        sql += " ORDER BY c.searchable=? DESC,m.title,m.rel,c.start_ms,c.end_ms,f.rel"
        items, unavailable, consulted = [], {}, []
        seen = set()
        sources = self.sources()
        with closing(self.connect()) as db:
            scanned = {r[0] for r in db.execute("SELECT source FROM subtitle_scans")}
            for location in ("local", "nas"):
                for src in (s for s in sources if s["kind"] == location):
                    consulted.append(src["id"])
                    try:
                        files = Files(src)
                        files.probe()
                        if src["id"] not in scanned:
                            unavailable[src["id"]] = {"id":src["id"], "state":"unindexed"}
                            continue
                        if src["state"] != "ready":
                            unavailable[src["id"]] = scan_issue(src)
                        versions = {}
                        for row in db.execute(sql, (src["id"], term, needle)):
                            hit = (row["media_id"], row["start_ms"], row["end_ms"], row["text"])
                            if hit in seen:
                                continue
                            for path, expected in subtitle_versions(files, row):
                                if path not in versions:
                                    try:
                                        versions[path] = files.fingerprint(path) if files.readable(path) else None
                                    except OSError as error:
                                        state = access_state(error)
                                        if state == "offline":
                                            raise
                                        versions[path] = None
                                        unavailable[src["id"]] = {"id":src["id"],"state":state,"error":str(error)}
                                if versions[path] is None:
                                    break
                                if versions[path] != expected:
                                    unavailable[src["id"]] = {"id":src["id"], "state":"stale",
                                                              "error":"视频或字幕已修改，请更新媒体索引。"}
                                    break
                            else:
                                seen.add(hit)
                                items.append({**self._record(row, src),
                                              "subtitle":{"text":row["text"], "excerpt":subtitles.excerpt(row["text"], needle),
                                                          "start_s":row["start_ms"]/1000,
                                                          "end_s":row["end_ms"]/1000, "version":subtitle_version(row)}})
                            if len(items) == limit:
                                break
                    except (OSError, ValueError, KeyError) as exc:
                        unavailable[src["id"]] = {"id":src["id"], "state":"denied" if access_state(exc) == "denied" else "offline", "error":str(exc)}
                    if len(items) == limit:
                        break
                if items:
                    break
        return {"items":items, "unavailable_sources":list(unavailable.values()), "consulted_sources":consulted, "online_calls":0}

    def subtitle_available(self, media_id, cue):
        saved = cue.get("version")
        with closing(self.connect()) as db:
            rows = db.execute("""SELECT m.path,m.size,m.modified,s.config,f.rel AS subtitle_rel,
                                        f.size AS subtitle_size,f.modified AS subtitle_modified
                                 FROM media m JOIN sources s ON s.id=m.source
                                 JOIN subtitle_files f USING(media_id) JOIN subtitle_cues c ON c.file_id=f.id
                                 WHERE m.media_id=? AND c.start_ms=? AND c.end_ms=? AND c.text=?""",
                              (media_id, round(cue["start_s"]*1000), round(cue["end_s"]*1000), cue["text"]))
            for row in rows:
                if saved is not None and saved != subtitle_version(row):
                    continue
                try:
                    files = Files(json.loads(row["config"]))
                    if all(files.readable(path) and files.fingerprint(path) == expected
                           for path, expected in subtitle_versions(files, row)):
                        return True
                except (OSError, ValueError, KeyError):
                    continue
        return False

    def catalog_summary(self):
        category_re = re.compile(r"^\d+[-_]\s*(.+)$")
        hidden = {".downpic", ".sldp", "lockscreenimage"}
        sources = self.sources()
        total_audio = playable_audio = unsupported_audio = total_video = total_image = 0
        artists = {}
        categories = {}
        with closing(self.connect()) as db:
            for row in db.execute("SELECT kind, artist, rel, path FROM media"):
                kind = row["kind"]
                if kind == "video":
                    total_video += 1
                    continue
                if kind == "image":
                    total_image += 1
                    continue
                if kind != "audio":
                    continue
                total_audio += 1
                suffix = ntpath.splitext(row["path"])[1].lower()
                playable = suffix not in BROWSER_UNSUPPORTED_AUDIO
                if playable:
                    playable_audio += 1
                else:
                    unsupported_audio += 1
                parts = [p for p in row["rel"].replace("/", "\\").split("\\") if p]
                top = parts[0] if len(parts) > 1 else ""
                if top.casefold() in hidden:
                    continue
                cat_match = category_re.match(top) if top else None
                if cat_match:
                    cat_name = cat_match.group(1).strip()
                    bucket = categories.setdefault(top, {"name": cat_name, "folder": top, "count": 0, "playable": 0})
                    bucket["count"] += 1
                    if playable:
                        bucket["playable"] += 1
                tag_artist = (row["artist"] or "").strip()
                artist_name = ""
                if tag_artist:
                    artist_name = re.split(r"\s*(?:[、,;&/]| feat\. | featuring )\s*", tag_artist)[0].strip()
                elif top and not cat_match:
                    artist_name = top
                if artist_name:
                    key = compact(artist_name) or artist_name.casefold()
                    entry = artists.setdefault(key, {"name": artist_name, "count": 0, "playable": 0})
                    entry["count"] += 1
                    if playable:
                        entry["playable"] += 1
        top_artists = sorted(artists.values(), key=lambda x: (-x["playable"], -x["count"], x["name"]))[:30]
        cat_list = sorted(categories.values(), key=lambda x: x["folder"])
        return {
            "sources": [{"id": s["id"], "label": s.get("label") or s["id"], "kind": s["kind"], "state": s["state"]}
                        for s in sources],
            "total_audio": total_audio,
            "playable_audio": playable_audio,
            "unsupported_audio": unsupported_audio,
            "total_video": total_video,
            "total_image": total_image,
            "artist_count": len(artists),
            "top_artists": top_artists,
            "categories": cat_list,
        }

    def search(self, query, *, kind=None, limit=10, artist=None, browser_audio=False, exclude_song=None,
               random_order=False, preferred_artists=(), recent_audio=(), folder_hint=None,
               exclude_keywords=(), prefer_keywords=()):
        if kind is not None and kind not in EXTENSIONS:
            raise ValueError("无效的媒体类型")
        pieces = words(query)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("返回条数应在 1 到 100 之间")
        source_filter = ["source=?"] + (["kind=?"] if kind else [])
        clauses = source_filter + ["instr(searchable,?)>0"] * len(pieces)
        folder_key = compact(folder_hint) if folder_hint else ""
        bad_keys = tuple(compact(k) for k in exclude_keywords if compact(k))
        good_keys = tuple(compact(k) for k in prefer_keywords if compact(k))
        unavailable, consulted = {}, []
        unsupported = set()
        needle = compact(query)
        default_audio = browser_audio and kind == "audio" and not query.strip() and not artist and not folder_hint
        preference_rank = {compact(name): rank for rank, name in enumerate(preferred_artists)}
        recent_rank = {title: rank for rank, title in enumerate(reversed(recent_audio), 1)}
        with closing(self.connect()) as db:
            items = []
            sources = self.sources()
            for location in ("local", "nas"):
                matches = []
                for src in (s for s in sources if s["kind"] == location):
                    consulted.append(src["id"])
                    try:
                        files = Files(src)
                        files.probe()
                        if src["state"] != "ready":
                            unavailable[src["id"]] = scan_issue(src)
                        root_artist = artist and needle == compact(artist) == compact(ntpath.basename(ntpath.normpath(src["path"])))
                        conditions = source_filter if root_artist else clauses
                        params = [src["id"], *([kind] if kind else []), *([] if root_artist else pieces)]
                        for row in db.execute("SELECT * FROM media WHERE " + " AND ".join(conditions), params):
                            if folder_key:
                                rel_parts = [p for p in row["rel"].replace("/", "\\").split("\\") if p]
                                if not any(folder_key in compact(part) for part in rel_parts[:-1]):
                                    continue
                            hay = compact(row["rel"] + " " + row["title"])
                            if bad_keys and any(k in hay for k in bad_keys):
                                continue
                            credit = artist_credit(row, src, artist) if artist else None
                            folder_artist = artist if credit == "folder" else None
                            if artist and not credit:
                                continue
                            if random_order:
                                rank = (0 if (not good_keys or any(k in hay for k in good_keys)) else 1,)
                            elif default_audio:
                                if preferred_artists and not row["artist"].strip():
                                    folder_artist = next((name for name in preferred_artists
                                                          if artist_credit(row, src, name) == "folder"), None)
                                    credit = "folder" if folder_artist else None
                                rank = (preference_rank.get(compact(folder_artist or row["artist"]), len(preference_rank))
                                        if preference_rank else 0,
                                        recent_rank.get(song_title_key(row["title"]), 0) if recent_rank else 0,
                                        not row["artist"].strip(), bool(TRACK_NUMBER.match(row["title"])),
                                        row["path"].casefold())
                            else:
                                title_key = compact(row["title"])
                                rank = (credit == "folder", title_key != needle, compact(row["artist"]) != needle,
                                        needle not in title_key, row["path"].casefold())
                            matches.append((rank, row, files, src, credit, folder_artist))
                    except (OSError, ValueError, KeyError) as exc:
                        unavailable[src["id"]] = {"id":src["id"], "state":"denied" if access_state(exc) == "denied" else "offline", "error":str(exc)}
                editions, titles = [], set()
                if random_order:
                    shuffle(matches)
                    if good_keys:
                        matches.sort(key=lambda item: item[0])
                else:
                    matches.sort(key=lambda item: item[0])
                for _, row, files, src, credit, folder_artist in matches:
                    if unavailable.get(src["id"], {}).get("state") == "offline":
                        continue
                    if exclude_song and same_song(row, exclude_song):
                        continue
                    try:
                        suffix = ntpath.splitext(row["path"])[1].lower()
                        version = (row["size"], row["modified"]) if row["kind"] == "audio" else None
                        if browser_audio and suffix in BROWSER_UNSUPPORTED_AUDIO:
                            if suffix not in unsupported and files.readable(row["path"], version):
                                unsupported.add(suffix)
                            continue
                        if files.readable(row["path"], version):
                            item = self._record(row, src)
                            if credit:
                                item["artist_source"] = credit
                                if folder_artist:
                                    item["artist"] = folder_artist
                            if browser_audio:
                                title_key = song_title_key(item["title"])
                                if title_key in titles:
                                    if len(editions) < limit:
                                        editions.append(item)
                                    continue
                                titles.add(title_key)
                            items.append(item)
                    except OSError as exc:
                        unavailable[src["id"]] = {"id":src["id"], "state":access_state(exc), "error":str(exc)}
                    if len(items) == limit:
                        break
                if items:
                    items.extend(editions[:limit - len(items)])
                    break
        return {"items": items, "unavailable_sources": list(unavailable.values()), "consulted_sources": consulted,
                **({"unsupported_audio": sorted(unsupported)} if unsupported else {})}

    def route(self, query, *, kind=None, limit=10, online=False, selected=None, provider=None, artist=None,
              browser_audio=False, exclude_song=None, random_order=False, preferred_artists=(), recent_audio=(),
              folder_hint=None, exclude_keywords=(), prefer_keywords=()):
        if selected:
            try:
                record, stream = self.open_record(selected)
                stream.close()
            except OSError as error:
                return {"status":"selected_unavailable", "reason":access_state(error), "items":[],
                        "unavailable_sources":[], "online_calls":0}
            return {"status":"selected", "items":[record], "unavailable_sources":[], "online_calls":0}
        result = self.search(query, kind=kind, limit=limit, artist=artist, browser_audio=browser_audio,
                             exclude_song=exclude_song, random_order=random_order, preferred_artists=preferred_artists,
                             recent_audio=recent_audio, folder_hint=folder_hint,
                             exclude_keywords=exclude_keywords, prefer_keywords=prefer_keywords)
        status = "found" if result["items"] else "unavailable" if result["unavailable_sources"] else "empty"
        result.update(status=status, online_calls=0)
        if online and status == "empty" and query.strip() and not result.get("unsupported_audio"):
            self.scan()
            result = self.search(query, kind=kind, limit=limit, artist=artist, browser_audio=browser_audio,
                                 exclude_song=exclude_song, random_order=random_order, preferred_artists=preferred_artists,
                                 recent_audio=recent_audio, folder_hint=folder_hint,
                                 exclude_keywords=exclude_keywords, prefer_keywords=prefer_keywords)
            if result["items"] or result["unavailable_sources"] or not self.sources():
                result.update(status="found" if result["items"] else "unavailable", online_calls=0)
            elif result.get("unsupported_audio"):
                result.update(status="empty", online_calls=0)
            else:
                result.update(status="online", online_calls=1)
                try:
                    result["items"] = (provider or commons)(query, kind, limit)
                except (OSError, ValueError, KeyError) as exc:
                    result.update(status="online_error", error=str(exc))
        return result


def commons(query, kind, limit):
    media_type = {"image": "bitmap", "audio": "audio", "video": "video"}.get(kind)
    criteria = f"{query} filetype:{media_type}" if media_type else query
    parameters = {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                  "gsrnamespace": 6, "gsrsearch": criteria, "gsrlimit": min(limit, 50),
                  "prop": "imageinfo", "iiprop": "url|mime|mediatype"}
    req = Request("https://commons.wikimedia.org/w/api.php?" + urlencode(parameters),
                  headers={"User-Agent": "MOSS-agent-lab/0.1 (local media search; Wikimedia Commons)"})
    with urlopen(req, timeout=12) as response:
        doc = json.load(response)
    if doc.get("error"):
        raise ValueError(doc["error"].get("info", "Wikimedia Commons 查询失败"))
    rows = []
    for page in sorted(doc.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0)):
        file = next(iter(page.get("imageinfo", [])), None)
        if file:
            file_kind = {"AUDIO": "audio", "VIDEO": "video", "BITMAP": "image", "DRAWING": "image"}.get(
                file.get("mediatype"), file["mime"].split("/")[0])
            if not kind or kind == file_kind:
                rows.append({"media_id": "commons:" + str(page["pageid"]), "url": file["descriptionurl"],
                             "title": page["title"], "kind": file_kind, "source_kind": "online"})
    return rows
