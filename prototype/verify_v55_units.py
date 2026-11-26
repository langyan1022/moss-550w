"""Comprehensive automated verification suite for MOSS v5.5 upgrades.

Verifies:
  1. Parallel read-only tool execution (`asyncio.gather`) + real-time `tool_trace` events
  2. Multi-cloud automatic hot failover before first token (`holo_kernel.run_holo`)
  3. Deterministic Rolling Session Summary (`core.summarize_omitted_turns`)
  4. Zero-dep Office document extraction (`doc_extract`: .docx w/ table, .xlsx w/ sharedStrings, .csv)
  5. Recurring scheduled tasks & daily briefings (`recurring_tasks`: schedule parsing + due firing)
  6. Music preferences, sibling .lrc lyrics parsing & natural sleep timer (`music_prefs`)
  7. 4-char case-insensitive CS/C Auth Code RBAC, per-identity persistent personalization & CS User Manager HTTP API
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from time import perf_counter
import zipfile

from aiohttp.test_utils import TestClient, TestServer

from core import Moss, summarize_omitted_turns
from doc_extract import extract_document_bytes
from holo_kernel import run_holo
from holojarvis_runtime.brain import Brain
from music_prefs import MusicPrefsStore, load_lrc_for_media_path, parse_sleep_timer_minutes
from recurring_tasks import RecurringTaskStore, parse_schedule_spec
from serve import application


async def test_parallel_tools_and_trace():
    traces = []
    calls_started = []

    async def dispatch(name, args):
        calls_started.append((name, perf_counter()))
        await asyncio.sleep(0.12)
        return f"res:{name}"

    step = 0

    async def stream_once(messages, schemas, required):
        nonlocal step
        step += 1
        if step == 1:
            tc1 = type("TC", (), {"index": 0, "id": "c1", "function": type("F", (), {"name": "get_time", "arguments": "{}"})()})()
            tc2 = type("TC", (), {"index": 1, "id": "c2", "function": type("F", (), {"name": "get_weather", "arguments": "{}"})()})()
            tc3 = type("TC", (), {"index": 2, "id": "c3", "function": type("F", (), {"name": "list_timers", "arguments": "{}"})()})()
            yield type("Ch", (), {"delta": type("D", (), {"content": None, "tool_calls": [tc1, tc2, tc3]})(), "finish_reason": "tool_calls"})()
        else:
            yield type("Ch", (), {"delta": type("D", (), {"content": "三项已并行查完。", "tool_calls": None})(), "finish_reason": "stop"})()

    tools = [{"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}
             for n in ("get_time", "get_weather", "list_timers")]
    b = Brain("sys", [], tools, stream_once, dispatch,
              parallel_tools={"get_time", "get_weather", "list_timers"},
              on_tool_trace=traces.append)
    t0 = perf_counter()
    ans = await b.ask_stream("同时查时间天气提醒")
    elapsed = perf_counter() - t0
    assert ans == "三项已并行查完。", ans
    assert elapsed < 0.28, f"Expected parallel <0.28s for 3x0.12s tools, got {elapsed:.3f}s"
    ok_traces = [tr for tr in traces if tr["status"] == "ok"]
    assert len(ok_traces) == 3 and all(tr["parallel"] for tr in ok_traces), traces
    return f"3-way parallel in {elapsed*1000:.0f}ms"


async def test_multicloud_failover():
    from openai import APITimeoutError

    class _Stream:
        def __init__(self, text):
            self._text = text
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def __aiter__(self):
            async def gen():
                yield type("K", (), {"choices": [type("C", (), {"delta": type("D", (), {"content": self._text, "tool_calls": None})(), "finish_reason": "stop"})()]})()
            return gen()

    class _FailClient:
        def __init__(self):
            self.calls = 0
            self.chat = self
            self.completions = self
        async def create(self, **kw):
            self.calls += 1
            raise APITimeoutError(request=None)

    class _OkClient:
        def __init__(self):
            self.calls = 0
            self.chat = self
            self.completions = self
        async def create(self, **kw):
            self.calls += 1
            return _Stream("备用核心接管成功。")

    primary = _FailClient()
    backup = _OkClient()
    events = []
    out = await run_holo(
        client=primary, provider_id="aliyun", model="qwen4-max",
        system="sys", history=[], text="你好", tools=[],
        dispatch=lambda n, a: "",
        failover_clients=[("deepseek", "deepseek-flash", backup)],
        on_failover=lambda f, t, m, r: events.append((f, t, m)),
    )
    assert out == "备用核心接管成功。", out
    assert primary.calls == 2 and backup.calls == 1
    assert events == [("aliyun", "deepseek", "deepseek-flash")], events
    return "aliyun->deepseek OK"


def test_rolling_summary():
    older = [
        {"user": "记住项目代号是北极星9号，预算18万", "reply": "好的，已记录北极星9号项目与18万预算。"},
        {"user": "下周三要飞上海见客户老陈", "reply": "已了解下周三飞上海见老陈的安排。"},
    ]
    block = summarize_omitted_turns(older)
    assert "早期会话要点摘要" in block and "北极星9号" in block and "老陈" in block, block
    return f"{len(block)} chars"


def test_doc_extract():
    # 1. CSV
    csv_res = extract_document_bytes("budget.csv", "月份,预算,实际\n1月,10000,9800\n2月,10000,12500\n".encode("utf-8"))
    assert "12500" in csv_res["text"] and csv_res["meta"]["rows"] == 3

    # 2. DOCX (paragraphs + table)
    docx_buf = io.BytesIO()
    with zipfile.ZipFile(docx_buf, "w") as zf:
        zf.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>550W 项目评审结论</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>阶段</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>通过</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>'
        ))
    docx_res = extract_document_bytes("review.docx", docx_buf.getvalue())
    assert "550W 项目评审结论" in docx_res["text"] and "阶段 | 通过" in docx_res["text"]

    # 3. XLSX (sharedStrings + sheet1)
    xlsx_buf = io.BytesIO()
    with zipfile.ZipFile(xlsx_buf, "w") as zf:
        zf.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="季度预算"/></sheets></workbook>')
        zf.writestr("xl/sharedStrings.xml", '<sst><si><t>项目</t></si><si><t>超支</t></si></sst>')
        zf.writestr("xl/worksheets/sheet1.xml", (
            '<worksheet><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>550W散热</t></is></c><c r="B2"><v>4200</v></c></row>'
            '</sheetData></worksheet>'
        ))
    xlsx_res = extract_document_bytes("q4.xlsx", xlsx_buf.getvalue())
    assert "季度预算" in xlsx_res["text"] and "550W散热 | 4200" in xlsx_res["text"]
    return "csv+docx+xlsx OK"


def test_recurring_tasks():
    db = sqlite3.connect(":memory:")
    store = RecurringTaskStore(db)
    assert parse_schedule_spec("工作日早上8点半")["kind"] == "weekdays"
    assert parse_schedule_spec("每周五下午5点")["weekday"] == 4
    assert parse_schedule_spec("每45分钟")["every_min"] == 45

    t1 = store.create_task(name="每日早报", schedule="每天 08:30", action_kind="briefing")
    due1 = store.pop_due_tasks(datetime(2026, 11, 24, 8, 32, 0))
    assert len(due1) == 1 and due1[0]["id"] == t1["id"]
    # Must NOT double-fire same day
    assert store.pop_due_tasks(datetime(2026, 11, 24, 8, 35, 0)) == []
    # Fires next day
    due2 = store.pop_due_tasks(datetime(2026, 11, 25, 8, 31, 0))
    assert len(due2) == 1
    assert store.delete_task("每日早报")["id"] == t1["id"]
    assert store.list_tasks() == []
    return "schedule+dedup+crud OK"


def test_music_prefs_lrc_sleep():
    db = sqlite3.connect(":memory:")
    mp = MusicPrefsStore(db)
    mp.set_status("trk-1", "favorite", title="冰雨", artist="刘德华")
    mp.set_status("trk-2", "blocked", title="噪音", artist="未知")
    assert mp.get_status("trk-1") == "favorite"
    assert "trk-2" in mp.blocked_ids()
    assert parse_sleep_timer_minutes("30分钟后停歌") == 30
    assert parse_sleep_timer_minutes("半小时后关闭音乐") == 30
    assert parse_sleep_timer_minutes("取消睡眠定时") == 0

    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "demo.mp3"
        audio.write_bytes(b"")
        (Path(td) / "demo.lrc").write_text("[00:12.50]冷冷的冰雨在脸上胡乱地拍\n[00:05.00]前奏\n", encoding="utf-8")
        lines = load_lrc_for_media_path(audio)
        assert len(lines) == 2 and lines[0]["text"] == "前奏" and lines[1]["time"] == 12.5
    return "prefs+lrc+sleep OK"


async def test_auth_rbac_and_persistence_http():
    root = Path(__file__).resolve().parent
    fui_public = root / "fui/public"
    with tempfile.TemporaryDirectory(prefix="moss-v55-auth-") as td:
        data_dir = Path(td)
        moss = Moss(data_dir, "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
        stopped = asyncio.Event()
        app = application(moss, lambda: "standby", data_dir, 18999, "tok123", stopped,
                          fui_public=fui_public, lan_hosts=("192.168.1.100",))
        try:
            async with TestClient(TestServer(app)) as cli:
                host = f"127.0.0.1:{cli.port}"
                # Adjust accepted_hosts dynamically for ephemeral test server port
                # Instead, test via explicit port matching: TestServer picks random port, so use app with custom host header:
                pass
        finally:
            await moss.close()

    # Use explicit free port so accepted_hosts matches Host header cleanly
    from testkit import safe_free_port
    port = safe_free_port()

    with tempfile.TemporaryDirectory(prefix="moss-v55-auth2-") as td:
        data_dir = Path(td)
        moss = Moss(data_dir, "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
        stopped = asyncio.Event()
        app = application(moss, lambda: "standby", data_dir, port, "tok123", stopped, fui_public=fui_public)
        server = TestServer(app, port=port)
        try:
            async with TestClient(server) as cli:
                hdr = {"Origin": f"http://127.0.0.1:{port}"}
                # 1. Invalid code length -> 400
                r_bad = await cli.post("/api/auth/verify", json={"code": "TOO_LONG"}, headers=hdr)
                assert r_bad.status == 400

                # 2. Switch to C-level operator "ab12" (case-insensitive -> AB12)
                r_c = await cli.post("/api/auth/verify", json={"code": "ab12"}, headers=hdr)
                assert r_c.status == 200
                d_c = await r_c.json()
                assert d_c["code"] == "AB12" and d_c["role"] == "C" and not d_c["is_admin"]

                # 3. C user CAN save personalization (badge, city, spectrum, personality)
                r_pref = await cli.post("/api/console-settings/prefs",
                                        json={"prefs": {"city_id": "shanghai", "oled_palette": "amber", "badge": {"name": "刘启"}}},
                                        headers=hdr)
                assert r_pref.status == 200

                # 4. C user is BLOCKED (403) from CS system admin actions (both POST & sensitive GETs)!
                for admin_ep, payload in (
                    ("/api/select-model", {"id": "local"}),
                    ("/api/console-settings/providers", {"id": "aliyun"}),
                    ("/api/console-settings/library", {"action": "scan"}),
                    ("/api/auth/config", {"enabled": False}),
                    ("/api/auth/users", {"code": "ZZ99", "label": "hack"}),
                ):
                    r_blk = await cli.post(admin_ep, json=payload, headers=hdr)
                    assert r_blk.status == 403, f"Expected 403 for C role on {admin_ep}, got {r_blk.status}"
                for get_ep in (
                    "/api/music/prefs",
                    "/api/skills",
                    "/api/hardware/moss/stream",
                    "/api/runtime-log/source",
                    "/api/online-music/browse",
                ):
                    r_gblk = await cli.get(get_ep)
                    assert r_gblk.status == 403, f"Expected 403 for C role on GET {get_ep}, got {r_gblk.status}"
                async with cli.get("/api/runtime-log/stream?mode=tail") as r_log_sse:
                    assert r_log_sse.status == 200
                    line = (await asyncio.wait_for(r_log_sse.content.readline(), timeout=2)).decode("utf-8", errors="replace")
                    assert "RESTRICTED C-MODE" in line, line

                # 5. Switch to CS admin "moss" (lowercase -> MOSS -> CS!)
                r_cs = await cli.post("/api/auth/verify", json={"code": "moss"}, headers=hdr)
                assert r_cs.status == 200
                d_cs = await r_cs.json()
                assert d_cs["code"] == "MOSS" and d_cs["role"] == "CS" and d_cs["is_admin"]

                # 6. CS admin CAN list & manage users (AB12 is registered with 3 saved prefs!)
                r_users = await cli.get("/api/auth/users")
                assert r_users.status == 200
                u_list = (await r_users.json())["users"]
                ab = next((u for u in u_list if u["code"] == "AB12"), None)
                assert ab is not None and ab["role"] == "C" and ab["pref_count"] == 3, u_list

                # 7. Re-entering "AB12" restores previously persisted personalization intact!
                r_c2 = await cli.post("/api/auth/verify", json={"code": "AB12"}, headers=hdr)
                assert r_c2.status == 200
                d_c2 = await r_c2.json()
                assert d_c2["prefs"]["city_id"] == "shanghai" and d_c2["prefs"]["badge"]["name"] == "刘启", d_c2

                # 8. CS disables AB12 -> AB12 login rejected with 403!
                await cli.post("/api/auth/verify", json={"code": "MOSS"}, headers=hdr)
                r_dis = await cli.post("/api/auth/users", json={"code": "AB12", "disabled": True, "label": "临时访客"}, headers=hdr)
                assert r_dis.status == 200
                r_rej = await cli.post("/api/auth/verify", json={"code": "ab12"}, headers=hdr)
                assert r_rej.status == 403, f"Disabled C user should get 403, got {r_rej.status}"

                # 9. CS deletes C user ZK88 -> pre-existing ZK88 cookie is immediately rejected (401) & NOT resurrected!
                r_zk = await cli.post("/api/auth/verify", json={"code": "zk88"}, headers=hdr)
                assert r_zk.status == 200
                zk_cookie = r_zk.cookies["moss_auth"].value
                await cli.post("/api/auth/verify", json={"code": "MOSS"}, headers=hdr)
                r_del = await cli.delete("/api/auth/users/ZK88", headers=hdr)
                assert r_del.status == 200
                import aiohttp
                async with aiohttp.ClientSession(trust_env=False) as stale_cli:
                    r_stale = await stale_cli.get(cli.make_url("/api/console-settings/prefs"), cookies={"moss_auth": zk_cookie})
                    assert r_stale.status == 401, f"Deleted C cookie should fail with 401, got {r_stale.status}"
                r_users_after = await cli.get("/api/auth/users")
                assert not any(u["code"] == "ZK88" for u in (await r_users_after.json())["users"]), "Deleted C user resurrected!"
                # 10. Headless mode (fui_public=None) still enforces moss_auth_check RBAC!
                with tempfile.TemporaryDirectory(prefix="moss-v55-nofui-") as td_nf:
                    nf_port = safe_free_port()
                    moss_nf = Moss(Path(td_nf), "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
                    app_nf = application(moss_nf, lambda: "standby", Path(td_nf), nf_port, "toknf", asyncio.Event(), fui_public=None)
                    try:
                        assert app_nf.get("moss_auth_check") is not None
                        async with TestClient(TestServer(app_nf, port=nf_port)) as cli_nf:
                            nf_hdr = {"Origin": f"http://127.0.0.1:{nf_port}"}
                            assert (await cli_nf.post("/api/auth/verify", json={"code": "AB12"}, headers=nf_hdr)).status == 200
                            assert (await cli_nf.get("/api/music/prefs")).status == 403
                    finally:
                        await moss_nf.close()
        finally:
            await moss.close()
    return "4-char CS/C RBAC + persist + disable + deleted cookie reject OK"


async def test_voice_process_guards():
    from voice_input import LocalVoice
    from voice_output import MossSpeech

    class _FakeStdin:
        def __init__(self):
            self.closed = False
        def is_closing(self):
            return self.closed
        def close(self):
            self.closed = True

    class _HungProc:
        def __init__(self):
            self.returncode = None
            self.stdin = _FakeStdin()
            self.terminated = False
            self.killed = False
        async def wait(self):
            if self.killed:
                self.returncode = -9
                return -9
            await asyncio.sleep(10)
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
            self.returncode = -9

    lv = LocalVoice()
    hung = _HungProc()
    lv.proc = hung
    await lv.close()
    assert lv.closing is True and lv.proc is None and hung.terminated and hung.killed

    class _FakeGroup:
        def __init__(self):
            self.closed = False
        def Close(self):
            self.closed = True

    with tempfile.TemporaryDirectory(prefix="moss-voice-guard-") as td:
        svc = MossSpeech(Path(td))
        svc.closing = True
        raised = False
        try:
            await svc._start()
        except ConnectionAbortedError:
            raised = True
        assert raised, "MossSpeech._start() must refuse when closing=True"
        hung_w = _HungProc()
        grp = _FakeGroup()
        svc.worker = hung_w
        svc.worker_group = grp
        await svc._stop()
        assert svc.worker is None and grp.closed and hung_w.terminated and hung_w.killed
    return "LocalVoice + MossSpeech closing/timeout cleanup OK"


def test_secret_store_and_iot_encryption():
    from iot_bridge import IoTAndHardwareBridge
    from secret_store import protect_secret, unprotect_secret

    with tempfile.TemporaryDirectory(prefix="moss-sec-") as td:
        td_path = Path(td)
        sealed = protect_secret("tok_secret_987", entropy="unit-scope", key_dir=td_path, prefer_portable=True)
        assert sealed.startswith("fernet1:")
        assert unprotect_secret(sealed, entropy="unit-scope", key_dir=td_path) == "tok_secret_987"
        assert unprotect_secret(sealed, entropy="wrong-scope", key_dir=td_path) == ""

        db = sqlite3.connect(":memory:")
        iot = IoTAndHardwareBridge(db)
        iot.update_config({"ha_url": "http://127.0.0.1:8123", "ha_token": "ha_tok_unit_xyz"})
        raw_doc = db.execute("SELECT doc FROM iot_bridge_config WHERE id=1").fetchone()[0]
        assert "ha_tok_unit_xyz" not in raw_doc and "ha_token_enc" in raw_doc
        iot_reloaded = IoTAndHardwareBridge(db)
        assert iot_reloaded.config["ha_token"] == "ha_tok_unit_xyz"
    return "DPAPI/Fernet + IoT ha_token encryption OK"


async def test_operator_conversation_isolation():
    import socket
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer
    from core import Moss
    from serve import application

    from testkit import safe_free_port

    port = safe_free_port()

    fui_public = Path(__file__).resolve().parent / "fui" / "public"
    with tempfile.TemporaryDirectory(prefix="moss-v56-isolation-") as td:
        data_dir = Path(td)
        moss = Moss(data_dir, "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
        stopped = asyncio.Event()
        app = application(moss, lambda: "standby", data_dir, port, "tok-iso", stopped, fui_public=fui_public)
        server = TestServer(app, port=port)
        try:
            async with TestClient(server) as cs_cli:
                hdr = {"Origin": f"http://127.0.0.1:{port}"}
                # Authenticate CS explicitly as MOSS
                assert (await cs_cli.post("/api/auth/verify", json={"code": "MOSS"}, headers=hdr)).status == 200
                r_m1 = await cs_cli.post("/api/message", json={"text": "记住：我是总控老丁"}, headers=hdr)
                assert r_m1.status == 200
                m_state = await r_m1.json()
                moss_chat_id = m_state["chat_id"]
                assert m_state["owner_code"] == "MOSS"
                assert any("总控老丁" in m["text"] for m in m_state["memories"])

                # Separate HTTP session for C operator OP02 (unsafe=True allows aiohttp CookieJar on 127.0.0.1 IP)
                async with aiohttp.ClientSession(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    cookie_jar=aiohttp.CookieJar(unsafe=True),
                ) as op2_cli:
                    r_v2 = await op2_cli.post("/api/auth/verify", json={"code": "OP02"}, headers=hdr)
                    assert r_v2.status == 200
                    d_v2 = await r_v2.json()
                    op2_chat_id = d_v2["last_chat_id"]
                    assert op2_chat_id and op2_chat_id != moss_chat_id

                    # /api/state MUST isolate turns, chats, and memories
                    st2 = await (await op2_cli.get("/api/state")).json()
                    assert st2["owner_code"] == "OP02"
                    assert st2["chat_id"] == op2_chat_id and st2["chat_id"] != moss_chat_id
                    assert st2["turns"] == [], f"Leaked MOSS turns to OP02: {st2['turns']}"
                    assert st2["memories"] == [], f"Leaked MOSS memories to OP02: {st2['memories']}"
                    assert all(c["id"] != moss_chat_id for c in st2["chats"])

                    # /api/events initial SSE state frame MUST be isolated to OP02
                    async with op2_cli.get("/api/events") as sse_resp:
                        assert sse_resp.status == 200
                        raw_evt = b""
                        for _ in range(12):
                            chunk = await sse_resp.content.readline()
                            raw_evt += chunk
                            if chunk == b"\n":
                                break
                        evt_txt = raw_evt.decode("utf-8")
                        assert '"owner_code":"OP02"' in evt_txt and "总控老丁" not in evt_txt, evt_txt

                    # /api/chats/search MUST NOT expose MOSS turns to OP02
                    sr2 = await (await op2_cli.get("/api/chats/search?q=总控老丁")).json()
                    assert sr2["hits"] == [], f"Search leaked MOSS turns: {sr2}"
                    sr_moss = await (await cs_cli.get("/api/chats/search?q=总控老丁")).json()
                    assert len(sr_moss["hits"]) == 1 and sr_moss["hits"][0]["chat_id"] == moss_chat_id

                    # Cross-operator export, switch-chat, message MUST return 403
                    assert (await op2_cli.get(f"/api/chats/{moss_chat_id}/export")).status == 403
                    assert (await op2_cli.post("/api/switch-chat", json={"id": moss_chat_id}, headers=hdr)).status == 403
                    assert (await op2_cli.post("/api/message", json={"chat_id": moss_chat_id, "text": "在吗"}, headers=hdr)).status == 403

                    # OP02 writes their own turn, memory, and personality without clobbering MOSS
                    r_p2 = await op2_cli.post("/api/select-personality", json={"id": "sassy"}, headers=hdr)
                    assert r_p2.status == 200
                    r_op2_mem = await op2_cli.post("/api/message", json={"chat_id": op2_chat_id, "text": "记住：我是访客小王"}, headers=hdr)
                    assert r_op2_mem.status == 200
                    st2_after = await r_op2_mem.json()
                    assert any("访客小王" in m["text"] for m in st2_after["memories"])
                    assert not any("总控老丁" in m["text"] for m in st2_after["memories"])
                    assert st2_after["personality_id"] == "sassy"

                    # Meanwhile MOSS still has its own chat, turns, memories, and partner personality!
                    st_moss = await (await cs_cli.get("/api/state")).json()
                    assert st_moss["chat_id"] == moss_chat_id
                    assert st_moss["personality_id"] == "partner"
                    assert not any("访客小王" in (t.get("user") or "") for t in st_moss["turns"])
                    assert not any("访客小王" in m["text"] for m in st_moss["memories"])

                    # OP02 new-chat does NOT hijack MOSS's active chat_id
                    r_nc2 = await op2_cli.post("/api/new-chat", json={}, headers=hdr)
                    assert r_nc2.status == 200
                    st2_nc = await r_nc2.json()
                    assert st2_nc["chat_id"] != op2_chat_id and len(st2_nc["chats"]) == 2
                    st_moss2 = await (await cs_cli.get("/api/state")).json()
                    assert st_moss2["chat_id"] == moss_chat_id

                    # Third operator OP03 cannot see MOSS or OP02
                    async with aiohttp.ClientSession(
                        base_url=f"http://127.0.0.1:{port}",
                        trust_env=False,
                        cookie_jar=aiohttp.CookieJar(unsafe=True),
                    ) as op3_cli:
                        assert (await op3_cli.post("/api/auth/verify", json={"code": "OP03"}, headers=hdr)).status == 200
                        st3 = await (await op3_cli.get("/api/state")).json()
                        assert st3["turns"] == [] and st3["memories"] == [] and len(st3["chats"]) == 1
                        sr3 = await (await op3_cli.get("/api/chats/search?q=访客小王")).json()
                        assert sr3["hits"] == []

                    # Brain Overview P7.1 telemetry: CS sees provider_health; OP02 stays restricted; concurrent SSE + speak-batch
                    ov_cs = await (await cs_cli.get("/api/brain/overview")).json()
                    assert isinstance(ov_cs.get("provider_health"), dict)
                    ov_op2 = await (await op2_cli.get("/api/brain/overview")).json()
                    assert ov_op2.get("iot", {}).get("restricted") is True and "provider_health" not in ov_op2

                    # CS deleting OP02 wipes OP02's chats, turns, and memories
                    assert (await cs_cli.delete("/api/auth/users/OP02", headers=hdr)).status == 200
                    assert (await op2_cli.get("/api/state")).status == 401
                    assert moss.db.execute("SELECT COUNT(*) FROM chats WHERE owner_code='OP02'").fetchone()[0] == 0
                    assert moss.db.execute("SELECT COUNT(*) FROM memories WHERE owner_code='OP02'").fetchone()[0] == 0
        finally:
            await moss.close()
    return "Multi-operator chats/turns/search/export/SSE/memories + overview RBAC OK"


async def test_concurrent_sse_and_speak_batch():
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer
    import struct, wave, io as _io
    from unittest.mock import AsyncMock, patch
    from core import Moss
    import serve as _serve
    from testkit import safe_free_port

    port = safe_free_port()
    fui_public = Path(__file__).resolve().parent / "fui" / "public"
    with tempfile.TemporaryDirectory(prefix="moss-v61-conc-") as td:
        data_dir = Path(td)
        moss = Moss(data_dir, "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
        stopped = asyncio.Event()
        buf = _io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000)
            wf.writeframes(struct.pack("<100h", *([120] * 100)))
        fake_wav = buf.getvalue()
        fake_speech = type(
            "FakeSpeech", (), {
                "batch_capable": True,
                "close": AsyncMock(),
                "render_batch": AsyncMock(return_value=([fake_wav, fake_wav], "batch")),
                "render": AsyncMock(return_value=fake_wav),
            }
        )()
        try:
            with patch.object(_serve, "MossSpeech", return_value=fake_speech):
                app = _serve.application(moss, lambda: "standby", data_dir, port, "tokconc", stopped, fui_public=fui_public)
            hdr = {"Origin": f"http://127.0.0.1:{port}"}
            async with TestClient(TestServer(app, port=port)) as cs_cli:
                await cs_cli.post("/api/auth/verify", json={"code": "MOSS"}, headers=hdr)
                assert (await cs_cli.post("/api/auth/users", json={"code": "OP11", "label": "S1"}, headers=hdr)).status == 200
                assert (await cs_cli.post("/api/auth/users", json={"code": "OP12", "label": "S2"}, headers=hdr)).status == 200
                # Multipart /api/speak-batch verification
                r_batch = await cs_cli.post("/api/speak-batch", json={"texts": ["第一段播报。", "第二段播报。"], "category": "reply"}, headers=hdr)
                assert r_batch.status == 200 and r_batch.headers.get("X-MOSS-Batch-Capable") == "1"
                raw_mp = await r_batch.read()
                assert raw_mp.count(b"RIFF") == 2 and raw_mp.count(b"WAVE") == 2

                # Concurrent OP11 + OP12 SSE streams stay completely isolated
                async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}", trust_env=False, cookie_jar=aiohttp.CookieJar(unsafe=True)) as c1, \
                           aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}", trust_env=False, cookie_jar=aiohttp.CookieJar(unsafe=True)) as c2:
                    assert (await c1.post("/api/auth/verify", json={"code": "OP11"}, headers=hdr)).status == 200
                    assert (await c2.post("/api/auth/verify", json={"code": "OP12"}, headers=hdr)).status == 200
                    await c1.post("/api/message", json={"text": "记住：OP11 秘密火星任务"}, headers=hdr)
                    await c2.post("/api/message", json={"text": "记住：OP12 秘密木星任务"}, headers=hdr)
                    async with c1.get("/api/events") as s1, c2.get("/api/events") as s2:
                        txt1 = (await s1.content.read(1600)).decode("utf-8", errors="ignore")
                        txt2 = (await s2.content.read(1600)).decode("utf-8", errors="ignore")
                        assert "OP11" in txt1 and "木星" not in txt1
                        assert "OP12" in txt2 and "火星" not in txt2
        finally:
            await moss.close()
    return "Concurrent OP11/OP12 SSE + /api/speak-batch multipart WAV OK"


async def test_v62_streaming_asr_rbac_and_cascade_cleanup():
    from unittest.mock import AsyncMock, patch
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer
    from core import Moss
    import serve as _serve
    from testkit import safe_free_port

    with tempfile.TemporaryDirectory(prefix="moss-v62-u-") as td:
        tp = Path(td)
        data_dir = tp / "data"
        data_dir.mkdir()
        port = safe_free_port()
        moss = Moss(data_dir, "http://127.0.0.1:0/v1", "moss-chat-qwen35-4b", prefer_cloud=False)
        stopped = asyncio.Event()
        try:
            fui_public = Path(__file__).resolve().parent / "fui" / "public"
            app = _serve.application(moss, lambda: "standby", data_dir, port, "tok62", stopped, fui_public=fui_public)
            hdr = {"Origin": f"http://127.0.0.1:{port}"}
            async with TestClient(TestServer(app, port=port)) as cli:
                await cli.post("/api/auth/verify", json={"code": "MOSS"}, headers=hdr)
                assert (await cli.post("/api/auth/users", json={"code": "OP19", "label": "Cascade"}, headers=hdr)).status == 200
                # Seed OP19 cognitive records
                moss.sag.upsert_event(title="OP19 档案", content="秘密测试数据", source="memory", source_ref="m:19", owner_code="OP19")
                moss.soul.upsert_profile(key="city", value="合肥", source="user_edit", owner_code="OP19")
                moss.rsi.promote_lesson(scope="fact", trigger="op19", rule="测试规则请勿泄露", owner_code="OP19")
                moss.routines.create_task(name="op19_r", schedule="interval 15m", action_kind="skill", payload="早报", owner_code="OP19")

                # Verify CS runtime-log guard & /api/rsi/eval endpoint
                r_eval = await cli.post("/api/rsi/eval", json={}, headers=hdr)
                assert r_eval.status == 200
                ev_data = (await r_eval.json())["eval"]
                assert "composite_score" in ev_data

                # Verify streaming ASR start -> chunk -> commit HTTP pipeline with odd bytes + reset
                r_st = await cli.post("/api/transcribe/stream/start", json={}, headers=hdr)
                assert r_st.status == 200
                sid = (await r_st.json())["session"]
                with patch("serve.LocalVoice.stream_chunk", AsyncMock(return_value={"buffered_bytes": 1000})), \
                     patch("serve.LocalVoice.stream_commit", AsyncMock(return_value="今天天气不错")), \
                     patch("serve.LocalVoice.stream_reset", AsyncMock(return_value=None)):
                    r_ch = await cli.post(f"/api/transcribe/stream/chunk?session={sid}", data=b"\x01\x02" * 500, headers={**hdr, "Content-Type": "application/octet-stream"})
                    assert r_ch.status == 200
                    r_cm = await cli.post("/api/transcribe/stream/commit", json={"session": sid}, headers=hdr)
                    assert r_cm.status == 200 and (await r_cm.json())["text"] == "今天天气不错"
                    r_rs = await cli.post("/api/transcribe/stream/reset", json={"session": sid}, headers=hdr)
                    assert r_rs.status == 200

                # Verify C-role gets 403 on direct /conversation/runtime-log, /api/context/ocr, and internal Navidrome cover/stream
                async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}", trust_env=False, cookie_jar=aiohttp.CookieJar(unsafe=True)) as c_cli:
                    assert (await c_cli.post("/api/auth/verify", json={"code": "OP19"}, headers=hdr)).status == 200
                    r_rt_c = await c_cli.get("/conversation/runtime-log")
                    assert r_rt_c.status == 403, f"Expected 403 for C runtime-log, got {r_rt_c.status}"
                    r_ocr_c = await c_cli.post("/api/context/ocr", json={}, headers=hdr)
                    assert r_ocr_c.status == 403, f"Expected 403 for C /api/context/ocr, got {r_ocr_c.status}"
                    with patch.object(moss.online_music, "resolve_cover_target", return_value={"url": "http://127.0.0.1:4533/rest/getCoverArt", "allow_http": True, "allow_internal": True}), \
                         patch.object(moss.online_music, "resolve_stream_target", return_value={"url": "http://127.0.0.1:4533/rest/stream", "allow_http": True, "allow_internal": True, "headers": {}}):
                        r_cov_c = await c_cli.get("/api/media/online-cover/nv123")
                        r_str_c = await c_cli.get("/api/media/online-stream/nv123")
                        assert r_cov_c.status == 403 and r_str_c.status == 403, f"C accessed internal Navidrome: {(r_cov_c.status, r_str_c.status)}"

                # Verify remote CS admin_pass & lan_cs_local_only enforcement + normalize_owner_code across stores
                from aiohttp.test_utils import make_mocked_request
                from cognitive_utils import normalize_owner_code
                from music_prefs import MusicPrefsStore
                from semantic_memory import LocalMemoryIndex
                from sleep_consolidator import SleepConsolidator

                assert (await cli.post("/api/auth/config", json={"admin_pass": "Secret99", "lan_cs_local_only": False}, headers=hdr)).status == 200
                verify_handler = next(
                    r.handler for r in app.router.routes()
                    if r.method == "POST" and getattr(r.resource, "canonical", "") == "/api/auth/verify"
                )
                async def _mock_remote_verify(payload: dict):
                    raw_b = json.dumps(payload).encode("utf-8")
                    req = make_mocked_request(
                        "POST",
                        "/api/auth/verify",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": f"http://127.0.0.1:{port}",
                            "Content-Type": "application/json",
                        },
                    ).clone(remote="192.168.50.99")
                    req._read_bytes = raw_b  # aiohttp cached body shortcut for req.json()
                    return await verify_handler(req)
                r_bad_pw = await _mock_remote_verify({"code": "MOSS", "password": "wrong"})
                assert r_bad_pw.status == 403, f"Remote CS with wrong admin_pass should return 403, got {r_bad_pw.status}"
                r_good_pw = await _mock_remote_verify({"code": "MOSS", "password": "Secret99"})
                assert r_good_pw.status == 200, f"Remote CS with valid admin_pass failed: {r_good_pw.status}"
                assert (await cli.post("/api/auth/config", json={"lan_cs_local_only": True, "admin_pass": ""}, headers=hdr)).status == 200
                r_lan_blk = await _mock_remote_verify({"code": "MOSS"})
                assert r_lan_blk.status == 403, f"lan_cs_local_only should block remote CS, got {r_lan_blk.status}"
                assert (await cli.post("/api/auth/config", json={"lan_cs_local_only": False, "admin_pass": ""}, headers=hdr)).status == 200

                assert normalize_owner_code("cs") == "MOSS" and normalize_owner_code("op19") == "OP19"
                MusicPrefsStore(moss.db).set_status("m1", "favorite", owner_code="cs")
                assert MusicPrefsStore(moss.db).get_status("m1", owner_code="MOSS") == "favorite"
                lmi = LocalMemoryIndex("http://127.0.0.1:8888", moss.db)
                assert lmi.scope and len(lmi.scope) >= 16
                assert SleepConsolidator._norm_owner("cs") == "MOSS" if hasattr(SleepConsolidator, "_norm_owner") else normalize_owner_code("cs") == "MOSS"

                # Verify DELETE /api/auth/users/OP19 cascades across all cognitive stores
                r_del = await cli.delete("/api/auth/users/OP19", headers=hdr)
                assert r_del.status == 200
                assert moss.db.execute("SELECT COUNT(*) FROM sag_events WHERE COALESCE(owner_code,'MOSS')='OP19'").fetchone()[0] == 0
                assert moss.db.execute("SELECT COUNT(*) FROM soul_profile WHERE COALESCE(owner_code,'MOSS')='OP19'").fetchone()[0] == 0
                assert moss.db.execute("SELECT COUNT(*) FROM rsi_lessons WHERE COALESCE(owner_code,'MOSS')='OP19'").fetchone()[0] == 0
                assert moss.db.execute("SELECT COUNT(*) FROM recurring_tasks WHERE COALESCE(owner_code,'MOSS')='OP19'").fetchone()[0] == 0
        finally:
            stopped.set()
            await moss.close()
    return "Streaming ASR HTTP + C runtime-log 403 + /api/rsi/eval + cascade user delete OK"


async def main():
    res = {
        "parallel_tools": await test_parallel_tools_and_trace(),
        "cloud_failover": await test_multicloud_failover(),
        "rolling_summary": test_rolling_summary(),
        "doc_extract": test_doc_extract(),
        "recurring_tasks": test_recurring_tasks(),
        "music_prefs_lrc_sleep": test_music_prefs_lrc_sleep(),
        "auth_cs_c_rbac": await test_auth_rbac_and_persistence_http(),
        "operator_isolation": await test_operator_conversation_isolation(),
        "concurrent_sse_and_speak_batch": await test_concurrent_sse_and_speak_batch(),
        "v62_stream_asr_rbac_cascade": await test_v62_streaming_asr_rbac_and_cascade_cleanup(),
        "voice_process_guards": await test_voice_process_guards(),
        "secret_store_iot": test_secret_store_and_iot_encryption(),
    }
    print("V55_UNITS_PASS:", json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
