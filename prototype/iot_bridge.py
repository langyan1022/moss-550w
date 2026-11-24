"""MOSS Home Assistant & Physical 550W Hardware Embodiment Bridge.

Provides two clean, production-ready, zero-extra-dependency integration layers:
  1. HomeAssistantBridge:
     - Connects to a real Home Assistant instance via REST API (`/api/states`, `/api/services/...`)
       using a user-configured local URL + Long-Lived Access Token stored in SQLite.
     - Enforces a safe allowed-domain whitelist (blocking lock/alarm unless explicitly unlocked).
     - Includes a zero-config `dry_run` preview mode so voice/chat smart-home flows can be tested
       even before Home Assistant credentials are entered.
  2. PhysicalMossBridge (Physical 550W Hardware Protocol v1):
     - Unified hardware state frame for future physical MOSS builds (ESP32-S3 / Raspberry Pi /
       Arduino + 2-axis pan-tilt servo + red lens LED ring + H50 IPS/OLED small screen + mmWave radar).
     - Outbound real-time SSE stream (`GET /api/hardware/moss/stream`) + optional HTTP push webhook.
     - Inbound sensor/button telemetry (`POST /api/hardware/moss/telemetry`) feeding Proactive Pulse & logs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sqlite3
from time import time
from urllib.parse import urlsplit

import aiohttp


SAFE_HA_DOMAINS = (
    "light", "switch", "climate", "scene", "script",
    "fan", "cover", "media_player", "sensor", "binary_sensor", "input_boolean",
)

DEFAULT_IOT_CONFIG = {
    "ha_enabled": False,
    "ha_mode": "dry_run",          # "dry_run" | "live"
    "ha_url": "",                  # e.g. "http://homeassistant.local:8123"
    "ha_token": "",                # Long-Lived Access Token (never leaked in full to UI)
    "ha_allowed_domains": list(SAFE_HA_DOMAINS),
    "hw_enabled": True,
    "hw_device_name": "MOSS-550W-PHYSICAL-01",
    "hw_webhook_url": "",          # Optional ESP32/Pi HTTP endpoint e.g. "http://192.168.1.50/moss/frame"
    "hw_protocol_version": "moss-hw-v1",
}

DEMO_HA_ENTITIES = (
    {"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "客厅主灯", "brightness": 180}},
    {"entity_id": "light.study_desk", "state": "on", "attributes": {"friendly_name": "书房台灯", "brightness": 220}},
    {"entity_id": "climate.study_ac", "state": "cool", "attributes": {"friendly_name": "书房空调", "temperature": 25, "current_temperature": 24.5}},
    {"entity_id": "switch.desk_fan", "state": "off", "attributes": {"friendly_name": "桌面风扇"}},
    {"entity_id": "sensor.study_temperature", "state": "24.5", "attributes": {"friendly_name": "书房温度", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.study_humidity", "state": "48", "attributes": {"friendly_name": "书房湿度", "unit_of_measurement": "%"}},
    {"entity_id": "scene.night_focus", "state": "scening", "attributes": {"friendly_name": "深夜专注模式"}},
)

LED_PRESETS = {
    "standby":   {"rgb": [61, 217, 235], "brightness": 90,  "pattern": "breathe", "pulse_hz": 0.4},
    "listening": {"rgb": [255, 52, 36],  "brightness": 220, "pattern": "pulse",   "pulse_hz": 1.8},
    "thinking":  {"rgb": [255, 168, 38], "brightness": 185, "pattern": "sweep",   "pulse_hz": 2.4},
    "speaking":  {"rgb": [255, 42, 26],  "brightness": 245, "pattern": "audio",   "pulse_hz": 0.0},
    "alert":     {"rgb": [255, 32, 32],  "brightness": 255, "pattern": "blink",   "pulse_hz": 3.5},
    "dnd":       {"rgb": [255, 42, 26],  "brightness": 24,  "pattern": "solid",   "pulse_hz": 0.0},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_local_or_lan_url(url: str) -> str:
    clean = str(url or "").strip().rstrip("/")
    if not clean:
        return ""
    parts = urlsplit(clean)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("地址必须是合法的 http:// 或 https:// URL。")
    return clean


class IoTAndHardwareBridge:
    """Unified Home Assistant + Physical 550W Embodiment Bridge."""

    def __init__(self, db: sqlite3.Connection, *, on_log=None, on_alert=None):
        self.db = db
        self.on_log = on_log
        self.on_alert = on_alert
        self._init_schema()
        self.config = self._load_config()
        self._demo_states = {e["entity_id"]: json.loads(json.dumps(e)) for e in DEMO_HA_ENTITIES}

        # Physical 550W Hardware State Frame
        self.hw_seq = 0
        self.hw_subscribers: set[asyncio.Event] = set()
        self.hw_state = {
            "protocol": "moss-hw-v1",
            "seq": 0,
            "mode": "standby",
            "led": dict(LED_PRESETS["standby"]),
            "servo": {"yaw_deg": 0.0, "pitch_deg": 0.0, "speed": 0.6, "gesture": "idle"},
            "screen": {"headline": "MOSS 550W", "line1": "SYSTEM ONLINE", "line2": "STANDBY", "level": "ok"},
            "speaker": {"active": False, "rms": 0.0, "volume_hint": 75},
            "camera": {"active": False, "track_face": False},
            "updated_at": _now_iso(),
        }
        self.hw_sensors = {
            "connected": False,
            "person_present": None,
            "distance_cm": None,
            "ambient_lux": None,
            "chassis_temp_c": None,
            "last_button": None,
            "last_seen_at": None,
        }

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS iot_bridge_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    doc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS iot_action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    ok INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
            """)

    def _load_config(self) -> dict:
        row = self.db.execute("SELECT doc FROM iot_bridge_config WHERE id=1").fetchone()
        if row:
            try:
                saved = json.loads(row[0])
                if isinstance(saved, dict):
                    return {**DEFAULT_IOT_CONFIG, **saved}
            except json.JSONDecodeError:
                pass
        return dict(DEFAULT_IOT_CONFIG)

    def _log(self, text: str, source: str = "IOT", level: str = "info"):
        if self.on_log is not None:
            try:
                self.on_log(text, source=source, level=level)
            except Exception:
                pass

    def _record_action(self, target: str, action: str, detail: str, ok: bool = True):
        with self.db:
            self.db.execute(
                "INSERT INTO iot_action_log(target, action, detail, ok, created_at) VALUES (?, ?, ?, ?, ?)",
                (target[:40], action[:80], detail[:240], 1 if ok else 0, _now_iso()),
            )

    def public_config(self) -> dict:
        cfg = dict(self.config)
        tok = cfg.pop("ha_token", "")
        cfg["ha_token_configured"] = bool(tok)
        cfg["ha_token_hint"] = f"***{tok[-4:]}" if len(tok) >= 8 else ("***" if tok else "")
        return cfg

    def update_config(self, patch: dict) -> dict:
        merged = dict(self.config)
        if "ha_enabled" in patch:
            merged["ha_enabled"] = bool(patch["ha_enabled"])
        if "ha_mode" in patch and patch["ha_mode"] in {"dry_run", "live"}:
            merged["ha_mode"] = patch["ha_mode"]
        prev_url = self.config.get("ha_url", "")
        if "ha_url" in patch:
            merged["ha_url"] = _validate_local_or_lan_url(patch["ha_url"])
        url_changed = bool(merged["ha_url"] and prev_url and urlsplit(merged["ha_url"]).netloc != urlsplit(prev_url).netloc)
        if patch.get("clear_ha_token") is True:
            merged["ha_token"] = ""
        elif "ha_token" in patch and isinstance(patch["ha_token"], str) and patch["ha_token"].strip():
            merged["ha_token"] = patch["ha_token"].strip()
        elif url_changed:
            merged["ha_token"] = ""
        if "hw_enabled" in patch:
            merged["hw_enabled"] = bool(patch["hw_enabled"])
        if "hw_device_name" in patch and isinstance(patch["hw_device_name"], str):
            merged["hw_device_name"] = patch["hw_device_name"].strip()[:48] or "MOSS-550W-PHYSICAL-01"
        if "hw_webhook_url" in patch:
            merged["hw_webhook_url"] = _validate_local_or_lan_url(patch["hw_webhook_url"])

        self.config = merged
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO iot_bridge_config(id, doc) VALUES (1, ?)",
                (json.dumps(merged, ensure_ascii=False),),
            )
        self._log(
            f"IoT config updated: HA={merged['ha_mode']}/{'on' if merged['ha_enabled'] else 'off'} HW={'on' if merged['hw_enabled'] else 'off'}",
            source="IOT",
            level="ok",
        )
        return self.public_config()

    # ── 1. Home Assistant Integration ──
    def _ha_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config['ha_token']}",
            "Content-Type": "application/json",
        }

    async def ha_list_entities(self, domain: str = "") -> dict:
        dom = str(domain or "").strip().lower()
        if self.config["ha_mode"] == "live" and self.config["ha_url"] and self.config["ha_token"]:
            url = f"{self.config['ha_url']}/api/states"
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as http:
                    async with http.get(url, headers=self._ha_headers()) as resp:
                        if resp.status != 200:
                            raise ValueError(f"Home Assistant 返回 HTTP {resp.status}")
                        raw = await resp.json()
                entities = [
                    {
                        "entity_id": e.get("entity_id", ""),
                        "state": e.get("state", ""),
                        "friendly_name": (e.get("attributes") or {}).get("friendly_name", e.get("entity_id", "")),
                        "attributes": e.get("attributes") or {},
                    }
                    for e in raw
                    if isinstance(e, dict) and e.get("entity_id", "").split(".")[0] in self.config["ha_allowed_domains"]
                    and (not dom or e.get("entity_id", "").startswith(f"{dom}."))
                ]
                return {"mode": "live", "count": len(entities), "entities": entities[:40]}
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                self._log(f"HA states failed: {exc}", source="HA", level="warn")
                return {"mode": "live_error", "error": str(exc), "count": 0, "entities": []}

        # Dry-run / pre-configuration preview entities
        entities = [
            {
                "entity_id": e["entity_id"],
                "state": e["state"],
                "friendly_name": e["attributes"].get("friendly_name", e["entity_id"]),
                "attributes": e["attributes"],
            }
            for e in self._demo_states.values()
            if not dom or e["entity_id"].startswith(f"{dom}.")
        ]
        return {"mode": "dry_run", "count": len(entities), "entities": entities}

    async def ha_call_service(self, entity_id: str, service: str, data: dict | None = None) -> dict:
        eid = str(entity_id or "").strip().lower()
        svc = str(service or "").strip().lower()
        if "." not in eid or not svc:
            raise ValueError("需要合法的 Home Assistant entity_id（如 light.living_room）与 service（如 turn_on）。")
        domain = eid.split(".", 1)[0]
        if domain not in self.config["ha_allowed_domains"]:
            raise ValueError(f"安全策略禁止控制域 `{domain}`（当前允许：{', '.join(self.config['ha_allowed_domains'])}）。")
        payload = {"entity_id": eid, **(data or {})}

        if self.config["ha_mode"] == "live" and self.config["ha_enabled"] and self.config["ha_url"] and self.config["ha_token"]:
            url = f"{self.config['ha_url']}/api/services/{domain}/{svc}"
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6), trust_env=False) as http:
                    async with http.post(url, headers=self._ha_headers(), json=payload) as resp:
                        if resp.status not in (200, 201):
                            text = await resp.text()
                            raise ValueError(f"HA HTTP {resp.status}: {text[:120]}")
                self._record_action("ha:live", f"{domain}.{svc}", json.dumps(payload, ensure_ascii=False), ok=True)
                self._log(f"LIVE {domain}.{svc} -> {eid}", source="HA", level="ok")
                return {"ok": True, "mode": "live", "entity_id": eid, "service": f"{domain}.{svc}", "payload": payload}
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                self._record_action("ha:live", f"{domain}.{svc}", str(exc), ok=False)
                self._log(f"LIVE error {domain}.{svc}: {exc}", source="HA", level="crit")
                raise ValueError(f"Home Assistant 调用失败：{exc}") from exc

        # Dry-run simulation: mutate virtual demo entity so the full loop is testable before hardware hookup!
        demo = self._demo_states.get(eid)
        if demo:
            if svc in {"turn_on", "open_cover"}:
                demo["state"] = "on"
            elif svc in {"turn_off", "close_cover"}:
                demo["state"] = "off"
            elif svc == "toggle":
                demo["state"] = "off" if demo["state"] == "on" else "on"
            elif svc == "set_temperature" and "temperature" in payload:
                demo["attributes"]["temperature"] = payload["temperature"]
            if "brightness_pct" in payload:
                demo["attributes"]["brightness"] = int(round(float(payload["brightness_pct"]) * 2.55))
        self._record_action("ha:dry_run", f"{domain}.{svc}", json.dumps(payload, ensure_ascii=False), ok=True)
        self._log(f"DRY-RUN {domain}.{svc} -> {eid} ({payload})", source="HA", level="info")
        return {
            "ok": True,
            "mode": "dry_run",
            "entity_id": eid,
            "service": f"{domain}.{svc}",
            "payload": payload,
            "note": "当前为 Home Assistant 预留演练模式（dry_run）；填入真实 HA URL 与长效令牌并切至 live 后即控制实体设备。",
        }

    # ── 2. Physical 550W Hardware Embodiment Protocol (`moss-hw-v1`) ──
    def push_hardware_frame(
        self,
        *,
        mode: str | None = None,
        yaw_deg: float | None = None,
        pitch_deg: float | None = None,
        gesture: str | None = None,
        screen_headline: str | None = None,
        screen_line1: str | None = None,
        screen_line2: str | None = None,
        screen_level: str | None = None,
    ) -> dict:
        """Update physical MOSS 550W actuator frame and notify all SSE / webhook listeners."""
        if mode and mode in LED_PRESETS:
            self.hw_state["mode"] = mode
            self.hw_state["led"] = dict(LED_PRESETS[mode])
        if yaw_deg is not None:
            self.hw_state["servo"]["yaw_deg"] = round(max(-60.0, min(60.0, float(yaw_deg))), 1)
        if pitch_deg is not None:
            self.hw_state["servo"]["pitch_deg"] = round(max(-30.0, min(30.0, float(pitch_deg))), 1)
        if gesture:
            self.hw_state["servo"]["gesture"] = str(gesture)[:24]
        if screen_headline is not None:
            self.hw_state["screen"]["headline"] = str(screen_headline)[:28]
        if screen_line1 is not None:
            self.hw_state["screen"]["line1"] = str(screen_line1)[:48]
        if screen_line2 is not None:
            self.hw_state["screen"]["line2"] = str(screen_line2)[:48]
        if screen_level in {"ok", "info", "warn", "crit"}:
            self.hw_state["screen"]["level"] = screen_level

        self.hw_seq += 1
        self.hw_state["seq"] = self.hw_seq
        self.hw_state["updated_at"] = _now_iso()
        self.hw_state["ts"] = int(time() * 1000)
        for sig in tuple(self.hw_subscribers):
            sig.set()

        self._log(
            f"Frame #{self.hw_seq} mode={self.hw_state['mode']} servo=({self.hw_state['servo']['yaw_deg']}°,{self.hw_state['servo']['pitch_deg']}°) gesture={self.hw_state['servo']['gesture']}",
            source="HW550W",
            level="info",
        )
        if self.config["hw_enabled"] and self.config["hw_webhook_url"]:
            try:
                asyncio.get_running_loop().create_task(self._post_webhook(dict(self.hw_state)))
            except RuntimeError:
                pass
        return dict(self.hw_state)

    async def _post_webhook(self, frame: dict):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5), trust_env=False) as http:
                await http.post(self.config["hw_webhook_url"], json=frame)
        except Exception:
            pass

    def ingest_hardware_telemetry(self, payload: dict) -> dict:
        """Accept sensor/radar/button telemetry from a physical MOSS 550W chassis (ESP32/Pi)."""
        self.hw_sensors["connected"] = True
        self.hw_sensors["last_seen_at"] = _now_iso()
        for key in ("person_present", "distance_cm", "ambient_lux", "chassis_temp_c", "last_button"):
            if key in payload:
                self.hw_sensors[key] = payload[key]
        self._log(
            f"Telemetry in: present={self.hw_sensors['person_present']} dist={self.hw_sensors['distance_cm']}cm btn={self.hw_sensors['last_button']}",
            source="HW550W",
            level="ok",
        )
        if payload.get("last_button") == "wake" and self.on_alert is not None:
            self.on_alert(
                kind="hw_button",
                level="info",
                title="实体 550W 唤醒键触发",
                body="物理机箱唤醒按键已按下，镜头已切至聆听态。",
                dedup_key=f"hwbtn:{int(time() // 5)}",
                cooldown_sec=5,
                bypass_budget=True,
            )
            self.push_hardware_frame(mode="listening", gesture="look_user", screen_line2="LISTENING")
        return dict(self.hw_sensors)

    def status_snapshot(self) -> dict:
        recent_actions = [
            dict(zip(("id", "target", "action", "detail", "ok", "created_at"), r))
            for r in self.db.execute(
                "SELECT id, target, action, detail, ok, created_at FROM iot_action_log ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]
        return {
            "config": self.public_config(),
            "hardware_frame": dict(self.hw_state),
            "hardware_sensors": dict(self.hw_sensors),
            "hardware_stream_subscribers": len(self.hw_subscribers),
            "recent_actions": recent_actions,
        }
