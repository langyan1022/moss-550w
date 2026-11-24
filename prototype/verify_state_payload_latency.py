"""Compare full state serialization and encoded-row reuse on real HTTP and SSE."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
import os
from statistics import median
from time import perf_counter
from unittest.mock import patch

from aiohttp import ClientSession, web

from browser_lab import ROOT
from core import Moss
from local_media.library import MediaLibrary
from memory_store import now
from serve import _StatePayload, application
from verify_local_overview_progressive import available_port


TEXT = '把「天气预报」保留为一段，再写 "回执" 和路径 G:\\音乐。\n' * 24
MODES = ("previous", "cached", "cached", "previous") * 4


def previous_read(self, transport_owner=None):
    snapshot = self.moss.state(transport_owner=transport_owner)
    snapshot["speech_prepare"] = self.speech_prepare
    return snapshot["revision"], self.encode(snapshot)


def seed(moss, count):
    with moss.db:
        for index in range(count):
            turn = {"id": f"test-{index}", "chat_id": moss.chat_id, "created_at": now(), "user": f"合成历史 {index}",
                    "status": "replied", "source": "holo", "reply": TEXT + str(index), "receipt": None}
            moss._insert_turn(turn)
        key = "new-reading"
        moss._insert_turn({"id": key, "chat_id": moss.chat_id, "created_at": now(), "user": "本轮流式读数",
                           "status": "thinking", "source": "holo", "reply": None, "receipt": None})
    return key


async def next_state(stream, marker):
    async with asyncio.timeout(10):
        async for line in stream:
            if line.startswith(b"data: "):
                state = json.loads(line[6:])
                if state["turns"][-1].get("partial_reply") == marker:
                    return state
    raise AssertionError(f"Stream closed before {marker}")


async def measure(folder, count):
    samples = []
    cache_read = _StatePayload.read
    data = folder / str(count)
    async with AsyncExitStack() as stack:
        moss = Moss(data, "http://127.0.0.1:9/v1", "state-qa-unused")
        stack.push_async_callback(moss.close)
        key = seed(moss, count)
        port = available_port()
        stop = asyncio.Event()
        runner = web.AppRunner(application(moss, lambda: "standby", data, port, "state-qa", stop),
                               access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        client = await stack.enter_async_context(ClientSession(read_bufsize=2**21, base_url=f"http://127.0.0.1:{port}"))
        streams = []
        for _ in range(4):
            response = await stack.enter_async_context(client.get("/api/events"))
            assert response.status == 200 and response.content_type == "text/event-stream"
            streams.append(response.content)

        async def sample(mode, index):
            selected = previous_read if mode == "previous" else cache_read
            with patch.object(_StatePayload, "read", selected):
                marker = f"已读取 {index} 项。"
                started = perf_counter()
                moss._update(moss._get(key), partial_reply=marker)
                states = await asyncio.gather(*(next_state(stream, marker) for stream in streams))
                batch_ms = (perf_counter() - started) * 1000
                started = perf_counter()
                async with client.get("/api/state") as response:
                    body = await response.read()
                    got = json.loads(body)
                get_ms = (perf_counter() - started) * 1000
            reference = {**moss.state(), "speech_prepare": got["speech_prepare"]}
            assert all(row == reference for row in (*states, got))
            assert len(got["turns"]) == count + 1 and got["turns"][0]["reply"] == TEXT + "0"
            return {"mode": mode, "get_ms": round(get_ms, 3), "four_streams_ms": round(batch_ms, 3), "bytes": len(body)}

        try:
            await sample("previous", 0)
            await sample("cached", 1)
            for index, mode in enumerate(MODES, 2):
                samples.append(await sample(mode, index))
        finally:
            stop.set()
            moss._notify()
    medians = {}
    for mode in ("previous", "cached"):
        group = [row for row in samples if row["mode"] == mode]
        medians[mode] = {key: round(median(row[key] for row in group), 3) for key in ("get_ms", "four_streams_ms")}
    return {"history_rows": count, "median_ms": medians, "samples": samples}


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("state-payload-latency-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    catalog = folder / "empty-media.sqlite3"
    MediaLibrary(catalog)
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
    outcome = folder / "result.json"
    report = {"passed": False, "scope": "synthetic history, actual localhost HTTP and four SSE listeners", "chats": []}
    print(outcome, flush=True)
    try:
        with patch.dict(os.environ, environment):
            for count in (35, 360):
                report["chats"].append(await measure(folder, count))
        report["passed"] = True
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
