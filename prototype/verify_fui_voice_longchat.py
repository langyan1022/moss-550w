"""Playwright regression test targeting fui/public/ directly (closing legacy /static/ blindspots).

Covers:
  1. Long-chat (>45 turns) .moss-load-older chronological pagination on fui/public/index.html.
  2. .moss-gen-widget DOM node preservation across EN <-> ZH locale switches (renderEpoch fullReset).
  3. Comms voice controls (#comms-mic) graceful handling on headless Chromium without crash.
  4. /mobile (fui/public/mobile.html) 展开更早 chronological order, internal #m-feed scroll containment, and C-operator zero-403s.
  5. Offline / 503 /api/auth/status fallback cleanly starts demo mode without deadlock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import Moss
from serve import application
from testkit import safe_free_port


def _find_edge() -> str:
    for p in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ):
        if p.is_file():
            return str(p)
    return ""


async def _seed_long_chat(moss: Moss, owner: str = "MOSS", count: int = 46) -> str:
    chat_id = moss.active_chat_for(owner)
    base_dt = datetime.now(timezone.utc) - timedelta(minutes=count + 2)
    with moss.db:
        for idx in range(1, count + 1):
            tid = f"seed_{idx:03d}_{uuid4().hex[:6]}"
            ts_iso = (base_dt + timedelta(seconds=idx * 10)).isoformat(timespec="seconds")
            doc = {
                "id": tid,
                "chat_id": chat_id,
                "owner_code": owner,
                "user": f"LONGCHAT_MARKER_{idx:03d}",
                "status": "replied",
                "reply": f"回复第 {idx:03d} 轮确认完成。",
                "created_at": ts_iso,
                "replied_at": ts_iso,
            }
            if idx == count:
                doc["fui_widget"] = {
                    "kind": "calculator",
                    "title": "FUI Widget 保活测试计算器",
                    "subtitle": "Preserve across locale switch",
                    "spec": {
                        "inputs": [{"id": "x", "label": "本金", "min": 100, "max": 1000, "step": 100, "default": 500}],
                        "outputs": [{"label": "两倍值", "expr": "x * 2"}],
                    },
                }
            moss.db.execute(
                "INSERT INTO turns(id, doc, chat_id, cloud_requests) VALUES (?, ?, ?, '{\"complete\":true,\"by_provider\":{}}')",
                (tid, json.dumps(doc, ensure_ascii=False), chat_id),
            )
        moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
    return chat_id


async def main() -> dict:
    port = safe_free_port()
    fui_public = ROOT / "fui" / "public"
    with tempfile.TemporaryDirectory(prefix="moss-fui-longchat-", ignore_cleanup_errors=True) as td:
        td_path = Path(td)
        moss = Moss(td_path, "http://127.0.0.1:19999/v1", "mock-local", prefer_cloud=False)
        stopped = asyncio.Event()
        app = application(moss, lambda: "standby", td_path, port, "tok_longchat", stopped, fui_public=fui_public)
        server = TestServer(app, port=port)
        base = f"http://127.0.0.1:{port}"
        try:
            await server.start_server()
            await _seed_long_chat(moss, owner="MOSS", count=46)
            await _seed_long_chat(moss, owner="AB64", count=28)

            async with async_playwright() as pw:
                edge = _find_edge()
                launch_kw = {"headless": True, "args": ["--mute-audio"]}
                if edge:
                    launch_kw["executable_path"] = edge
                browser = await pw.chromium.launch(**launch_kw)
                try:
                    # 1. Desktop fui/public/index.html: pagination + widget preservation + voice toggle
                    ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
                    page = await ctx.new_page()
                    page_errors: list[str] = []
                    page.on("pageerror", lambda e: page_errors.append(str(e)))
                    await page.add_init_script("""
                        sessionStorage.setItem('moss-fui.opened', '1');
                        localStorage.setItem('moss-fui.badge.v1.MOSS', JSON.stringify({showAfterOpening: false, speakGreeting: false}));
                    """)
                    await page.goto(f"{base}/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                    await page.wait_for_function("document.body.dataset.backend === 'live'", timeout=15000)
                    await page.wait_for_selector(".moss-load-older", timeout=10000)

                    # Store reference to rendered .moss-gen-widget before clicking EN -> ZH toggle
                    await page.wait_for_selector(".moss-gen-widget", timeout=10000)
                    await page.evaluate("() => { window.__widgetBefore = document.querySelector('.moss-gen-widget'); }")
                    await page.click('.fui-lang__opt[lang="en"]')
                    await asyncio.sleep(0.15)
                    await page.click('.fui-lang__opt[lang="zh-CN"]')
                    await asyncio.sleep(0.15)
                    widget_preserved = await page.evaluate(
                        """() => {
                            const w = document.querySelector('.moss-gen-widget');
                            if (!w || w !== window.__widgetBefore) return false;
                            const inp = w.querySelector('input');
                            if (!inp) return false;
                            inp.value = '800';
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            const txt = w.textContent || '';
                            return txt.includes('1,600') || txt.includes('1600');
                        }"""
                    )
                    assert widget_preserved, ".moss-gen-widget DOM node or live reactivity failed after locale fullReset!"

                    # Click .moss-load-older -> earlier turns must appear chronologically BEFORE newer turns
                    await page.click(".moss-load-older")
                    await page.wait_for_function(
                        "() => (document.getElementById('comms-log')?.textContent || '').includes('LONGCHAT_MARKER_001')",
                        timeout=8000,
                    )
                    order_info = await page.evaluate("""() => {
                        const txt = document.getElementById('comms-log')?.textContent || '';
                        const iEarly = txt.indexOf('LONGCHAT_MARKER_001');
                        const iMid = txt.indexOf('LONGCHAT_MARKER_020');
                        const iLate = txt.indexOf('LONGCHAT_MARKER_046');
                        return { iEarly, iMid, iLate };
                    }""")
                    assert (
                        0 <= order_info["iEarly"] < order_info["iMid"] < order_info["iLate"]
                    ), f"Desktop pagination chronological order broken: {order_info}"

                    # Click #comms-mic twice (toggle & recover) without throwing pageerror
                    await page.click("#comms-mic")
                    await asyncio.sleep(0.2)
                    voice_status = (await page.locator("#comms-voice-status").text_content() or "").strip()
                    assert len(voice_status) > 0, "Empty voice status after #comms-mic click"
                    await page.click("#comms-mic")
                    await asyncio.sleep(0.15)
                    await ctx.close()

                    # 2. /mobile as C-operator AB64: 0 403s + internal #m-feed scroll + 展开更早 order
                    m_ctx = await browser.new_context(viewport={"width": 390, "height": 844})
                    m_err_403: list[str] = []
                    m_page = await m_ctx.new_page()
                    m_page.on("pageerror", lambda e: page_errors.append(f"mobile:{e}"))
                    m_page.on("response", lambda r: m_err_403.append(f"{r.status}:{r.url}") if r.status == 403 else None)
                    await m_ctx.request.post(f"{base}/api/auth/verify", data=json.dumps({"code": "AB64"}),
                                             headers={"Content-Type": "application/json", "Origin": base})
                    await m_page.goto(f"{base}/mobile", wait_until="domcontentloaded")
                    await m_page.wait_for_function("document.getElementById('m-link')?.textContent === 'ONLINE'", timeout=10000)
                    await m_page.wait_for_selector('#m-feed li[data-load-older="1"] button', timeout=8000)

                    scroll_box = await m_page.evaluate("""() => {
                        const f = document.getElementById('m-feed');
                        return { sh: f?.scrollHeight || 0, ch: f?.clientHeight || 0, docSh: document.documentElement.scrollHeight, docCh: window.innerHeight };
                    }""")
                    assert scroll_box["sh"] > scroll_box["ch"] and scroll_box["docSh"] <= scroll_box["docCh"] + 4, (
                        f"Mobile #m-feed did not contain scroll internally: {scroll_box}"
                    )

                    await m_page.click('#m-feed li[data-load-older="1"] button')
                    await m_page.wait_for_function(
                        "() => (document.getElementById('m-feed')?.textContent || '').includes('LONGCHAT_MARKER_001')",
                        timeout=8000,
                    )
                    mob_order = await m_page.evaluate("""() => {
                        const txt = document.getElementById('m-feed')?.textContent || '';
                        return {
                            i1: txt.indexOf('LONGCHAT_MARKER_001'),
                            i15: txt.indexOf('LONGCHAT_MARKER_015'),
                            i28: txt.indexOf('LONGCHAT_MARKER_028'),
                        };
                    }""")
                    assert 0 <= mob_order["i1"] < mob_order["i15"] < mob_order["i28"], f"Mobile 展开更早 order broken: {mob_order}"
                    assert len(m_err_403) == 0, f"Unexpected 403s on C-role /mobile: {m_err_403}"
                    await m_ctx.close()

                    # 3. 503 auth/status resilience -> enters offline mode WITHOUT deadlocking behind #dlg-auth-gate
                    off_ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
                    off_page = await off_ctx.new_page()
                    off_page.on("pageerror", lambda e: page_errors.append(f"offline:{e}"))
                    await off_page.add_init_script("sessionStorage.setItem('moss-fui.opened', '1');")
                    await off_page.route("**/api/auth/status", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"offline"}'))
                    await off_page.route("**/api/state", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"offline"}'))
                    await off_page.route("**/api/events*", lambda route: route.abort())
                    await off_page.goto(f"{base}/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                    await off_page.wait_for_function("() => ['demo', 'offline'].includes(document.body.dataset.backend)", timeout=12000)
                    await asyncio.sleep(0.25)
                    off_info = await off_page.evaluate("""() => ({
                        backend: document.body.dataset.backend,
                        gateOpen: Boolean(document.getElementById('dlg-auth-gate')?.open),
                    })""")
                    assert not off_info["gateOpen"], f"503 offline mode deadlocked behind #dlg-auth-gate: {off_info}"
                    off_backend = off_info["backend"]
                    await off_ctx.close()

                    assert len(page_errors) == 0, f"Uncaught page errors: {page_errors}"
                    summary = {
                        "desktop_order": order_info,
                        "widget_preserved": widget_preserved,
                        "mobile_scroll": scroll_box,
                        "mobile_order": mob_order,
                        "mobile_403": len(m_err_403),
                        "offline_fallback": off_backend,
                    }
                    print("FUI_VOICE_LONGCHAT_PASS:", json.dumps(summary, ensure_ascii=False))
                    return summary
                finally:
                    await browser.close()
        finally:
            stopped.set()
            await server.close()
            await moss.close()


if __name__ == "__main__":
    asyncio.run(main())
