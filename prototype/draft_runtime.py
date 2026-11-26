"""Lazy-loaded pydantic_ai runtime for deferred save_draft confirmation & legacy agent calls."""
from __future__ import annotations


INSTRUCTIONS = (
    "你是 MOSS，一位冷静、体贴的本地中文助手。用简短自然的中文回答，结合上下文。"
    "普通聊天直接回复。用户要建议或安排时，按已有条件直接给一个可执行的方案，答完就结束。"
    "不要在结尾追加“需要我……吗”或其他服务邀约。只有缺少完成任务必需的信息时才提问。"
    "用户要求保存草稿时必须调用一次 save_draft，text 只填要保存的原文。"
    "用户想听歌、观看视频或让你代选时，按明确的歌手、标题、版本和当前列表选取最匹配的一项。"
    "有可播放结果就执行并列出备选。只想浏览、拒绝播放或询问事实时，仅回答相应内容。"
    "系统会让用户核对预览和确认。只有在拿到工具成功回执后才能说已保存。"
    "收到工具回执后用一句中文确认。"
)


def build_draft_agent(moss, key: str):
    """Build a pydantic_ai Agent lazily so startup/chat never imports pydantic_ai eagerly."""
    from pydantic_ai import Agent, DeferredToolRequests
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.profiles.openai import OpenAIModelProfile
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(openai_client=moss.model_client(key))
    if key == "local":
        name = moss.local_model
        settings = {
            "temperature": 0,
            "max_tokens": 768,
            "timeout": 420,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
    else:
        profile = moss.cloud[key]
        name = profile.model
        settings = {"max_tokens": 768, "timeout": 120}
        if key in {"deepseek", "kimi"}:
            settings["extra_body"] = {"thinking": {"type": "disabled"}}
    chat_model = OpenAIChatModel(
        name,
        provider=provider,
        profile=OpenAIModelProfile(
            openai_chat_supports_max_completion_tokens=False,
            openai_chat_supports_multiple_system_messages=False,
        ),
    )
    agent = Agent(
        chat_model,
        deps_type=str,
        instructions=INSTRUCTIONS,
        output_type=[str, DeferredToolRequests],
        retries={"tools": 0, "output": 1},
        model_settings=settings,
    )
    agent.tool(
        name="save_draft",
        description="将指定文字原样保存到本轮的本地草稿。",
        requires_approval=True,
        prepare=moss._prepare_draft,
    )(moss._save_draft)
    agent.tool(
        name="search_media",
        sequential=True,
        prepare=moss._prepare_search,
        description="用户想找自己收藏的内容时检索本机和 NAS，例如只提到一位歌手。query 只写标题、作者或关键词。",
    )(moss._search_media)
    agent.tool(
        name="inspect_audio",
        sequential=True,
        prepare=moss._prepare_search,
        description="读取当前选中音轨的时长、采样率和格式。",
    )(moss._inspect_audio)
    agent.tool(
        name="play_audio",
        sequential=True,
        prepare=moss._prepare_play,
        description="试听检索结果中第 index 首真实音轨，编号从 1 起。",
    )(moss._play_audio)
    agent.output_validator(moss._verify_answer)
    return agent


def validate_pending_draft(raw_pending: str, turn: dict):
    from pydantic_ai.conversation import ConversationTypeAdapter

    conversation = ConversationTypeAdapter.validate_json(raw_pending)
    requests = conversation.deferred_tool_requests
    if not requests or len(requests.approvals) != 1:
        raise ValueError("模型会话没有唯一待确认调用")
    call = requests.approvals[0]
    if (
        call.tool_name != "save_draft"
        or call.tool_call_id != turn["call_id"]
        or call.args_as_dict() != {"text": turn["preview"]}
    ):
        raise ValueError("待确认的参数与预览不一致")
    return conversation, requests.build_results(approvals={call.tool_call_id: True})


def to_pydantic_history(history_entries: list[dict]) -> list:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    out = []
    for entry in history_entries:
        role = entry.get("role")
        if role == "user":
            out.append(ModelRequest(parts=[UserPromptPart(entry["content"])]))
        elif role == "draft_call":
            out.append(ModelResponse(parts=[ToolCallPart("save_draft", {"text": entry["text"]}, entry["call_id"])]))
            out.append(ModelRequest(parts=[ToolReturnPart("save_draft", "本地草稿已保存并读回核实。", entry["call_id"])]))
        elif role == "assistant":
            out.append(ModelResponse(parts=[TextPart(entry["content"])]))
    return out


async def run_draft_stream_events(ctx, events, *, should_publish, on_partial):
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta

    if ctx.tools:
        return
    text = ""
    last_write = 0.0
    from time import perf_counter

    async for event in events:
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            chunk = event.part.content
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            chunk = event.delta.content_delta
        else:
            continue
        if not chunk:
            continue
        text += chunk
        timestamp = perf_counter()
        if timestamp - last_write < 0.15:
            continue
        if not should_publish(text):
            return
        on_partial(text)
        last_write = timestamp


def handle_draft_agent_result(result) -> tuple[str, dict]:
    from pydantic_ai import DeferredToolRequests
    from pydantic_ai.conversation import ConversationTypeAdapter

    if isinstance(result.output, DeferredToolRequests):
        approvals = result.output.approvals
        if len(approvals) != 1 or approvals[0].tool_name != "save_draft":
            raise ValueError("本轮需要恰好一个待确认的草稿调用")
        call = approvals[0]
        preview = call.args_as_dict().get("text")
        if not isinstance(preview, str) or not preview.strip():
            raise ValueError("模型返回了空草稿")
        return "pending", {
            "preview": preview,
            "call_id": call.tool_call_id,
            "pending": ConversationTypeAdapter.dump_json(result.conversation),
        }
    if not str(result.output or "").strip():
        raise ValueError("模型没有返回文字")
    return "replied", {"reply": result.output}


async def run_agent_with_limits(agent, kwargs: dict, turn_id: str, token):
    from pydantic_ai.usage import UsageLimits

    bridge_token = token.as_pydantic() if hasattr(token, "as_pydantic") else token
    async with agent:
        return await agent.run(
            **kwargs,
            deps=turn_id,
            cancellation_token=bridge_token,
            usage_limits=UsageLimits(request_limit=4),
        )


def format_draft_error(error: Exception, user_text: str, has_call_id: bool, is_draft_request: bool) -> str:
    from openai import APIStatusError
    from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError, RunCancelled
    from window_image import WindowImageError

    if isinstance(error, ModelConnectionError) and is_draft_request and not has_call_id:
        return "模型连接中断，草稿预览尚未生成。连接恢复后可重新发送。"
    if isinstance(error, (APIStatusError, ModelHTTPError)):
        status = error.status_code
        if status == 429:
            message = "模型服务正在限流，稍后可以重试（HTTP 429）。"
        elif 500 <= status < 600:
            message = f"模型服务暂时不可用，稍后可以重试（HTTP {status}）。"
        elif status in (401, 403):
            message = f"模型服务拒绝访问，请检查所选模型的接口凭据和权限（HTTP {status}）。"
        elif status == 402:
            message = "所选模型的账户余额不足（HTTP 402）。"
        else:
            message = f"模型服务未接受本次请求（HTTP {status}）。"
        if is_draft_request and not has_call_id:
            message += "草稿预览尚未生成。"
        return message
    if isinstance(error, RunCancelled):
        return "模型运行意外终止"
    if isinstance(error, WindowImageError):
        return str(error)
    return f"{type(error).__name__}: {error}"
