import asyncio
import os
import re
from time import perf_counter
from urllib.parse import urlsplit

import httpx2

from holojarvis_runtime.brain import Brain
from model_catalog import CompletionTransport


STREAM_TAIL_SECONDS = .075
TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
FAILOVER_HTTP_STATUS = TRANSIENT_HTTP_STATUS | frozenset({401, 402, 403, 404})
CLOUD_RETRY_BACKOFF_SECONDS = 0.6


def local_client(base_url):
    target = urlsplit(base_url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("本机模型需要回环 HTTP 地址")
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=base_url, api_key="local-only", max_retries=0,
                       http_client=httpx2.AsyncClient(transport=CompletionTransport(), trust_env=False,
                                                     follow_redirects=False, timeout=120))


_LOCAL_LOW_CONFIDENCE_RE = re.compile(
    r"^(?:抱歉|对不起|很抱歉|不好意思)?[，, ]*(?:作为(?:一个)?(?:AI|人工智能)|我(?:目前)?(?:无法|不能|不太|难以)(?:确定|核实|回答|完成|处理)|(?:这个|该)问题超出|需要联网|暂未获取到)"
)


def _should_cascade_local_answer(answer: str, *, prompt_text: str, ran_tools: bool, had_tools: bool) -> str | None:
    """Return escalation reason when a local 4B answer shows clear uncertainty or inability."""
    clean = (answer or "").strip()
    if not clean:
        return "empty_local_reply"
    if not ran_tools and (len(prompt_text) >= 48 or had_tools) and len(clean) <= 120:
        if _LOCAL_LOW_CONFIDENCE_RE.search(clean):
            return "low_confidence_refusal"
    return None


async def run_holo(*, client, provider_id, model, system, history, text, tools, dispatch, on_text=None,
                   required_tool=None, terminal_tools=(), finish_after_tool=None, stream_prose=False,
                   parallel_tools=(), idempotent_tools=None, on_tool_trace=None, failover_clients=(),
                   cascade_clients=(), on_failover=None, health_tracker=None, cancellation_token=None,
                   temperature=None, local_wake_fn=None, on_usage=None):
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    active_provider = [provider_id, model, client]
    streamed_preview = False

    def _preview_hook(content):
        nonlocal streamed_preview
        if content:
            streamed_preview = True
        elif not content:
            streamed_preview = False
        if on_text is not None:
            on_text(content)

    def _build_options(pid, schemas, required):
        options = {"stream_options": {"include_usage": True}}
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
            options["temperature"] = float(temperature) if (temperature is not None and not schemas) else 0
        return options

    async def stream_once(messages, schemas, required):
        if health_tracker is not None and (failover_clients or (active_provider[0] == "local" and cascade_clients)):
            alt_pool = tuple(failover_clients or ()) + (tuple(cascade_clients or ()) if active_provider[0] == "local" else ())
            candidates, bypassed_primary = health_tracker.rank_candidates(tuple(active_provider), alt_pool)
        else:
            bypassed_primary = False
            candidates = [tuple(active_provider)]
            alt_pool = tuple(failover_clients or ()) + (tuple(cascade_clients or ()) if active_provider[0] == "local" else ())
            for alt in alt_pool:
                if alt and alt[0] != active_provider[0]:
                    candidates.append(alt)

        last_err = None
        origin_pid = active_provider[0]
        for idx, (pid, mdl, cli) in enumerate(candidates):
            options = _build_options(pid, schemas, required)
            if pid == "local":
                max_attempts = 2 if local_wake_fn else 1
            else:
                max_attempts = 2 if idx == 0 else 1
            for attempt in range(max_attempts):
                if cancellation_token and getattr(cancellation_token, "cancelled", False):
                    raise asyncio.CancelledError("turn cancelled")
                yielded = 0
                meaningful_yielded = 0
                started = perf_counter()
                try:
                    if (idx > 0 or bypassed_primary) and pid != origin_pid and on_failover:
                        try:
                            reason = "health_cooldown" if bypassed_primary and idx == 0 else str(last_err or "cloud_unavailable")
                            on_failover(origin_pid, pid, mdl, reason)
                            if health_tracker is not None:
                                health_tracker.record_failover(origin_pid)
                        except Exception:
                            pass
                    try:
                        stream = await cli.chat.completions.create(model=mdl, messages=messages, stream=True,
                                                                   max_tokens=1024, timeout=120, **options)
                    except TypeError:
                        fallback_opts = {k: v for k, v in options.items() if k != "stream_options"}
                        stream = await cli.chat.completions.create(model=mdl, messages=messages, stream=True,
                                                                   max_tokens=1024, timeout=120, **fallback_opts)
                    async with stream:
                        tail_deadline = asyncio.timeout(None)
                        try:
                            async with tail_deadline:
                                async for chunk in stream:
                                    if cancellation_token and getattr(cancellation_token, "cancelled", False):
                                        raise asyncio.CancelledError("turn cancelled")
                                    usage_obj = getattr(chunk, "usage", None)
                                    if usage_obj is not None and on_usage:
                                        try:
                                            pt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
                                            ct = int(getattr(usage_obj, "completion_tokens", 0) or 0)
                                            tt = int(getattr(usage_obj, "total_tokens", 0) or (pt + ct))
                                            if tt or pt or ct:
                                                on_usage({"prompt": pt, "completion": ct, "total": tt})
                                        except Exception:
                                            pass
                                    if not chunk.choices or tail_deadline.when() is not None:
                                        continue
                                    choice = chunk.choices[0]
                                    delta = getattr(choice, "delta", None)
                                    has_payload = bool(
                                        (delta and (getattr(delta, "content", None) or getattr(delta, "tool_calls", None)))
                                        or getattr(choice, "finish_reason", None)
                                    )
                                    if meaningful_yielded == 0 and has_payload and pid != active_provider[0]:
                                        active_provider[:] = [pid, mdl, cli]
                                    if has_payload:
                                        meaningful_yielded += 1
                                    yielded += 1
                                    yield choice
                                    if choice.finish_reason:
                                        tail_deadline.reschedule(asyncio.get_running_loop().time() + STREAM_TAIL_SECONDS)
                        except TimeoutError:
                            if not tail_deadline.expired():
                                raise
                    if health_tracker is not None:
                        health_tracker.record_success(pid, (perf_counter() - started) * 1000)
                    return
                except APITimeoutError as exc:
                    last_err = RuntimeError("等待模型服务超时")
                    last_err.__cause__ = exc
                    if health_tracker is not None:
                        health_tracker.record_failure(pid, "APITimeoutError")
                    if meaningful_yielded > 0:
                        raise last_err from exc
                    if attempt + 1 < max_attempts:
                        if pid == "local" and local_wake_fn:
                            await local_wake_fn()
                        else:
                            await asyncio.sleep(CLOUD_RETRY_BACKOFF_SECONDS)
                        continue
                    break
                except APIConnectionError as exc:
                    last_err = RuntimeError("无法连接到所选模型服务")
                    last_err.__cause__ = exc
                    if health_tracker is not None:
                        health_tracker.record_failure(pid, "APIConnectionError")
                    if meaningful_yielded > 0:
                        raise last_err from exc
                    if attempt + 1 < max_attempts:
                        if pid == "local" and local_wake_fn:
                            await local_wake_fn()
                        else:
                            await asyncio.sleep(CLOUD_RETRY_BACKOFF_SECONDS)
                        continue
                    break
                except APIStatusError as exc:
                    last_err = exc
                    code = getattr(exc, "status_code", None)
                    if health_tracker is not None:
                        health_tracker.record_failure(pid, f"HTTP {code}", status_code=code)
                    if meaningful_yielded > 0:
                        raise
                    if code in TRANSIENT_HTTP_STATUS and attempt + 1 < max_attempts:
                        await asyncio.sleep(CLOUD_RETRY_BACKOFF_SECONDS)
                        continue
                    if pid != "local" and code in FAILOVER_HTTP_STATUS and idx + 1 < len(candidates):
                        break
                    raise
            if pid == "local" and not cascade_clients:
                break
        if last_err is not None:
            raise last_err

    try:
        local_ctx = max(4096, int(os.environ.get("MOSS_LOCAL_CTX_SIZE", "16384") or 16384))
    except ValueError:
        local_ctx = 16384
    local_budget = 4800 if local_ctx >= 12288 else 2600
    preview_cb = _preview_hook if on_text is not None else None
    brain = Brain(system, history, tools, stream_once, dispatch,
                  parallel_tools=parallel_tools, idempotent_tools=idempotent_tools,
                  on_tool_trace=on_tool_trace, cancellation_token=cancellation_token,
                  tool_budget_chars=local_budget if provider_id == "local" else 7200,
                  compact_tools=(provider_id == "local"))

    async def _run_cascade(cpid, cmdl, ccli, reason):
        nonlocal active_provider
        active_provider[:] = [cpid, cmdl, ccli]
        if preview_cb is not None and streamed_preview:
            preview_cb("")
        if on_failover:
            try:
                on_failover("local", cpid, cmdl, reason)
            except Exception:
                pass
        c_brain = Brain(system, history, tools, stream_once, dispatch,
                        parallel_tools=parallel_tools, idempotent_tools=idempotent_tools,
                        on_tool_trace=on_tool_trace, cancellation_token=cancellation_token,
                        tool_budget_chars=7200, compact_tools=False)
        return await c_brain.ask_stream(text, on_text=preview_cb, required_tool=required_tool,
                                        terminal_tools=terminal_tools,
                                        finish_after_tool=finish_after_tool, stream_prose=stream_prose)

    try:
        local_reply = await brain.ask_stream(text, on_text=preview_cb, required_tool=required_tool,
                                             terminal_tools=terminal_tools,
                                             finish_after_tool=finish_after_tool, stream_prose=stream_prose)
    except (ValueError, RuntimeError) as exc:
        if provider_id == "local" and active_provider[0] == "local" and cascade_clients and not (
            cancellation_token and getattr(cancellation_token, "cancelled", False)
        ):
            cpid, cmdl, ccli = cascade_clients[0]
            return await _run_cascade(cpid, cmdl, ccli, f"local_cascade:{type(exc).__name__}")
        raise

    if provider_id == "local" and active_provider[0] == "local" and cascade_clients and not (stream_prose and streamed_preview):
        ran_tools = any(m.get("role") == "tool" for m in getattr(brain, "_messages", ()))
        esc_reason = _should_cascade_local_answer(local_reply, prompt_text=text, ran_tools=ran_tools, had_tools=bool(tools))
        if esc_reason:
            cpid, cmdl, ccli = cascade_clients[0]
            return await _run_cascade(cpid, cmdl, ccli, f"local_cascade:{esc_reason}")
    return local_reply
