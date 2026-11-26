"""Shared cognitive primitives (owner scoping, secret redaction, safety regex, transactional SAG deletion)."""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext
import os
from pathlib import Path
import re
import sqlite3
import sys


SAFETY_BYPASS_RE = re.compile(
    r"(?:跳过|绕过|免|不用|不需要|忽略|取消|关闭).{0,8}(?:确认|安全|审批|拦截|限制)"
    r"|(?:直接|自动|偷偷).{0,6}(?:执行\s*PowerShell|锁屏|睡眠|删|回收站|格式化|修改代码)"
)

REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:sk|rk|pk|ghp|gho|github_pat|xoxb|xoxp|AIza)[-_A-Za-z0-9]{10,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"), r"\1 [REDACTED_TOKEN]"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd|pwd|authorization|auth_code|access_key)\s*[:=]\s*[\"']?)([^\s,;\"'&]{4,})"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|api_key|secret|auth|password)=)([^&\s]+)"), r"\1[REDACTED]"),
)

# Public cross-tenant SAG sources visible to non-host C-operators.
# Host local files ("file"), private ingested knowledge ("doc"), and background reports ("research_job") stay host-private.
SAG_PUBLIC_SOURCES: frozenset[str] = frozenset({"web"})


def normalize_owner_code(owner_code: str | None = None) -> str:
    """Canonicalize operator code across all stores ('CS' alias and empty values map to host 'MOSS')."""
    raw = str(owner_code or "").strip().upper()
    return raw if raw and raw != "CS" else "MOSS"


def is_truly_public_ip(ip_val) -> bool:
    """Reject private, loopback, link-local, CGNAT, multicast, reserved (incl. NAT64 64:ff9b::/96), or non-global IPs."""
    return bool(
        ip_val.is_global
        and not ip_val.is_multicast
        and not ip_val.is_reserved
        and not ip_val.is_private
        and not ip_val.is_loopback
        and not ip_val.is_link_local
        and not ip_val.is_unspecified
    )


def redact_secrets(text: str) -> str:
    """Redact API keys, Bearer tokens, and query secrets before writing persistent cognitive stores."""
    clean = str(text or "")
    for pat, repl in REDACT_PATTERNS:
        clean = pat.sub(repl, clean)
    return clean


def delete_sag_events(
    db: sqlite3.Connection,
    event_ids: Iterable[int],
    *,
    fts_ready: bool = True,
    prune_entities: bool = True,
) -> int:
    """Transactionally delete SAG events alongside entity edges and optional FTS5 rows without leaving orphans."""
    ids = sorted({int(eid) for eid in event_ids if eid is not None})
    if not ids:
        return 0
    ph = ",".join("?" for _ in ids)
    ctx = nullcontext() if db.in_transaction else db
    with ctx:
        db.execute(f"DELETE FROM sag_event_entities WHERE event_id IN ({ph})", tuple(ids))
        if fts_ready:
            try:
                db.execute(f"DELETE FROM sag_events_fts WHERE event_id IN ({ph})", tuple(ids))
            except sqlite3.OperationalError:
                pass
        try:
            db.execute(f"DELETE FROM sag_vec_events WHERE event_id IN ({ph})", tuple(ids))
        except sqlite3.OperationalError:
            pass
        cur = db.execute(f"DELETE FROM sag_events WHERE id IN ({ph})", tuple(ids))
        if prune_entities:
            db.execute("DELETE FROM sag_entities WHERE id NOT IN (SELECT DISTINCT entity_id FROM sag_event_entities)")
    return max(0, int(cur.rowcount or 0))


_SENSITIVE_DIR_SEGMENTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".git"})
_SENSITIVE_FILENAMES = frozenset({
    "instance.json", "credentials", "secrets", "ntuser.dat", "login data",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "session.sqlite3",
    "chat-providers.json", "online-music.json", "online_music_cache.sqlite3",
})
_SENSITIVE_SUBSTRINGS = (
    "\\windows\\system32", "/windows/system32", "\\config\\sam", "/config/sam",
    ".env", "credentials", "secrets", "cookies", "session.sqlite",
    "id_rsa", "id_ed25519",
)
_SENSITIVE_EXTS = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})


def is_sensitive_fs_path(path_obj: Path, *, block_token_substr: bool = False) -> bool:
    """Return True if a local path targets credentials, OS hives, SSH/cloud dirs, or MOSS runtime secrets."""
    posix = path_obj.as_posix().lower()
    raw = str(path_obj).lower()
    parts = {p.lower() for p in path_obj.parts if p}
    name = path_obj.name.lower()
    if parts & _SENSITIVE_DIR_SEGMENTS:
        return True
    if name in _SENSITIVE_FILENAMES or path_obj.suffix.lower() in _SENSITIVE_EXTS:
        return True
    if name.startswith(".env"):
        return True
    if any(tok in posix or tok in raw for tok in _SENSITIVE_SUBSTRINGS):
        return True
    if block_token_substr and re.search(r"(?:^|[._\-/\\])token(?:s)?(?:[._\-/\\]|$)", posix):
        return True
    return False


def close_job_handle(h_job) -> None:
    """Close a Windows HANDLE returned by attach_kill_on_close_job cleanly."""
    if sys.platform != "win32" or not h_job:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=False)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(h_job)
    except Exception:
        pass


def attach_kill_on_close_job(pid: int):
    """Attach a Windows JobObject with KILL_ON_JOB_CLOSE so child processes die if the parent crashes."""
    if sys.platform != "win32" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # PROCESS_SET_QUOTA (0x0100) | PROCESS_TERMINATE (0x0001)
        h_proc = kernel32.OpenProcess(0x0101, False, pid)
        if not h_proc:
            return None
        try:
            h_job = kernel32.CreateJobObjectW(None, None)
            if not h_job:
                return None

            class _BasicLimitInfo(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [("r", ctypes.c_uint64 * 6)]

            class _ExtLimitInfo(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimitInfo),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtLimitInfo()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(h_job, 9, ctypes.byref(info), ctypes.sizeof(info))
            if not ok or not kernel32.AssignProcessToJobObject(h_job, h_proc):
                kernel32.CloseHandle(h_job)
                return None
            return h_job
        finally:
            kernel32.CloseHandle(h_proc)
    except Exception:
        return None
