import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import socket
from time import perf_counter

from aiohttp import ClientSession, ClientTimeout, web
import aiohttp
import psutil


ASSETS = Path(__file__).resolve().parent / "static/conversation-assets"
EXPECTED_HASH = "43f54ceb3235e4d5ffca4e95d2cd3f39c9dcb335a712681b854b78dc4b2f176b"
PATH = "/conversation-assets/moss-exterior.glb?v=" + EXPECTED_HASH


async def request(client, base):
    requested = perf_counter()
    async with client.get(base + PATH) as response:
        response.raise_for_status()
        assert response.status == 200
        headers = perf_counter()
        first = None
        digest = hashlib.sha256()
        size = 0
        async for chunk in response.content.iter_chunked(256 * 1024):
            if first is None:
                first = perf_counter()
            size += len(chunk)
            digest.update(chunk)
        end = perf_counter()
        assert digest.hexdigest() == EXPECTED_HASH and size == response.content_length
        return {
            "url": str(response.url), "status": response.status, "bytes": size, "sha256": digest.hexdigest(),
            "requestToHeadersMs": round((headers - requested) * 1000, 2),
            "headersToFirstChunkMs": round((first - headers) * 1000, 2),
            "headersToEndMs": round((end - headers) * 1000, 2),
            "mibPerSecondAfterHeaders": round(size / 2**20 / (end - headers), 2),
            "contentEncoding": response.headers.get("Content-Encoding"),
            "etag": response.headers.get("ETag"), "server": response.headers.get("Server"),
        }


async def verify(live_port):
    process = psutil.Process()
    app = web.Application()
    app.router.add_static("/conversation-assets", ASSETS)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        await web.SockSite(runner, listener).start()
        try:
            async with ClientSession(trust_env=False, auto_decompress=False, timeout=ClientTimeout(total=35),
                                     headers={"Accept-Encoding": "identity"}) as client:
                live = await request(client, f"http://127.0.0.1:{live_port}")
                isolated = await request(client, f"http://127.0.0.1:{port}")
        finally:
            await runner.cleanup()
    return {
        "live": live, "isolatedStaticRoute": isolated, "aiohttp": aiohttp.__version__,
        "loop": type(asyncio.get_running_loop()).__name__,
        "peakWorkingSetMiB": round(process.memory_info().peak_wset / 2**20, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-port", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(verify(args.live_port))
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.out.write_text(output, encoding="utf-8")
    print(output)
