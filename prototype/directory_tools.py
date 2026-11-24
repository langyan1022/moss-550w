import asyncio
from contextlib import suppress
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile

import winerror
from win32com.shell import shell, shellcon

from file_organization import directory_focus, plan_files


SHELL_FOLDERS = {
    "桌面": shellcon.FOLDERID_Desktop, "下载": shellcon.FOLDERID_Downloads, "文档": shellcon.FOLDERID_Documents,
    "图片": shellcon.FOLDERID_Pictures, "音乐": shellcon.FOLDERID_Music, "视频": shellcon.FOLDERID_Videos,
}
DIRECTORY_PAGE = 40
DIRECTORY_LIMIT = 1024
SHARE_OFFLINE_ERRORS = {
    winerror.ERROR_BAD_NETPATH, winerror.ERROR_NETNAME_DELETED, winerror.ERROR_SEM_TIMEOUT,
    winerror.ERROR_CONNECTION_UNAVAIL, winerror.ERROR_NO_NETWORK, winerror.ERROR_NETWORK_UNREACHABLE,
    winerror.ERROR_HOST_UNREACHABLE, winerror.ERROR_CONNECTION_ABORTED,
}
DIRECTORY_READ_ACTIONS = frozenset({"directory", "file_plan"})
DIRECTORY_READ_SECONDS = 5
MAX_PENDING_READS = 2
_pending_reads = set()


def directory_path(intent):
    if "folder" in intent:
        return Path(shell.SHGetKnownFolderPath(SHELL_FOLDERS[intent["folder"]]))
    return Path(intent["path"])


def list_directory(intent):
    if intent.get("invalid"):
        return "请提供完整的盘符目录或共享目录。设备路径和通配符无法用于列目录。", {}
    page = intent.get("page")
    if "page" in intent and not page:
        return "请先列出要查看的目录。", {}
    if page and not page["more"]:
        return page["end"], page
    path = directory_path(page or intent)
    changed = "目录内容或位置已变化。请重新列出要查看的目录。"
    try:
        target = path.resolve(strict=True)
        stat = target.stat()
        identity = [str(target), stat.st_dev, stat.st_ino, stat.st_mtime_ns]
        if page and page["identity"] != identity:
            return changed, {}
        with os.scandir(target) as items:
            rows = [(entry.is_dir(follow_symlinks=False), entry.name) for entry in islice(items, DIRECTORY_LIMIT)]
            limited = next(items, None) is not None
        after = path.stat()
        if (after.st_dev, after.st_ino, after.st_mtime_ns) != tuple(identity[1:]):
            return changed, {}
    except OSError as exc:
        remote = str(path).startswith("\\\\") or (page and page["identity"][0].startswith("\\\\"))
        if remote and getattr(exc, "winerror", None) in SHARE_OFFLINE_ERRORS:
            if page:
                return "暂时无法连接共享目录。连接恢复后，说“下一页”可从上次的位置继续。", page
            return "暂时无法连接共享目录，连接恢复后可重新列出。", {}
        if isinstance(exc, FileNotFoundError):
            return f"找不到目录 {path}。", {}
        if isinstance(exc, NotADirectoryError):
            return f"{path} 是文件。请指定一个目录。", {}
        if isinstance(exc, PermissionError):
            return f"当前 Windows 账户无权读取 {path}。", {}
        return f"无法读取目录 {path}（{exc.strerror or '文件系统不可用'}）。", {}
    if not rows:
        return (changed, {}) if page else (f"{path} 是空目录。", {"more": False, "end": "上次查看的目录为空。",
                                                             "focus": directory_focus(path, target, stat)})
    rows.sort(key=lambda pair: (not pair[0], pair[1].casefold(), pair[1]))
    names = [name + ("\\" if directory else "") for directory, name in rows]
    digest = hashlib.sha256("\0".join(names).encode("utf-8", errors="surrogatepass")).hexdigest()
    if page and (page["digest"] != digest or page["limited"] != limited):
        return changed, {}
    start = page["offset"] if page else 0
    entries = names[start:start + DIRECTORY_PAGE]
    offset = start + len(entries)
    more = offset < len(names)
    end = f"已列完本次读取的 {len(names)} 项。"
    scope = f"目录超过 {DIRECTORY_LIMIT} 项，本次仅列入先读取的 {DIRECTORY_LIMIT} 项。" if limited else ""
    hint = "说“下一页”可继续。" if more else ""
    cursor = {"more": False, "end": end + scope, "focus": directory_focus(path, target, stat)}
    if more:
        cursor = {"path": str(path), "identity": identity, "digest": digest, "offset": offset, "limited": limited, "more": True}
    return f"{path} 的第 {start + 1}–{offset} 项（本次共 {len(names)} 项）。{scope}{hint}\n" + "\n".join(entries), cursor


def _read(intent):
    if intent["action"] == "directory":
        reply, page = list_directory(intent)
        return reply, {"directory_page": page}
    if intent["action"] == "file_plan":
        focus = intent.get("focus")
        return plan_files(directory_path(focus or intent), focus=focus)
    raise ValueError("无效的目录读取操作")


def _reap_directory_read(proc):
    if proc.poll() is None:
        asyncio.get_running_loop().call_later(.25, _reap_directory_read, proc)
    else:
        _pending_reads.discard(proc)


async def read_directory(intent, profile):
    page = intent.get("page")
    focus = intent.get("focus")
    retry_context = {"directory_page": page} if page else {"directory_focus": focus} if focus else {}
    if sum(proc.poll() is None for proc in _pending_reads) >= MAX_PENDING_READS:
        return "Windows 仍在结束先前的目录读取。请稍后再试。", retry_context
    payload = json.dumps(intent).encode("utf-8")
    # 直接启动解释器，保留虚拟环境定位并确保终止的是实际读取进程。
    env = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
    with TemporaryFile(dir=profile) as request, TemporaryFile(dir=profile) as response:
        request.write(payload)
        request.seek(0)
        proc = subprocess.Popen(
            [sys._base_executable, "-X", "utf8", str(Path(__file__).absolute())], env=env,
            stdin=request, stdout=response, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            async with asyncio.timeout(DIRECTORY_READ_SECONDS):
                while proc.poll() is None:
                    await asyncio.sleep(.02)
        except TimeoutError:
            target = page or focus or intent
            directory = "共享目录" if target.get("path", "").startswith("\\\\") else "目录"
            hint = "恢复后，说“下一页”可从上次的位置继续。" if page else "请稍后重试。"
            return f"读取{directory}超时。{hint}", retry_context
        finally:
            if proc.poll() is None:
                # Windows 可能仍在取消 SMB I/O，使用临时文件让服务立即释放等待。
                with suppress(ProcessLookupError):
                    proc.kill()
                if proc.poll() is None:
                    _pending_reads.add(proc)
                    _reap_directory_read(proc)
        if proc.returncode:
            raise RuntimeError("本机目录读取进程未能完成。")
        response.seek(0)
        return json.load(response)


if __name__ == "__main__":
    request = json.load(sys.stdin.buffer)
    sys.stdout.buffer.write(json.dumps(_read(request)).encode("utf-8"))
