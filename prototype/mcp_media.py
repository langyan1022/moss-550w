import os
from pathlib import Path
import re
import sys

from local_media.library import DEFAULT_DB
from mcp_stdio import connect_stdio


KINDS = {"音乐": "audio", "歌曲": "audio", "视频": "video", "图片": "image", "媒体": None}
TOOL_LABELS = {"search_media": "查询配置内的本机或 NAS 媒体", "list_sources": "查看索引来源及条目数"}
SOURCE_LABELS = {"local": "本机", "nas": "NAS"}
INDEX_LABELS = {"ready": "已索引", "partial": "索引不完整", "new": "待索引",
                "offline": "上次检查失联", "denied": "上次检查无权读取"}


def parse_media_mcp(text):
    if re.fullmatch(r"(?:查看|列出|看看)\s*(?:本机|本地)?媒体\s*MCP\s*(?:的)?工具(?:列表)?", text, re.I):
        return {"action": "mcp_media", "mode": "tools"}
    if re.fullmatch(r"(?:用|通过)\s*MCP\s*(?:查看|列出|看看)(?:本机|本地)?媒体(?:库)?来源", text, re.I):
        return {"action": "mcp_media", "mode": "list_sources"}
    match = re.fullmatch(
        r"(?:用|通过)\s*MCP\s*(?:查找|查询|搜索|查|搜)\s*(?:本机|本地)?(音乐|歌曲|视频|图片|媒体)(?:[:： ]\s*)?(.*)",
        text, re.I,
    )
    if match:
        return {"action": "mcp_media", "mode": "search_media", "kind": KINDS[match[1]],
                "query": match[2].strip().strip("《》“”").strip()}
    return None


def format_result(mode, result):
    if mode == "list_sources":
        sources = result["sources"]
        if not sources:
            return "本机媒体 MCP 目前没有已配置的索引来源。"
        lines = [f"本机媒体 MCP · {len(sources)} 个来源"]
        for i, source in enumerate(sources, 1):
            state = INDEX_LABELS.get(source["state"], "索引状态未知")
            lines.append(f"{i}. {SOURCE_LABELS[source['kind']]} · {source['count']} 项 · {state}")
        return "\n".join(lines)
    entries = dict.fromkeys((item["title"], item["artist"], item["source_kind"]) for item in result["items"])
    if entries:
        lines = [f"本机媒体 MCP · 找到 {len(entries)} 项"]
        for i, (title, artist, location) in enumerate(entries, 1):
            credit = " · " + artist if artist else ""
            lines.append(f"{i}. {title}{credit} · {SOURCE_LABELS[location]}")
    else:
        lines = ["本机媒体 MCP 没有找到匹配的可读媒体。" if result["consulted"]
                 else "本机媒体 MCP 目前没有已配置的索引来源。"]
    if result["unavailable"]:
        lines.append(f"有 {result['unavailable']} 个来源暂时无法完整查询。")
    return "\n".join(lines)


async def run_media_mcp(intent):
    mode = intent["mode"]
    if mode == "search_media":
        query = intent["query"]
        if len(query) > 160 or not any(char.isalnum() for char in query):
            return "请输入 1 至 160 字的媒体名称或关键词，例如“用 MCP 查本地音乐 西海情歌”。"
    index = Path(os.environ.get("MOSS_MEDIA_DB") or DEFAULT_DB).resolve()
    if not index.is_file():
        return "本机媒体索引尚未建立，请先更新媒体索引。"
    try:
        async with connect_stdio(sys.executable, ["-m", "local_media.mcp_search", str(index)],
                                 expected_name="moss_media_readonly") as session:
            if mode == "tools":
                listed = await session.list_tools()
                labels = [TOOL_LABELS[tool.name] for tool in listed.tools if tool.name in TOOL_LABELS]
                if not labels:
                    raise ValueError("发现不到允许的工具")
                return "\n".join(["本机媒体 MCP 已连接", *labels])
            if mode not in TOOL_LABELS:
                raise ValueError("未知 MCP 工具")
            args = {"query":query, "kind":intent["kind"]} if mode == "search_media" else {}
            response = await session.call_tool(mode, args)
            if response.isError or not isinstance(response.structuredContent, dict):
                raise ValueError("本机 MCP 未返回工具结果")
            return format_result(mode, response.structuredContent)
    except TimeoutError:
        return "本机媒体 MCP 查询超时。可稍后重试或直接搜索媒体库。"
    except (OSError, ValueError, KeyError, ExceptionGroup):
        return "本机媒体 MCP 未能完成这次查询。可直接搜索媒体库。"
