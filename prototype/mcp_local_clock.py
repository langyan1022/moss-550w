from datetime import datetime, timezone
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


clock = FastMCP("moss_local_clock")
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@clock.tool(annotations=READ, description="从本机系统时钟读取当地时间或 UTC。", structured_output=True)
def read_clock(zone: Literal["local", "UTC"] = "local") -> dict[str, str]:
    current = datetime.now(timezone.utc)
    visible = current if zone == "UTC" else current.astimezone()
    label = "协调世界时" if zone == "UTC" else "本机时间"
    return {"时间":visible.isoformat(timespec="seconds"), "时区":visible.tzname() or visible.strftime("%z"),
            "答复":f"{label}现在是 {visible.hour} 点 {visible.minute:02d} 分。"}


@clock.tool(annotations=READ, description="从本机系统时钟读取当地日期。", structured_output=True)
def read_date() -> dict[str, str]:
    today = datetime.now().date()
    return {"日期":today.isoformat(), "答复":f"今天是 {today.year} 年 {today.month} 月 {today.day} 日。"}


if __name__ == "__main__":
    clock.run(transport="stdio")
