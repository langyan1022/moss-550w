import asyncio
from urllib.parse import urlsplit

import httpx2

from holojarvis_runtime.brain import Brain
from model_catalog import CompletionTransport


STREAM_TAIL_SECONDS = .075
TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
CLOUD_RETRY_BACKOFF_SECONDS = 0.6


def local_client(base_url):
    target = urlsplit(base_url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("本机模型需要回环 HTTP 地址")
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=base_url, api_key="local-only", max_retries=0,
                       http_client=httpx2.AsyncClient(transport=CompletionTransport(), trust_env=False,
                                                     follow_redirects=False, timeout=120))


async def run_holo(*, client, provider_id, model, system, history, text, tools, dispatch, on_text=None,
                   required_tool=None, terminal_tools=(), finish_after_tool=None, stream_prose=False,
                   parallel_tools=(), on_tool_trace=None, failover_clients=(), on_failover=None):
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    active_provider = [provider_id, model, client]

    def _build_options(pid, schemas, required):
        options = {}
        if schemas:
            options["tools"] = schemas
            options["tool_choice"] = ({"type": "function", "function": {"name": required}}
                                      if required else "auto")
        if pid in {"deepseek", "kimi"}:
            options["extra_body"] = {"thinking": {"type": "disabled"}}
        elif pid == "aliyun":
            options["extra_body"] = {"enable_thinking": False}
        elif pid == "local":
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            options["temperature"] = 0
        return options

    async def stream_once(messages, schemas, required):
        candidates = [tuple(active_provider)]
        for alt in (failover_clients or ()):
            if alt and alt[0] != active_provider[0]:
                candidates.append(alt)

        last_err = None
        for idx, (pid, mdl, cli) in enumerate(candidates):
            options = _build_options(pid, schemas, required)
            max_attempts = 2 if (pid != "local" and idx == 0) else 1
            for attempt in range(max_attempts):
                yielded = 0
                try:
                    if idx > 0 and on_failover:
                        try:
                            on_failover(active_provider[0], pid, mdl, str(last_err or "cloud_unavailable"))
                        except Exception:
                            pass
                    stream = await cli.chat.completions.create(model=mdl, messages=messages, stream=True,
                                                               max_tokens=1024, timeout=120, **options)
                    async with stream:
                        tail_deadline = asyncio.timeout(None)
                        try:
                            async with tail_deadline:
                                async for chunk in stream:
                                    if not chunk.choices or tail_deadline.when() is not None:
                                        continue
                                    choice = chunk.choices[0]
                                    if yielded == 0 and idx > 0:
                                        active_provider[:] = [pid, mdl, cli]
                                    yielded += 1
                                    yield choice
                                    if choice.finish_reason:
                                        tail_deadline.reschedule(asyncio.get_running_loop().time() + STREAM_TAIL_SECONDS)
                        except TimeoutError:
                            if not tail_deadline.expired():
                                raise
                    return
                except APITimeoutError as exc:
                    last_err = RuntimeError("等待模型服务超时")
                    last_err.__cause__ = exc
                    if yielded > 0:
                        raise last_err from exc
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(CLOUD_RETRY_BACKOFF_SECONDS)
                        continue
                    break
                except APIConnectionError as exc:
                    last_err = RuntimeError("无法连接到所选模型服务")
                    last_err.__cause__ = exc
                    if yielded > 0:
                        raise last_err from exc
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(CLOUD_RETRY_BACKOFF_SECONDS)
                        continue
                    break
                except APIStatusError as exc:
                    last_err = exc
                    if yielded > 0 or getattr(exc, "status_code", None) not in TRANSIENT_HTTP_STATUS:
                        raise
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(CLOUD_RETRY_BACKOFF_SECONDS)
                        continue
                    break
            if pid == "local":
                break
        if last_err is not None:
            raise last_err

    brain = Brain(system, history, tools, stream_once, dispatch,
                  parallel_tools=parallel_tools, on_tool_trace=on_tool_trace)
    return await brain.ask_stream(text, on_text=on_text, required_tool=required_tool, terminal_tools=terminal_tools,
                                  finish_after_tool=finish_after_tool, stream_prose=stream_prose)
