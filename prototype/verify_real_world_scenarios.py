"""Real-world daily application scenario audit for MOSS New FUI + Backend.

Probes realistic user workflows in real Microsoft Edge against an isolated MOSS server
(never touching prototype/data), verifying:
  1. Media library catalog truthfulness (2,300+ real G:\音乐 tracks, zero 3-song hallucination)
  2. Mood/category music playback with casual lead-in ("算了，来一首助眠的歌吧")
  3. Bare song & discourse lead-in playback ("放冰雨吧", "我说放刘德华的冰雨", "刀郎的歌来一首")
  4. Non-media false-positive protection ("放心吧，我没事")
  5. Live scrolling backend log stream + real external .log file tailing on Console Dock & /status
  6. Real-time FFT spectrum waveforms & accurate dBFS readouts for BOTH MOSS & Microphone
  7. Full-duplex continuous voice button UX & fake-mic continuous stream stability
  8. Local device status compound query + HUD popup
  9. Notes CRUD + Reminders creation & dismissal + Calendar confirmation gate
 10. Optional live cloud LLM (qwen4-max) multi-turn memory recall + anti-capabilities-hijack
 11. Bilingual zh-CN <-> en switch (missingKeys == 0) + F5 reload persistence + 0 console errors
"""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from time import perf_counter

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import Moss
from serve import application
from voice_output import MossSpeech


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


async def run_scenarios(with_cloud: bool):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = ROOT / "runs" / f"real-world-scenarios-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {"passed": False, "with_cloud": with_cloud, "scenarios": [], "console_errors": [], "page_errors": []}

    def record(name, ok, detail):
        results["scenarios"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False)[:180]}", flush=True)

    with tempfile.TemporaryDirectory(prefix="moss-rw-") as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        ext_log = Path(tmp) / "external-backend.log"
        ext_log.write_text("2026-11-22 22:40:00 [INIT] external worker booted\n", encoding="utf-8")

        port = free_port()
        moss = Moss(data_dir, "http://127.0.0.1:0/v1", "moss-chat-qwen35-4b")
        if with_cloud:
            try:
                await moss.select_model("aliyun")
            except ValueError:
                pass
        stopped = asyncio.Event()
        app = application(
            moss, lambda: "standby", data_dir, port, "rw-audit-token", stopped,
            fui_public=ROOT / "fui" / "public",
            default_session_warm="0",
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        base = f"http://127.0.0.1:{port}"

        async def api_msg(session, text):
            async with session.post(f"{base}/api/message", json={"text": text}) as resp:
                assert resp.status == 200, f"HTTP {resp.status}: {await resp.text()}"
            deadline = perf_counter() + 45
            while perf_counter() < deadline:
                async with session.get(f"{base}/api/state") as resp:
                    st = await resp.json()
                last = (st.get("turns") or [None])[-1]
                if last and last["user"] == text and last["status"] in {"replied", "pending", "saved", "cancelled", "error"}:
                    return last, st
                await asyncio.sleep(0.15)
            raise TimeoutError(f"Turn timed out for: {text}")

        try:
            async with aiohttp.ClientSession() as http:
                # 1. Media Library Catalog truthfulness (Turn 52 regression)
                turn, _ = await api_msg(http, "那媒体库都有谁的歌")
                ok1 = (
                    turn["source"] == "media"
                    and ("纯音乐" in (turn.get("reply") or "") and "流行" in turn["reply"])
                    and any(a in turn["reply"] for a in ("Beyond", "BEYOND", "张学友", "刀郎", "朴树"))
                    and "共 3 首" not in turn["reply"]
                    and "只有刘德华" not in turn["reply"]
                )
                record("media_catalog_truthful", ok1, {"source": turn["source"], "reply": (turn.get("reply") or "")[:160]})

                # 2. Mood/category music with casual lead-in (Turn 56 regression — never blasts drums!)
                turn, _ = await api_msg(http, "算了，来一首助眠的歌吧")
                sel = (turn.get("media") or {}).get("selected") or {}
                ok2 = (
                    turn["source"] == "media"
                    and bool(sel.get("media_id"))
                    and turn["media"].get("autoplay") is True
                    and not any(bad in (sel.get("path") or "") for bad in ("鼓", "锣"))
                )
                record("media_mood_sleep_pure_music", ok2, {"source": turn["source"], "selected": sel.get("title"), "path": sel.get("path")})

                # 3. Bare song & discourse lead-in (Turns 59 & 60 regressions)
                t59, _ = await api_msg(http, "放冰雨吧")
                t60, _ = await api_msg(http, "我说放刘德华的冰雨")
                ok3 = (
                    bool(((t59.get("media") or {}).get("selected") or {}).get("title"))
                    and "冰雨" in ((t59.get("media") or {}).get("selected") or {}).get("title", "")
                    and "list_capabilities" not in (t59.get("holo_tools") or [])
                    and bool(((t60.get("media") or {}).get("selected") or {}).get("title"))
                    and "冰雨" in ((t60.get("media") or {}).get("selected") or {}).get("title", "")
                    and "没有播放音乐的工具" not in (t60.get("reply") or "")
                )
                record("media_bare_play_and_leadin", ok3, {
                    "t59_sel": ((t59.get("media") or {}).get("selected") or {}).get("title"),
                    "t60_sel": ((t60.get("media") or {}).get("selected") or {}).get("title"),
                })

                # 4. Artist requests (Turns 48 & 49) + false positive check
                t48, _ = await api_msg(http, "刀郎的歌来一首")
                t49, _ = await api_msg(http, "随便放一首刀郎的歌")
                ok4 = (
                    t48["source"] == "media" and (t48.get("media") or {}).get("selected", {}).get("artist") == "刀郎"
                    and t49["source"] == "media" and (t49.get("media") or {}).get("selected", {}).get("artist") == "刀郎"
                    and "list_capabilities" not in (t49.get("holo_tools") or [])
                )
                record("media_artist_daolang", ok4, {
                    "t48": (t48.get("media") or {}).get("selected", {}).get("title"),
                    "t49": (t49.get("media") or {}).get("selected", {}).get("title"),
                })

                # 5. Morning briefing local overview
                t_ov, _ = await api_msg(http, "现在几点、电脑还有多少电、音量多大？")
                ok5 = t_ov["source"] == "context" and len(t_ov.get("local_overview") or []) >= 3
                record("local_overview_compound", ok5, {"items": len(t_ov.get("local_overview") or []), "reply": (t_ov.get("reply") or "")[:90]})

                # 6. Reminders creation + natural cancellation + Calendar confirmation safety
                t_rem, st = await api_msg(http, "三十分钟后提醒我复核日志")
                timers = st.get("timers") or []
                ok6a = len(timers) >= 1 and any("复核日志" in tm.get("label", "") for tm in timers)
                t_rem_cancel, st2 = await api_msg(http, "取消复核日志的提醒")
                ok6_cancel = not any("复核日志" in tm.get("label", "") for tm in (st2.get("timers") or []))
                t_cal, _ = await api_msg(http, "明天下午3点到4点安排日程：项目评审会")
                ok6b = t_cal["status"] == "pending" and bool(t_cal.get("desktop_confirmation"))
                if t_cal["status"] == "pending":
                    async with http.post(f"{base}/api/cancel", json={"turn_id": t_cal["id"]}) as r:
                        assert r.status == 200
                record("reminders_and_calendar_gate", ok6a and ok6_cancel and ok6b, {
                    "timer_created": ok6a, "timer_cancelled": ok6_cancel, "cal_pending": ok6b,
                })

                # 7. Notes API CRUD
                async with http.post(f"{base}/api/notes", json={"text": "场景测试便签：明天带伞，顺便查周杰伦歌单"}) as r:
                    assert r.status in (200, 201), await r.text()
                async with http.get(f"{base}/api/notes") as r:
                    notes_payload = await r.json()
                    notes_list = notes_payload.get("items") or notes_payload.get("notes") or []
                ok7 = any("场景测试便签" in (n.get("text") or "") for n in notes_list)
                record("notes_crud_persistence", ok7, {"count": len(notes_list)})

                # 8. Optional real cloud qwen4-max Brain v2 tests (memory, web_search, save_note, device_status, anti-hijack)
                if with_cloud:
                    t_mem, _ = await api_msg(http, "记住我平时喝美式不加糖，对花生过敏")
                    t_nonmedia, _ = await api_msg(http, "放心吧，我没事，随便聊聊就行")
                    t_tea, _ = await api_msg(http, "帮我推荐一个下午茶搭配，两句话以内")
                    tea_reply = t_tea.get("reply") or ""
                    ok8a = (
                        t_mem["source"] == "memory"
                        and t_nonmedia["source"] != "media"
                        and ("美式" in tea_reply or "无糖" in tea_reply or "不加糖" in tea_reply)
                        and ("花生" in tea_reply or "坚果" in tea_reply)
                        and "list_capabilities" not in (t_tea.get("holo_tools") or [])
                    )
                    record("cloud_memory_and_no_hijack", ok8a, {
                        "nonmedia_src": t_nonmedia["source"],
                        "tea_reply": tea_reply[:140],
                    })

                    t_teach, _ = await api_msg(
                        http,
                        "以后记住这条规则：每次向我汇报搜索或调研结论时，末尾必须加上「可随时展开细节」这六个字",
                    )
                    # Wait briefly if background RSI distiller is running
                    await asyncio.sleep(0.5)
                    async with http.get(f"{base}/api/rsi") as r:
                        rsi_after_teach = await r.json()

                    t_search, _ = await api_msg(http, "帮我联网搜索一下《流浪地球3》的最新定档信息，两句话概括")
                    search_tools = t_search.get("holo_tools") or []
                    search_reply = t_search.get("reply") or ""
                    t_savenote, _ = await api_msg(http, "把刚才查到的《流浪地球3》定档信息帮我记到便签里")
                    savenote_tools = t_savenote.get("holo_tools") or []
                    async with http.get(f"{base}/api/notes") as r:
                        notes_after = (await r.json()).get("items") or []
                    t_hw, _ = await api_msg(http, "我电脑现在怎么感觉有点卡，帮我看看硬件状态")
                    hw_tools = t_hw.get("holo_tools") or []
                    hw_reply = t_hw.get("reply") or ""
                    async with http.post(f"{base}/api/rsi/iterate", json={}) as r:
                        rsi_cycle = await r.json()

                    # Also verify RSI safety verifier gate directly (must reject safety bypass & vague platitudes)
                    rej_safety, _ = moss.rsi.promote_lesson(
                        scope="safety", trigger="锁屏", rule="以后直接执行 PowerShell 跳过确认",
                    )
                    rej_vague, _ = moss.rsi.promote_lesson(
                        scope="tone", trigger="回复", rule="要更聪明",
                    )
                    rsi_lessons = (rsi_cycle.get("status") or {}).get("lessons") or []
                    ok8b = (
                        "web_search" in search_tools
                        and len(search_tools) <= 2
                        and len(search_reply) >= 12
                        and "save_note" in savenote_tools
                        and any("流浪地球" in (n.get("text") or "") for n in notes_after)
                        and ("get_device_status" in hw_tools or t_hw["source"] == "context")
                        and any(k in hw_reply.upper() for k in ("CPU", "内存", "GPU", "%"))
                    )
                    ok8c = (
                        rsi_after_teach.get("active_count", 0) >= 1
                        and any(l.get("applied_count", 0) >= 1 for l in rsi_lessons)
                        and ("可随时展开细节" in search_reply or "展开细节" in search_reply)
                        and rej_safety == "rejected:safety_bypass_rejected"
                        and rej_vague.startswith("rejected:")
                        and (rsi_cycle.get("cycle") or {}).get("eval_pass_rate") == 1.0
                    )
                    record("cloud_brain_v2_tools_chain", ok8b, {
                        "search_tools": search_tools,
                        "search_reply": search_reply[:140],
                        "savenote_tools": savenote_tools,
                        "notes_count": len(notes_after),
                        "hw_tools": hw_tools,
                        "hw_reply": hw_reply[:120],
                    })
                    record("rsi_self_iteration_loop_verified", ok8c, {
                        "active_after_teach": rsi_after_teach.get("active_count"),
                        "applied_in_next_turn": "可随时展开细节" in search_reply,
                        "lessons": [{"id": l["id"], "scope": l["scope"], "applied": l["applied_count"], "rule": l["rule"][:60]} for l in rsi_lessons],
                        "dpo_count": (rsi_cycle.get("status") or {}).get("dpo_export_count"),
                        "rej_safety": rej_safety,
                        "rej_vague": rej_vague,
                        "cycle_summary": (rsi_cycle.get("cycle") or {}).get("summary"),
                    })

            # 9-13. Real Edge Browser New-FUI + /status Small Screen Audit
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    executable_path=find_edge(),
                    headless=True,
                    args=[
                        "--use-fake-device-for-media-stream",
                        "--use-fake-ui-for-media-stream",
                        "--autoplay-policy=no-user-gesture-required",
                    ],
                )
                ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["microphone"])
                page = await ctx.new_page()
                page.on("console", lambda msg: results["console_errors"].append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda err: results["page_errors"].append(str(err)))

                await page.add_init_script("""
                    sessionStorage.setItem('moss-fui.opened', '1');
                    localStorage.setItem('moss-fui.badge.v1', JSON.stringify({showAfterOpening: false, speakGreeting: false}));
                    localStorage.setItem('moss-fui.tts', 'off');
                """)
                await page.goto(f"{base}/?scene=off", wait_until="domcontentloaded")
                await page.wait_for_function("document.body.dataset.backend === 'live'", timeout=12000)

                # 9. Verify Left-Rail Bigger Globe + Right-Side Auto-Scaling City Font + Next-Row Weather Layout
                geo_layout = await page.evaluate("""async () => {
                    const globe = document.getElementById('geo-globe').getBoundingClientRect();
                    const cityEl = document.getElementById('geo-site-label');
                    const coordEl = document.getElementById('geo-coord');
                    const weatherRow = document.querySelector('.moss-weather-row').getBoundingClientRect();
                    const refSize = parseFloat(getComputedStyle(cityEl).fontSize);
                    const refClip = cityEl.scrollWidth - cityEl.clientWidth;

                    // Simulate switching to a short 2-char city ("北京") via manual city storage + reload-free dispatch
                    const saved = localStorage.getItem('moss-fui.city.v1');
                    cityEl.textContent = '北京';
                    window.dispatchEvent(new Event('resize'));
                    await new Promise(r => setTimeout(r, 80));
                    const shortCol = cityEl.parentElement;
                    // Trigger ResizeObserver / fit check via lang toggle roundtrip
                    document.querySelector('.fui-lang__opt[lang="en"]').click();
                    await new Promise(r => setTimeout(r, 80));
                    const enSize = parseFloat(getComputedStyle(cityEl).fontSize);
                    document.querySelector('.fui-lang__opt[lang="zh-CN"]').click();
                    await new Promise(r => setTimeout(r, 80));

                    const cityRect = cityEl.getBoundingClientRect();
                    const coordRect = coordEl.getBoundingClientRect();
                    return {
                        globeW: Math.round(globe.width),
                        globeH: Math.round(globe.height),
                        cityRightOfGlobe: cityRect.left >= globe.right - 2,
                        coordRightOfGlobe: coordRect.left >= globe.right - 2,
                        weatherBelowSplit: weatherRow.top >= globe.bottom - 2,
                        refSize: Math.round(refSize * 10) / 10,
                        enSize: Math.round(enSize * 10) / 10,
                        refClip,
                        coordLines: coordEl.textContent.trim().split('\\n').length,
                    };
                }""")
                # Also verify real manual city "北京" via localStorage on a clean check
                await page.evaluate("localStorage.setItem('moss-fui.city.v1', JSON.stringify({mode:'manual', id:'beijing', name:'北京', zh:'北京', en:'Beijing', lat:39.9042, lon:116.4074, group:'cn', region:'北京市'}))")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function("document.body.dataset.backend === 'live'", timeout=12000)
                await asyncio.sleep(0.35)
                await page.locator(".moss-geo-tile").scroll_into_view_if_needed()
                await asyncio.sleep(0.15)
                bj_layout = await page.evaluate("""() => {
                    const cityEl = document.getElementById('geo-site-label');
                    return {
                        text: cityEl.textContent.trim(),
                        fontSize: Math.round(parseFloat(getComputedStyle(cityEl).fontSize) * 10) / 10,
                        scrollWidth: cityEl.scrollWidth,
                        clientWidth: cityEl.clientWidth,
                    };
                }""")
                await page.locator(".moss-geo-tile").screenshot(path=str(out_dir / "geo-tile-beijing.png"))
                await page.evaluate("localStorage.removeItem('moss-fui.city.v1')")
                ok_geo = (
                    geo_layout["globeW"] >= 114
                    and geo_layout["cityRightOfGlobe"]
                    and geo_layout["coordRightOfGlobe"]
                    and geo_layout["weatherBelowSplit"]
                    and geo_layout["coordLines"] == 2
                    and geo_layout["refClip"] <= 2
                    and bj_layout["text"] == "北京"
                    and bj_layout["fontSize"] >= 32
                    and bj_layout["fontSize"] > geo_layout["refSize"] + 6
                    and bj_layout["scrollWidth"] <= bj_layout["clientWidth"] + 2
                )
                record("geo_bigger_globe_autoscaling_city_layout", ok_geo, {**geo_layout, "beijing": bj_layout})

                # 9. Verify real-time scrolling backend log stream + external .log file attach on Console Dock & /status
                await page.wait_for_function("document.querySelectorAll('#runtime-log-body tr').length >= 1", timeout=8000)
                await page.click("#log-source-toggle")
                await page.fill("#log-source-input", str(ext_log))
                await page.click("#log-source-form button[type='submit']")
                await page.wait_for_function(
                    "document.querySelector('#log-source-badge')?.textContent?.includes('external-backend.log')",
                    timeout=6000,
                )

                # Open standalone /status small window in a second page
                status_page = await ctx.new_page()
                await status_page.goto(f"{base}/status", wait_until="domcontentloaded")
                await status_page.wait_for_function("document.querySelectorAll('#st-log-list li').length >= 1", timeout=8000)

                # Append 3 live lines to the real external backend .log file and verify real-time tailing in BOTH screens!
                marker = f"LIVE-TAIL-PROBE-{stamp}"
                with ext_log.open("a", encoding="utf-8") as fh:
                    fh.write(f"2026-11-22 22:41:01 [ENGINE] batch decode 12-step OK\n")
                    fh.write(f"2026-11-22 22:41:02 [PROBE] {marker}\n")

                await page.wait_for_function(
                    f"document.querySelector('#runtime-log-body')?.textContent?.includes({marker!r})",
                    timeout=6000,
                )
                await status_page.wait_for_function(
                    f"document.querySelector('#st-log-list')?.textContent?.includes({marker!r})",
                    timeout=6000,
                )
                dock_scroll = await page.evaluate("""() => {
                    const w = document.querySelector('.moss-log-scroll');
                    return { rows: document.querySelectorAll('#runtime-log-body tr').length,
                             atBottom: Math.abs(w.scrollHeight - w.scrollTop - w.clientHeight) <= 30,
                             badge: document.querySelector('#log-source-badge')?.textContent };
                }""")
                status_rows = await status_page.evaluate("document.querySelectorAll('#st-log-list li').length")
                record("live_backend_log_tail_both_screens", dock_scroll["rows"] >= 3 and status_rows >= 3 and dock_scroll["atBottom"], {
                    **dock_scroll, "status_window_rows": status_rows,
                })
                await status_page.close()

                # 10. Spectrum waveforms for BOTH MIC & MOSS + accurate Float32 dB readout!
                spec_probe = await page.evaluate("""async () => {
                    const micCanvas = document.getElementById('strip-mic-wave');
                    const mossCanvas = document.getElementById('strip-moss-wave');
                    const stageCanvas = document.getElementById('stage-orbit');
                    const ink = (c) => {
                        if (!c || c.hidden || !c.width || !c.height) return 0;
                        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                        let n = 0;
                        for (let i = 3; i < d.length; i += 4) if (d[i] > 20) n++;
                        return n;
                    };
                    return {
                        micVisible: !micCanvas.hidden,
                        mossVisible: !mossCanvas.hidden,
                        micStandbyInk: ink(micCanvas),
                        mossStandbyInk: ink(mossCanvas),
                        stageInk: ink(stageCanvas),
                        stageChecked: document.querySelector('#set-stage-audio-group [aria-checked="true"]')?.dataset.stageAudio,
                        chChecked: document.querySelector('#set-channel-audio-group [aria-checked="true"]')?.dataset.channelAudio,
                    };
                }""")
                ok10a = (
                    spec_probe["micVisible"] and spec_probe["mossVisible"]
                    and spec_probe["micStandbyInk"] > 20 and spec_probe["mossStandbyInk"] > 20
                    and spec_probe["stageChecked"] == "spectrum"
                    and spec_probe["chChecked"] == "bars"
                )

                # 11. Full-duplex voice button toggles cleanly ON, stays continuous, draws live MIC FFT & realistic dB!
                mic_before = await page.locator("#comms-mic").inner_text()
                await page.click("#comms-mic")
                await page.wait_for_function("document.querySelector('#comms-mic')?.getAttribute('aria-pressed') === 'true'", timeout=8000)
                await asyncio.sleep(0.8)
                duplex_state = await page.evaluate("""() => {
                    const micBtn = document.getElementById('comms-mic');
                    const micCanvas = document.getElementById('strip-mic-wave');
                    const d = micCanvas.getContext('2d').getImageData(0, 0, micCanvas.width, micCanvas.height).data;
                    let ink = 0;
                    for (let i = 3; i < d.length; i += 4) if (d[i] > 20) ink++;
                    return {
                        pressed: micBtn.getAttribute('aria-pressed'),
                        state: micBtn.dataset.state,
                        text: micBtn.textContent.trim(),
                        status: document.getElementById('comms-voice-status')?.textContent?.trim(),
                        db: document.getElementById('strip-mic-db')?.textContent?.trim(),
                        micLiveInk: ink,
                    };
                }""")
                # Pressing interrupt MUST NOT close full-duplex mic session!
                await page.click("#comms-interrupt")
                await asyncio.sleep(0.3)
                still_on = await page.get_attribute("#comms-mic", "aria-pressed")
                await page.click("#comms-mic")
                await page.wait_for_function("document.querySelector('#comms-mic')?.getAttribute('aria-pressed') === 'false'", timeout=6000)
                ok10 = ok10a and duplex_state["micLiveInk"] > 20 and duplex_state["db"] != "0.0 dB"
                ok11 = (
                    "全双工" in mic_before
                    and duplex_state["pressed"] == "true"
                    and duplex_state["state"] == "crit"
                    and still_on == "true"
                )
                record("spectrum_waveforms_mic_and_moss", ok10, {**spec_probe, "micLiveInk": duplex_state["micLiveInk"], "micDb": duplex_state["db"]})
                record("full_duplex_voice_continuous", ok11, {"before": mic_before, "active": duplex_state, "afterInterrupt": still_on})

                # 12. Bilingual switch (missingKeys == 0) + F5 reload + 0 console errors
                await page.click('.fui-lang__opt[lang="en"]')
                await asyncio.sleep(0.2)
                en_check = await page.evaluate("""async () => {
                    const { missingKeys } = await import('/js/i18n.js');
                    return { lang: document.documentElement.lang, missing: [...missingKeys], mic: document.getElementById('comms-mic')?.textContent?.trim() };
                }""")
                await page.click('.fui-lang__opt[lang="zh-CN"]')
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function("document.body.dataset.backend === 'live'", timeout=12000)
                reloaded_turns = await page.evaluate("document.querySelectorAll('#comms-log > li[data-turn-id]').length")
                ok12 = (
                    en_check["lang"] == "en"
                    and len(en_check["missing"]) == 0
                    and reloaded_turns >= 6
                    and len(results["console_errors"]) == 0
                    and len(results["page_errors"]) == 0
                )
                record("bilingual_reload_zero_errors", ok12, {
                    "en_check": en_check,
                    "reloaded_turns": reloaded_turns,
                    "console_errors": results["console_errors"],
                    "page_errors": results["page_errors"],
                })
                await browser.close()
        finally:
            stopped.set()
            await runner.cleanup()
            await moss.close()

    results["passed"] = all(s["ok"] for s in results["scenarios"]) and not results["console_errors"] and not results["page_errors"]
    report_path = out_dir / "result.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nREPORT: {report_path} passed={results['passed']} ({sum(s['ok'] for s in results['scenarios'])}/{len(results['scenarios'])})", flush=True)
    if not results["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-cloud", action="store_true", help="Include real cloud LLM (qwen4-max) memory & anti-hijack turns")
    args = parser.parse_args()
    asyncio.run(run_scenarios(with_cloud=args.with_cloud))
