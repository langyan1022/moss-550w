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
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

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

                # 4. C user is BLOCKED (403) from CS system admin actions!
                for admin_ep, payload in (
                    ("/api/select-model", {"id": "local"}),
                    ("/api/console-settings/providers", {"id": "aliyun"}),
                    ("/api/console-settings/library", {"action": "scan"}),
                    ("/api/auth/config", {"enabled": False}),
                    ("/api/auth/users", {"code": "ZZ99", "label": "hack"}),
                ):
                    r_blk = await cli.post(admin_ep, json=payload, headers=hdr)
                    assert r_blk.status == 403, f"Expected 403 for C role on {admin_ep}, got {r_blk.status}"

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


async def main():
    res = {
        "parallel_tools": await test_parallel_tools_and_trace(),
        "cloud_failover": await test_multicloud_failover(),
        "rolling_summary": test_rolling_summary(),
        "doc_extract": test_doc_extract(),
        "recurring_tasks": test_recurring_tasks(),
        "music_prefs_lrc_sleep": test_music_prefs_lrc_sleep(),
        "auth_cs_c_rbac": await test_auth_rbac_and_persistence_http(),
        "voice_process_guards": await test_voice_process_guards(),
    }
    print("V55_UNITS_PASS:", json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
