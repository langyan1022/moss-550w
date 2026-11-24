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
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx2
from pydantic import SecretStr
import win32crypt
import pywintypes


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
    config = Path(os.environ["LOCALAPPDATA"]) / "MOSS-550W/chat-providers.json"
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
                entropy = f"MOSS-550W\0{key}\0{url}".encode("utf-8")
                try:
                    secret = win32crypt.CryptUnprotectData(base64.b64decode(encrypted, validate=True), entropy, None, None, 1)[1].decode("ascii")
                except (ValueError, TypeError, pywintypes.error):
                    reason = "此 Windows 用户无法读取已保存的该提供方凭据"
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
