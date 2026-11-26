import argparse
import asyncio
import base64
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx2
from pydantic import SecretStr

from secret_store import SecretStoreError, app_config_dir, unprotect_secret


SPECS = {
    "deepseek": ("DeepSeek", ("DEEPSEEK",), "https://api.deepseek.com", "deepseek-flash"),
    "aliyun": ("阿里云百炼", ("BAILIAN", "DASHSCOPE", "ALIYUN"), "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen4-max"),
    "kimi": ("Kimi", ("MOONSHOT", "KIMI"), "https://api.moonshot.cn/v1", "kimi-k2.6"),
}
HOSTS = {
    "deepseek": {"api.deepseek.com"}, "kimi": {"api.moonshot.cn", "api.moonshot.ai", "api.kimi.com"},
    "aliyun": {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"},
}
before_model_request = ContextVar("before_model_request", default=None)


async def request_json(client, model, provider_id, system, payload, tokens, schema=None, *, timeout=50):
    extra = {}
    if provider_id == "local":
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
    elif provider_id == "aliyun":
        extra = {"enable_thinking": False}
    elif provider_id in {"deepseek", "kimi"}:
        extra = {"thinking": {"type": "disabled"}}
    response_format = {"type": "json_object"}
    if provider_id in {"local", "aliyun"} and schema:
        response_format = {"type": "json_schema", "json_schema": {"name": "reply_result", "strict": True, "schema": schema}}
    answer = await client.chat.completions.create(
        model=model, max_tokens=tokens, timeout=timeout,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        response_format=response_format, extra_body=extra,
    )
    content = answer.choices[0].message.content if answer.choices else ""
    try:
        output = json.loads(content or "")
    except ValueError as exc:
        raise ValueError("回复模型未返回可读取的候选结果。") from exc
    if not isinstance(output, dict):
        raise ValueError("回复模型返回的候选格式有误。")
    return output


def short_retry_delay(header):
    if not header:
        return None
    if header.isascii() and header.isdigit():
        if len(header) > 8:
            return None
        delay = int(header)
    else:
        try:
            until = parsedate_to_datetime(header)
            if until.tzinfo is None:
                return None
            delay = max(0, (until - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return delay if delay <= 2 else None


class CompletionTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, provider_id=None):
        super().__init__(trust_env=False)
        self.provider_id = provider_id

    async def handle_async_request(self, request):
        check = before_model_request.get()
        if check is not None and request.method == "POST" and request.url.path.endswith("/chat/completions"):
            check(self.provider_id)
        return await super().handle_async_request(request)


class CloudHTTPClient(httpx2.AsyncClient):
    async def send(self, request, **options):
        response = await super().send(request, **options)
        if (response.status_code not in (429, 503) or request.method != "POST"
                or not request.url.path.endswith("/chat/completions")
                or response.headers.get("x-should-retry", "").lower() == "false"):
            return response
        delay = short_retry_delay(response.headers.get("retry-after"))
        if delay is None:
            return response
        try:
            request.content
        except httpx2.RequestNotRead:
            return response
        # 只重发这一份已缓冲的模型请求。
        await response.aclose()
        await asyncio.sleep(delay)
        return await super().send(request, **options)


@dataclass
class Profile:
    id: str
    label: str
    url: str
    model: str
    secret: SecretStr = field(repr=False)
    reason: str = ""

    def public(self):
        return {"id": self.id, "label": self.label, "model": self.model,
                "available": bool(self.secret.get_secret_value()), "reason": self.reason}

    def client(self):
        if not self.secret.get_secret_value():
            raise ValueError(f"{self.label} 未配置可用凭据")
        from openai import AsyncOpenAI

        return AsyncOpenAI(base_url=self.url, api_key=self.secret.get_secret_value(), max_retries=0,
                           http_client=CloudHTTPClient(transport=CompletionTransport(self.id), trust_env=False,
                                                       follow_redirects=False, timeout=90))


class ProviderHealthTracker:
    """Adaptive provider health, EWMA latency, and bounded cooldown circuit-breaker."""

    def __init__(self, *, clock=monotonic, base_cooldown_s: float = 25.0, max_cooldown_s: float = 120.0):
        self._clock = clock
        self._base_cooldown_s = float(base_cooldown_s)
        self._max_cooldown_s = float(max_cooldown_s)
        self._stats: dict[str, dict] = {}

    def _entry(self, provider_id: str) -> dict:
        key = str(provider_id or "local")
        if key not in self._stats:
            self._stats[key] = {
                "ok_count": 0,
                "err_count": 0,
                "consecutive_failures": 0,
                "ewma_latency_ms": None,
                "cooldown_until": 0.0,
                "last_error": "",
                "failover_hits": 0,
            }
        return self._stats[key]

    def record_success(self, provider_id: str, latency_ms: float | None = None):
        row = self._entry(provider_id)
        row["ok_count"] += 1
        row["consecutive_failures"] = 0
        row["cooldown_until"] = 0.0
        row["last_error"] = ""
        if latency_ms is not None and latency_ms >= 0:
            prev = row["ewma_latency_ms"]
            row["ewma_latency_ms"] = round(float(latency_ms) if prev is None else 0.35 * float(latency_ms) + 0.65 * prev, 1)

    def record_failure(self, provider_id: str, error_text: str = "", *, status_code: int | None = None):
        row = self._entry(provider_id)
        row["err_count"] += 1
        row["consecutive_failures"] += 1
        row["last_error"] = str(error_text or "")[:160]
        streak = row["consecutive_failures"]
        factor = 1.8 if status_code == 429 else (1 if streak == 1 else min(4, 2 ** (streak - 1)))
        cooldown = min(self._max_cooldown_s, self._base_cooldown_s * factor)
        row["cooldown_until"] = self._clock() + cooldown

    def record_failover(self, provider_id: str):
        self._entry(provider_id)["failover_hits"] += 1

    def reset(self, provider_id: str | None = None):
        if provider_id is None:
            self._stats.clear()
        else:
            self._stats.pop(str(provider_id or "local"), None)

    def is_cooling(self, provider_id: str) -> bool:
        row = self._stats.get(str(provider_id or "local"))
        return bool(row and self._clock() < row["cooldown_until"])

    def rank_candidates(self, primary_tuple, backups: list | tuple) -> tuple[list, bool]:
        """Return latency-and-health-ordered candidates plus whether primary was bypassed via cooldown."""
        primary_id = primary_tuple[0]
        unique = []
        seen = {primary_id}
        for item in (backups or ()):
            if item and item[0] not in seen:
                seen.add(item[0])
                unique.append(item)
        if not unique:
            return [primary_tuple], False

        now_t = self._clock()

        def score(item):
            row = self._stats.get(item[0]) or {}
            cooling = now_t < float(row.get("cooldown_until") or 0.0)
            streak = int(row.get("consecutive_failures") or 0)
            ewma = row.get("ewma_latency_ms")
            return (1 if cooling else 0, streak, float(ewma) if ewma is not None else 1800.0)

        healthy_backups = [it for it in unique if not self.is_cooling(it[0])]
        ordered_backups = sorted(unique, key=score)
        if self.is_cooling(primary_id) and healthy_backups:
            return [*ordered_backups, primary_tuple], True
        return [primary_tuple, *ordered_backups], False

    def snapshot(self) -> dict:
        now_t = self._clock()
        out = {}
        for pid, row in sorted(self._stats.items()):
            rem = max(0.0, float(row["cooldown_until"]) - now_t)
            status = "cooldown" if rem > 0 else ("degraded" if row["consecutive_failures"] > 0 else "healthy")
            out[pid] = {
                "status": status,
                "ok_count": row["ok_count"],
                "err_count": row["err_count"],
                "consecutive_failures": row["consecutive_failures"],
                "cooldown_remaining_s": round(rem, 1),
                "ewma_latency_ms": row["ewma_latency_ms"],
                "failover_hits": row["failover_hits"],
                "last_error": row["last_error"],
            }
        return out


def allowed(provider_id, url):
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return False
    official = host in HOSTS[provider_id] or (provider_id == "aliyun" and host.endswith((".dashscope.aliyuncs.com", ".maas.aliyuncs.com")))
    return (official and parts.scheme == "https" and port in (443, None) and not
            (parts.username or parts.password or parts.query or parts.fragment or "\\" in url))


def profiles():
    legacy = Path(os.environ.get("MOSS_PROVIDER_DIR", r"F:\moss\tts"))
    config = app_config_dir() / "chat-providers.json"
    try:
        document = json.loads(config.read_text(encoding="utf-8-sig"))
        saved = document.get("profiles") if isinstance(document, dict) else None
        saved = saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        saved = {}
    sources = [os.environ]
    for path in (Path(__file__).parent / ".env", legacy / "voice-trials/.env", legacy / ".env"):
        if path.is_file():
            sources.append(dotenv_values(path, encoding="utf-8", interpolate=False))
    results = {}
    for key, (label, prefixes, default_url, default_model) in SPECS.items():
        layers = []
        for source in sources:
            def value(endings):
                return next((v.strip() for prefix in prefixes for end in endings
                             if (v := source.get(prefix + "_" + end)) and v.strip()), "")
            layers.append((value(("BASE_URL", "API_BASE", "API_BASE_URL", "URL")).rstrip("/"),
                           value(("API_KEY", "KEY")), value(("MODEL_ID", "MODEL", "MODEL_NAME"))))
        entry = saved.get(key) if isinstance(saved.get(key), dict) else {}
        url = str(entry.get("base_url") or next((u for u, _, _ in layers if u), default_url)).rstrip("/")
        model = str(entry.get("model") or next((m for _, _, m in layers if m), default_model))
        secret = ""
        reason = ""
        if allowed(key, url):
            encrypted = entry.get("key")
            if encrypted:
                try:
                    secret = unprotect_secret(
                        encrypted,
                        entropy=f"MOSS-550W\0{key}\0{url}",
                        flags=1,
                        raise_on_error=True,
                    )
                except (ValueError, TypeError, SecretStoreError):
                    reason = "此用户环境无法读取已保存的该提供方凭据"
            else:
                secret = next((s for u, s, _ in layers if s and (u or default_url) == url), "")
        else:
            reason = "配置的服务地址未通过该提供方的 HTTPS 地址检查"
        results[key] = Profile(key, label, url, model, SecretStr(secret), reason or ("" if secret else "缺少可用凭据"))
    return results


async def probe(profile, mode):
    row = profile.public()
    try:
        async with profile.client() as client:
            if mode == "list":
                row["models"] = [r.id for r in (await client.models.list()).data]
            else:
                additional = {"thinking": {"type": "disabled"}} if profile.id in {"deepseek", "kimi"} else {}
                r = await client.chat.completions.create(model=profile.model, max_tokens=32,
                    messages=[{"role": "user", "content": "这是一条连接测试。请只回复 OK。"}], extra_body=additional)
                row.update(response_model=r.model, reply=r.choices[0].message.content,
                           status="ok" if r.choices[0].message.content else "empty")
    except Exception as error:
        row.update(status="error", error=type(error).__name__, http=getattr(error, "status_code", None))
    return row


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["list", "test"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    async def test():
        return await asyncio.gather(*(probe(profile, args.mode) for profile in profiles().values()))
    rows = asyncio.run(test())
    args.out.parent.mkdir(exist_ok=True, parents=True)
    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(row["id"], row["model"], row.get("status", "ok"), row.get("http"), len(row.get("models", [])))
