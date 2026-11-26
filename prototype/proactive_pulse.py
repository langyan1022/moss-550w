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
import random
import sqlite3
from time import monotonic, time

from cognitive_utils import normalize_owner_code
from device_status import GIB
from local_calendar import list_events


DEFAULT_CONFIG = {
    "enabled": True,
    "dnd": False,
    "auto_quiet_night": True,
    "quiet_hours_start": 23,
    "quiet_hours_end": 7,
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
        self._deferred_alerts: list[dict] = []
        self._last_presence: bool | None = None
        self._dismiss_streak: int = 0
        self.on_push = None
        self._bandit_ctx_by_alert: dict[int, tuple[str, str]] = {}
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
                    owner_code TEXT NOT NULL DEFAULT 'MOSS',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pulse_bandit_stats (
                    kind TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    alpha REAL NOT NULL DEFAULT 2.0,
                    beta REAL NOT NULL DEFAULT 1.0,
                    accepts INTEGER NOT NULL DEFAULT 0,
                    dismisses INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (kind, context_key)
                );
            """)
            cols = {row[1] for row in self.db.execute("PRAGMA table_info(proactive_alerts)").fetchall()}
            if "owner_code" not in cols:
                self.db.execute("ALTER TABLE proactive_alerts ADD COLUMN owner_code TEXT NOT NULL DEFAULT 'MOSS'")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_proactive_alerts_owner ON proactive_alerts(owner_code, id DESC)")

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
        for key in ("enabled", "dnd", "auto_quiet_night"):
            if key in patch:
                merged[key] = bool(patch[key])
        for key in ("quiet_hours_start", "quiet_hours_end"):
            if key in patch and isinstance(patch[key], (int, float)) and not isinstance(patch[key], bool):
                merged[key] = max(0, min(23, int(patch[key])))
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

    def _is_quiet_hour(self, hour: int | None = None) -> bool:
        if not self.config.get("auto_quiet_night", True):
            return False
        h = datetime.now().astimezone().hour if hour is None else int(hour)
        start = int(self.config.get("quiet_hours_start", 23))
        end = int(self.config.get("quiet_hours_end", 7))
        if start == end:
            return False
        return (h >= start or h < end) if start > end else (start <= h < end)

    def note_user_activity(self):
        self._dismiss_streak = 0

    @staticmethod
    def context_bucket(*, hour: int | None = None, media_playing: bool = False, user_active: bool = False) -> str:
        h = datetime.now().astimezone().hour if hour is None else int(hour) % 24
        if 7 <= h < 12:
            period = "morning"
        elif 12 <= h < 18:
            period = "afternoon"
        elif 18 <= h < 23:
            period = "evening"
        else:
            period = "night"
        act = "media" if media_playing else ("active" if user_active else "idle")
        return f"{period}:{act}"

    def _bandit_stats(self, kind: str, ctx_key: str) -> tuple[float, float, int, int]:
        row = self.db.execute(
            "SELECT alpha, beta, accepts, dismisses FROM pulse_bandit_stats WHERE kind=? AND context_key=?",
            (kind, ctx_key),
        ).fetchone()
        if not row:
            return 2.0, 1.0, 0, 0
        return float(row[0]), float(row[1]), int(row[2]), int(row[3])

    def record_bandit_outcome(self, kind: str, ctx_key: str, *, accepted: bool) -> dict:
        alpha, beta, acc, dis = self._bandit_stats(kind, ctx_key)
        if accepted:
            alpha = min(50.0, alpha + 1.0)
            acc += 1
        else:
            beta = min(50.0, beta + 1.25)
            dis += 1
        now = _now_iso()
        with self.db:
            self.db.execute(
                """INSERT INTO pulse_bandit_stats(kind, context_key, alpha, beta, accepts, dismisses, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(kind, context_key) DO UPDATE SET
                     alpha=excluded.alpha, beta=excluded.beta,
                     accepts=excluded.accepts, dismisses=excluded.dismisses,
                     updated_at=excluded.updated_at""",
                (kind, ctx_key, alpha, beta, acc, dis, now),
            )
        return {"kind": kind, "context_key": ctx_key, "alpha": round(alpha, 2), "beta": round(beta, 2), "accepts": acc, "dismisses": dis}

    def bandit_cooldown_mul(self, kind: str, ctx_key: str, *, deterministic: bool = False) -> float:
        """Sample Beta(alpha, beta) expected acceptance via Thompson Sampling to damp annoying alerts per context."""
        alpha, beta, _, dis = self._bandit_stats(kind, ctx_key)
        if dis == 0:
            return 1.0
        sample = (alpha / (alpha + beta)) if deterministic else random.betavariate(max(0.2, alpha), max(0.2, beta))
        # Low sampled acceptance scales cooldown multiplier smoothly up to 3.5x
        if sample >= 0.55:
            return 1.0
        return round(min(3.5, 1.0 + (0.55 - sample) * 4.5), 2)

    def _emit_alert(self, *, kind: str, level: str, title: str, body: str, dedup_key: str, cooldown_sec: float, bypass_budget: bool = False, respect_quiet: bool = False, owner_code: str = "MOSS", _hour: int | None = None, is_away: bool = False, media_playing: bool = False, user_active: bool = False, on_hw_frame=None) -> dict | None:
        now_m = monotonic()
        norm_owner = normalize_owner_code(owner_code)
        ctx_key = self.context_bucket(hour=_hour, media_playing=media_playing, user_active=user_active)
        bandit_mul = 1.0 if (bypass_budget or level == "crit") else self.bandit_cooldown_mul(kind, ctx_key)
        if dedup_key in self._cooldowns and now_m - self._cooldowns[dedup_key] < cooldown_sec * bandit_mul:
            return None
        interval_mul = max(bandit_mul, 1.0 + 0.5 * min(2, self._dismiss_streak))
        if not bypass_budget and self._last_nudge_at and now_m - self._last_nudge_at < self.config["min_interval_seconds"] * interval_mul:
            return None
        if (self.config["dnd"] or ((respect_quiet or not bypass_budget) and self._is_quiet_hour(_hour))) and level != "crit":
            return None
        if is_away and level != "crit":
            self._cooldowns[dedup_key] = now_m
            self._deferred_alerts = [
                *[x for x in self._deferred_alerts if x["dedup_key"] != dedup_key][-5:],
                {"kind": kind, "level": level, "title": title[:80], "body": body[:360], "dedup_key": dedup_key, "cooldown_sec": cooldown_sec, "owner_code": norm_owner},
            ]
            return None

        self._cooldowns[dedup_key] = now_m
        self._last_nudge_at = now_m
        self._alert_seq += 1
        created = _now_iso()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO proactive_alerts(kind, level, title, body, dismissed, owner_code, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (kind, level, title[:80], body[:360], norm_owner, created),
            )
            aid = cur.lastrowid
        alert = {
            "id": aid,
            "kind": kind,
            "level": level,
            "title": title[:80],
            "body": body[:360],
            "dismissed": False,
            "owner_code": norm_owner,
            "created_at": created,
            "ts": int(time() * 1000),
        }
        self.alerts.append(alert)
        self._bandit_ctx_by_alert[aid] = (kind, ctx_key)
        self._log(f"[{level.upper()}] {title} — {body[:90]}", level=level if level in {"ok", "info", "warn", "crit"} else "info")
        if callable(on_hw_frame) and level in {"warn", "crit"} and norm_owner == "MOSS":
            try:
                on_hw_frame(
                    mode="alert" if level == "crit" else "thinking",
                    screen_level="crit" if level == "crit" else "warn",
                    screen_line2=title[:18],
                )
            except Exception:
                pass
        if callable(self.on_push):
            try:
                self.on_push(alert)
            except Exception:
                pass
        for sig in tuple(self.subscribers):
            sig.set()
        return alert

    def dismiss_alert(self, alert_id: int, *, owner_code: str | None = None, accepted: bool = False) -> bool:
        if owner_code is not None:
            norm_owner = normalize_owner_code(owner_code)
            with self.db:
                cur = self.db.execute(
                    "UPDATE proactive_alerts SET dismissed=1 WHERE id=? AND COALESCE(owner_code, 'MOSS')=?",
                    (alert_id, norm_owner),
                )
        else:
            with self.db:
                cur = self.db.execute("UPDATE proactive_alerts SET dismissed=1 WHERE id=?", (alert_id,))
        matched_kind = None
        for item in self.alerts:
            if item["id"] == alert_id and (owner_code is None or item.get("owner_code", "MOSS") == normalize_owner_code(owner_code)):
                item["dismissed"] = True
                matched_kind = item.get("kind")
        if cur.rowcount > 0:
            pair = self._bandit_ctx_by_alert.pop(alert_id, None)
            if pair is not None:
                self.record_bandit_outcome(pair[0], pair[1], accepted=accepted)
            elif matched_kind:
                self.record_bandit_outcome(matched_kind, self.context_bucket(), accepted=accepted)
            if not accepted:
                self._dismiss_streak = min(4, self._dismiss_streak + 1)
        return cur.rowcount > 0

    def alerts_for(self, *, owner_code: str = "MOSS", active_only: bool = True, limit: int = 12) -> list[dict]:
        norm_owner = normalize_owner_code(owner_code)
        only_flag = 1 if active_only else 0
        rows = self.db.execute(
            """SELECT id, kind, level, title, body, dismissed, COALESCE(owner_code, 'MOSS'), created_at
               FROM proactive_alerts
               WHERE COALESCE(owner_code, 'MOSS')=? AND (? = 0 OR COALESCE(dismissed, 0) = 0)
               ORDER BY id DESC LIMIT ?""",
            (norm_owner, only_flag, max(1, int(limit))),
        ).fetchall()
        out = [
            {
                "id": r[0], "kind": r[1], "level": r[2], "title": r[3],
                "body": r[4], "dismissed": bool(r[5]), "owner_code": r[6], "created_at": r[7],
            }
            for r in rows
        ]
        out.reverse()
        return out

    def evaluate_tick(
        self,
        *,
        device_sample: dict | None = None,
        is_busy: bool = False,
        weather_sample: dict | None = None,
        hw_sensors: dict | None = None,
        on_hw_frame=None,
    ) -> list[dict]:
        """Run one lightweight non-blocking causal sentinel pass (hardware, presence, calendar, weather)."""
        self._last_eval_at = time()
        if not self.config["enabled"] or is_busy:
            return []
        emitted: list[dict] = []

        has_presence_sensor = isinstance(hw_sensors, dict) and "person_present" in hw_sensors and hw_sensors.get("person_present") is not None
        is_away = bool(has_presence_sensor and hw_sensors.get("person_present") is False)
        if has_presence_sensor:
            cur_present = not is_away
            if self._last_presence is False and cur_present and self._deferred_alerts:
                queued = list(self._deferred_alerts)
                self._deferred_alerts.clear()
                for item in queued:
                    # Flush alerts deferred while the user stepped away from the desk
                    self._cooldowns.pop(item["dedup_key"], None)
                    flushed = self._emit_alert(
                        kind=item["kind"], level=item["level"], title=item["title"],
                        body=item["body"], dedup_key=item["dedup_key"], cooldown_sec=item["cooldown_sec"],
                        bypass_budget=True, owner_code=item.get("owner_code", "MOSS"), is_away=False, on_hw_frame=on_hw_frame,
                    )
                    if flushed:
                        emitted.append(flushed)
            self._last_presence = cur_present

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
                    is_away=is_away,
                    on_hw_frame=on_hw_frame,
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
                    is_away=is_away,
                    on_hw_frame=on_hw_frame,
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
                        is_away=is_away,
                        on_hw_frame=on_hw_frame,
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
                    is_away=is_away,
                    on_hw_frame=on_hw_frame,
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
                    is_away=is_away,
                    on_hw_frame=on_hw_frame,
                )
                if al:
                    emitted.append(al)

        # 2. Upcoming Local Calendar Event Sentinel (next N minutes) + Weather Commute Causality
        now_local = datetime.now().astimezone()
        lead = timedelta(minutes=int(self.config["calendar_lead_minutes"]))
        win_start = int(now_local.timestamp())
        win_end = int((now_local + lead).timestamp())
        try:
            events = list_events(self.db, {"start": win_start, "end": win_end, "detail": "即将开始"})
        except Exception:
            events = []
        for ev in events:
            ev_id = ev.get("operation_id") or ev.get("id") or ""
            start_epoch = ev.get("start") if ev.get("start") is not None else ev.get("start_at")
            if start_epoch is None:
                continue
            ev_key = f"cal:{ev_id}:{start_epoch}"
            already = self.db.execute("SELECT 1 FROM proactive_notified WHERE key=?", (ev_key,)).fetchone()
            if already:
                continue
            start_dt = datetime.fromtimestamp(start_epoch).astimezone()
            mins_left = max(1, int(round((start_dt - now_local).total_seconds() / 60)))
            al = self._emit_alert(
                kind="calendar_upcoming",
                level="info",
                title=f"日程即将开始（约 {mins_left} 分钟后）",
                body=f"{start_dt.strftime('%H:%M')} · {ev.get('title') or '未命名日程'}",
                dedup_key=ev_key,
                cooldown_sec=86400,
                bypass_budget=True,
                is_away=is_away,
                on_hw_frame=on_hw_frame,
            )
            if al:
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO proactive_notified(key, notified_at) VALUES (?, ?)", (ev_key, _now_iso()))
                emitted.append(al)

        # 3. Causal Cross-Signal: Precipitation Weather + Today Calendar Event
        if isinstance(weather_sample, dict) and weather_sample:
            w_desc = str(weather_sample.get("weather") or weather_sample.get("condition") or weather_sample.get("summary") or "")
            if any(k in w_desc for k in ("雨", "雪", "雷")):
                today_end = int(datetime.combine(now_local.date() + timedelta(days=1), dt_time.min).astimezone().timestamp())
                commute_end = max(today_end, win_start + 6 * 3600)
                try:
                    rem_today = list_events(self.db, {"start": win_start, "end": commute_end, "detail": "今日"})
                except Exception:
                    rem_today = []
                if rem_today:
                    first = rem_today[0]
                    first_ev = first.get("title") or "今日日程"
                    first_start = first.get("start") or first.get("start_at") or win_start
                    ev_date = datetime.fromtimestamp(first_start, tz=timezone.utc).astimezone().date().isoformat()
                    ev_id = first.get("operation_id") or first.get("id") or ev_date
                    w_key = f"weather_commute:{ev_date}:{ev_id}"
                    if not self.db.execute("SELECT 1 FROM proactive_notified WHERE key=?", (w_key,)).fetchone():
                        al = self._emit_alert(
                            kind="weather_commute",
                            level="info",
                            title="恶劣天气出行提示",
                            body=f"当前天气「{w_desc[:24]}」，今日尚有「{first_ev[:24]}」日程，出门请带伞并留足路途时间。",
                            dedup_key=w_key,
                            cooldown_sec=43200,
                            bypass_budget=True,
                            is_away=is_away,
                            on_hw_frame=on_hw_frame,
                        )
                        if al:
                            with self.db:
                                self.db.execute("INSERT OR IGNORE INTO proactive_notified(key, notified_at) VALUES (?, ?)", (w_key, _now_iso()))
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
        weather_sample: dict | None = None,
        owner_code: str = "MOSS",
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
        if normalize_owner_code(owner_code) == "MOSS":
            try:
                today_events = list_events(self.db, {"start": start_ts, "end": end_ts, "detail": "今日"})
            except Exception:
                today_events = []
        else:
            today_events = []

        parts = [f"现在是 {now_local.year:04d}年{now_local.month:02d}月{now_local.day:02d}日 {now_local:%H:%M}。"]
        if today_events:
            titles = "、".join(e.get("title", "日程") for e in today_events[:3])
            parts.append(f"今日安排 {len(today_events)} 项日程：{titles}。")
        else:
            parts.append("今日暂无排期日程。")

        if isinstance(weather_sample, dict):
            w_desc = weather_sample.get("weather") or weather_sample.get("condition") or ""
            w_temp = weather_sample.get("temperature")
            if w_desc:
                t_str = f" {w_temp}°C" if w_temp is not None else ""
                parts.append(f"当前天气：{w_desc}{t_str}。")

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

    def status_snapshot(self, *, owner_code: str = "MOSS") -> dict:
        norm_owner = normalize_owner_code(owner_code)
        rows = self.db.execute(
            """SELECT id, kind, level, title, body, dismissed, COALESCE(owner_code, 'MOSS'), created_at
               FROM proactive_alerts WHERE COALESCE(owner_code, 'MOSS')=? ORDER BY id DESC LIMIT 12""",
            (norm_owner,),
        ).fetchall()
        alerts = [
            {
                "id": r[0], "kind": r[1], "level": r[2], "title": r[3],
                "body": r[4], "dismissed": bool(r[5]), "owner_code": r[6], "created_at": r[7],
            }
            for r in rows
        ]
        active_alerts = [a for a in alerts if not a["dismissed"]]
        bandit_rows = self.db.execute(
            "SELECT kind, context_key, alpha, beta, accepts, dismisses FROM pulse_bandit_stats ORDER BY (accepts + dismisses) DESC LIMIT 12"
        ).fetchall()
        bandit_stats = [
            {
                "kind": r[0], "context_key": r[1],
                "mean_accept": round(float(r[2]) / max(0.1, float(r[2]) + float(r[3])), 3),
                "accepts": int(r[4]), "dismisses": int(r[5]),
            }
            for r in bandit_rows
        ]
        return {
            "config": dict(self.config),
            "active_alert_count": len(active_alerts),
            "alerts": alerts,
            "bandit_stats": bandit_stats,
            "last_eval_at": self._last_eval_at,
        }
