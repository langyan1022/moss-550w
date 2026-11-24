"""Verify memory-derived chat history through the application HTTP/SSE boundary."""

import asyncio
from datetime import datetime
import hashlib
import json
import os
import socket
from unittest.mock import patch

from aiohttp import ClientSession, web

from browser_lab import BrowserServer, ROOT
from local_media.library import MediaLibrary
from memory_store import local_recall_only, other_person_query, select_memories


OLD = "QA-37291"
NEW = "QA-82645"
ANCHOR = "春雨轻轻落在窗上。"
OTHER = "本次没有使用档案代号。"
DIRECT = "我的档案试验代号是什么？请用一句话回答。"
FOLLOWUPS = ("把刚才那句话改成疑问句。", "换一种句式，保留原意。")
OPTOUT = "不要参考我的记忆，请把刚才的话改为感叹句。"
GENERAL = "用一句话解释为什么纸张可以折叠。"
DRAFT = "帮我保存一份本地草稿，沿用刚才那句话的内容。"
TOOL_TURN = "请结合刚才那句话，补上此刻的本机时间。"
SELF_SIGN = "山杏签名"
LIN_SIGN = "墨桥签名"
YAO_SIGN = "木林签名"
EDITED_SIGN = "柳池签名"
PERSON_MARKERS = (SELF_SIGN, LIN_SIGN, YAO_SIGN, EDITED_SIGN)
PERSON_SELF = "用一句话说说，我的电子邮件签名平时怎么落款？"
PERSON_COLLEAGUE = "同事的电子邮件签名是什么？"
PERSON_LIN = "请问我同事小林的电子邮件签名是什么？"
PERSON_UNKNOWN = "同事小白的电子邮件签名是什么？"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("memory-history-provenance-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "checks": [], "calls": [], "app_pids": []}
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    tool_stream_started = asyncio.Event()
    release_tool_stream = asyncio.Event()

    def check(name, passed, **details):
        report["checks"].append({"case": name, "passed": bool(passed), **details})

    def event_chunk(model, delta, finish):
        chunk = {"id": "memory-qa", "object": "chat.completion.chunk", "created": 0, "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")

    async def complete(request):
        body = await request.json()
        messages = body["messages"]
        assert body["model"] == "moss-memory-fixture"
        stream = body.get("stream", False)
        query = messages[-1].get("content")
        previous = messages[:-1]
        system = "\n".join(msg.get("content") or "" for msg in previous if msg["role"] in ("system", "developer"))
        history = "\n".join(msg.get("content") or "" for msg in previous if msg["role"] in ("user", "assistant"))
        markers = [value for value in (OLD, NEW) if value in system or value in history]
        wire = json.dumps(messages, ensure_ascii=False)
        report["calls"].append({"query": query, "stream": stream, "last_role": messages[-1]["role"],
                                "payload_markers": [v for v in (OLD, NEW) if v in wire],
                                "system_markers": [v for v in (OLD, NEW) if v in system],
                                "history_markers": [v for v in (OLD, NEW) if v in history],
                                "person_system_markers": [v for v in PERSON_MARKERS if v in system],
                                "person_history_markers": [v for v in PERSON_MARKERS if v in history],
                                "neutral_history_present": ANCHOR in history})
        if query == TOOL_TURN:
            assert stream and any(tool["function"]["name"] == "get_time" for tool in body["tools"]), body
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            tool = {"index": 0, "id": "memory-qa-clock", "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"}}
            await response.write(event_chunk(body["model"], {"role": "assistant", "tool_calls": [tool]}, None))
            # Hold the end of the completion so dispatch starts only after the panel action commits.
            tool_stream_started.set()
            async with asyncio.timeout(12):
                await release_tool_stream.wait()
            await response.write(event_chunk(body["model"], {}, "tool_calls") + b"data: [DONE]\n\n")
            return response
        if not stream:
            response = {"id": "memory-qa-draft", "object": "chat.completion", "model": body["model"], "created": 0}
            if query == DRAFT:
                assert any(tool["function"]["name"] == "save_draft" for tool in body.get("tools", []))
                assert markers, "Draft needs to derive its contents from conversational history"
                preview = f"档案试验代号是 {markers[-1]} 吗？"
                tool = {"id": "memory-qa-save", "type": "function", "function": {"name": "save_draft",
                        "arguments": json.dumps({"text": preview}, ensure_ascii=False)}}
                message = {"role": "assistant", "content": None, "tool_calls": [tool]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "已保存草稿。"}
                finish = "stop"
            return web.json_response({**response, "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
        if query == "写一句关于春雨的描述。":
            reply = ANCHOR
        elif query in (PERSON_SELF, PERSON_COLLEAGUE, PERSON_LIN, PERSON_UNKNOWN):
            reply = "、".join(value for value in PERSON_MARKERS if value in wire) or "这一轮没有对应的签名记录。"
        elif query in FOLLOWUPS and markers:
            reply = (f"档案试验代号是 {markers[-1]} 吗？" if query == FOLLOWUPS[0]
                     else f"用 {markers[-1]} 作为档案试验代号。")
        else:
            reply = f"档案试验代号是 {markers[-1]}。" if markers else OTHER
        event = event_chunk(body["model"], {"role": "assistant", "content": reply}, "stop")
        return web.Response(body=event + b"data: [DONE]\n\n", content_type="text/event-stream")

    model = web.Application()
    model.router.add_post("/v1/chat/completions", complete)
    runner = web.AppRunner(model)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    server = BrowserServer(folder, free_port(), model_url=f"http://127.0.0.1:{runner.addresses[0][1]}/v1",
                           model="moss-memory-fixture", write_delay=2, draft_model_reply=True)
    environment = {"LOCALAPPDATA": str(folder / "profile"), "MOSS_PROVIDER_DIR": str(folder / "providers"),
                   "MOSS_MEDIA_DB": str(catalog), "MOSS_MEM0_ENABLED": "0", "MOSS_WINDOWS_REMINDERS": "0",
                   "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0",
                   "PYTHONDONTWRITEBYTECODE": "1"}

    async def start():
        with patch.dict(os.environ, environment):
            await server.start()
        report["app_pids"].append(server.pid)

    async def post(action, **payload):
        code, snapshot = await server.post("/api/" + action, payload)
        assert code == 200, (action, code, snapshot.get("error"))
        return snapshot

    async def act(action, *, after_submit=None, **payload):
        # Observe the status delivered to existing pages rather than only polling SQLite.
        async with ClientSession() as client:
            async with client.get(server.url + "/api/events") as response:
                assert response.status == 200 and response.content_type == "text/event-stream", response.status
                snapshot = await post(action, **payload)
                turn_id = payload.get("turn_id") or snapshot["turns"][-1]["id"]
                if after_submit:
                    await after_submit(snapshot)
                transient = {"pending", "saving"} if action == "approve" else {"thinking"}
                async with asyncio.timeout(15):
                    async for line in response.content:
                        if not line.startswith(b"data: "):
                            continue
                        view = json.loads(line[6:])
                        match = next((turn for turn in view["turns"] if turn["id"] == turn_id), None)
                        if match and match["status"] not in transient:
                            assert "_memory_versions" not in match, match
                            return match
                raise RuntimeError("SSE closed before reporting the submitted turn")

    async def say(text):
        return await act("message", text=text)

    async def ask(text):
        before = len(report["calls"])
        turn = await say(text)
        assert len(report["calls"]) == before + 1 and turn["status"] == "replied", (text, turn)
        assert turn["source"] == "holo", turn
        call = report["calls"][-1]
        assert call["query"] == text and call["stream"], call
        return turn, call

    def excludes(call):
        return not call["payload_markers"]

    def check_draft_receipt(name, saved_draft, draft, before):
        receipt = saved_draft.get("receipt")
        assert saved_draft["status"] == "replied" and receipt, saved_draft
        data = (server.data / "drafts" / receipt["filename"]).read_bytes()
        check(name, len(report["calls"]) == before and saved_draft["reply_source"] == "receipt"
              and receipt["text"] == draft["preview"] and data == draft["preview"].encode("utf-8")
              and hashlib.sha256(data).hexdigest() == receipt["sha256"], model_posts=len(report["calls"]) - before)

    try:
        await start()
        state = await server.state()
        first_chat = state["chat_id"]
        assert state["model_id"] == "local" and not state["turns"] and not state["memories"]
        saved = await post("remember", text=f"我的档案试验代号是 {OLD}。")
        fact, = saved["memories"]
        key = fact["id"]
        assert all(not select_memories(text, [fact]) for text in FOLLOWUPS)
        await ask("写一句关于春雨的描述。")
        direct, call = await ask(DIRECT)
        check("direct recall supplies the stored fact", direct.get("memory_ids") == [key] and call["system_markers"] == [OLD])
        derived = []
        for text in FOLLOWUPS:
            turn, call = await ask(text)
            derived.append(turn)
            check("follow-up uses previous text only", OLD in turn["reply"] and call["history_markers"] == [OLD]
                  and not call["system_markers"] and not turn.get("memory_ids"), turn=turn["id"])

        second_chat = (await post("new-chat"))["chat_id"]
        assert second_chat != first_chat
        turn, call = await ask(GENERAL)
        check("different chat excludes old conversational history", excludes(call) and not call["neutral_history_present"])
        turn, call = await ask(DIRECT)
        check("different chat can deliberately query the shared memory", call["system_markers"] == [OLD]
              and turn.get("memory_ids") == [key])
        await post("switch-chat", id=first_chat)

        turn, call = await ask(OPTOUT)
        check("opt-out excludes direct and derived memory, preserves unrelated context",
              excludes(call) and call["neutral_history_present"] and turn["reply"] == OTHER)
        revised = await say(f"更正记忆：{key} → 我的档案试验代号是 {NEW}。")
        assert revised["source"] == "memory" and revised["memory_id"] == key
        turn, call = await ask(GENERAL)
        check("same-ID correction excludes old history", excludes(call) and turn["reply"] == OTHER)
        newest, call = await ask(DIRECT)
        check("same-ID correction restores explicitly requested new fact", call["system_markers"] == [NEW]
              and OLD not in call["history_markers"] and OLD not in newest["reply"]
              and NEW in newest["reply"] and newest.get("memory_ids") == [key])
        follow, call = await ask(FOLLOWUPS[0])
        check("new version can be echoed through conversation only",
              not call["system_markers"] and call["history_markers"] == [NEW] and NEW in follow["reply"])

        before = len(report["calls"])
        draft = await say(DRAFT)
        assert draft["status"] == "pending" and len(report["calls"]) == before + 1, draft
        call = report["calls"][-1]
        check("pending draft uses memory only through conversational history",
              call["query"] == DRAFT and not call["stream"] and not call["system_markers"]
              and call["history_markers"] == [NEW] and NEW in draft["preview"] and not draft.get("memory_ids"))
        await post("forget", id=key)
        before = len(report["calls"])
        saved_draft = await act("approve", turn_id=draft["id"])
        check_draft_receipt("approving a pending draft after deletion writes the reviewed text with zero model posts",
                            saved_draft, draft, before)
        turn, call = await ask("请把刚才的话换成一句更短的。")
        check("deletion excludes the revised fact and derived text", excludes(call) and turn["reply"] == OTHER)
        await server.stop()
        await start()
        state = await server.state()
        check("restart keeps the active chat and forgotten memory stays absent",
              state["chat_id"] == first_chat and not state["memories"] and len(set(report["app_pids"])) == 2)
        all_turns = {turn["id"]: turn for turn in state["turns"]}
        check("old transcript is still visible",
              all(all_turns[item["id"]]["reply"] == item["reply"]
                  for item in [direct, *derived, newest, follow]))
        turn, call = await ask(DIRECT)
        check("restart does not re-send forgotten context", excludes(call) and turn["reply"] == OTHER)

        await post("new-chat")
        saved = await post("remember", text=f"我的档案试验代号是 {NEW}。")
        tool_key = saved["memories"][0]["id"]
        await ask(DIRECT)
        # Check that an unchanged memory normally travels through the real nonterminal clock path twice.
        release_tool_stream.set()
        before = len(report["calls"])
        normal = await say(TOOL_TURN)
        first, after_tool = report["calls"][before:]
        check("valid memory remains available through a clock tool completion",
              normal["status"] == "replied" and after_tool["last_role"] == "tool"
              and first["payload_markers"] == after_tool["payload_markers"] == [NEW])

        tool_stream_started.clear()
        release_tool_stream.clear()
        before = len(report["calls"])
        async with asyncio.TaskGroup() as group:
            running = group.create_task(say(TOOL_TURN))
            try:
                async with asyncio.timeout(8):
                    await tool_stream_started.wait()
                    assert report["calls"][before]["payload_markers"] == [NEW]
                    forgotten = await post("forget", id=tool_key)
                    assert not forgotten["memories"] and len(report["calls"]) == before + 1
            finally:
                release_tool_stream.set()
        followup_posts = report["calls"][before + 1:]
        check("deletion during a streaming clock turn blocks stale data in the automatic second completion",
              all(excludes(call) for call in followup_posts) and NEW not in (running.result().get("reply") or ""),
              second_posts=len(followup_posts), status=running.result()["status"])

        await post("new-chat")
        saved = await post("remember", text=f"我的档案试验代号是 {NEW}。")
        race_key = saved["memories"][0]["id"]
        await ask(DIRECT)
        draft = await say(DRAFT)
        assert draft["status"] == "pending" and NEW in draft["preview"], draft
        before = len(report["calls"])

        async def revoke_during_write(approved):
            row = approved["turns"][-1]
            assert row["id"] == draft["id"] and row["status"] == "saving" and row["approval_model"], row
            async with asyncio.timeout(8):
                while not row.get("tool_started_at"):
                    await asyncio.sleep(.02)
                    row = (await server.state())["turns"][-1]
                assert row["status"] == "saving" and not row["receipt"], row
                forgotten = await post("forget", id=race_key)
                row = forgotten["turns"][-1]
                assert not forgotten["memories"] and row["status"] == "saving" and not row["receipt"], row
                assert len(report["calls"]) == before

        saved_draft = await act("approve", turn_id=draft["id"], after_submit=revoke_during_write)
        check_draft_receipt("deletion during approved write preserves the receipt and blocks the model follow-up",
                            saved_draft, draft, before)

        await post("new-chat")
        machine = {"id":"client-machine", "text":"客户机的操作系统是 Debian。"}
        check("technical compounds and colleague mentions do not identify someone else's fact",
              all(not other_person_query(text) for text in ("客户机的操作系统是什么？", "客户端的设置是什么？",
                                                              "客户服务器怎样配置？", "同事推荐我买电脑"))
              and select_memories("客户机的操作系统是什么？", [machine]) == [machine]
              and other_person_query("同事写电子邮件怎么署名？")
              and other_person_query("请帮我给同事推荐一种咖啡。"))
        self_saved = await say(f"记住：我的电子邮件签名是{SELF_SIGN}，给同事发邮件也会使用。")
        self_key = self_saved["memory_id"]
        await ask("写一句关于春雨的描述。")
        original, call = await ask(PERSON_SELF)
        check("personal answer really receives and uses its own saved fact",
              call["person_system_markers"] == [SELF_SIGN] and original["reply"] == SELF_SIGN
              and original.get("memory_ids") == [self_key])
        turn, call = await ask(PERSON_COLLEAGUE)
        check("colleague question excludes both self memory and its earlier model answer",
              not call["person_system_markers"] and not call["person_history_markers"]
              and call["neutral_history_present"] and not turn.get("memory_ids")
              and all(value not in turn["reply"] for value in PERSON_MARKERS))
        await say(f"记住：同事的邮件里提到我的电子邮件签名是{SELF_SIGN}。")
        lin = (await say(f"记住：我同事小林的电子邮件签名是{LIN_SIGN}。"))["memory_id"]
        early, call = await ask(PERSON_LIN)
        check("explicitly named colleague still receives the party's current fact",
              local_recall_only(PERSON_LIN) and call["person_system_markers"] == [LIN_SIGN]
              and not call["person_history_markers"] and early.get("memory_ids") == [lin]
              and early["reply"] == LIN_SIGN)
        before = len(report["calls"])
        direct_read = await say(f"查记忆：{PERSON_LIN}")
        check("explicit local recall selects the same party with zero model requests",
              direct_read.get("memory_ids") == [lin] and len(report["calls"]) == before
              and LIN_SIGN in direct_read["reply"] and SELF_SIGN not in direct_read["reply"])
        await say(f"记住：我同事小姚的电子邮件签名是{YAO_SIGN}。")
        for label, question in (("unnamed colleague is ambiguous", PERSON_COLLEAGUE),
                                ("unknown named colleague does not reuse another identity", PERSON_UNKNOWN)):
            turn, call = await ask(question)
            check(label, not call["person_history_markers"] and not call["person_system_markers"]
                  and not turn.get("memory_ids"))
        edit = await say(f"修改记忆：{lin} → 我同事小林的电子邮件签名是{EDITED_SIGN}。")
        assert edit["memory_id"] == lin
        changed, call = await ask(PERSON_LIN)
        check("corrected colleague fact keeps ID and excludes both old and other peers",
              call["person_system_markers"] == [EDITED_SIGN] and not call["person_history_markers"]
              and changed.get("memory_ids") == [lin] and changed["reply"] == EDITED_SIGN)
        await say(f"撤回记忆：{lin}")
        await server.stop()
        await start()
        turn, call = await ask(PERSON_LIN)
        check("restart preserves withdrawal and other-person history boundary",
              not call["person_history_markers"] and not call["person_system_markers"]
              and not turn.get("memory_ids") and self_key in {item["id"] for item in (await server.state())["memories"]})
        report["passed"] = all(row["passed"] for row in report["checks"])
    finally:
        await server.stop()
        await runner.cleanup()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)
    assert report["passed"], [row["case"] for row in report["checks"] if not row["passed"]]


if __name__ == "__main__":
    asyncio.run(run())
