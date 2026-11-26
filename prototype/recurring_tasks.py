"""Recurring scheduled routines & daily briefings for MOSS 550W (v5.5).

Stores persistent recurring schedules in `session.sqlite3` (`recurring_tasks` table):
  - daily HH:MM        (e.g. every day at 08:30)
  - weekdays HH:MM     (Mon-Fri at 08:30)
  - weekly <0-6> HH:MM (0=Mon .. 6=Sun, e.g. Fri 17:00)
  - interval <N>m      (every N minutes, N in [5..1440])

Polled inside `serve.py`'s background pulse loop; respects quiet mode / meeting mute.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
import json
import re
import sqlite3
import uuid

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_DOW_MAP = {
    "一": 0, "1": 0, "mon": 0, "monday": 0,
    "二": 1, "2": 1, "tue": 1, "tuesday": 1,
    "三": 2, "3": 2, "wed": 2, "wednesday": 2,
    "四": 3, "4": 3, "thu": 3, "thursday": 3,
    "五": 4, "5": 4, "fri": 4, "friday": 4,
    "六": 5, "6": 5, "sat": 5, "saturday": 5,
    "日": 6, "天": 6, "7": 6, "sun": 6, "sunday": 6,
}


def parse_schedule_spec(raw: str) -> dict:
    """Normalize a schedule spec such as 'daily 08:30', 'weekdays 08:30', 'weekly 4 17:00',
    'interval 45m', or Chinese phrases like '每天早上8:30', '工作日8点半', '每周五下午5点', '每45分钟'."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("缺少周期计划表达式")
    low = s.lower()

    # 1. Machine formats
    m = re.fullmatch(r"interval\s+(\d+)\s*m(?:in)?", low)
    if m:
        mins = int(m.group(1))
        if not 5 <= mins <= 1440:
            raise ValueError("间隔分钟数需在 5 到 1440 之间")
        return {"kind": "interval", "every_min": mins, "label": f"每 {mins} 分钟"}

    m = re.fullmatch(r"(daily|weekdays)\s+(\d{1,2}):(\d{2})", low)
    if m:
        kind, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("时间必须在 00:00 到 23:59 之间")
        lbl = f"{'工作日' if kind == 'weekdays' else '每天'} {hh:02d}:{mm:02d}"
        return {"kind": kind, "hour": hh, "minute": mm, "label": lbl}

    m = re.fullmatch(r"weekly\s+([a-z0-6])\s+(\d{1,2}):(\d{2})", low)
    if m:
        dow = _DOW_MAP.get(m.group(1))
        if dow is None:
            raise ValueError("星期参数无效（0=周一 .. 6=周日）")
        hh, mm = int(m.group(2)), int(m.group(3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("时间必须在 00:00 到 23:59 之间")
        return {"kind": "weekly", "weekday": dow, "hour": hh, "minute": mm, "label": f"每{WEEKDAY_NAMES[dow]} {hh:02d}:{mm:02d}"}

    # Parse HH:MM or X点Y分 / X点半 (ASCII & basic Chinese numerals 零一二两三四五六七八九十)
    def _cn_int(tok: str) -> int | None:
        if tok.isdigit():
            return int(tok)
        digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if tok == "十":
            return 10
        if tok in digits:
            return digits[tok]
        if tok.startswith("十") and len(tok) == 2 and tok[1] in digits:
            return 10 + digits[tok[1]]
        if tok.endswith("十") and len(tok) == 2 and tok[0] in digits:
            return digits[tok[0]] * 10
        if len(tok) == 3 and tok[1] == "十" and tok[0] in digits and tok[2] in digits:
            return digits[tok[0]] * 10 + digits[tok[2]]
        return None

    m_iv = re.search(r"每隔?\s*([0-9]+|[零〇一二两三四五六七八九十]+)\s*(?:分钟|分|小时|个?钟头)", s)
    if m_iv:
        val = _cn_int(m_iv.group(1))
        if val is not None:
            mins = val * 60 if any(u in m_iv.group(0) for u in ("小时", "钟头")) else val
            if not 5 <= mins <= 1440:
                raise ValueError("间隔分钟数需在 5 到 1440 之间")
            return {"kind": "interval", "every_min": mins, "label": f"每 {mins} 分钟"}

    hh = mm = None
    m_hm = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", s)
    if m_hm:
        hh, mm = int(m_hm.group(1)), int(m_hm.group(2))
    else:
        m_zh = re.search(r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*点\s*(半|(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*分?)?", s)
        if m_zh:
            hh = _cn_int(m_zh.group(1))
            if hh is not None:
                if m_zh.group(2) == "半":
                    mm = 30
                elif m_zh.group(3):
                    mm = _cn_int(m_zh.group(3))
                else:
                    mm = 0
    if hh is not None and mm is not None:
        if any(k in s for k in ("凌晨", "半夜")) and hh == 12:
            hh = 0
        elif any(k in s for k in ("下午", "傍晚", "晚上", "晚间")) and 1 <= hh <= 11:
            hh += 12
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("时间必须在 00:00 到 23:59 之间")
        m_wk = re.search(r"(?:每周|每星期|每(?:个?)?礼拜)([一二三四五六日天])", s)
        if m_wk:
            dow = _DOW_MAP[m_wk.group(1)]
            return {"kind": "weekly", "weekday": dow, "hour": hh, "minute": mm, "label": f"每{WEEKDAY_NAMES[dow]} {hh:02d}:{mm:02d}"}
        if "工作日" in s or "周一到周五" in s:
            return {"kind": "weekdays", "hour": hh, "minute": mm, "label": f"工作日 {hh:02d}:{mm:02d}"}
        if "每天" in s or "每日" in s:
            return {"kind": "daily", "hour": hh, "minute": mm, "label": f"每天 {hh:02d}:{mm:02d}"}

    raise ValueError(f"未能识别周期计划「{s}」，示例：每天 08:30、工作日 09:00、每周五 17:00 或 每 45 分钟")


class RecurringTaskStore:
    def __init__(self, db: sqlite3.Connection, *, on_log=None):
        self.db = db
        self.on_log = on_log
        with (nullcontext() if self.db.in_transaction else self.db):
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS recurring_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    schedule_json TEXT NOT NULL,
                    action_kind TEXT NOT NULL DEFAULT 'reminder',
                    payload TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    owner_code TEXT NOT NULL DEFAULT 'MOSS'
                )"""
            )
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(recurring_tasks)").fetchall()}
            if "owner_code" not in cols:
                self.db.execute("ALTER TABLE recurring_tasks ADD COLUMN owner_code TEXT NOT NULL DEFAULT 'MOSS'")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_recurring_owner ON recurring_tasks(owner_code, created_at DESC)")

    @staticmethod
    def _norm_owner(owner_code: str | None) -> str:
        raw = str(owner_code or "").strip().upper()
        return raw if raw and raw != "CS" else "MOSS"

    def _row_to_dict(self, row) -> dict:
        tid, name, sched_json, kind, payload, enabled, last_run, created = row[:8]
        owner_code = str(row[8] if len(row) > 8 and row[8] else "MOSS")
        try:
            sched = json.loads(sched_json)
        except ValueError:
            sched = {}
        return {
            "id": tid,
            "name": name,
            "schedule": sched,
            "schedule_label": sched.get("label", ""),
            "action_kind": kind,
            "payload": payload,
            "enabled": bool(enabled),
            "last_run_at": last_run or "",
            "created_at": created,
            "owner_code": owner_code,
        }

    def list_tasks(self, *, owner_code: str | None = None) -> list[dict]:
        if owner_code is not None:
            owner = self._norm_owner(owner_code)
            rows = self.db.execute(
                "SELECT id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, COALESCE(owner_code, 'MOSS') "
                "FROM recurring_tasks WHERE COALESCE(owner_code, 'MOSS')=? ORDER BY created_at DESC",
                (owner,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, COALESCE(owner_code, 'MOSS') "
                "FROM recurring_tasks ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def create_task(
        self,
        *,
        name: str,
        schedule: str,
        action_kind: str = "reminder",
        payload: str = "",
        owner_code: str = "MOSS",
    ) -> dict:
        clean_name = (name or "").strip()[:80]
        if not clean_name:
            raise ValueError("周期任务名称不能为空")
        kind = action_kind if action_kind in {"reminder", "briefing", "dream", "prompt", "skill"} else "reminder"
        spec = parse_schedule_spec(schedule)
        owner = self._norm_owner(owner_code)
        tid = uuid.uuid4().hex[:10]
        created = datetime.now().isoformat(timespec="seconds")
        with (nullcontext() if self.db.in_transaction else self.db):
            self.db.execute(
                "INSERT INTO recurring_tasks (id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, owner_code) "
                "VALUES (?, ?, ?, ?, ?, 1, '', ?, ?)",
                (tid, clean_name, json.dumps(spec, ensure_ascii=False), kind, (payload or clean_name).strip()[:500], created, owner),
            )
        if self.on_log:
            self.on_log(f"创建周期任务 #{tid} [{owner}]「{clean_name}」({spec['label']})", source="PULSE", level="ok")
        return self.get_task(tid)

    def get_task(self, task_id: str, *, owner_code: str | None = None) -> dict | None:
        if owner_code is not None:
            owner = self._norm_owner(owner_code)
            row = self.db.execute(
                "SELECT id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, COALESCE(owner_code, 'MOSS') "
                "FROM recurring_tasks WHERE id=? AND COALESCE(owner_code, 'MOSS')=?",
                (task_id, owner),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, COALESCE(owner_code, 'MOSS') "
                "FROM recurring_tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def toggle_task(self, task_id: str, enabled: bool, *, owner_code: str | None = None) -> dict | None:
        with (nullcontext() if self.db.in_transaction else self.db):
            if owner_code is not None:
                self.db.execute(
                    "UPDATE recurring_tasks SET enabled=? WHERE id=? AND COALESCE(owner_code, 'MOSS')=?",
                    (1 if enabled else 0, task_id, self._norm_owner(owner_code)),
                )
            else:
                self.db.execute("UPDATE recurring_tasks SET enabled=? WHERE id=?", (1 if enabled else 0, task_id))
        return self.get_task(task_id, owner_code=owner_code)

    def delete_task(self, task_id_or_name: str, *, owner_code: str | None = None) -> dict | None:
        needle = (task_id_or_name or "").strip()
        if not needle:
            return None
        norm_owner = self._norm_owner(owner_code) if owner_code is not None else None
        owner_clause = " AND COALESCE(owner_code, 'MOSS')=?" if norm_owner is not None else ""
        owner_args = (norm_owner,) if norm_owner is not None else ()
        row = self.db.execute(
            f"SELECT id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, COALESCE(owner_code, 'MOSS') "
            f"FROM recurring_tasks WHERE id=?{owner_clause}",
            (needle, *owner_args),
        ).fetchone()
        if not row:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            matches = self.db.execute(
                f"SELECT id, name, schedule_json, action_kind, payload, enabled, last_run_at, created_at, COALESCE(owner_code, 'MOSS') "
                f"FROM recurring_tasks WHERE name LIKE ? ESCAPE '\\'{owner_clause}",
                (f"%{escaped}%", *owner_args),
            ).fetchall()
            if len(matches) == 1:
                row = matches[0]
            elif len(matches) > 1:
                raise ValueError(f"匹配到多个周期任务：{'、'.join(r[1] for r in matches[:5])}，请指定更准确的名称或编号。")
        if not row:
            return None
        item = self._row_to_dict(row)
        with (nullcontext() if self.db.in_transaction else self.db):
            self.db.execute("DELETE FROM recurring_tasks WHERE id=?", (item["id"],))
        return item

    def pop_due_tasks(self, now_dt: datetime | None = None) -> list[dict]:
        """Return enabled tasks due at `now_dt`, marking `last_run_at` to prevent duplicate firing."""
        cur = now_dt or datetime.now()
        due: list[dict] = []
        updates: list[tuple[str, str]] = []
        for task in self.list_tasks():
            if not task["enabled"]:
                continue
            sched = task["schedule"]
            kind = sched.get("kind")
            last_dt = None
            if task["last_run_at"]:
                try:
                    last_dt = datetime.fromisoformat(task["last_run_at"])
                except ValueError:
                    last_dt = None
            should_fire = False
            if kind == "interval":
                every = int(sched.get("every_min", 60))
                if last_dt is None:
                    created_dt = datetime.fromisoformat(task["created_at"])
                    should_fire = (cur - created_dt) >= timedelta(minutes=every)
                else:
                    should_fire = (cur - last_dt) >= timedelta(minutes=every)
            elif kind in {"daily", "weekdays", "weekly"}:
                if kind == "weekdays" and cur.weekday() >= 5:
                    continue
                if kind == "weekly" and cur.weekday() != int(sched.get("weekday", 0)):
                    continue
                target = cur.replace(hour=int(sched.get("hour", 8)), minute=int(sched.get("minute", 30)), second=0, microsecond=0)
                if cur >= target and (cur - target) <= timedelta(minutes=10):
                    if last_dt is None or last_dt.date() != cur.date():
                        should_fire = True
            if should_fire:
                stamp = cur.isoformat(timespec="seconds")
                updates.append((stamp, task["id"]))
                task["last_run_at"] = stamp
                due.append(task)
        if updates:
            with (nullcontext() if self.db.in_transaction else self.db):
                for stamp, task_id in updates:
                    self.db.execute("UPDATE recurring_tasks SET last_run_at=? WHERE id=?", (stamp, task_id))
        return due
