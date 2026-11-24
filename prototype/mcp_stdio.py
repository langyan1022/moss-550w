import asyncio
from contextlib import asynccontextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def connect_stdio(command, args, *, cwd=ROOT, timeout=9, expected_name=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=command, args=args, cwd=cwd,
        env={"PYTHONUTF8":"1", "PYTHONUNBUFFERED":"1", "PYTHONIOENCODING":"utf-8",
             "PYTHONDONTWRITEBYTECODE":"1"},
    )
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline, stdio_client(params) as channels:
            async with ClientSession(*channels) as session:
                ready = await session.initialize()
                if expected_name and ready.serverInfo.name != expected_name:
                    raise ValueError("本机 MCP 服务身份不一致")
                yield session
    except Exception as error:
        if deadline.expired() and not isinstance(error, TimeoutError):
            # SDK 关闭进程组时可能把已触发的超时包进异常组。
            raise TimeoutError("MCP 到达调用时限") from error
        raise
