from collections import Counter
from itertools import islice
import os
from pathlib import Path
import stat

from file_organization_apply import snapshot_file


SCAN_LIMIT = 1024
PREVIEW_LIMIT = 12
EXTENSIONS = {
    suffix: category
    for category, suffixes in {
        "文档": ".doc .docx .wps .pdf .txt .md .rtf .odt .csv .tsv .xls .xlsx .xlsm .ods .ppt .pptx .odp .epub",
        "图片": ".jpg .jpeg .png .gif .bmp .webp .tif .tiff .heic .heif .avif .svg .dng",
        "音频": ".mp3 .wav .flac .aac .m4a .ogg .opus .wma .aiff .ape .mka",
        "视频": ".mp4 .mkv .webm .mov .avi .wmv .m4v .mpeg .mpg .flv",
        "压缩包": ".zip .7z .rar .tar .gz .tgz .bz2 .tbz2 .xz .zst",
    }.items()
    for suffix in suffixes.split()
}
CONFLICTS = {
    "destination_not_directory": "分类位置已被非目录项目占用",
    "destination_reparse": "分类位置是目录连接或重解析点",
    "destination_unreadable": "当前无法核对分类位置",
    "name_exists": "目标已存在同名项目",
    "name_collision": "多份源文件会使用同一个目标名称",
}


def directory_focus(path: Path, target: Path, info: os.stat_result) -> dict:
    return {"path": str(path), "identity": [str(target), info.st_dev, info.st_ino]}


def _directory_snapshot(path):
    target = path.resolve(strict=True)
    info = target.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(path))
    return target, info


def _reparse(info):
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _destination_state(folder):
    try:
        info = folder.lstat()
    except FileNotFoundError:
        return "missing"
    except PermissionError:
        return "destination_unreadable"
    if _reparse(info):
        return "destination_reparse"
    if not stat.S_ISDIR(info.st_mode):
        return "destination_not_directory"
    return "directory"


def _destination_conflict(folder, state, name):
    if state == "missing":
        return None
    if state != "directory":
        return state
    try:
        (folder / name).lstat()
    except FileNotFoundError:
        return None
    except PermissionError:
        return "destination_unreadable"
    return "name_exists"


def _scan(target):
    candidates = []
    skipped = Counter()
    with os.scandir(target) as entries:
        for entry in islice(entries, SCAN_LIMIT):
            info = entry.stat(follow_symlinks=False)
            if _reparse(info) or stat.S_ISLNK(info.st_mode):
                skipped["reparse"] += 1
            elif stat.S_ISDIR(info.st_mode):
                skipped["directory"] += 1
            elif not stat.S_ISREG(info.st_mode):
                skipped["other"] += 1
            elif category := EXTENSIONS.get(Path(entry.name).suffix.lower()):
                candidates.append((entry.name, category))
            else:
                skipped["unknown_type"] += 1
        limited = next(entries, None) is not None

    categories = dict.fromkeys(category for _, category in candidates)
    destinations = {category: _destination_state(target / category) for category in categories}
    names = Counter((category, os.path.normcase(name)) for name, category in candidates)
    rows = []
    for name, category in candidates:
        conflict = _destination_conflict(target / category, destinations[category], name)
        if not conflict and names[category, os.path.normcase(name)] > 1:
            conflict = "name_collision"
        row = {"name": name, "category": category, "destination": str(Path(category) / name),
               "status": "conflict" if conflict else "suggested"}
        if conflict:
            row["reason"] = conflict
        rows.append(row)
    rows.sort(key=lambda row: (row["status"] != "conflict", row["category"],
                               os.path.normcase(row["name"]), row["name"]))
    return rows, skipped, limited


def plan_files(path: Path, *, focus: dict | None = None) -> tuple[str, dict]:
    path_text = str(path)
    if (not path.is_absolute() or path_text.startswith(("\\\\?\\", "\\\\.\\"))
            or ":" in path_text[len(path.anchor):]):
        return "请提供完整的本机目录或共享目录。", {}
    try:
        target, info = _directory_snapshot(path)
        anchor = directory_focus(path, target, info)
        if focus is not None and focus.get("identity") != anchor["identity"]:
            return "原目录的位置已变化。请重新列出要整理的目录。", {}
        rows, skipped, limited = _scan(target)
        snapshots = {}
        for row in rows[:PREVIEW_LIMIT]:
            if row["status"] == "suggested" and path.drive and not path_text.startswith("\\\\"):
                try:
                    snapshots[row["name"]] = snapshot_file(path, row["name"], row["category"])
                except ValueError:
                    # 无法锁定的项目仍可显示为只读建议。
                    continue
        after_target, after_info = _directory_snapshot(path)
        if (directory_focus(path, after_target, after_info) != anchor or after_info.st_mtime_ns != info.st_mtime_ns):
            return "生成预览时目录发生变化，请重新查看整理计划。", {}
    except FileNotFoundError:
        return f"暂时找不到目录或其中的项目：{path}。", {}
    except NotADirectoryError:
        return f"{path} 不是可查看的目录。", {}
    except PermissionError:
        return f"当前 Windows 账户无权读取目录：{path}。", {}
    except OSError as error:
        return f"无法读取目录 {path}（{error.strerror or '文件系统不可用'}）。", {}

    conflicts = sum(row["status"] == "conflict" for row in rows)
    suggested = len(rows) - conflicts
    examined = len(rows) + sum(skipped.values())
    visible = rows[:PREVIEW_LIMIT]
    omitted = len(rows) - len(visible)
    plan = {"status": "preview", "path": path_text, "scanned": examined, "limited": limited,
            "suggested": suggested, "conflicts": conflicts, "skipped": dict(skipped),
            "items": visible, "omitted": omitted}
    lines = [f"{path} 的整理预览。建议分类 {suggested} 项，冲突 {conflicts} 项，保留 {sum(skipped.values())} 项。"]
    if limited:
        lines.append(f"此次只检查先读取的 {SCAN_LIMIT} 个直属项目。")
    for row in visible:
        if row["status"] == "conflict":
            lines.append(f"{row['name']} → {row['destination']}（{CONFLICTS[row['reason']]}）")
        else:
            lines.append(f"{row['name']} → {row['destination']}")
    if omitted:
        lines.append(f"另有 {omitted} 项已计入上方数量。")
    if snapshots:
        lines.append("要归类一份，可以说“按计划归类 文件名”。")
    return "\n".join(lines), {"file_plan": plan, "directory_focus": anchor, "_file_plan_snapshots": snapshots}
