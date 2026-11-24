import argparse
import asyncio
from datetime import datetime
import json
import os
from time import perf_counter, time_ns
from unittest.mock import patch
from uuid import uuid4

from aiohttp import web

from browser_lab import BrowserServer, ROOT
from media_transport import parse_transport
from model_catalog import SPECS
from verify_context_reply import free_port
from verify_music_delegation import catalog_at


CASES = (
    ("暂停一下音乐", "pause"),
    ("帮我暂停一下音乐", "pause"),
    ("先暂停下这首歌", "pause"),
    ("继续播放这首", "resume"),
    ("接着放这首", "resume"),
    ("把这首暂停一下", "pause"),
    ("莫斯，暂停音乐", "pause"),
    ("moss、继续播放这首歌", "resume"),
)
GUARDED = (
    "不要暂停一下音乐", "暂停一下音乐然后锁屏", "我说的是‘暂停一下音乐’", "刚才暂停音乐了吗？",
    "可以暂停一下音乐吗？", "莫斯，别暂停一下音乐", "继续播放下一首", "把这首删掉",
    "暂停这首歌并且静音", "暂停一下舞台视频然后继续音乐",
)


async def run(baseline):
    folder = ROOT / "runs" / datetime.now().strftime("transport-wording-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    report = {"passed": False, "baseline": baseline, "cases": [], "model_requests": [], "uses_page_snapshot_fixture": True}
    print(folder, flush=True)

    async def probe(request):
        report["model_requests"].append(request.path)
        return web.Response(status=503)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", probe)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        model_url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1"
        catalog = catalog_at(folder, actual_index=False)
        settings = {"MOSS_WINDOWS_REMINDERS":"0", "MOSS_MEDIA_DB":str(catalog), "MOSS_MEM0_ENABLED":"0",
                    "MOSS_PROVIDER_DIR":str(folder / "providers"), "LOCALAPPDATA":str(folder / "profile"),
                    "MOSS_SPEECH_STANDBY":"0", "MOSS_SPEECH_MIC_WARM":"0"}
        for _, prefixes, _, _ in SPECS.values():
            for prefix in prefixes:
                settings[prefix + "_BASE_URL"] = model_url
                settings[prefix + "_API_KEY"] = "isolated-qa"
        with patch.dict(os.environ, settings):
            server = BrowserServer(folder / "server", free_port(), model_url=model_url)
            try:
                await server.start()
                await verify(server, report)
            finally:
                await server.stop()
        report["passed"] = True
    finally:
        await runner.cleanup()
        path = folder / "result.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(path, report["passed"], flush=True)


async def verify(server, report):
    baseline = report["baseline"]
    assert all(parse_transport(text, True, stoppable=True) is None for text in GUARDED)
    assert all(parse_transport(text, True, stoppable=True, explicit=True) is None for text in GUARDED)
    report["negative_parser_phrases"] = list(GUARDED)

    async def latest():
        async with asyncio.timeout(12):
            while True:
                turn = (await server.state())["turns"][-1]
                if turn["status"] not in {"thinking", "saving"}:
                    return turn
                await asyncio.sleep(.025)

    async def origin():
        assert (await server.post("/api/new-chat", {}))[0] == 200
        assert (await server.post("/api/message", {"text":"来一首歌"}))[0] == 200
        turn = await latest()
        assert turn["status"] == "replied" and turn["source"] == "media" and turn["media"]["autoplay"], turn
        selected = turn["media"]["selected"]
        media = {"chat_id":turn["chat_id"], "turn_id":turn["id"], "media_id":selected["media_id"]}
        assert (await server.post("/api/media-event", {**media, "event":"started", "play_id":uuid4().hex,
                                                       "started_at_us":time_ns() // 1000}))[0] == 200
        return media, selected

    async def ask(media, text, player_state):
        request = {"text":text, "chat_id":media["chat_id"], "transport_request_id":uuid4().hex,
                   "player":{key:media[key] for key in ("turn_id", "media_id")} | {"state":player_state}}
        start = perf_counter()
        code, state = await server.post("/api/message", request)
        return request["transport_request_id"], code, state, round((perf_counter() - start) * 1000, 1)

    async def finish(media, item, turn, request, action):
        instruction = turn["transport"]
        assert turn["status"] == "player_requested" and not turn["reply"], turn
        assert (instruction["request_id"], instruction["action"], instruction["target_turn"], instruction["media_id"]) == (
            request, action, media["turn_id"], media["media_id"]), instruction
        event = "paused" if action == "pause" else "playing"
        code, state = await server.post("/api/transport-result", {**media, "turn_id":turn["id"], "request_id":request,
                                          "target_turn":media["turn_id"], "event":event, "position":1.25})
        saved = state["turns"][-1]
        assert code == 200 and saved["id"] == turn["id"] and saved["status"] == "replied", state
        assert saved["transport"]["event"] == event and item["title"] in saved["reply"], saved
        return event

    for text, action in CASES:
        assert parse_transport(text, True, stoppable=True, explicit=True) == (None if baseline else (action, "audio"))
        media, selection = await origin()
        before_model = len(report["model_requests"])
        request, code, state, elapsed = await ask(media, text, "playing" if action == "pause" else "paused")
        assert code == 200, state
        turn = await latest()
        assert turn["id"] == state["turns"][-1]["id"]
        outcome = {"text":text, "request_ms":elapsed, "source":turn.get("source"), "status":turn["status"],
                   "model_requests":len(report["model_requests"]) - before_model}
        if baseline:
            assert turn.get("source") != "transport" and not turn.get("transport"), turn
            outcome["autoplay"] = bool((turn.get("media") or {}).get("autoplay"))
        else:
            assert turn["source"] == "transport" and not outcome["model_requests"], turn
            outcome["test_receipt"] = await finish(media, selection, state["turns"][-1], request, action)
        report["cases"].append(outcome)

    media, selection = await origin()
    code, state = await server.post("/api/message", {"text":"明天上午10点到11点安排日程：回归夹具"})
    pending = state["turns"][-1]
    assert code == 200 and pending["status"] == "pending", state
    request, code, state, elapsed = await ask(media, CASES[0][0], "playing")
    if baseline:
        assert code == 409 and "当前请求" in state["error"], state
        report["during_confirmation"] = {"http_status":code, "error":state["error"]}
    else:
        assert code == 200, state
        event = await finish(media, selection, state["turns"][-1], request, "pause")
        assert (await server.state())["turns"][-2] == pending
        report["during_confirmation"] = {"http_status":code, "request_ms":elapsed, "test_receipt":event, "preview_unchanged":True}
        assert not report["model_requests"], report["model_requests"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    asyncio.run(run(parser.parse_args().baseline))
