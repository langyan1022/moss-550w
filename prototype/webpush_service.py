"""MOSS Self-Hosted VAPID Web Push Engine (0 MiB GPU VRAM, DPAPI-protected keys, SQLite-backed)."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import sqlite3
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from cognitive_utils import normalize_owner_code
from secret_store import protect_secret, unprotect_secret


_VAPID_ENTROPY = "MOSS-550W\0webpush\0vapid_priv"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WebPushService:
    """Manages VAPID keypair persistence and encrypted background push delivery for MOSS alerts."""

    def __init__(self, db: sqlite3.Connection, *, on_log=None):
        self.db = db
        self.on_log = on_log
        self._init_schema()
        self._vapid, self._public_key = self._load_or_create_vapid()

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS webpush_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    public_key TEXT NOT NULL,
                    private_pem_enc TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webpush_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_code TEXT NOT NULL DEFAULT 'MOSS',
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wp_owner ON webpush_subscriptions(owner_code);
            """)

    def _load_or_create_vapid(self) -> tuple[Vapid, str]:
        row = self.db.execute("SELECT public_key, private_pem_enc FROM webpush_config WHERE id=1").fetchone()
        if row:
            try:
                pem_str = unprotect_secret(row[1], entropy=_VAPID_ENTROPY)
                if pem_str:
                    v = Vapid.from_pem(pem_str.encode("utf-8"))
                    return v, str(row[0])
            except Exception:
                pass
        v = Vapid()
        v.generate_keys()
        pub_raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        pub_b64 = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode("ascii")
        priv_pem = v.private_pem().decode("utf-8")
        enc_priv = protect_secret(priv_pem, entropy=_VAPID_ENTROPY, description="MOSS-VAPID")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO webpush_config(id, public_key, private_pem_enc, updated_at) VALUES (1, ?, ?, ?)",
                (pub_b64, enc_priv, _now_iso()),
            )
        return v, pub_b64

    @property
    def public_key(self) -> str:
        return self._public_key

    def subscribe(self, subscription: dict, *, owner_code: str = "MOSS", user_agent: str = "") -> dict:
        if not isinstance(subscription, dict):
            raise ValueError("无效的 Push 订阅对象")
        endpoint = str(subscription.get("endpoint") or "").strip()
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("Push 终端地址必须是合法的 https:// URL")
        keys = subscription.get("keys")
        if not isinstance(keys, dict):
            raise ValueError("缺少 Web Push 加密公钥参数")
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        if len(p256dh) < 16 or len(auth) < 8:
            raise ValueError("Web Push keys (p256dh / auth) 无效")
        owner = normalize_owner_code(owner_code)
        now = _now_iso()
        with self.db:
            self.db.execute(
                """INSERT INTO webpush_subscriptions(owner_code, endpoint, p256dh, auth, user_agent, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(endpoint) DO UPDATE SET
                     owner_code=excluded.owner_code, p256dh=excluded.p256dh, auth=excluded.auth,
                     user_agent=excluded.user_agent, updated_at=excluded.updated_at""",
                (owner, endpoint[:1024], p256dh[:256], auth[:128], str(user_agent or "")[:200], now, now),
            )
        return {"subscribed": True, "owner_code": owner, "count": self.subscription_count(owner_code=owner)}

    def unsubscribe(self, endpoint: str, *, owner_code: str | None = None) -> bool:
        ep = str(endpoint or "").strip()
        if not ep:
            return False
        with self.db:
            if owner_code is not None:
                cur = self.db.execute(
                    "DELETE FROM webpush_subscriptions WHERE endpoint=? AND owner_code=?",
                    (ep, normalize_owner_code(owner_code)),
                )
            else:
                cur = self.db.execute("DELETE FROM webpush_subscriptions WHERE endpoint=?", (ep,))
        return cur.rowcount > 0

    def subscription_count(self, *, owner_code: str | None = None) -> int:
        if owner_code is not None:
            return int(
                self.db.execute(
                    "SELECT COUNT(*) FROM webpush_subscriptions WHERE owner_code=?",
                    (normalize_owner_code(owner_code),),
                ).fetchone()[0]
            )
        return int(self.db.execute("SELECT COUNT(*) FROM webpush_subscriptions").fetchone()[0])

    def list_subscriptions(self, *, owner_code: str = "MOSS") -> list[dict]:
        rows = self.db.execute(
            "SELECT endpoint, p256dh, auth FROM webpush_subscriptions WHERE owner_code=? ORDER BY id DESC LIMIT 16",
            (normalize_owner_code(owner_code),),
        ).fetchall()
        return [{"endpoint": r[0], "keys": {"p256dh": r[1], "auth": r[2]}} for r in rows]

    def send_sync(self, payload: dict, *, owner_code: str = "MOSS", _sender=None) -> dict:
        """Synchronously dispatch Web Push notifications (call via asyncio.to_thread)."""
        subs = self.list_subscriptions(owner_code=owner_code)
        if not subs:
            return {"sent": 0, "pruned": 0, "failed": 0}
        sender = _sender or webpush
        body_str = json.dumps(payload, ensure_ascii=False)
        sent = pruned = failed = 0
        gone_endpoints = []
        for sub in subs:
            try:
                sender(
                    subscription_info=sub,
                    data=body_str,
                    vapid_private_key=self._vapid,
                    vapid_claims={"sub": "mailto:moss-550w@localhost.invalid"},
                    ttl=1800,
                    timeout=6,
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    gone_endpoints.append(sub["endpoint"])
                    pruned += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        if gone_endpoints:
            with self.db:
                for ep in gone_endpoints:
                    self.db.execute("DELETE FROM webpush_subscriptions WHERE endpoint=?", (ep,))
        return {"sent": sent, "pruned": pruned, "failed": failed}

    async def send_async(self, payload: dict, *, owner_code: str = "MOSS") -> dict:
        return await asyncio.to_thread(self.send_sync, payload, owner_code=owner_code)
