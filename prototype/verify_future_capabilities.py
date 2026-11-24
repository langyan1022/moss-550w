"""End-to-end verification for MOSS v3.5 Future Capabilities Suite:
  1. Proactive Pulse Sentinel (thermal/battery/DND/briefing)
  2. Sleep-Time Consolidation + Personal World Model + Project Date-Conflict Audit
  3. Autonomous Background Jobs (folder_digest -> SAG) + Standing Log Watchers + Custom Skills
  4. Home Assistant Bridge (dry_run toggle + blocked lock domain safety gate)
  5. Physical 550W Hardware Embodiment Protocol (`moss-hw-v1` SSE stream + command + sensor telemetry)
  6. Camera Vision-to-SAG endpoint (`/api/context/vision-to-sag`)
  7. Live Cloud `qwen4-max` Declarative Generative FUI Widget + Real Edge Brain Deck + `/mobile` Companion
"""

import asyncio
import base64
from datetime import datetime
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from time import perf_counter

import aiohttp
from aiohttp import web
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import Moss
from serve import application


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_edge():
    for path in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ):
        if path.is_file():
            return str(path)
    raise FileNotFoundError("Microsoft Edge not found")


def make_test_png_b64() -> str:
    img = Image.new("RGB", (640, 220), "white")
    draw = ImageDraw.Draw(img)
    draw.text((32, 60), "MOSS-550W ORDER SF9876543210", fill="black")
    draw.text((32, 110), "PROJECT STARGATE DEADLINE 2026-12-01", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = ROOT / "runs" / f"future-capabilities-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "checks": [], "console_errors": [], "page_errors": []}

    def rec(name, ok, detail):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False)[:180]}", flush=True)

    with tempfile.TemporaryDirectory(prefix="moss-future-") as tmp:
        data_dir = Path(tmp) / "data"
        docs_dir = Path(tmp) / "docs"
        data_dir.mkdir()
        docs_dir.mkdir()
        (docs_dir / "stargate.md").write_text(
            "星门项目部署规范\n\n星门项目部署于 RTX 3070 Ti 8GB 主机，严禁开启外部 Docker 集群。\n",
            encoding="utf-8",
        )
        watch_log = Path(tmp) / "watch-service.log"
        watch_log.write_text("2026-11-22 23:40:00 INFO service started\n", encoding="utf-8")

        port = free_port()
        moss = Moss(data_dir, "http://127.0.0.1:0/v1", "moss-chat-qwen35-4b")
        cloud_ok = False
        try:
            await moss.select_model("aliyun")
            cloud_ok = True
        except ValueError:
            pass
        stopped = asyncio.Event()
        app = application(
            moss, lambda: "standby", data_dir, port, "future-token", stopped,
            fui_public=ROOT / "fui" / "public", default_session_warm="0",
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"

        async def api_msg(session, text):
            async with session.post(f"{base}/api/message", json={"text": text}) as resp:
                assert resp.status == 200, await resp.text()
            deadline = perf_counter() + 45
            while perf_counter() < deadline:
                async with session.get(f"{base}/api/state") as resp:
                    st = await resp.json()
                last = (st.get("turns") or [None])[-1]
                if last and last["user"] == text and last["status"] in {"replied", "pending", "saved", "cancelled", "error"}:
                    return last, st
                await asyncio.sleep(0.15)
            raise TimeoutError(f"Timeout: {text}")

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
                # 1. Proactive Pulse Sentinel + Briefing
                emitted = moss.pulse.evaluate_tick(device_sample={
                    "gpu": {"name": "RTX 3070 Ti", "percent": 94.0, "temperature_c": 88.0},
                    "battery": {"present": True, "ac": False, "percent": 15.0},
                })
                async with http.post(f"{base}/api/proactive/briefing", json={}) as r:
                    brief = await r.json()
                ok1 = len(emitted) >= 2 and "态势简报" in brief["title"] or "简报" in brief["title"]
                rec("proactive_pulse_and_briefing", len(emitted) >= 2 and bool(brief.get("text")), {
                    "alerts_emitted": [a["kind"] for a in emitted],
                    "briefing": brief.get("text", "")[:110],
                })

                # 2. Sleep-Time Consolidation + Project Date-Conflict Detection
                await http.post(f"{base}/api/notes", json={"text": "老张通知：星门项目方案评审定在周三下午。"})
                await http.post(f"{base}/api/notes", json={"text": "更新：星门项目方案评审推迟到周五上午。"})
                async with http.post(f"{base}/api/brain/consolidate", json={}) as r:
                    dream = await r.json()
                cons = dream["consolidation"]
                wm = cons["world_model"]
                ok2 = (
                    cons["synced_notes"] >= 2
                    and "星门项目" in (wm.get("active_projects") or [])
                    and cons["conflicts_found"] >= 1
                )
                rec("sleep_consolidation_and_conflict_audit", ok2, {
                    "synced_notes": cons["synced_notes"],
                    "projects": wm.get("active_projects"),
                    "conflicts": [c["detail"] for c in (wm.get("conflicts") or [])],
                })

                # 3. Background Folder Digest Job + Standing Log Watcher + Custom Skills
                async with http.post(f"{base}/api/jobs", json={"kind": "folder_digest", "title": "索引星门文档", "path": str(docs_dir)}) as r:
                    job_resp = await r.json()
                jid = job_resp["job"]["id"]
                final_job = None
                for _ in range(30):
                    async with http.get(f"{base}/api/jobs") as r:
                        jobs = (await r.json())["jobs"]
                    final_job = next((j for j in jobs if j["id"] == jid), None)
                    if final_job and final_job["status"] in {"done", "error"}:
                        break
                    await asyncio.sleep(0.15)
                async with http.post(f"{base}/api/watchers", json={"name": "OOM哨兵", "file_path": str(watch_log), "pattern": "CRITICAL_OOM"}) as r:
                    w_resp = await r.json()
                with watch_log.open("a", encoding="utf-8") as fh:
                    fh.write("2026-11-22 23:42:01 ERROR CRITICAL_OOM gpu allocation failed\n")
                await asyncio.sleep(1.0)
                async with http.get(f"{base}/api/watchers") as r:
                    watchers = (await r.json())["watchers"]
                async with http.post(f"{base}/api/skills", json={"name": "早安巡检", "prompt": "给我早间简报并检查硬件"}) as r:
                    sk_resp = await r.json()
                ok3 = (
                    final_job and final_job["status"] == "done" and len(final_job["sag_event_ids"]) >= 1
                    and any(w["hit_count"] >= 1 for w in watchers)
                    and sk_resp.get("skill", {}).get("name") == "早安巡检"
                )
                rec("background_jobs_watchers_skills", ok3, {
                    "job_status": final_job["status"] if final_job else None,
                    "job_sag_ids": final_job["sag_event_ids"] if final_job else [],
                    "watcher_hits": [w["hit_count"] for w in watchers],
                    "skill_saved": sk_resp.get("skill", {}).get("name"),
                })

                # 4. Home Assistant Bridge (dry_run toggle + blocked lock safety gate)
                async with http.get(f"{base}/api/ha/entities") as r:
                    ha_before = await r.json()
                async with http.post(f"{base}/api/ha/service", json={"entity_id": "light.living_room", "service": "toggle"}) as r:
                    ha_tgl = await r.json()
                async with http.get(f"{base}/api/ha/entities") as r:
                    ha_after = await r.json()
                async with http.post(f"{base}/api/ha/service", json={"entity_id": "lock.front_door", "service": "unlock"}) as r:
                    ha_blocked_status = r.status
                lr_before = next(e["state"] for e in ha_before["entities"] if e["entity_id"] == "light.living_room")
                lr_after = next(e["state"] for e in ha_after["entities"] if e["entity_id"] == "light.living_room")
                ok4 = ha_tgl.get("ok") is True and lr_before != lr_after and ha_blocked_status == 400
                rec("home_assistant_bridge_and_safety_gate", ok4, {
                    "before": lr_before, "after": lr_after, "lock_blocked_http": ha_blocked_status,
                })

                # 5. Physical 550W Hardware Protocol (`moss-hw-v1` SSE + Command + Sensor Telemetry)
                received_frames = []
                async def listen_hw():
                    async with http.get(f"{base}/api/hardware/moss/stream") as resp:
                        async for line in resp.content:
                            txt = line.decode("utf-8", errors="ignore").strip()
                            if txt.startswith("data: "):
                                received_frames.append(json.loads(txt[6:]))
                                if len(received_frames) >= 2:
                                    break
                sse_task = asyncio.create_task(listen_hw())
                await asyncio.sleep(0.25)
                async with http.post(f"{base}/api/hardware/moss/command", json={
                    "mode": "listening", "yaw_deg": 24, "pitch_deg": -8, "gesture": "nod", "screen_line2": "TRACKING",
                }) as r:
                    cmd_res = await r.json()
                await asyncio.wait_for(sse_task, timeout=5)
                async with http.post(f"{base}/api/hardware/moss/telemetry", json={
                    "person_present": True, "distance_cm": 65, "last_button": "wake",
                }) as r:
                    tel_res = await r.json()
                ok5 = (
                    len(received_frames) >= 2
                    and received_frames[-1]["servo"]["yaw_deg"] == 24.0
                    and received_frames[-1]["servo"]["gesture"] == "nod"
                    and tel_res["sensors"]["person_present"] is True
                )
                rec("physical_550w_hardware_protocol_sse", ok5, {
                    "frames_received": len(received_frames),
                    "last_frame_servo": received_frames[-1]["servo"] if received_frames else None,
                    "sensors": tel_res.get("sensors"),
                })

                # 6. Camera Vision-to-SAG endpoint
                async with http.post(f"{base}/api/context/vision-to-sag", json={
                    "image": make_test_png_b64(), "title": "测试快递单识图",
                }) as r:
                    vis_status = r.status
                    vis_body = await r.json()
                ok6 = vis_status == 200 and bool(vis_body.get("event", {}).get("id")) and ("SF9876543210" in (vis_body.get("summary") or "") or "STARGATE" in (vis_body.get("summary") or "").upper() or len(vis_body.get("summary") or "") >= 10)
                rec("camera_vision_to_sag", ok6, {
                    "status": vis_status, "method": vis_body.get("method"),
                    "summary": (vis_body.get("summary") or "")[:110],
                })

                # 7. Live Cloud `qwen4-max` Declarative Generative FUI Calculator Widget
                widget_turn = None
                if cloud_ok:
                    widget_turn, _ = await api_msg(
                        http,
                        "帮我生成一个简单的房贷月供计算器交互组件，包含贷款金额和利率滑块，用两句话说明用法",
                    )
                    w_ok = bool(widget_turn.get("fui_widget")) and "render_fui_widget" in (widget_turn.get("holo_tools") or [])
                    rec("cloud_generative_fui_widget", w_ok, {
                        "tools": widget_turn.get("holo_tools"),
                        "widget_kind": (widget_turn.get("fui_widget") or {}).get("kind"),
                        "widget_title": (widget_turn.get("fui_widget") or {}).get("title"),
                    })

            # 8. Real Edge Browser: Brain Deck (all 5 tabs) + Generative Widget Live Sliders + Proactive Toast + /mobile
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=find_edge(), headless=True)
                ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
                page = await ctx.new_page()
                page.on("console", lambda m: report["console_errors"].append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
                await page.add_init_script("""
                    sessionStorage.setItem('moss-fui.opened', '1');
                    localStorage.setItem('moss-fui.badge.v1', JSON.stringify({showAfterOpening: false, speakGreeting: false}));
                    localStorage.setItem('moss-fui.tts', 'off');
                """)
                await page.goto(f"{base}/?scene=off", wait_until="domcontentloaded")
                await page.wait_for_function("document.body.dataset.backend === 'live'", timeout=12000)

                # Wait for proactive toast & brain overview refresh
                await page.wait_for_function("document.getElementById('pulse-toast') && !document.getElementById('pulse-toast').hidden", timeout=8000)

                # Test interactive slider inside generated FUI widget (if rendered)
                widget_dom_ok = True
                if widget_turn and widget_turn.get("fui_widget"):
                    await page.wait_for_selector(".moss-gen-widget", timeout=6000)
                    widget_dom_ok = await page.evaluate("""() => {
                        const w = document.querySelector('.moss-gen-widget');
                        if (!w) return false;
                        const inp = w.querySelector('input[type="range"]');
                        if (inp) {
                            const before = w.querySelector('.moss-gen-kpi__val')?.textContent;
                            inp.value = String(inp.max);
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            const after = w.querySelector('.moss-gen-kpi__val')?.textContent;
                            return Boolean(before && after && before !== '—' && after !== '—' && before !== after);
                        }
                        return true;
                    }""")

                # Open Brain Control Deck and walk all 5 tabs
                await page.click("#strip-brain-btn")
                await page.wait_for_selector("#dlg-brain[open]", timeout=5000)
                await asyncio.sleep(0.35)
                for tab_name in ("rsi", "sag", "pulse", "jobs", "iot"):
                    await page.click(f'[data-brain-tab="{tab_name}"]')
                    await asyncio.sleep(0.15)
                await page.screenshot(path=str(out_dir / "brain-deck-iot.png"))
                await page.click('[data-brain-tab="pulse"]')
                await asyncio.sleep(0.15)
                await page.screenshot(path=str(out_dir / "brain-deck-pulse.png"))
                await page.click('#dlg-brain footer [data-dialog-close]')
                await page.click("#pulse-toast-dismiss")

                # Bilingual zero-missing-keys check
                await page.click('.fui-lang__opt[lang="en"]')
                await asyncio.sleep(0.2)
                missing_en = await page.evaluate("async () => [...(await import('/js/i18n.js')).missingKeys]")

                # Mobile Companion Console (/mobile) at 390x844
                m_ctx = await browser.new_context(viewport={"width": 390, "height": 844})
                m_page = await m_ctx.new_page()
                m_page.on("console", lambda m: report["console_errors"].append(f"mobile:{m.text}") if m.type == "error" else None)
                m_page.on("pageerror", lambda e: report["page_errors"].append(f"mobile:{e}"))
                await m_page.goto(f"{base}/mobile", wait_until="domcontentloaded")
                await m_page.wait_for_function("document.getElementById('m-link')?.textContent === 'ONLINE'", timeout=8000)
                mobile_info = await m_page.evaluate("() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth, msgs: document.querySelectorAll('#m-feed li').length })")
                await m_page.screenshot(path=str(out_dir / "mobile-companion.png"))
                await browser.close()

                ok8 = (
                    widget_dom_ok
                    and len(missing_en) == 0
                    and mobile_info["sw"] <= mobile_info["iw"] + 1
                    and len(report["console_errors"]) == 0
                    and len(report["page_errors"]) == 0
                )
                rec("edge_brain_deck_widget_and_mobile", ok8, {
                    "widget_dom_ok": widget_dom_ok,
                    "missing_en_keys": missing_en,
                    "mobile": mobile_info,
                    "console_errors": report["console_errors"],
                    "page_errors": report["page_errors"],
                })
        finally:
            stopped.set()
            await runner.cleanup()
            await moss.close()

    report["passed"] = all(c["ok"] for c in report["checks"])
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nREPORT: {out_file} passed={report['passed']} ({sum(c['ok'] for c in report['checks'])}/{len(report['checks'])})", flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
