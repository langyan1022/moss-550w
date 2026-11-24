"""Back up a stopped MOSS profile or restore into a new directory. Never prints message contents."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import msvcrt
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile


SESSION = "session.sqlite3"
PLAYER = "media-playback.sqlite3"
LAUNCH = "windows-reminder-launch.json"
PROFILE_CONFIG = (LAUNCH, "mcp-servers.json")
MANIFEST = "backup-manifest.json"
DRAFT = re.compile(r"[0-9a-f]{32}\.txt")
EXTERNAL = ["media files and source catalog", "cloud credentials", "optional Mem0 indexes", "Windows registrations",
            "installed application, model weights and MCP executable files", "browser preferences", "PowerShell working files"]
REGENERATED = ["voice caches", "model slots and logs", "instance token and owner lock"]


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def plain(path):
    if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("备份不能包含重解析点：" + path.name)
    return path


def checked_db(path):
    if not path.is_file():
        raise FileNotFoundError("缺少数据库：" + path.name)
    return sqlite3.connect(plain(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def check_integrity(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("SQLite 数据库未通过完整性检查")


def copy_database(src, dest):
    with closing(checked_db(src)) as db, closing(sqlite3.connect(dest)) as out:
        db.backup(out)
        check_integrity(out)


def check_receipts(directory, *, external_drafts=True):
    count = 0
    with closing(checked_db(directory / SESSION)) as db:
        check_integrity(db)
        for key, doc in db.execute("SELECT id,doc FROM turns"):
            turn = json.loads(doc)
            receipt = turn.get("receipt")
            if not receipt:
                if not external_drafts and (turn.get("verify_only") or turn.get("status") == "saving" or
                                            turn.get("status") == "pending" and turn.get("tool_started_at")):
                    raise ValueError("旧备份有已开始写入的操作，请提供同一备份时点的完整 drafts 目录")
                continue
            name = receipt.get("filename", "")
            if not DRAFT.fullmatch(name) or name != key + ".txt":
                raise ValueError("草稿回执的文件标识无效")
            path = directory / "drafts" / name
            if not path.is_file() or sha(plain(path)) != receipt.get("sha256"):
                raise ValueError("草稿回执对应的文件缺失或哈希不符")
            if "bytes" in receipt and path.stat().st_size != receipt["bytes"]:
                raise ValueError("草稿文件大小与回执不一致")
            for text in (receipt.get("text"), turn.get("preview")):
                if text is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt["sha256"]:
                    raise ValueError("已保存的草稿正文或预览与回执不一致")
            count += 1
    return count


@contextmanager
def new_directory(destination, source):
    destination = destination.resolve()
    source = source.resolve()
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("请选择尚不存在且与源数据分开的目标目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A sibling staging directory keeps validation failures out of the published profile.
    with tempfile.TemporaryDirectory(prefix=".moss-restore-", dir=destination.parent) as tmp:
        stage = Path(tmp) / "profile"
        stage.mkdir()
        yield stage
        stage.rename(destination)


def copy_drafts(source, dest):
    dest.mkdir()
    if not source.is_dir():
        return
    plain(source)
    for path in source.iterdir():
        if DRAFT.fullmatch(path.name) and plain(path).is_file():
            shutil.copyfile(path, dest / path.name)


def files_in(directory):
    files = [directory / SESSION]
    files.extend(directory / name for name in (PLAYER, *PROFILE_CONFIG) if (directory / name).is_file())
    folder = directory / "drafts"
    if folder.is_dir():
        files.extend(p for p in plain(folder).iterdir() if DRAFT.fullmatch(p.name) and plain(p).is_file())
    return {path.relative_to(directory).as_posix(): sha(plain(path)) for path in sorted(files)}


def backup(source, destination):
    source = source.resolve()
    if not (source / SESSION).is_file():
        raise ValueError("源目录缺少会话数据库")
    with plain(source / "owner.lock").open("r+b") as owner:
        try:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("此数据目录仍由运行中的 MOSS 持有，本次未备份") from error
        with new_directory(destination, source) as stage:
            copy_database(source / SESSION, stage / SESSION)
            if (source / PLAYER).exists():
                copy_database(source / PLAYER, stage / PLAYER)
            for name in PROFILE_CONFIG:
                if (source / name).exists():
                    shutil.copyfile(plain(source / name), stage / name)
            copy_drafts(source / "drafts", stage / "drafts")
            receipts = check_receipts(stage)
            files = files_in(stage)
            manifest = {"format":1, "files":files, "verified_draft_receipts":receipts,
                        "external":EXTERNAL, "regenerated":REGENERATED}
            (stage / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"format":"profile", "file_count":len(files), "verified_draft_receipts":receipts}


def restore(source, destination, *, legacy_drafts=None):
    source = source.resolve()
    with new_directory(destination, source) as stage:
        if source.is_file():
            # Legacy deployments contain a consistent single-file sqlite3.backup snapshot.
            for suffix in ("-wal", "-journal"):
                if Path(str(source)+suffix).exists():
                    raise ValueError("旧备份带有活动日志，请提供独立、已完成的 SQLite 备份")
            if legacy_drafts is not None and not legacy_drafts.is_dir():
                raise ValueError("指定的草稿备份目录不存在")
            shutil.copyfile(plain(source), stage / SESSION)
            if legacy_drafts is not None:
                copy_drafts(legacy_drafts, stage / "drafts")
            else:
                (stage / "drafts").mkdir()
            receipts = check_receipts(stage, external_drafts=legacy_drafts is not None)
            return {"format":"legacy-sqlite", "file_count":len(files_in(stage)), "verified_draft_receipts":receipts}
        manifest = json.loads(plain(source / MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("不支持的备份清单")
        if legacy_drafts is not None:
            raise ValueError("完整备份已经包含草稿文件")
        allowed = files_in(source)
        if manifest["files"] != allowed:
            raise ValueError("备份清单与文件不一致")
        (stage / "drafts").mkdir()
        for name in allowed:
            shutil.copyfile(source / name, stage / name)
        if files_in(stage) != allowed:
            raise ValueError("还原时文件发生变化")
        receipts = check_receipts(stage)
        if (stage / PLAYER).exists():
            with closing(checked_db(stage / PLAYER)) as db:
                check_integrity(db)
        if receipts != manifest.get("verified_draft_receipts"):
            raise ValueError("还原的草稿回执数量不一致")
        return {"format":"profile", "file_count":len(allowed), "verified_draft_receipts":receipts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("backup", "restore"):
        sub = commands.add_parser(name)
        sub.add_argument("source", type=Path)
        sub.add_argument("destination", type=Path)
        if name == "restore":
            sub.add_argument("--legacy-drafts", type=Path, help="与旧 SQLite 备份同一时点的草稿目录")
    args = parser.parse_args()
    try:
        result = (backup(args.source, args.destination) if args.action == "backup" else
                  restore(args.source, args.destination, legacy_drafts=args.legacy_drafts))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False))
