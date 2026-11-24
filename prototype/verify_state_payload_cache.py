"""Check the public state contract across real HTTP, SSE, and service restart."""

import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientSession, web

from browser_lab import ROOT
from core import Moss
from local_media.library import MediaLibrary
from memory_store import now
from serve import application


OWNER_ID = "payload-qa-owner-5121a594-c702-4079"
PRIVATE = "payload-qa-private-field"
MEDIA_ID = "payload-qa-silent-media"
TITLE = "隔离验收音轨"
WAKE_TOKEN = "payload-qa-wake"
EXTERNAL_REPLY = "隔离验收快照已在停机时修订。"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@asynccontextmanager
async def start(data):
    moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
    port = free_port()
    stopped = asyncio.Event()
    app = application(moss, lambda: "standby", data, port, WAKE_TOKEN, stopped)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        yield moss, f"http://127.0.0.1:{port}"
    finally:
        stopped.set()
        moss._notify()
        await runner.cleanup()
        await moss.close()


async def sse(stream, wanted, *, revision=-1):
    event = "message"
    data = []
    async with asyncio.timeout(4):
        async for raw in stream:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            elif not line:
                if data and event == wanted:
                    value = "\n".join(data)
                    if wanted != "state":
                        return value
                    row = json.loads(value)
                    if row["revision"] >= revision:
                        return row
                elif data and event == "wake-offer":
                    raise AssertionError("A stream received an unexpected wake offer")
                event, data = "message", []
    raise AssertionError(f"SSE ended before {wanted} at revision {revision}")


def assert_public(state):
    payload = json.dumps(state, ensure_ascii=False)
    assert OWNER_ID not in payload and PRIVATE not in payload, state
    assert all(not any(key.startswith("_") for key in row) for row in state["turns"]), state


def seed_media(moss):
    with moss.db:
        key = uuid4().hex
        item = {"media_id": MEDIA_ID, "title": TITLE, "artist": "隔离验收", "kind": "audio", "source_kind": "local"}
        row = {"id": key, "chat_id": moss.chat_id, "user": "隔离媒体测试样本", "source": "media",
               "status": "replied", "reply": f"已找到《{TITLE}》。", "created_at": now(), "replied_at": now(),
               "receipt": None, "call_id": None, "media": {"selected": item, "items": [item]},
               "_memory_context": PRIVATE, "_memory_versions": {PRIVATE: now()}, "_weather_memory_versions": {PRIVATE: now()},
               "_file_plan_snapshots": {PRIVATE: None}, "_local_overview_request": True,
               "desktop_confirmation": {"action": "organize_file", "snapshot": PRIVATE}}
        moss._insert_turn(row)
    return key


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("state-payload-cache-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result, flush=True)
    report = {"passed": False, "checks": []}
    data = folder / "data"
    catalog = folder / "empty-catalog.sqlite3"
    MediaLibrary(catalog)
    env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
           "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
           "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}

    def checked(name, revision):
        report["checks"].append({"case": name, "revision": revision})

    try:
        with patch.dict(os.environ, env):
            async with start(data) as (moss, url), ClientSession() as http:
                media = seed_media(moss)

                async def get():
                    async with http.get(url + "/api/state") as response:
                        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
                        state = await response.json()
                    assert_public(state)
                    return state

                async def post(verb, *, public=True, headers=None, **body):
                    async with http.post(url + "/api/" + verb, json=body, headers=headers) as response:
                        assert response.status in {200, 204}, (verb, response.status, await response.text())
                        if response.status == 204:
                            return None
                        state = await response.json()
                    if public and "turns" in state:
                        assert_public(state)
                    return state

                async with http.get(url + "/api/events?wake=1") as awake, http.get(url + "/api/events") as quiet:
                    assert awake.content_type == quiet.content_type == "text/event-stream"

                    async def both(expected):
                        revision = expected["revision"]
                        views = await asyncio.gather(sse(awake.content, "state", revision=revision),
                                                     sse(quiet.content, "state", revision=revision))
                        assert views[0] == views[1] == expected == await get(), views

                    initial = await get()
                    first_chat = initial["chat_id"]
                    await both(initial)
                    assert initial["turns"][0]["id"] == media
                    checked("initial GET and both streams agree and redact private fields", initial["revision"])

                    async with asyncio.TaskGroup() as tasks:
                        activation = tasks.create_task(post("wake/activate", token=WAKE_TOKEN))
                        offer = await sse(awake.content, "wake-offer")
                        proof = await post("wake/claim", headers={"Origin": url}, ticket=offer)
                        await post("wake/shown", headers={"Origin": url}, ticket=offer, grant=proof["grant"])
                    assert activation.result() == {"result": "page_shown"}
                    assert await get() == initial
                    checked("wake-capable stream receives an offer without changing public state", initial["revision"])

                    greeting = await post("message", text="你好。", chat_id=first_chat, transport_request_id=OWNER_ID)
                    assert greeting["turns"][-1]["status"] == "replied"
                    await both(greeting)
                    play_id = "payload-qa-play"
                    event = await post("media-event", chat_id=first_chat, turn_id=media, media_id=MEDIA_ID,
                                       event="started", play_id=play_id)
                    assert event == {"ok": True}
                    played = await get()
                    assert played["revision"] > greeting["revision"] and played["turns"][0]["browser_playback"]["play_id"] == play_id
                    assert played["turns"][-1]["id"] == greeting["turns"][-1]["id"]
                    await both(played)
                    checked("media receipt invalidates the earlier-turn projection", played["revision"])

                    player = {"state": "playing", "turn_id": media, "media_id": MEDIA_ID}
                    owner = await post("message", public=False, text="暂停音乐", chat_id=first_chat,
                                       transport_request_id=OWNER_ID, player=player)
                    requested = owner["turns"][-1]
                    assert requested["status"] == "player_requested" and requested["transport"]["request_id"] == OWNER_ID, owner
                    assert PRIVATE not in json.dumps(owner, ensure_ascii=False), owner
                    redacted = await get()
                    assert redacted["revision"] == owner["revision"]
                    assert redacted["turns"][-1]["transport"] == {k: v for k, v in requested["transport"].items() if k != "request_id"}
                    await both(redacted)
                    checked("owner POST alone can see the same-revision transport request ID", redacted["revision"])

                    paused = await post("transport-result", chat_id=first_chat, turn_id=requested["id"],
                                        target_turn=media, media_id=MEDIA_ID, request_id=OWNER_ID,
                                        event="paused", position=.5)
                    assert paused["turns"][-1]["reply"] == f"已暂停《{TITLE}》。"
                    await both(paused)
                    checked("all public paths see transport completion", paused["revision"])

                    saved = await post("remember", text="隔离验收临时事实 QA-82695。")
                    key = saved["memories"][0]["id"]
                    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
                    with moss.db:
                        moss.db.execute("UPDATE memories SET expires_at=? WHERE id=?", (deadline.isoformat(), key))
                    # Give the public cache an ordinary committed revision with the short-lived fact already present.
                    warm = await post("message", text="你好。", chat_id=first_chat)
                    assert warm["memories"] and await get() == warm
                    await both(warm)
                    await asyncio.sleep(max(0, (deadline - datetime.now(timezone.utc)).total_seconds()) + .05)
                    expired = await get()
                    assert expired["revision"] == warm["revision"] and not expired["memories"]
                    assert expired["memory_limits"]["used_bytes"] < warm["memory_limits"]["used_bytes"]
                    async with http.get(url + "/api/events") as reconnect:
                        assert await sse(reconnect.content, "state", revision=expired["revision"]) == expired
                    checked("GET and a reconnect see memory expiry without revision change", expired["revision"])

                    other = await post("new-chat")
                    assert other["chat_id"] != first_chat and not other["turns"]
                    await both(other)
                    restored = await post("switch-chat", id=first_chat)
                    assert restored["chat_id"] == first_chat and restored["turns"] == expired["turns"]
                    await both(restored)
                    checked("both clients follow new chat and switch back without stale turns", restored["revision"])

            # The production database is exclusively locked while running; edit only this isolated stopped profile.
            with closing(sqlite3.connect(data / "session.sqlite3")) as db, db:
                revision, = db.execute("SELECT revision FROM state WHERE id=1").fetchone()
                assert revision == restored["revision"]
                raw, = db.execute("SELECT doc FROM turns WHERE id=?", (media,)).fetchone()
                doc = json.loads(raw)
                doc["reply"] = EXTERNAL_REPLY
                counts = {"complete": True, "by_provider": {"deepseek": 2}}
                db.execute("UPDATE turns SET doc=?, cloud_requests=? WHERE id=?",
                           (json.dumps(doc, ensure_ascii=False), json.dumps(counts), media))

            async with start(data) as (_, url), ClientSession() as http:
                async with http.get(url + "/api/state") as response:
                    after_restart = await response.json()
                    assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
                assert_public(after_restart)
                assert after_restart["revision"] == revision and after_restart["chat_id"] == first_chat
                assert not after_restart["memories"] and after_restart["turns"][0]["reply"] == EXTERNAL_REPLY
                assert after_restart["turns"][0]["cloud_requests"] == {**counts, "total": 2}
                async with http.get(url + "/api/events") as stream:
                    assert await sse(stream.content, "state", revision=revision) == after_restart
                checked("restarted app serves same-revision external edits and request counts", revision)

            prepare_calls = []

            async def record_prepare(_):
                prepare_calls.append(True)

            for enabled in (False, True):
                setting = {"MOSS_SPEECH_PREPARE": "1" if enabled else "0"}
                with patch.dict(os.environ, setting), patch("voice_output.MossSpeech.prepare_imports", record_prepare):
                    async with start(folder / ("prepare-on" if enabled else "prepare-off")) as (_, url), ClientSession() as http:
                        async with http.get(url + "/api/state") as response:
                            state = await response.json()
                            assert state["speech_prepare"] is enabled
                        async with http.post(url + "/api/prepare-speech", json={"unexpected": True}) as response:
                            assert response.status == 400 and not prepare_calls
                        async with http.post(url + "/api/prepare-speech", json={}) as response:
                            assert response.status == 204 and len(prepare_calls) == int(enabled)
                        checked(f"speech readiness matches the advertised switch ({enabled})", state["revision"])
            report["passed"] = True
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
