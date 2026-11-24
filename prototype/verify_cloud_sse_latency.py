import asyncio
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from statistics import median
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from pydantic import SecretStr

from assistant_brain import ONE_ACTION_GUIDANCE, SYSTEM
from browser_lab import ROOT, BrowserServer
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import CloudHTTPClient, Profile, SPECS
from serve import application
from verify_context_reply import free_port
from verify_chat_providers import PROMPTS, limited_server


async def next_state(stream):
    async for line in stream.content:
        if line.startswith(b"data: "):
            received = perf_counter()
            return received, json.loads(line.removeprefix(b"data: "))
    raise RuntimeError("本机 SSE 连接提前结束")


async def measure(http, server, prompt):
    code, blank = await server.post("/api/new-chat", {})
    assert code == 200 and not blank["turns"] and not blank["memories"]
    async with http.get(server.url + "/api/events") as stream:
        stream.raise_for_status()
        _, ready = await next_state(stream)
        assert ready["chat_id"] == blank["chat_id"] and not ready["turns"]
        start = perf_counter()
        async def post():
            async with http.post(server.url + "/api/message", json={"text":prompt}) as response:
                response.raise_for_status()
                return await response.json()
        writing = asyncio.create_task(post())
        first, complete, partials = None, None, []
        try:
            async with asyncio.timeout(130):
                while complete is None:
                    received, state = await next_state(stream)
                    if not state["turns"]:
                        continue
                    turn, = state["turns"]
                    assert state["chat_id"] == blank["chat_id"] and turn["user"] == prompt
                    assert not turn.get("error") and not turn.get("memory_ids") and not turn.get("holo_tools")
                    final = turn["status"] == "replied"
                    text = (turn.get("reply" if final else "partial_reply") or "").strip()
                    if text:
                        elapsed = round((received-start)*1000)
                        if first is None:
                            first = elapsed
                        if final:
                            complete = elapsed
                        elif not partials or text != partials[-1]:
                            partials.append(text)
            sent = await writing
            assert sent["chat_id"] == state["chat_id"] and turn["model_id"] == "deepseek" and turn["source"] == "holo"
            return {"prompt":prompt, "first_text_ms":first, "final_text_ms":complete,
                    "distinct_partial_updates":len(partials), "first_text":partials[0] if partials else text,
                    "reply":text}
        finally:
            if not writing.done():
                writing.cancel()
                await asyncio.gather(writing, return_exceptions=True)


LOOPBACK_PREVIEW_CASES = (
    ("我今天有点累，只想找个人聊聊。", "我在，慢慢说。", "stable"),
    ("我打算周末换个环境，能陪我聊聊吗？", "可以，周末先留点空。", "stable"),
    ("我想找个人聊聊。", "让我想一想。", "changes_to_tool"),
    ("现在几点了，我想按上午或下午安排工作。", "现在是验收时刻甲。", "tool_intent"),
    ("我想休息一会儿，你陪我坐坐吧。", "好，我陪你坐会儿。", "unsupported"),
)


class LoopbackPreview:
    model = "fixture-deepseek"
    tools = {"get_time", "get_weather", "list_capabilities"}
    tail = "我听着呢。"
    tool_answer = "已按本机时钟回答。"
    unsupported_answer = "这次没有查到已核实的本地曲目。"
    stable_gap = .7

    def __init__(self, prompt, first, mode):
        self.prompt, self.first, self.mode = prompt, first, mode
        self.with_tool = mode in {"changes_to_tool", "tool_intent"}
        self.wrote_first = asyncio.Event()
        self.wrote_first_at = None
        self.advance = asyncio.Event()
        self.finish = asyncio.Event()
        self.requests = []

    @staticmethod
    def chunk(delta, finish=None):
        payload = {"id":"preview-fixture", "object":"chat.completion.chunk", "model":LoopbackPreview.model,
                   "created":1, "choices":[{"index":0, "delta":delta, "finish_reason":finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    async def completion(self, request):
        body = await request.json()
        assert body["model"] == self.model and body["stream"] is True and body["tool_choice"] == "auto", body
        assert body.get("thinking") == {"type":"disabled"}, body
        assert {tool["function"]["name"] for tool in body["tools"]} == self.tools, body
        last = body["messages"][-1]
        self.requests.append(last["role"])
        response = web.StreamResponse(headers={"Content-Type":"text/event-stream"})
        await response.prepare(request)
        if last["role"] == "tool":
            assert self.with_tool and last["tool_call_id"] == "preview-clock" and last["content"], last
            await response.write(self.chunk({"role":"assistant", "content":self.tool_answer}, "stop"))
        else:
            assert last == {"role":"user", "content":self.prompt}, last
            await response.write(self.chunk({"role":"assistant", "content":self.first}))
            self.wrote_first_at = perf_counter()
            self.wrote_first.set()
            await self.advance.wait()
            if self.mode == "stable":
                await asyncio.sleep(max(0, self.wrote_first_at + self.stable_gap - perf_counter()))
                await response.write(self.chunk({"content":self.tail}, "stop"))
            elif self.with_tool:
                call = {"id":"preview-clock", "index":0, "type":"function",
                        "function":{"name":"get_time", "arguments":"{}"}}
                await response.write(self.chunk({"tool_calls":[call]}))
                await self.finish.wait()
                await response.write(self.chunk({}, "tool_calls"))
            else:
                await response.write(self.chunk({"content":"已为你找到《虚构曲目》。"}))
                await self.finish.wait()
                await response.write(self.chunk({}, "stop"))
        await response.write(b"data: [DONE]\n\n")
        return response


async def measure_loopback_preview(http, url, case, baseline):
    async with http.post(url + "/api/new-chat", json={}) as response:
        response.raise_for_status()
        chat = (await response.json())["chat_id"]
    async with http.get(url + "/api/events") as stream:
        stream.raise_for_status()
        _, ready = await next_state(stream)
        assert ready["chat_id"] == chat and not ready["turns"]
        start = perf_counter()
        expected = not baseline and case.mode != "tool_intent"

        async def state_when(condition):
            while True:
                stamp, state = await next_state(stream)
                if state["turns"]:
                    turn, = state["turns"]
                    assert turn["user"] == case.prompt and turn["status"] != "error", turn
                    assert "虚构曲目" not in (turn.get("partial_reply") or ""), turn
                    assert expected or not turn.get("partial_reply"), turn
                    if condition(turn):
                        return stamp, turn

        async with http.post(url + "/api/message", json={"text":case.prompt, "chat_id":chat}) as response:
            response.raise_for_status()
            await response.read()
        try:
            await asyncio.wait_for(case.wrote_first.wait(), 5)
            try:
                first, row = await asyncio.wait_for(state_when(lambda row: row.get("partial_reply")), 2 if expected else .25)
            except TimeoutError:
                assert not expected, f"模型已写首段，但 SSE 仍无预览：{case.prompt}"
                first = None
            else:
                assert expected and row["partial_reply"] == case.first and row["status"] == "thinking", row
            case.advance.set()
            cleared = None
            if case.mode in {"changes_to_tool", "unsupported"} and expected:
                cleared, row = await asyncio.wait_for(state_when(lambda row: not row.get("partial_reply")), 2)
                assert row["status"] == "thinking" and not row.get("holo_tools") and case.requests == ["user"], row
            case.finish.set()
            complete, row = await asyncio.wait_for(state_when(lambda row: row["status"] == "replied"), 5)
        finally:
            case.advance.set()
            case.finish.set()
        assert row["model_id"] == "deepseek" and row["source"] == "holo", row
        assert (row.get("holo_tools") or []) == (["get_time"] if case.with_tool else []), row
        assert row["cloud_requests"]["total"] == (2 if case.with_tool else 1), row
        assert case.requests == (["user", "tool"] if case.with_tool else ["user"]), case.requests
        assert first is None or first < complete, (first, complete)
        answer = case.tool_answer if case.with_tool else case.first + case.tail
        if case.mode == "unsupported":
            answer = case.unsupported_answer
        assert row["reply"] == answer, row
        return {"prompt":case.prompt, "case":case.mode, "first_ms":round((first-start)*1000, 2) if first else None,
                "cleared_ms":round((cleared-start)*1000, 2) if cleared else None,
                "upstream_first_ms":round((case.wrote_first_at-start)*1000, 2),
                "upstream_to_sse_ms":round((first-case.wrote_first_at)*1000, 2) if first else None,
                "complete_ms":round((complete-start)*1000, 2), "reply":row["reply"], "requests":case.requests}


async def loopback_previews(baseline):
    folder = ROOT / "runs" / datetime.now().strftime("chat-loopback-preview-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder / "result.json", flush=True)
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    report = {"passed":False, "baseline":baseline, "stable_upstream_gap_ms":LoopbackPreview.stable_gap * 1000, "measurements":[]}
    case = None

    async def completion(request):
        return await case.completion(request)

    mock = web.Application()
    mock.router.add_post("/v1/chat/completions", completion)
    model_runner = web.AppRunner(mock, shutdown_timeout=1)
    await model_runner.setup()
    await web.TCPSite(model_runner, "127.0.0.1", 0).start()
    model_url = f"http://127.0.0.1:{model_runner.addresses[0][1]}/v1"
    profiles = {key:Profile(key, label, model_url, model, SecretStr(""))
                for key, (label, _, _, model) in SPECS.items()}
    profiles["deepseek"] = Profile("deepseek", "DeepSeek fixture", model_url, LoopbackPreview.model, SecretStr("fixture"))
    env = {"MOSS_MEDIA_DB":str(catalog), "MOSS_WINDOWS_REMINDERS":"0", "MOSS_SPEECH_PREPARE":"0"}
    try:
        with patch("core.profiles", return_value=profiles), patch.dict(os.environ, env):
            data, port = folder / "data", free_port()
            moss = Moss(data, "http://127.0.0.1:9/v1", "unused", prefer_cloud=True)
            runner = web.AppRunner(application(moss, lambda:"standby", data, port, "isolated", asyncio.Event()),
                                   access_log=None, shutdown_timeout=1)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
                    for values in LOOPBACK_PREVIEW_CASES:
                        case = LoopbackPreview(*values)
                        row = await measure_loopback_preview(http, f"http://127.0.0.1:{port}", case, baseline)
                        report["measurements"].append(row)
                report["passed"] = True
            finally:
                await runner.cleanup()
                await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        await model_runner.cleanup()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(cases_path=None):
    prompts = PROMPTS
    action_guidance_cases = set()
    if cases_path:
        fixture = json.loads(cases_path.read_text(encoding="utf-8"))
        prompts = tuple(item["prompt"] for item in fixture["cases"])
        action_guidance_cases = {i for i, item in enumerate(fixture["cases"]) if item.get("one_action") is True}
        assert len(prompts) == 3
    folder = ROOT / "runs" / datetime.now().strftime("chat-provider-sse-%Y%m%d-%H%M%S-%f")
    with socket.socket() as free:
        free.bind(("127.0.0.1",0))
        port = free.getsockname()[1]
    server = BrowserServer(folder, port)
    spend = []
    client_for = Profile.client
    def guarded(profile):
        client = client_for(profile)
        assert profile.id == "deepseek" and isinstance(client._client, CloudHTTPClient)
        async def inspect(request):
            index = len(spend)
            if index >= len(prompts):
                raise RuntimeError("已发送本次三题")
            data = json.loads(request.content)
            assert request.method == "POST" and request.url.path.endswith("/chat/completions")
            focused = index in action_guidance_cases
            system = SYSTEM + ("\n" + ONE_ACTION_GUIDANCE if focused else "")
            assert data["messages"] == [{"role":"system", "content":system}, {"role":"user", "content":prompts[index]}]
            assert not focused or not data.get("tools")
            assert data["stream"] is True
            spend.append({"prompt_index":index+1, "model":data["model"], "static_system_only":True,
                          "one_action_guidance":focused, "tool_schema_count":len(data.get("tools") or []),
                          "user_messages":1, "history_messages":0})
        client._client.event_hooks["request"].append(inspect)
        return client
    Profile.client = guarded
    args = SimpleNamespace(port=port, paid_cap=3, resume_server=False, model_url="http://127.0.0.1:9/v1")
    serving = asyncio.create_task(limited_server(args, folder))
    report = {"passed":False, "start":datetime.now().astimezone().isoformat(), "warmup_requests":0,
              "measurements":[], "outbound":spend, "method":"计时始于本机发送 /api/message 之前，止于 /api/events 客户端读到非空首段或完整回复。"}
    owner = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=135)) as http:
            async with asyncio.timeout(30):
                while not (folder/"data/instance.json").exists():
                    if serving.done():
                        await serving
                    await asyncio.sleep(.04)
            owner = json.loads((folder/"data/instance.json").read_text(encoding="utf-8"))
            assert owner["port"] == port
            async with http.get(server.url+"/api/health") as response:
                response.raise_for_status()
                assert (await response.json())["pid"] == owner["pid"]
            code, state = await server.post("/api/select-model", {"id":"deepseek"})
            assert code == 200 and state["model_id"] == "deepseek"
            report["model"] = state["model"]
            if cases_path:
                report["fixture_sha256"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()
                report["criteria"] = fixture
            for prompt in prompts:
                row = await measure(http, server, prompt)
                report["measurements"].append(row)
                print(row["first_text_ms"], row["final_text_ms"], flush=True)
            report["paid_guard"] = json.loads((folder/"paid-limit.json").read_text(encoding="utf-8"))
            assert len(spend) == 3 and report["paid_guard"] == {"allowed":3, "outbound":3}
            report["median_ms"] = {k: median(r[k] for r in report["measurements"])
                                   for k in ("first_text_ms","final_text_ms")}
            if cases_path:
                for row in report["measurements"]:
                    row["sentence_count"] = len([s for s in re.split(r"[。!?！？]+", row["reply"]) if s.strip()])
                report["quality_review"] = "需按预先保存的语义标准逐题核对，句数仅记录格式。"
            report["passed"] = True
    finally:
        if owner and not serving.done():
            await server.post("/api/shutdown", {"token":owner["token"]})
        await serving
        Profile.client = client_for
        report["end"] = datetime.now().astimezone().isoformat()
        (folder/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--concise-cases", type=Path)
    modes.add_argument("--loopback-preview", choices=("baseline", "enabled"))
    args = parser.parse_args()
    if args.loopback_preview:
        asyncio.run(loopback_previews(args.loopback_preview == "baseline"))
    else:
        asyncio.run(main(args.concise_cases))
