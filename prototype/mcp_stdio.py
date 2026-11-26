import asyncio
from contextlib import asynccontextmanager
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from mcp_modern import resolve_headers


ROOT = Path(__file__).resolve().parent


def validate_mcp_http_url(url: str, *, allow_remote: bool = False) -> str:
    """Validate Streamable HTTP MCP URL against loopback/LAN defaults and safe public HTTPS."""
    clean = str(url or "").strip()
    parts = urlsplit(clean)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("MCP HTTP 地址必须是合法的 http:// 或 https:// URL。")
    host = parts.hostname.strip().lower()
    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    is_lan = host.endswith(".local")
    if not is_loopback and not is_lan:
        try:
            ip = ipaddress.ip_address(host)
            is_lan = ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            is_lan = False
    if not is_loopback and not is_lan:
        if not allow_remote:
            raise ValueError("外部 MCP 服务默认仅允许本机/局域网地址；如需连接公网 HTTPS 请开启 allow_remote。")
        if parts.scheme != "https":
            raise ValueError("公网远程 MCP 服务必须使用 https://。")
    return clean


@asynccontextmanager
async def _mcp_session(channels, *, expected_name=None, elicitation_callback=None, experimental_task_handlers=None):
    from mcp import ClientSession

    read_stream, write_stream = channels[0], channels[1]
    async with ClientSession(
        read_stream,
        write_stream,
        elicitation_callback=elicitation_callback,
        experimental_task_handlers=experimental_task_handlers,
    ) as session:
        ready = await session.initialize()
        if expected_name and ready.serverInfo.name != expected_name:
            raise ValueError("MCP 服务身份不一致")
        yield session


@asynccontextmanager
async def connect_stdio(
    command,
    args,
    *,
    cwd=ROOT,
    timeout=9,
    expected_name=None,
    elicitation_callback=None,
    experimental_task_handlers=None,
):
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=command,
        args=args,
        cwd=cwd,
        env={
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline, stdio_client(params) as channels:
            async with _mcp_session(
                channels,
                expected_name=expected_name,
                elicitation_callback=elicitation_callback,
                experimental_task_handlers=experimental_task_handlers,
            ) as session:
                yield session
    except Exception as error:
        if deadline.expired() and not isinstance(error, TimeoutError):
            # SDK 关闭进程组时可能把已触发的超时包进异常组。
            raise TimeoutError("MCP 到达调用时限") from error
        raise


@asynccontextmanager
async def connect_streamable_http(
    url,
    *,
    headers=None,
    timeout=15,
    expected_name=None,
    allow_remote=False,
    elicitation_callback=None,
    experimental_task_handlers=None,
):
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    clean_url = validate_mcp_http_url(url, allow_remote=allow_remote)
    hdrs = resolve_headers(headers)
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            async with httpx.AsyncClient(
                headers=hdrs,
                timeout=httpx.Timeout(min(float(timeout), 30.0), read=max(30.0, float(timeout) * 2)),
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with streamable_http_client(clean_url, http_client=http_client) as channels:
                    async with _mcp_session(
                        channels,
                        expected_name=expected_name,
                        elicitation_callback=elicitation_callback,
                        experimental_task_handlers=experimental_task_handlers,
                    ) as session:
                        yield session
    except Exception as error:
        if deadline.expired() and not isinstance(error, TimeoutError):
            raise TimeoutError("Streamable HTTP MCP 到达调用时限") from error
        raise
