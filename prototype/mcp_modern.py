"""MOSS Modern MCP Primitives: Streamable HTTP helpers, Interactive Elicitation Broker & Task/Progress Registry."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import os
import re
from typing import Any
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Expand `$ENV:VAR` placeholders in MCP HTTP headers at call time without persisting raw secrets."""
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        m = re.fullmatch(r"\$ENV:([A-Za-z_][A-Za-z0-9_]{0,63})", v.strip())
        if m:
            env_val = os.environ.get(m.group(1), "")
            if env_val:
                out[k] = env_val
        elif v.startswith("Bearer $ENV:"):
            var = v.removeprefix("Bearer $ENV:").strip()
            env_val = os.environ.get(var, "")
            if env_val:
                out[k] = f"Bearer {env_val}"
        else:
            out[k] = v
    return out


def mask_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return masked header preview safe for UI/diagnostic exposure."""
    masked: dict[str, str] = {}
    for k, v in (headers or {}).items():
        kl = str(k).lower()
        if kl in {"authorization", "x-api-key", "cookie", "x-auth-token"} or "token" in kl or "secret" in kl:
            masked[k] = f"***({len(str(v))} chars)" if v else ""
        else:
            masked[k] = str(v)
    return masked


def _schema_defaults(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    required = set(schema.get("required") or ())
    values: dict[str, Any] = {}
    for key, spec in props.items():
        if isinstance(spec, dict) and "default" in spec:
            values[key] = spec["default"]
        elif key in required:
            return None
    return values


class McpElicitationBroker:
    """Interactive bridge turning MCP `ElicitRequest` prompts into live FUI / HTTP confirmation cards."""

    def __init__(self, *, on_change=None):
        self._pending: dict[str, dict[str, Any]] = {}
        self._on_change = on_change

    def _notify(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def list_pending(self) -> list[dict[str, Any]]:
        out = []
        for eid, entry in self._pending.items():
            out.append({
                "id": eid,
                "server": entry["server"],
                "mode": entry["mode"],
                "message": entry["message"],
                "schema": entry["schema"],
                "url": entry["url"],
                "elicitation_id": entry["elicitation_id"],
                "created_at": entry["created_at"],
            })
        return out

    async def handle(self, server_name: str, params: Any, *, mode: str = "prompt", timeout: float = 45.0):
        from mcp.types import ElicitResult

        req_mode = getattr(params, "mode", None) or ("url" if getattr(params, "url", None) else "form")
        message = str(getattr(params, "message", "") or "MCP 服务请求补充信息。")
        schema = getattr(params, "requestedSchema", None)
        url = getattr(params, "url", None)
        ext_id = getattr(params, "elicitationId", None)

        if mode == "deny":
            return ElicitResult(action="decline")
        if mode == "auto" and req_mode == "form":
            defaults = _schema_defaults(schema)
            if defaults is not None:
                return ElicitResult(action="accept", content=defaults)
            return ElicitResult(action="decline")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        eid = f"elicit-{uuid4().hex[:12]}"
        self._pending[eid] = {
            "id": eid,
            "server": server_name,
            "mode": req_mode,
            "message": message,
            "schema": schema if isinstance(schema, dict) else None,
            "url": str(url) if url else "",
            "elicitation_id": str(ext_id) if ext_id else "",
            "created_at": _now_iso(),
            "_future": fut,
        }
        self._notify()
        try:
            async with asyncio.timeout(max(2.0, min(float(timeout), 180.0))):
                decision = await fut
        except (TimeoutError, asyncio.CancelledError):
            return ElicitResult(action="cancel")
        finally:
            self._pending.pop(eid, None)
            self._notify()

        action = str((decision or {}).get("action") or "decline").lower()
        if action not in {"accept", "decline", "cancel"}:
            action = "decline"
        content = (decision or {}).get("content")
        if action == "accept":
            return ElicitResult(action="accept", content=content if isinstance(content, dict) else {})
        return ElicitResult(action=action)

    def resolve(self, elicit_id: str, action: str, content: dict[str, Any] | None = None) -> bool:
        entry = self._pending.get(elicit_id)
        if not entry:
            return False
        fut = entry.get("_future")
        if fut is not None and not fut.done():
            fut.set_result({"action": action, "content": content})
            return True
        return False

    def cancel_all(self):
        for entry in list(self._pending.values()):
            fut = entry.get("_future")
            if fut is not None and not fut.done():
                fut.cancel()
        self._pending.clear()


class McpProgressRegistry:
    """Live MCP task/progress state tracker for long-running Streamable HTTP & stdio calls."""

    def __init__(self, *, on_change=None):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._on_change = on_change

    def start(self, server: str, tool: str) -> str:
        tid = f"mcptask-{uuid4().hex[:10]}"
        self._tasks[tid] = {
            "id": tid,
            "server": server,
            "tool": tool,
            "status": "running",
            "progress": 0.0,
            "total": None,
            "message": "",
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        if len(self._tasks) > 32:
            oldest = next((k for k, v in self._tasks.items() if v["status"] != "running"), None)
            if oldest:
                self._tasks.pop(oldest, None)
        return tid

    def update(self, tid: str, progress: float, total: float | None, message: str | None):
        item = self._tasks.get(tid)
        if not item:
            return
        item["progress"] = float(progress)
        item["total"] = float(total) if total is not None else None
        if message is not None:
            item["message"] = str(message)[:240]
        item["updated_at"] = _now_iso()

    def finish(self, tid: str, *, status: str = "completed", message: str = ""):
        item = self._tasks.get(tid)
        if not item:
            return
        item["status"] = status
        if message:
            item["message"] = str(message)[:240]
        item["updated_at"] = _now_iso()

    def snapshot(self) -> list[dict[str, Any]]:
        return list(reversed(list(self._tasks.values())))[:16]


ACTIVE_MCP_BROKER: ContextVar[McpElicitationBroker | None] = ContextVar("active_mcp_broker", default=None)
ACTIVE_MCP_PROGRESS: ContextVar[McpProgressRegistry | None] = ContextVar("active_mcp_progress", default=None)


def make_elicitation_callback(server_name: str, *, mode: str = "prompt", timeout: float = 45.0):
    """Build an `ElicitationFnT` callback wired to the active `McpElicitationBroker` (or policy fallback)."""
    broker = ACTIVE_MCP_BROKER.get()

    async def _callback(_context, params):
        if broker is not None and mode != "deny":
            return await broker.handle(server_name, params, mode=mode, timeout=timeout)
        from mcp.types import ElicitResult

        req_mode = getattr(params, "mode", None) or "form"
        if mode == "auto" and req_mode == "form":
            defaults = _schema_defaults(getattr(params, "requestedSchema", None))
            if defaults is not None:
                return ElicitResult(action="accept", content=defaults)
        return ElicitResult(action="decline")

    return _callback
