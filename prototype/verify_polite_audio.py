"""Replay conversational music requests on an isolated snapshot of the G: library."""

import argparse
import asyncio
from contextlib import AsyncExitStack, closing, nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from unittest.mock import patch

from aiohttp import ClientSession, web

from browser_lab import ROOT
from core import Moss
from local_media.library import song_title_key
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_music_delegation import catalog_at


MUSIC_REQUESTS = (
    ("能帮我放首歌吗？", None, None, True),
    ("可以帮我放一首刘德华的歌吗？", "刘德华", None, True),
    ("能不能给我来一首适合现在听的歌？", None, None, True),
    ("你能帮我挑首歌吗？", None, None, True),
    ("能帮我播放刘德华的《冰雨》吗？", "刘德华", "冰雨", True),
    ("能帮我找找刘德华的歌吗？", "刘德华", None, False),
)
NO_AUTOPLAY = (
    "能帮我找找刘德华的歌吗？先别放。",
    "能帮我放首歌吗？顺便告诉我现在几点。",
    "你能放歌吗？",
    "你的音乐播放功能支持什么格式？",
    "先别帮我放首歌。",
    "举例说明“能帮我放首歌吗？”这句话。",
    "系统音量大不大，能帮我放首歌吗？",
)


async def exercise(folder, report):
    catalog = catalog_at(folder, True)
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
        library = {media_id: (artist, Path(path)) for media_id, artist, path in db.execute(
            "SELECT media_id, artist, path FROM media WHERE kind='audio'")}
    assert any(path.drive.upper() == "G:" and (artist == "刘德华" or "刘德华" in path.parent.parts)
               for artist, path in library.values())

    async def unavailable_model(**request):
        report["model_fallback"].append(request["text"])
        return "隔离回归的模型回复。"

    async def block_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_MEDIA_DB": str(catalog),
                   "MOSS_PROVIDER_DIR": str(folder / "providers"), "MOSS_MEM0_ENABLED": "0",
                   "MOSS_WINDOWS_REMINDERS": "0", "MOSS_KEV_ENABLED": "0",
                   "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}
    async with AsyncExitStack() as stack:
        guard = await stack.enter_async_context(await asyncio.start_server(block_model, "127.0.0.1", 0))
        model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
        for _, prefixes, _, _ in SPECS.values():
            for prefix in prefixes:
                environment[prefix + "_BASE_URL"] = model_url
                environment[prefix + "_API_KEY"] = "isolated-qa"
        stack.enter_context(patch.dict(os.environ, environment))
        stack.enter_context(patch("assistant_brain.run_holo", unavailable_model))
        moss = Moss(folder / "data", model_url, "isolated-qa")
        stack.push_async_callback(moss.close)
        port = free_port()
        app = application(moss, lambda: "standby", folder / "data", port, "polite-audio-qa", asyncio.Event())
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        client = await stack.enter_async_context(ClientSession(base_url=f"http://127.0.0.1:{port}"))

        async def ask(text):
            started = perf_counter()
            fallback_before = len(report["model_fallback"])
            async with client.post("/api/message", json={"text": text, "chat_id": moss.chat_id}) as response:
                state = await response.json()
                assert response.ok, state
            turn_id = state["turns"][-1]["id"]
            async with asyncio.timeout(10):
                while state["turns"][-1]["status"] == "thinking":
                    await asyncio.sleep(.01)
                    async with client.get("/api/state") as response:
                        state = await response.json()
            turn = state["turns"][-1]
            assert turn["id"] == turn_id and turn["user"] == text, turn
            media = turn.get("media") or {}
            selected = media.get("selected") or {}
            row = {"text": text, "status": turn["status"], "source": turn["source"], "reply": turn["reply"],
                   "autoplay": bool(media.get("autoplay")), "selected": selected.get("media_id"),
                   "title": selected.get("title"), "artist": selected.get("artist"),
                   "cloud_requests": turn["cloud_requests"]["total"], "online_calls": media.get("online_calls"),
                   "model_fallbacks": len(report["model_fallback"]) - fallback_before,
                   "elapsed_ms": round((perf_counter() - started) * 1000, 1)}
            report["cases"].append(row)
            assert turn["status"] == "replied" and row["cloud_requests"] == 0, row
            return turn, media, selected, row

        async def check_track(turn, media, selected, row):
            assert row["autoplay"] and media.get("play_mode") == "track" and selected.get("media_id"), row
            assert selected["title"] in media["announcement"] and selected["title"] in turn["reply"], row
            assert all(word not in turn["reply"].splitlines()[0] for word in ("？", "?", "请告诉我")), row
            _, path = library[selected["media_id"]]
            async with client.get("/media/" + selected["media_id"], headers={"Range": "bytes=0-127"}) as response:
                with path.open("rb") as original:
                    assert response.status == 206 and await response.read() == original.read(128), row
                row["range_status"] = response.status

        second_item = None
        for text, artist, title, autoplay in MUSIC_REQUESTS:
            async with client.post("/api/new-chat", json={}) as response:
                assert response.ok
            turn, media, selected, row = await ask(text)
            try:
                assert row["online_calls"] == row["model_fallbacks"] == 0, row
                if title:
                    assert row["source"] == "holo" and turn.get("media_actions") == ["named_song"], row
                else:
                    assert row["source"] == "media", row
                assert row["autoplay"] is autoplay and media["items"], row
                expected_items = []
                for item in media["items"]:
                    credit, path = library[item["media_id"]]
                    assert path.drive.upper() == "G:" and item["source_kind"] == "local", item["media_id"]
                    if artist:
                        assert credit == artist or artist in path.parent.parts, item["media_id"]
                    expected_items.append(item["title"])
                row["results"] = expected_items
                if autoplay:
                    if artist:
                        assert selected["artist"] == artist, row
                    if title:
                        assert song_title_key(selected["title"]) == song_title_key(title), row
                    await check_track(turn, media, selected, row)
                else:
                    assert not media.get("announcement") and not media.get("play_mode") and len(expected_items) >= 2, row
                    assert all(title in turn["reply"] for title in expected_items), row
                    second_item = media["items"][1]
            except AssertionError as error:
                report["failures"].append({"text": text, "reason": str(error)})

        if second_item:
            turn, media, selected, row = await ask("能帮我放第二首吗？")
            assert row["source"] == "media" and row["online_calls"] == row["model_fallbacks"] == 0, row
            assert selected.get("media_id") == second_item["media_id"], (row, second_item["title"])
            await check_track(turn, media, selected, row)

        for text in NO_AUTOPLAY:
            turn, media, _, row = await ask(text)
            assert not row["autoplay"] and not turn.get("browser_playback") and not media.get("announcement"), row
        assert not report["failures"] and report["model_connections"] == 0, report["failures"]


async def run(baseline=False):
    folder = ROOT / "runs" / datetime.now().strftime("polite-audio-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    evidence = folder / "result.json"
    print(evidence, flush=True)
    report = {"passed": False, "catalog": "read-only G: index snapshot", "baseline": baseline, "model_connections": 0,
              "model_fallback": [], "cases": [], "failures": []}
    try:
        with (patch("media_chat.polite_audio_request", return_value=None) if baseline else nullcontext()):
            await exercise(folder, report)
        report["passed"] = True
    finally:
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Run with polite normalization disabled")
    asyncio.run(run(parser.parse_args().baseline))
