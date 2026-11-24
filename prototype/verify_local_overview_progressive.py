"""Verify progressive local tool readings through the actual HTTP/SSE endpoints."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
import os
import re
import socket
from time import perf_counter
from unittest.mock import patch

from aiohttp import ClientSession, web

from browser_lab import BrowserServer, ROOT
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application


QUESTION = "系统音量多大、现在几点、电脑还有多少电？"
BATTERY = "电量 73%（未接电）。"
VOLUMES = ("系统音量为 37%。", "系统音量为 61%。", "系统音量为 82%。")
FAST_KINDS = ["clock", "battery"]
ALL_KINDS = ["volume", *FAST_KINDS]


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def wait_turn(stream, turn_id, status, *, with_partial=False):
    async with asyncio.timeout(8):
        async for line in stream:
            if not line.startswith(b"data: "):
                continue
            state = json.loads(line[6:])
            for turn in state["turns"]:
                if turn["id"] == turn_id and turn["status"] == status:
                    if not with_partial or turn.get("local_overview_partial"):
                        return turn
    raise RuntimeError(f"SSE closed before {turn_id} reached {status}")


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("local-overview-progressive-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    report = {"passed": False, "model_connections": 0, "cases": []}
    volume_reads = 0
    started = [asyncio.Event() for _ in VOLUMES]
    release = [asyncio.Event() for _ in VOLUMES]
    cancelled = [asyncio.Event() for _ in VOLUMES]

    async def read_volume(intent):
        nonlocal volume_reads
        assert intent["action"] == "get_volume", intent
        index = volume_reads
        volume_reads += 1
        started[index].set()
        try:
            await release[index].wait()
        except asyncio.CancelledError:
            cancelled[index].set()
            raise
        return VOLUMES[index]

    async def read_device(intent):
        assert intent["metric"] == "battery", intent
        return BATTERY

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def verify_cases(server, stream):
        async def begin(index):
            start = perf_counter()
            code, state = await server.post("/api/message", {"text": QUESTION})
            turn = state["turns"][-1]
            assert code == 200 and turn["status"] == "thinking", (code, turn)
            async with asyncio.timeout(3):
                await started[index].wait()
            while True:
                early = await wait_turn(stream, turn["id"], "thinking", with_partial=True)
                if len(early["local_overview_partial"]) == len(FAST_KINDS):
                    break
            assert early["source"] == "context" and not early.get("reply"), early
            assert [item["kind"] for item in early["local_overview_partial"]] == FAST_KINDS, early
            assert all(item["reply"] in early["partial_reply"] for item in early["local_overview_partial"]), early
            assert re.search(r"\d\d:\d\d:\d\d", early["partial_reply"]) and BATTERY in early["partial_reply"], early
            assert not release[index].is_set() and not cancelled[index].is_set(), "Readings must arrive with volume still blocked"
            return early, start

        def assert_final(turn, early):
            readings = turn["local_overview"]
            assert [reading["kind"] for reading in readings] == ALL_KINDS, turn
            assert readings[1:] == early["local_overview_partial"], (turn, early)
            assert turn["reply"] == "\n".join(reading["reply"] for reading in readings), turn
            assert not turn.get("local_overview_partial") and not turn.get("partial_reply"), turn
            assert turn["cloud_requests"]["total"] == 0, turn
            return readings

        first, started_at = await begin(0)
        early_ms = round((perf_counter() - started_at) * 1000, 1)
        release[0].set()
        done = await wait_turn(stream, first["id"], "replied")
        assert assert_final(done, first)[0]["reply"] == VOLUMES[0], done
        report["cases"].append({"case": "fast readings arrive before slow volume and final rows preserve request order",
                                "partial_ms": early_ms, "final_ms": round((perf_counter() - started_at) * 1000, 1),
                                "partial": first["local_overview_partial"], "final": done["local_overview"]})

        second, _ = await begin(1)
        start_cancel = perf_counter()
        code, state = await server.post("/api/cancel", {"turn_id": second["id"]})
        assert code == 200 and state["turns"][-1]["status"] == "cancelled", (code, state)
        stopped = await wait_turn(stream, second["id"], "cancelled")
        async with asyncio.timeout(2):
            await cancelled[1].wait()
        cancel_ms = round((perf_counter() - start_cancel) * 1000, 1)
        assert not release[1].is_set(), "Cancellation cannot wait for the blocked reader"
        code, state = await server.post("/api/message", {"text": "你好"})
        next_turn = state["turns"][-1]
        assert code == 200 and next_turn["status"] == "replied" and next_turn["id"] != second["id"], (code, state)
        await wait_turn(stream, next_turn["id"], "replied")
        release[1].set()
        await asyncio.sleep(0)
        latest = next(turn for turn in (await server.state())["turns"] if turn["id"] == stopped["id"])
        assert latest["status"] == "cancelled" and not latest.get("local_overview"), latest
        assert VOLUMES[1] not in json.dumps(latest, ensure_ascii=False), latest
        report["cases"].append({"case": "cancellation unblocks the next turn and rejects a late result",
                                "cancel_ms": cancel_ms, "reader_cancelled": True, "next_turn_replied": True})

        third, started_at = await begin(2)
        early_ms = round((perf_counter() - started_at) * 1000, 1)
        done = await wait_turn(stream, third["id"], "replied")
        assert "音量" in assert_final(done, third)[0]["reply"] and "超时" in done["reply"], done
        assert cancelled[2].is_set() and not release[2].is_set() and VOLUMES[2] not in done["reply"], done
        report["cases"].append({"case": "timeout preserves verified readings and reports only the stalled metric",
                                "partial_ms": early_ms, "final_ms": round((perf_counter() - started_at) * 1000, 1),
                                "partial": third["local_overview_partial"], "final": done["local_overview"]})

    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch("local_overview.run_desktop", read_volume))
            stack.enter_context(patch("local_overview.device_status", read_device))
            sink = await stack.enter_async_context(await asyncio.start_server(guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
                    os.environ[prefix + "_API_KEY"] = "isolated-qa"
            server = BrowserServer(folder, available_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "moss-overview-fixture")
            stack.push_async_callback(moss.close)
            for gate in release:
                stack.callback(gate.set)
            stopped = asyncio.Event()
            app = application(moss, lambda: "standby", server.data, server.port, "overview-qa", stopped)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", server.port).start()
            client = await stack.enter_async_context(ClientSession())
            events = await stack.enter_async_context(client.get(server.url + "/api/events"))
            assert events.status == 200 and events.content_type == "text/event-stream"
            try:
                await verify_cases(server, events.content)
                assert volume_reads == len(VOLUMES) and report["model_connections"] == 0
                report["passed"] = True
            finally:
                stopped.set()
                moss._notify()
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
