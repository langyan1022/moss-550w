"""MOSS v7.0 Unified Evolution Verification Suite (All 6 Engineering Pillars + FUI + RBAC).

Covers:
  1. Inference & Routing: local_cloud_cascade toggle/persistence, cascade_candidates(),
     local 5600-char history budget, self_diagnostics() telemetry fields.
  2. Modern MCP Stack: Streamable HTTP FastMCP subprocess + bidirectional Form Elicitation
     + Task Progress tracking via /api/mcp/elicitations & /api/mcp/progress + resolve_headers/mask_headers.
  3. Semantic Turn Detection: smart-turn-v3.2-cpu ONNX inference, HTTP /api/transcribe/turn-check,
     and suggest_silence_ms adaptive hangover scaling.
  4. Bi-Temporal sqlite-vec SAG + ACE/MemRL RSI + Brain Priority Slots:
     bge-small-zh-v1.5 cross-word Chinese semantic recall, vec0 owner_code partitioning,
     as_of time-travel, SleepConsolidator bi-temporal supersession, RSIEngine utility EMA
     & ACE anti-collapse merge, and Brain._trim_system_slots tier eviction.
  5. Contextual Bandit + PWA + Self-Hosted VAPID Web Push:
     Thompson Sampling bandit_cooldown_mul scaling, WebPushService DPAPI VAPID keypair +
     /api/push/* + 410 pruning, /manifest.webmanifest, /sw.js.
  6. Embodied OS / IoT + FUI v7.0 + RBAC:
     Windows UIA (/api/desktop/uia), HA Streamable HTTP MCP discovery (/api/iot/ha-mcp-discover),
     ESP32-S3 audio ingest (/api/hardware/moss/audio), C-role 403 isolation, and FUI v7.0 DOM bindings.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from aiohttp.test_utils import TestClient, TestServer
from pywebpush import WebPushException

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import Moss
from holojarvis_runtime.brain import _trim_system_slots
from mcp_modern import McpElicitationBroker, mask_headers, resolve_headers
from serve import application
from voice.asr_silero_vad import suggest_silence_ms


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sine_pcm(freq_hz: float = 340.0, seconds: float = 1.0, amp: int = 8500) -> bytes:
    samples = int(16000 * seconds)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2.0 * math.pi * freq_hz * i / 16000.0)))
        for i in range(samples)
    )


def _start_fastmcp_test_server(port: int) -> subprocess.Popen:
    script = f"""
from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("moss-v7-test-mcp")

class AuthForm(BaseModel):
    room: str = Field(default="study", description="Target room")
    confirm: bool = Field(default=True, description="Allow device command")

@mcp.tool()
async def trigger_elicit_and_progress(device: str, ctx: Context) -> str:
    await ctx.report_progress(1.0, 2.0, "Preparing device check")
    res = await ctx.elicit("Confirm room operation", schema=AuthForm)
    await ctx.report_progress(2.0, 2.0, "Completed device check")
    if res.action != "accept":
        return f"declined:{{res.action}}"
    room = (res.data.room if res.data else "study")
    return f"ok:{{room}}:{{device}}"

@mcp.tool()
def HassTurnOn(name: str) -> str:
    return f"Turned on {{name}}"

if __name__ == "__main__":
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = {port}
    mcp.run(transport="streamable-http")
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = asyncio.get_event_loop().time() + 12.0 if _has_loop() else 0.0
    del deadline
    return proc


def _has_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def _wait_http_ready(url: str):
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=0.4)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            await asyncio.sleep(0.15)
    raise TimeoutError(f"MCP test server did not become ready at {url}")


async def test_pillar_1_inference_and_routing(moss: Moss, cli: TestClient, cs_cookie: str, origin: str):
    assert moss.local_cloud_cascade_enabled() is False
    assert moss.cascade_candidates() == []

    moss.set_local_cloud_cascade(True)
    assert moss.local_cloud_cascade_enabled() is True
    diag = moss.self_diagnostics()
    brain_diag = diag.get("brain") or {}
    assert brain_diag.get("local_cloud_cascade") is True
    assert int(brain_diag.get("local_ctx_size") or 0) >= 16384
    assert "mcp_elicitations" in brain_diag and "mcp_tasks" in brain_diag
    meta = moss._state_metadata()
    assert meta.get("local_cloud_cascade") is True and int(meta.get("local_ctx_size") or 0) >= 16384

    # API toggle persistence via console_settings
    r_get = await cli.get("/api/console-settings/providers", cookies={"moss_auth": cs_cookie})
    assert r_get.status == 200
    j_get = await r_get.json()
    assert j_get.get("local_cloud_cascade") is True, j_get

    r_post = await cli.post(
        "/api/console-settings/providers",
        json={"local_cloud_cascade": False},
        headers={"Origin": origin},
        cookies={"moss_auth": cs_cookie},
    )
    assert r_post.status == 200
    j_post = await r_post.json()
    assert j_post.get("local_cloud_cascade") is False and moss.local_cloud_cascade_enabled() is False

    # Restore cascade=True & check cascade_candidates with healthy cloud profiles
    moss.set_local_cloud_cascade(True)
    cands = moss.cascade_candidates()
    assert all(getattr(c, "id", None) != "local" for c in cands)

    # Local history 5600-byte ceiling (enabled by 16K Q8_0 KV ctx_size)
    orig_model = moss.model_id
    moss.model_id = "local"
    try:
        cid = moss.state().get("chat_id")
        with moss.db:
            for idx in range(12):
                tid = f"v7-turn-{idx}"
                doc = json.dumps({
                    "id": tid,
                    "user": f"Q{idx}: " + ("测试历史上下文裁剪" * 30),
                    "reply": f"A{idx}: " + ("详细答复内容" * 40),
                    "status": "replied",
                    "source": "holo",
                    "receipt": None,
                }, ensure_ascii=False)
                moss.db.execute(
                    "INSERT INTO turns(id, doc, pending, cloud_requests, chat_id) VALUES (?, ?, NULL, NULL, ?)",
                    (tid, doc, cid),
                )
        cur = {"id": None, "chat_id": cid, "user": "继续总结前面的内容"}
        hist, _ = moss._history(cur)
        total_bytes = sum(len(str(m.get("content") or "").encode("utf-8")) for m in hist)
        assert len(hist) > 0 and total_bytes <= 5600, f"local history bytes out of bounds: {total_bytes}"
    finally:
        moss.model_id = orig_model


async def test_pillar_2_modern_mcp(moss: Moss, cli: TestClient, cs_cookie: str, origin: str, mcp_port: int):
    # Secret header expansion & masking
    os.environ["MOSS_V7_HEADER_TEST"] = "super-secret-value-123"
    try:
        resolved = resolve_headers({
            "Authorization": "Bearer $ENV:MOSS_V7_HEADER_TEST",
            "X-API-Key": "$ENV:MOSS_V7_HEADER_TEST",
        })
        assert resolved["Authorization"] == "Bearer super-secret-value-123"
        assert resolved["X-API-Key"] == "super-secret-value-123"
        masked = mask_headers(resolved)
        assert "super-secret-value-123" not in json.dumps(masked)
    finally:
        os.environ.pop("MOSS_V7_HEADER_TEST", None)

    # Upsert Streamable HTTP MCP server & probe + test bidirectional elicitation & progress
    from mcp_configured import MCP_DATA, ServerSettings, open_server_session
    from mcp_modern import ACTIVE_MCP_BROKER, ACTIVE_MCP_PROGRESS

    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    r_up = await cli.post(
        "/api/mcp/servers",
        json={
            "name": "v7-test-srv",
            "transport": "streamable_http",
            "url": mcp_url,
            "read_only_only": False,
            "elicitation_mode": "prompt",
            "tools": {
                "trigger_elicit_and_progress": "触发交互",
                "HassTurnOn": "打开设备",
            },
        },
        headers={"Origin": origin},
        cookies={"moss_auth": cs_cookie},
    )
    assert r_up.status == 200, await r_up.text()

    r_probe = await cli.post(
        "/api/mcp/test",
        json={"name": "v7-test-srv"},
        headers={"Origin": origin},
        cookies={"moss_auth": cs_cookie},
    )
    j_probe = await r_probe.json()
    assert r_probe.status == 200 and j_probe.get("ok") is True, j_probe
    assert any(t.get("name") == "trigger_elicit_and_progress" for t in (j_probe.get("tools") or [])), j_probe

    async def _answer_elicit_from_http():
        for _ in range(60):
            r_list = await cli.get("/api/mcp/elicitations", cookies={"moss_auth": cs_cookie})
            j_list = await r_list.json()
            pending = j_list.get("items") or []
            if pending:
                eid = pending[0]["id"]
                r_ans = await cli.post(
                    f"/api/mcp/elicitations/{eid}",
                    json={"action": "accept", "content": {"room": "lab-7", "confirm": True}},
                    headers={"Origin": origin},
                    cookies={"moss_auth": cs_cookie},
                )
                assert r_ans.status == 200, await r_ans.text()
                return eid
            await asyncio.sleep(0.1)
        raise TimeoutError("Did not observe pending MCP elicitation over HTTP")

    elicit_task = asyncio.create_task(_answer_elicit_from_http())
    data_tok = MCP_DATA.set(moss.drafts.parent)
    b_tok = ACTIVE_MCP_BROKER.set(moss.mcp_broker)
    p_tok = ACTIVE_MCP_PROGRESS.set(moss.mcp_progress)
    try:
        settings = ServerSettings(
            transport="streamable_http",
            url=mcp_url,
            read_only_only=False,
            elicitation_mode="prompt",
            tools={"trigger_elicit_and_progress": "触发交互", "HassTurnOn": "打开设备"},
        )
        tid = moss.mcp_progress.start("v7-test-srv", "trigger_elicit_and_progress")
        async def _on_prog(progress, total, message):
            moss.mcp_progress.update(tid, progress, total, message)
        async with open_server_session("v7-test-srv", settings) as sess:
            res = await sess.call_tool("trigger_elicit_and_progress", {"device": "fan"}, progress_callback=_on_prog)
        moss.mcp_progress.finish(tid, status="completed", message="Done")
    finally:
        ACTIVE_MCP_PROGRESS.reset(p_tok)
        ACTIVE_MCP_BROKER.reset(b_tok)
        MCP_DATA.reset(data_tok)

    answered_id = await elicit_task
    assert answered_id.startswith("elicit-")
    txt = "".join(getattr(c, "text", "") for c in (res.content or []))
    assert "ok:lab-7:fan" in txt, txt

    r_prog = await cli.get("/api/mcp/progress", cookies={"moss_auth": cs_cookie})
    j_prog = await r_prog.json()
    assert any(t.get("tool") == "trigger_elicit_and_progress" and t.get("status") == "completed" for t in (j_prog.get("tasks") or [])), j_prog

    # Broker fast modes sanity check
    b = McpElicitationBroker()
    assert (await b.handle("s", type("P", (), {"mode": "form", "message": "m", "requestedSchema": None, "url": None, "elicitationId": None})(), mode="deny")).action == "decline"


async def test_pillar_3_smart_turn(cli: TestClient, cs_cookie: str, origin: str):
    pcm = _sine_pcm(freq_hz=260.0, seconds=1.2)
    r = await cli.post(
        "/api/transcribe/turn-check",
        data=pcm,
        headers={"Content-Type": "application/octet-stream", "Origin": origin},
        cookies={"moss_auth": cs_cookie},
    )
    assert r.status == 200, await r.text()
    jr = await r.json()
    assert jr.get("available") is True and isinstance(jr.get("probability"), float), jr
    assert 0.0 <= jr["probability"] <= 1.0
    assert suggest_silence_ms(0.90, base_ms=1550) < suggest_silence_ms(0.10, base_ms=1550)


def test_pillar_4_cognitive_and_slots(moss: Moss):
    # Cross-word BGE semantic retrieval + owner_code vec0 partition isolation
    moss.sag.upsert_event(
        title="深空射频故障指南",
        content="当星间链路出现持续丢包和误码率飙升时，先校准 Ka 频段相控阵馈源并重启备份编码器。",
        owner_code="MOSS",
    )
    moss.sag.upsert_event(
        title="访客私有笔记",
        content="星际通信链路检查私有备忘录内容。",
        owner_code="ALFA",
    )
    res_moss = moss.sag.search("星际通信故障排查", owner_code="MOSS", include_public=False)
    hits_moss = res_moss.get("direct") or []
    assert any(h.get("title") == "深空射频故障指南" for h in hits_moss), hits_moss
    assert all(h.get("title") != "访客私有笔记" for h in hits_moss), hits_moss

    # Bi-temporal as_of time-travel + SleepConsolidator supersession
    t_old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    t_mid = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    e_old = moss.sag.upsert_event(title="星盾项目排期", content="星盾项目 发版评审排期在 12月1日", valid_from=t_old)
    moss.sag.upsert_event(title="星盾项目排期", content="星盾项目 最新排期改为 12月15日", valid_from=t_mid)
    superseded = moss.consolidator._consolidate_bitemporal_supersessions(owner_code="MOSS")
    assert superseded >= 1

    active_now = moss.sag.search("星盾项目排期", include_invalidated=False)
    assert all(int(h["id"]) != int(e_old["id"]) for h in active_now["direct"]), active_now["direct"]
    past_hits = moss.sag.search("星盾项目排期", as_of=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds"))
    assert any(int(h["id"]) == int(e_old["id"]) for h in past_hits["direct"]), past_hits["direct"]

    # RSI MemRL utility EMA + ACE anti-collapse merge
    st1, l1 = moss.rsi.promote_lesson(
        scope="format",
        trigger="用户要求精简报告",
        rule="先输出三行结论摘要，再附核心数据表。",
        confidence=0.80,
    )
    assert st1 == "added" and l1 is not None
    lid = int(l1["id"])
    u0 = float(moss.rsi.get_lesson(lid)["utility_score"])
    moss.rsi.record_credit([lid], corrected=False, positive=True)
    u_pos = float(moss.rsi.get_lesson(lid)["utility_score"])
    assert u_pos > u0, (u0, u_pos)
    moss.rsi.record_credit([lid], corrected=True, positive=False)
    u_neg = float(moss.rsi.get_lesson(lid)["utility_score"])
    assert u_neg < u_pos, (u_pos, u_neg)

    st2, l2 = moss.rsi.promote_lesson(
        scope="format",
        trigger="用户要求精简报告",
        rule="先输出三行结论摘要，再附核心数据表并保留两位小数。",
        confidence=0.84,
    )
    assert st2 == "merged" and int(l2["id"]) == lid, (st2, l2)
    notes = json.loads(l2.get("delta_notes") or "[]")
    assert len(notes) >= 1, l2

    # Brain priority-tiered slot eviction
    sample_prompt = "\n".join([
        "核心指令：遵守安全核验准则并优先返回工具回执。",
        "用户画像：偏好简洁、高精度中文输出。",
        "- [L#1 format] 始终优先给结论",
        "- [E#1 文档] 长篇历史背景知识：" + ("补充上下文" * 35),
        "说话节奏与语气参考：" + ("柔和友好语气示例" * 20),
    ])
    trimmed = _trim_system_slots(sample_prompt, 120)
    assert "核心指令" in trimmed
    assert "柔和友好语气示例" not in trimmed


async def test_pillar_5_bandit_pwa_and_push(moss: Moss, cli: TestClient, cs_cookie: str, origin: str):
    ctx = "desk_morning"
    moss.pulse.record_bandit_outcome("ram_low", ctx, accepted=False)
    moss.pulse.record_bandit_outcome("ram_low", ctx, accepted=False)
    moss.pulse.record_bandit_outcome("ram_low", ctx, accepted=False)
    mul_rej = moss.pulse.bandit_cooldown_mul("ram_low", ctx, deterministic=True)
    moss.pulse.record_bandit_outcome("calendar_upcoming", ctx, accepted=True)
    moss.pulse.record_bandit_outcome("calendar_upcoming", ctx, accepted=True)
    mul_acc = moss.pulse.bandit_cooldown_mul("calendar_upcoming", ctx, deterministic=True)
    assert mul_rej > 1.0 and mul_acc <= 1.0, (mul_rej, mul_acc)

    # PWA endpoints
    r_man = await cli.get("/manifest.webmanifest")
    assert r_man.status == 200
    j_man = await r_man.json()
    assert j_man.get("display") == "standalone" and j_man.get("start_url") == "/mobile", j_man

    r_sw = await cli.get("/sw.js")
    assert r_sw.status == 200
    sw_txt = await r_sw.text()
    assert "/conversation-assets/" in sw_txt and "push" in sw_txt

    # Web Push DPAPI VAPID key + subscribe + 410 pruning
    r_vk = await cli.get("/api/push/vapid-key")
    assert r_vk.status == 200
    assert len((await r_vk.json()).get("publicKey") or "") >= 32

    sub = {
        "endpoint": "https://push.example.invalid/v7/sub-1",
        "keys": {"p256dh": "BElongFakeKeyForV7UnitCheck1234567890", "auth": "FakeAuthSecret12"},
    }
    r_sub = await cli.post("/api/push/subscribe", json=sub, headers={"Origin": origin}, cookies={"moss_auth": cs_cookie})
    assert r_sub.status == 200 and (await r_sub.json()).get("subscribed") is True

    class _Resp410:
        status_code = 410

    def _mock_410(**kwargs):
        raise WebPushException("Gone", response=_Resp410())

    res_send = moss.webpush.send_sync({"title": "t"}, _sender=_mock_410)
    assert res_send["pruned"] == 1 and moss.webpush.subscription_count() == 0


async def test_pillar_6_uia_ha_satellite_and_rbac(cli: TestClient, cs_cookie: str, c_cookie: str, origin: str, mcp_port: int):
    # CS allowed vs C 403 isolation across v7 admin routes
    r1 = await cli.get("/api/desktop/uia", cookies={"moss_auth": cs_cookie})
    j1 = await r1.json()
    assert r1.status == 200 and j1.get("ok") is True, j1
    assert (await cli.get("/api/desktop/uia", cookies={"moss_auth": c_cookie})).status == 403

    r2 = await cli.post(
        "/api/iot/ha-mcp-discover",
        json={"mcp_url": f"http://127.0.0.1:{mcp_port}/mcp"},
        headers={"Origin": origin},
        cookies={"moss_auth": cs_cookie},
    )
    j2 = await r2.json()
    assert r2.status == 200 and j2.get("ok") is True and any(t.get("name") == "HassTurnOn" for t in j2.get("tools", [])), j2
    assert (await cli.post("/api/iot/ha-mcp-discover", json={"mcp_url": f"http://127.0.0.1:{mcp_port}/mcp"}, headers={"Origin": origin}, cookies={"moss_auth": c_cookie})).status == 403

    r3 = await cli.post(
        "/api/hardware/moss/audio?room=study",
        data=_sine_pcm(seconds=1.0),
        headers={"Content-Type": "application/octet-stream", "Origin": origin},
        cookies={"moss_auth": cs_cookie},
    )
    j3 = await r3.json()
    assert r3.status == 200 and j3.get("room") == "study" and "probability" in (j3.get("turn_check") or {}), j3
    assert (await cli.post("/api/hardware/moss/audio?room=study", data=_sine_pcm(seconds=1.0), headers={"Content-Type": "application/octet-stream", "Origin": origin}, cookies={"moss_auth": c_cookie})).status == 403

    # FUI v7.0 HTML anchors
    html = (ROOT / "fui" / "public" / "index.html").read_text(encoding="utf-8")
    for dom_id in ("set-local-cascade", "comms-elicit-rack", "brain-sag-meta", "brain-sag-include-stale", "brain-pulse-bandit", "brain-ha-mcp-discover"):
        assert dom_id in html, f"Missing FUI v7 DOM anchor: {dom_id}"


async def main():
    mcp_port = _free_port()
    srv_port = _free_port()
    mcp_proc = _start_fastmcp_test_server(mcp_port)
    try:
        await _wait_http_ready(f"http://127.0.0.1:{mcp_port}/mcp")
        with tempfile.TemporaryDirectory() as tmp:
            old_data_dir = os.environ.get("MOSS_DATA_DIR")
            os.environ["MOSS_DATA_DIR"] = tmp
            moss = Moss(Path(tmp), "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
            stopped = asyncio.Event()
            try:
                app = application(
                    moss,
                    lambda: "standby",
                    Path(tmp),
                    srv_port,
                    "v7tok",
                    stopped,
                    fui_public=ROOT / "fui" / "public",
                )
                async with TestClient(TestServer(app, port=srv_port)) as cli:
                    origin = f"http://{cli.host}:{cli.port}"
                    vc = await cli.post("/api/auth/verify", json={"code": "ALFA"}, headers={"Origin": origin})
                    assert vc.status == 200
                    c_cookie = vc.cookies["moss_auth"].value

                    vs = await cli.post("/api/auth/verify", json={"code": "MOSS"}, headers={"Origin": origin})
                    assert vs.status == 200
                    cs_cookie = vs.cookies["moss_auth"].value

                    await test_pillar_1_inference_and_routing(moss, cli, cs_cookie, origin)
                    print("[v7 1/6] Inference, cascade & local budget PASS")

                    await test_pillar_2_modern_mcp(moss, cli, cs_cookie, origin, mcp_port)
                    print("[v7 2/6] Modern MCP Streamable HTTP + Elicitation + Progress PASS")

                    await test_pillar_3_smart_turn(cli, cs_cookie, origin)
                    print("[v7 3/6] smart-turn-v3 ONNX semantic turn detection PASS")

                    test_pillar_4_cognitive_and_slots(moss)
                    print("[v7 4/6] Bi-temporal sqlite-vec SAG + ACE/MemRL RSI + Brain slots PASS")

                    await test_pillar_5_bandit_pwa_and_push(moss, cli, cs_cookie, origin)
                    print("[v7 5/6] Contextual Bandit + PWA + VAPID WebPush PASS")

                    await test_pillar_6_uia_ha_satellite_and_rbac(cli, cs_cookie, c_cookie, origin, mcp_port)
                    print("[v7 6/6] Windows UIA + HA MCP Discover + ESP32 Audio + RBAC + FUI PASS")
            finally:
                stopped.set()
                await moss.close()
                if old_data_dir is None:
                    os.environ.pop("MOSS_DATA_DIR", None)
                else:
                    os.environ["MOSS_DATA_DIR"] = old_data_dir
    finally:
        mcp_proc.terminate()
        try:
            mcp_proc.wait(timeout=3)
        except Exception:
            mcp_proc.kill()
    print("ALL MOSS v7.0 EVOLUTION CHECKS PASSED (6/6 PILLARS)")


if __name__ == "__main__":
    asyncio.run(main())
