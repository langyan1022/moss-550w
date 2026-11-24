"""MOSS Proactive Pulse Engine (自主脉搏与主动守望).

Gives MOSS an autonomous, non-intrusive heartbeat with strict interruption budgets:
  - Hardware Thermal / Battery / Memory / Disk Sentinel (piggybacks on DeviceStatusCache, 0 MiB VRAM).
  - Upcoming Local Calendar Sentinel (alerts once within 15 minutes before an event).
  - Real-Time Situational Briefing Generator (clock + weather + calendar + timers + notes + brain telemetry).
  - Quiet / Do-Not-Disturb mode + per-alert cooldowns so MOSS never spams or interrupts focus/speech.
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, time as dt_time, timedelta, timezone
import json
import sqlite3
from time import monotonic, time

from device_status import GIB
from local_calendar import list_events


DEFAULT_CONFIG = {
    "enabled": True,
    "dnd": False,
    "gpu_temp_warn_c": 85.0,
    "cpu_temp_warn_c": 90.0,
    "battery_low_pct": 20,
    "ram_low_gib": 3.5,
    "disk_low_gib": 15.0,
    "calendar_lead_minutes": 15,
    "min_interval_seconds": 600,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProactivePulseEngine:
    """Bounded, respectful ambient sentinel and briefing generator for MOSS."""

    def __init__(self, db: sqlite3.Connection, on_log=None):
        self.db = db
        self.on_log = on_log
        self.alerts = deque(maxlen=40)
        self.subscribers: set = set()
        self._cooldowns: dict[str, float] = {}
        self._last_nudge_at = 0.0
        self._last_eval_at = 0.0
        self._alert_seq = 0
        self._init_schema()
        self.config = self._load_config()

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS proactive_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    doc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_notified (
                    key TEXT PRIMARY KEY,
                    notified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    level TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    dismissed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)

    def _load_config(self) -> dict:
        row = self.db.execute("SELECT doc FROM proactive_config WHERE id=1").fetchone()
        if row:
            try:
                saved = json.loads(row[0])
                if isinstance(saved, dict):
                    return {**DEFAULT_CONFIG, **saved}
            except json.JSONDecodeError:
                pass
        return dict(DEFAULT_CONFIG)

    def update_config(self, patch: dict) -> dict:
        merged = dict(self.config)
        for key in ("enabled", "dnd"):
            if key in patch:
                merged[key] = bool(patch[key])
        for key in ("gpu_temp_warn_c", "cpu_temp_warn_c", "ram_low_gib", "disk_low_gib"):
            if key in patch and isinstance(patch[key], (int, float)):
                merged[key] = max(1.0, min(120.0, float(patch[key])))
        for key in ("battery_low_pct", "calendar_lead_minutes", "min_interval_seconds"):
            if key in patch and isinstance(patch[key], (int, float)):
                merged[key] = max(1, min(3600, int(patch[key])))
        self.config = merged
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO proactive_config(id, doc) VALUES (1, ?)",
                (json.dumps(merged, ensure_ascii=False),),
            )
        self._log(
            f"Config updated: enabled={merged['enabled']} dnd={merged['dnd']}",
            level="info",
        )
        return dict(self.config)

    def _log(self, text: str, level: str = "info"):
        if self.on_log is not None:
            try:
                self.on_log(text, source="PULSE", level=level)
            except Exception:
                pass

    def _emit_alert(self, *, kind: str, level: str, title: str, body: str, dedup_key: str, cooldown_sec: float, bypass_budget: bool = False) -> dict | None:
        now_m = monotonic()
        if dedup_key in self._cooldowns and now_m - self._cooldowns[dedup_key] < cooldown_sec:
            return None
        if not bypass_budget and self._last_nudge_at and now_m - self._last_nudge_at < self.config["min_interval_seconds"]:
            return None
        if self.config["dnd"] and level != "crit":
            return None

        self._cooldowns[dedup_key] = now_m
        self._last_nudge_at = now_m
        self._alert_seq += 1
        created = _now_iso()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO proactive_alerts(kind, level, title, body, dismissed, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (kind, level, title[:80], body[:360], created),
            )
            aid = cur.lastrowid
        alert = {
            "id": aid,
            "kind": kind,
            "level": level,
            "title": title[:80],
            "body": body[:360],
            "dismissed": False,
            "created_at": created,
            "ts": int(time() * 1000),
        }
        self.alerts.append(alert)
        self._log(f"[{level.upper()}] {title} — {body[:90]}", level=level if level in {"ok", "info", "warn", "crit"} else "info")
        for sig in tuple(self.subscribers):
            sig.set()
        return alert

    def dismiss_alert(self, alert_id: int) -> bool:
        with self.db:
            cur = self.db.execute("UPDATE proactive_alerts SET dismissed=1 WHERE id=?", (alert_id,))
        for item in self.alerts:
            if item["id"] == alert_id:
                item["dismissed"] = True
        return cur.rowcount > 0

    def evaluate_tick(self, *, device_sample: dict | None = None, is_busy: bool = False) -> list[dict]:
        """Run one lightweight non-blocking sentinel pass."""
        self._last_eval_at = time()
        if not self.config["enabled"] or is_busy:
            return []
        emitted: list[dict] = []

        # 1. Hardware Thermal / Battery / Memory / Disk Sentinel
        if isinstance(device_sample, dict):
            gpu = device_sample.get("gpu") or {}
            gpu_t = gpu.get("temperature_c")
            if isinstance(gpu_t, (int, float)) and gpu_t >= self.config["gpu_temp_warn_c"]:
                al = self._emit_alert(
                    kind="thermal_gpu",
                    level="crit" if gpu_t >= self.config["gpu_temp_warn_c"] + 5 else "warn",
                    title=f"显卡温度偏高 ({gpu_t:.0f}°C)",
                    body=f"{gpu.get('name') or 'GPU'} 当前 {gpu_t:.0f}°C（负载 {gpu.get('percent') or 0:.0f}%），建议检查散热或高负载任务。",
                    dedup_key="thermal_gpu",
                    cooldown_sec=900,
                    bypass_budget=True,
                )
                if al:
                    emitted.append(al)

            cpu = device_sample.get("cpu") or {}
            cpu_t = cpu.get("temperature_c")
            if isinstance(cpu_t, (int, float)) and cpu_t >= self.config["cpu_temp_warn_c"]:
                al = self._emit_alert(
                    kind="thermal_cpu",
                    level="crit" if cpu_t >= self.config["cpu_temp_warn_c"] + 5 else "warn",
                    title=f"处理器温度偏高 ({cpu_t:.0f}°C)",
                    body=f"CPU 当前 {cpu_t:.0f}°C，注意散热风道与后台占用。",
                    dedup_key="thermal_cpu",
                    cooldown_sec=900,
                    bypass_budget=True,
                )
                if al:
                    emitted.append(al)

            bat = device_sample.get("battery") or {}
            if bat.get("present") is True and bat.get("ac") is False and isinstance(bat.get("percent"), (int, float)):
                if bat["percent"] <= self.config["battery_low_pct"]:
                    al = self._emit_alert(
                        kind="battery_low",
                        level="crit" if bat["percent"] <= 10 else "warn",
                        title=f"低电量提醒 ({bat['percent']:.0f}%)",
                        body="当前使用电池供电且电量偏低，建议及时接通电源以免中断本机任务。",
                        dedup_key="battery_low",
                        cooldown_sec=900,
                        bypass_budget=True,
                    )
                    if al:
                        emitted.append(al)

            mem = device_sample.get("memory") or {}
            if isinstance(mem.get("available"), (int, float)) and mem["available"] / GIB < self.config["ram_low_gib"]:
                avail_g = mem["available"] / GIB
                al = self._emit_alert(
                    kind="ram_low",
                    level="warn",
                    title=f"可用内存吃紧 ({avail_g:.1f} GiB)",
                    body="本机剩余内存低于阈值，建议清理占用进程以保持语音与模型流畅。",
                    dedup_key="ram_low",
                    cooldown_sec=1200,
                )
                if al:
                    emitted.append(al)

            disk = device_sample.get("disk") or {}
            if isinstance(disk.get("free"), (int, float)) and disk["free"] / GIB < self.config["disk_low_gib"]:
                free_g = disk["free"] / GIB
                al = self._emit_alert(
                    kind="disk_low",
                    level="warn",
                    title=f"系统盘余量不足 ({free_g:.1f} GiB)",
                    body=f"磁盘 {disk.get('drive') or 'C:'} 仅剩 {free_g:.1f} GiB，注意及时清理缓存或归档大文件。",
                    dedup_key="disk_low",
                    cooldown_sec=3600,
                )
                if al:
                    emitted.append(al)

        # 2. Upcoming Local Calendar Event Sentinel (next N minutes)
        now_local = datetime.now().astimezone()
        lead = timedelta(minutes=int(self.config["calendar_lead_minutes"]))
        win_start = int(now_local.timestamp())
        win_end = int((now_local + lead).timestamp())
        try:
            events = list_events(self.db, {"start": win_start, "end": win_end, "detail": "即将开始"})
        except Exception:
            events = []
        for ev in events:
            ev_key = f"cal:{ev.get('id')}:{ev.get('start_at')}"
            already = self.db.execute("SELECT 1 FROM proactive_notified WHERE key=?", (ev_key,)).fetchone()
            if already:
                continue
            start_dt = datetime.fromtimestamp(ev["start_at"]).astimezone()
            mins_left = max(1, int(round((start_dt - now_local).total_seconds() / 60)))
            al = self._emit_alert(
                kind="calendar_upcoming",
                level="info",
                title=f"日程即将开始（约 {mins_left} 分钟后）",
                body=f"{start_dt.strftime('%H:%M')} · {ev.get('title') or '未命名日程'}",
                dedup_key=ev_key,
                cooldown_sec=86400,
                bypass_budget=True,
            )
            if al:
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO proactive_notified(key, notified_at) VALUES (?, ?)", (ev_key, _now_iso()))
                emitted.append(al)

        return emitted

    def generate_briefing(
        self,
        *,
        device_sample: dict | None = None,
        timers_count: int = 0,
        notes_count: int = 0,
        rsi_active: int = 0,
        sag_events: int = 0,
        world_model: dict | None = None,
    ) -> dict:
        """Build a concise, real-data 550W situational briefing."""
        now_local = datetime.now().astimezone()
        hour = now_local.hour
        if 5 <= hour < 11:
            period = "早间态势简报"
        elif 11 <= hour < 14:
            period = "午间态势简报"
        elif 14 <= hour < 19:
            period = "午后态势简报"
        elif 19 <= hour < 23:
            period = "晚间态势简报"
        else:
            period = "深夜值守简报"

        today = date.today()
        start_ts = int(datetime.combine(today, dt_time.min).astimezone().timestamp())
        end_ts = int(datetime.combine(today + timedelta(days=1), dt_time.min).astimezone().timestamp())
        try:
            today_events = list_events(self.db, {"start": start_ts, "end": end_ts, "detail": "今日"})
        except Exception:
            today_events = []

        parts = [f"现在是 {now_local.strftime('%Y年%m月%d日 %H:%M')}。"]
        if today_events:
            titles = "、".join(e.get("title", "日程") for e in today_events[:3])
            parts.append(f"今日安排 {len(today_events)} 项日程：{titles}。")
        else:
            parts.append("今日暂无排期日程。")

        if timers_count > 0 or notes_count > 0:
            parts.append(f"当前有 {timers_count} 条待办提醒、{notes_count} 条本机便签。")

        if isinstance(device_sample, dict):
            hw_bits = []
            if isinstance(device_sample.get("cpu_percent"), (int, float)):
                hw_bits.append(f"CPU {device_sample['cpu_percent']:.0f}%")
            gpu = device_sample.get("gpu") or {}
            if isinstance(gpu.get("temperature_c"), (int, float)):
                hw_bits.append(f"GPU {gpu['temperature_c']:.0f}°C")
            mem = device_sample.get("memory") or {}
            if isinstance(mem.get("available"), (int, float)):
                hw_bits.append(f"可用内存 {mem['available'] / GIB:.1f} GiB")
            if hw_bits:
                parts.append("主机状态：" + "，".join(hw_bits) + "。")

        parts.append(f"MOSS 大脑当前生效 {rsi_active} 条自我迭代规则，SAG 知识图谱已索引 {sag_events} 条节点。")
        if world_model and world_model.get("conflicts"):
            parts.append(f"另外检测到 {len(world_model['conflicts'])} 处知识线索存在时间或内容差异，可在大脑中枢核对。")
        if hour >= 23 or hour < 5:
            parts.append("夜深了，如需收尾可直接让我播放班得瑞纯音乐或设个休息提醒。")

        text = "".join(parts)
        self._log(f"Briefing generated: {period} ({len(text)} chars)", level="ok")
        return {
            "title": period,
            "generated_at": _now_iso(),
            "text": text,
            "today_events_count": len(today_events),
            "timers_count": timers_count,
            "notes_count": notes_count,
        }

    def status_snapshot(self) -> dict:
        rows = self.db.execute(
            """SELECT id, kind, level, title, body, dismissed, created_at
               FROM proactive_alerts ORDER BY id DESC LIMIT 12"""
        ).fetchall()
        alerts = [
            {
                "id": r[0], "kind": r[1], "level": r[2], "title": r[3],
                "body": r[4], "dismissed": bool(r[5]), "created_at": r[6],
            }
            for r in rows
        ]
        active_alerts = [a for a in alerts if not a["dismissed"]]
        return {
            "config": dict(self.config),
            "active_alert_count": len(active_alerts),
            "alerts": alerts,
            "last_eval_at": self._last_eval_at,
        }
