import argparse
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from local_media.library import MediaLibrary


def server(index):
    library = MediaLibrary(index, read_only=True)
    service = FastMCP("moss_media_readonly")

    @service.tool(description="只在已配置的本地与 NAS 媒体库查找曲目、视频或图片。返回可读媒体的名称与来源类型。",
                  structured_output=True)
    def search_media(query: str, kind: Literal["audio", "video", "image"] | None = None) -> dict[str, Any]:
        query = query.strip()
        if len(query) > 160 or not any(char.isalnum() for char in query):
            raise ValueError("请输入 1 至 160 字的媒体名称或关键词")
        found = library.search(query, kind=kind, limit=6, browser_audio=kind == "audio")
        return {
            "items": [{key:item[key] for key in ("title", "artist", "kind", "source_kind")}
                      for item in found["items"]],
            "consulted": len(found["consulted_sources"]),
            "unavailable": len(found["unavailable_sources"]),
        }

    @service.tool(description="读取已索引的本机和 NAS 媒体来源数量、类型及索引条目数。", structured_output=True)
    def list_sources() -> dict[str, Any]:
        return {"sources": [{"kind":src["kind"], "count":src["count"], "state":src["state"]}
                            for src in library.sources()]}

    return service


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    server(parser.parse_args().index).run(transport="stdio")
