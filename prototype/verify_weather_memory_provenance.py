"""Exercise weather context after memory changes using isolated HTTP fixtures."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import datetime
import json
import os
import socket
from unittest.mock import patch

from aiohttp import ClientSession, web
from pydantic import SecretStr

from browser_lab import ROOT
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application


OLD = "琥珀城"
NEW = "琉璃城"
EXPLICIT = "杭州市"
POINTS = {OLD: (30.125, 115.375), NEW: (-12.125, 43.375), EXPLICIT: (30.2741, 120.1551)}
SEED = "按我的留档 WX-741 选择城市，查今天天气并给出行建议。"
NEUTRAL = "查看我的便签"
REFERENCE = "刚才那个城市明天的天气呢？"
OPTOUT = "不要参考我的记忆，查今天天气并给出行建议。"
NO_PLACE = "请提供城市、区县或经纬度。"


def fact(city):
    return f"我的留档 WX-741 记录的常用天气城市是{city}。"


def mentions_location(payload, city):
    values = (city, *(str(number) for number in POINTS[city]))
    raw = json.dumps(payload, ensure_ascii=False)
    return any(value in raw for value in values)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def listen(stack, app, port=0):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    stack.push_async_callback(runner.cleanup)
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return f"http://127.0.0.1:{runner.addresses[0][1]}"


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("weather-memory-provenance-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    outcome = folder / "result.json"
    print(outcome, flush=True)
    report = {"passed": False, "checks": [], "weather_calls": [], "model_calls": []}
    pause_lookup = False
    lookup_entered = asyncio.Event()
    release_lookup = asyncio.Event()

    async def weather(city, period, *, location=None, previous=None):
        report["weather_calls"].append({"city": city, "period": period, "location": location, "previous": previous})
        resolved = (location or {}).get("name", city)
        if pause_lookup:
            lookup_entered.set()
            await asyncio.wait_for(release_lookup.wait(), 5)
        if resolved not in POINTS:
            return NO_PLACE, None
        lat, lon = POINTS[resolved]
        row = {"city": resolved, "period": period, "source": "weather-qa", "location_kind": "city",
               "latitude": lat, "longitude": lon, "parent": None, "grid_latitude": lat, "grid_longitude": lon}
        return f"{resolved}（{period}）18 °C。", row

    async def complete(request):
        body = await request.json()
        messages = body["messages"]
        tail = messages[-1]
        tools = [entry["function"]["name"] for entry in body.get("tools", [])]
        system = [entry.get("content") for entry in messages if entry["role"] == "system"]
        places = [city for city in POINTS if mentions_location(messages, city)]
        report["model_calls"].append({"messages": messages, "tools": tools, "locations": places})
        assert body["stream"] and body["model"] == "moss-weather-fixture"
        if tail["role"] == "user" and tail["content"] == SEED:
            city, = [name for name in POINTS if mentions_location(system, name)]
            assert "get_weather" in tools
            arguments = json.dumps({"city": city, "period": "today"}, ensure_ascii=False)
            calls = [{"id": "weather-qa-call", "index": 0, "type": "function",
                      "function": {"name": "get_weather", "arguments": arguments}}]
            delta = {"role": "assistant", "tool_calls": calls}
            finish = "tool_calls"
        else:
            # Echo any location visible to the model so a leak shows up in the reply too.
            reply = tail["content"] if tail["role"] == "tool" else "、".join(places) or NO_PLACE
            delta = {"role": "assistant", "content": reply}
            finish = "stop"
        chunk = {"id": "weather-qa", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        events = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
        return web.Response(text=events, content_type="text/event-stream")

    catalog = folder / "empty-catalog.sqlite3"
    data = folder / "data"
    MediaLibrary(catalog)
    isolated_env = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                    "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                    "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0"}

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, isolated_env))
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    for key in tuple(os.environ):
                        if key.startswith(prefix + "_"):
                            del os.environ[key]
            stack.enter_context(patch("assistant_brain.get_weather", weather))
            model = web.Application()
            model.router.add_post("/v1/chat/completions", complete)
            endpoint = await listen(stack, model)
            moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
            stack.push_async_callback(moss.close)
            moss.cloud["deepseek"] = replace(moss.cloud["deepseek"], url=f"{endpoint}/v1",
                                              secret=SecretStr("weather-qa-only"), model="moss-weather-fixture")
            port = free_port()
            app = application(moss, lambda: "standby", data, port, "weather-qa", asyncio.Event())
            base = await listen(stack, app, port)
            client = await stack.enter_async_context(ClientSession())

            async def submit(action, **body):
                async with client.post(f"{base}/api/{action}", json=body) as response:
                    result = await response.json()
                    assert response.status == 200, (action, response.status, result)
                    return result

            async def ask(text):
                result = await submit("message", text=text)
                turn = result["turns"][-1]
                key = turn["id"]
                async with asyncio.timeout(8):
                    while turn["status"] == "thinking":
                        await asyncio.sleep(.02)
                        async with client.get(f"{base}/api/state") as response:
                            turn = (await response.json())["turns"][-1]
                assert turn["id"] == key and turn["status"] == "replied", (text, turn)
                assert "_memory_versions" not in turn and "_weather_memory_versions" not in turn
                return turn

            async def exercise(name, text):
                wx, llm = len(report["weather_calls"]), len(report["model_calls"])
                turn = await ask(text)
                weather_calls, model_calls = report["weather_calls"][wx:], report["model_calls"][llm:]
                report["checks"].append({"case": name, "text": text, "reply": turn["reply"],
                                         "weather_calls": weather_calls, "model_posts": len(model_calls),
                                         "model_locations": [row["locations"] for row in model_calls]})
                return turn, weather_calls, model_calls

            async def forecast(name, text, city, period, *, model_posts, retired=()):
                turn, lookups, requests = await exercise(name, text)
                assert len(lookups) == 1 and lookups[0]["city"] == city and lookups[0]["period"] == period, lookups
                assert len(requests) == model_posts, requests
                assert not any(mentions_location(row, place) for row in [*lookups, *requests] for place in retired)
                assert turn["weather"]["city"] == city and city in turn["reply"], turn
                assert turn["holo_tools"] == ["get_weather"], turn
                if model_posts:
                    assert all(row["locations"] == [city] for row in requests), requests
                    assert turn["memory_ids"] == [key], turn
                return turn

            async def blocked(name, text, city, *, require_model=False):
                turn, lookups, requests = await exercise(name, text)
                if require_model:
                    assert len(requests) == 1, requests
                assert not any(mentions_location(row, city) for row in [*lookups, *requests]), (lookups, requests)
                assert not lookups and not turn.get("weather") and city not in (turn["reply"] or ""), turn

            state = await submit("select-model", id="deepseek")
            assert not state["turns"] and not state["memories"]
            saved = await submit("remember", text=fact(OLD))
            key = saved["memories"][0]["id"]
            seed = await forecast("mixed cloud turn resolves saved city", SEED, OLD, "today", model_posts=2)
            assert (await ask(NEUTRAL))["source"] == "notes"
            await forecast("non-immediate reference reuses current location locally", REFERENCE, OLD, "tomorrow", model_posts=0)
            old_follow = await forecast("next local follow-up carries the same location", "那后天呢？", OLD,
                                        "day_after_tomorrow", model_posts=0)
            await blocked("opt-out sends no remembered location in a mixed request", OPTOUT, OLD, require_model=True)

            correction = await ask(f"更正记忆：{key} → {fact(NEW)}")
            assert correction["source"] == "memory" and correction["memory_id"] == key
            await blocked("same-ID correction retires older non-immediate location", REFERENCE, OLD)
            await forecast("fresh mixed request resolves corrected city", SEED, NEW, "today", model_posts=2)
            await forecast("referential forecast uses corrected city locally", REFERENCE, NEW, "tomorrow", model_posts=0)
            new_follow = await forecast("bare follow-up uses corrected city locally", "那后天呢？", NEW,
                                        "day_after_tomorrow", model_posts=0)

            forgotten = await submit("forget", id=key)
            assert not forgotten["memories"]
            assert all(any(row["id"] == original["id"] and row["reply"] == original["reply"] for row in forgotten["turns"])
                       for original in [seed, old_follow, new_follow])
            await blocked("deletion retires location from immediate local follow-up", "那今天呢？", NEW)
            await blocked("deletion retires location from older explicit reference", REFERENCE, NEW)
            await blocked("deletion keeps older location out of ordinary model history",
                          "用一句话解释纸张为什么能折叠。", NEW, require_model=True)
            await forecast("naming a city restores local weather after a blocked bare follow-up",
                           f"{EXPLICIT}今天天气", EXPLICIT, "today", model_posts=0, retired=(OLD, NEW))
            await forecast("bare follow-up then uses the explicitly named city", "那明天呢？", EXPLICIT,
                           "tomorrow", model_posts=0, retired=(OLD, NEW))

            await submit("new-chat")
            saved = await submit("remember", text=fact(OLD))
            key = saved["memories"][0]["id"]
            await forecast("mixed forecast establishes a separate valid remembered city", SEED, OLD, "today", model_posts=2)
            await forecast("full city name replaces active memory-derived forecast locally",
                           f"{EXPLICIT}今天天气", EXPLICIT, "today", model_posts=0)
            forgotten = await submit("forget", id=key)
            assert not forgotten["memories"]
            await forecast("explicit city remains valid after deleting the separate remembered city", "那明天呢？",
                           EXPLICIT, "tomorrow", model_posts=0, retired=(OLD,))
            await submit("new-chat")
            saved = await submit("remember", text=fact(OLD))
            key = saved["memories"][0]["id"]
            await forecast("seed source before provider response race", SEED, OLD, "today", model_posts=2)
            requests = len(report["model_calls"])
            pause_lookup = True
            in_flight = asyncio.create_task(ask(REFERENCE))
            try:
                await asyncio.wait_for(lookup_entered.wait(), 3)
                forgotten = await submit("forget", id=key)
                assert not forgotten["memories"] and forgotten["turns"][-1]["status"] == "thinking"
            finally:
                release_lookup.set()
                raced = await in_flight
            assert not raced.get("weather") and OLD not in raced["reply"] and not raced["cloud_requests"]["total"]
            assert len(report["model_calls"]) == requests
            report["checks"].append({"case": "response from weather already in flight is discarded after deletion", "reply": raced["reply"]})
            report["passed"] = True
    finally:
        outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(outcome, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
