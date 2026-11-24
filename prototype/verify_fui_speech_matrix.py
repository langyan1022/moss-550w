"""Verify MOSS New-FUI speech latency matrix (cold vs foreground pre-warm, 3D ON vs light, CPU load, VRAM release)."""

import argparse
import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import ROOT, confirm_muted_edge
from core import Moss
from device_status import GIB, memory_sample
from local_media.library import MediaLibrary
from model_catalog import Profile
from pydantic import SecretStr
from serve import application
from verify_context_reply import free_port
from verify_fui_speech_latency import FUI_PUBLIC, FUI_SPEECH_PROBE, LONG_SAMPLE, make_tone_wav, resources_snapshot, summarize_trace
from verify_speech_prepare import gpu_processes
from verify_speech_prepare_ab import free_gpu, other_speaker
from voice_output import MossSpeech


def speech_worker_pids(root_pid=None):
    parent = psutil.Process(root_pid or os.getpid())
    found = []
    for child in parent.children(recursive=True):
        try:
            if any(Path(arg).name == "managed_worker.py" for arg in child.cmdline()):
                found.append(child.pid)
        except psutil.NoSuchProcess:
            continue
    return sorted(found)


def _cpu_burn_worker(stop_epoch: float):
    import math
    import time
    x = 1.0001
    while time.monotonic() < stop_epoch:
        for _ in range(50000):
            x = math.sin(x) * math.cos(x) + 1.0001
    return x


def build_probe(*, scene_3d: bool, voice_resident: bool) -> str:
    scene_flag = "on" if scene_3d else "off"
    resident_flag = "on" if voice_resident else "off"
    extra = f"""
localStorage.setItem('moss-fui.scene3d', '{scene_flag}');
localStorage.setItem('moss-fui.voice-resident', '{resident_flag}');
window.fuiSpeechWarmEvents = [];
window.addEventListener('moss-speech-warm-state', (ev) => {{
  window.fuiSpeechWarmEvents.push({{ ...(ev.detail || {{}}), at: performance.now() }});
}});
"""
    return FUI_SPEECH_PROBE + extra


@asynccontextmanager
async def isolated_matrix_server(
    data_dir: Path,
    *,
    speech=None,
    session_warm: bool = True,
    speech_standby_seconds: int = 0,
    cloud_model: bool = True,
):
    port = free_port()
    stopped = asyncio.Event()
    db_path = data_dir / "media.sqlite3"
    MediaLibrary(db_path)
    env = {
        "MOSS_MEDIA_DB": str(db_path),
        "MOSS_PROVIDER_DIR": str(data_dir / "providers"),
        "LOCALAPPDATA": str(data_dir / "profile"),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "1",
        "MOSS_SPEECH_SESSION_WARM": "1" if session_warm else "0",
        "MOSS_SPEECH_STANDBY": str(speech_standby_seconds),
    }
    async with AsyncExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        if speech is not None:
            stack.enter_context(patch("serve.MossSpeech", return_value=speech))
        if cloud_model:
            offline_profiles = {
                "deepseek": Profile(
                    id="deepseek",
                    label="DeepSeek",
                    url="https://api.deepseek.com",
                    model="deepseek-flash",
                    secret=SecretStr("offline-matrix-test-key"),
                ),
                "aliyun": Profile(
                    id="aliyun",
                    label="阿里云百炼",
                    url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen4-max",
                    secret=SecretStr("offline-matrix-test-key"),
                ),
                "kimi": Profile(
                    id="kimi",
                    label="Kimi",
                    url="https://api.moonshot.cn/v1",
                    model="kimi-k2.6",
                    secret=SecretStr(""),
                ),
            }
            stack.enter_context(patch("core.profiles", return_value=offline_profiles))
            stack.enter_context(
                patch.object(Profile, "client", side_effect=AssertionError("offline matrix must not call cloud LLM"))
            )
        moss = Moss(data_dir, "http://127.0.0.1:1/v1", "matrix-loopback", prefer_cloud=cloud_model)
        stack.push_async_callback(moss.close)
        if cloud_model:
            await moss.select_model("deepseek")
        app = application(
            moss,
            lambda: "standby",
            data_dir,
            port,
            "matrix-token",
            stopped,
            fui_public=FUI_PUBLIC,
            default_mic_warm_mode="activity",
            default_session_warm="1" if session_warm else "0",
            speech_standby_seconds=speech_standby_seconds,
        )
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)

        def _stop():
            stopped.set()
            moss._notify()

        stack.callback(_stop)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        yield f"http://127.0.0.1:{port}", moss


async def run_unit_suite(out_dir: Path, tag: str):
    """Zero-GPU verification of MossSpeech session pre-warm lifecycle, memory/VRAM guards, and New-FUI UI."""
    report = {
        "mode": "unit",
        "tag": tag,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "checks": {},
        "page_errors": [],
        "passed": False,
    }

    # --- Part A: Direct MossSpeech unit tests (no GPU needed) ---
    unit_dir = out_dir / "unit-speech"
    unit_dir.mkdir(parents=True, exist_ok=True)
    wav_sample = make_tone_wav(seconds=0.5, hz=440)

    class MockWorkerSpeech(MossSpeech):
        def __init__(self, data, *, refuse_vram=False, prime_delay=0.0):
            super().__init__(data, idle_seconds=10, standby_seconds=0)
            self.refuse_vram = refuse_vram
            self.prime_delay = prime_delay
            self.starts = 0
            self.stops = 0
            self.requests = []

        async def _start(self):
            if self.worker and self.worker.returncode is None:
                return
            self.starts += 1
            self.worker = SimpleNamespace(returncode=None, pid=90000 + self.starts)

        async def _stop(self):
            if self.worker is not None:
                self.stops += 1
            self.worker = None
            self._batch_capable = False
            self._used_worker = False
            self._session_warm_ready = False

        async def _worker_reply(self, req, cancel, disconnected, timeout):
            self.requests.append(dict(req))
            if req.get("prepare"):
                if self.prime_delay:
                    deadline = asyncio.get_running_loop().time() + self.prime_delay
                    while asyncio.get_running_loop().time() < deadline:
                        if cancel.exists():
                            return {
                                "code": 0,
                                "info": {"device": "cuda", "batch_capable": True, "priming": "yielded"},
                            }
                        await asyncio.sleep(0.01)
                if self.refuse_vram:
                    return {"code": 75, "preload_refused": True}
                return {"code": 0, "info": {"device": "cuda", "batch_capable": True, "priming": "completed"}}
            Path(req["output"]).write_bytes(wav_sample)
            return {
                "code": 0,
                "info": {
                    "mode": "single",
                    "device": "cuda",
                    "batch_capable": True,
                    "imports_s": 0.0,
                    "load_seconds": 0.0,
                    "generation_seconds": 0.05,
                    "load_durations_s": {},
                    "room_s": 0.01,
                },
            }

    # A1: Eligible session pre-warm succeeds, sends cancel_file + prime=True + cuda_min_free_mib, and idempotent renew works
    mem_ok = SimpleNamespace(available=16 * GIB, available_pagefile=16 * GIB)
    with patch("voice_output.memory_sample", return_value=mem_ok), \
         patch("voice_output.SESSION_MEMORY_CHECK_SECONDS", 0.05):
        s1 = MockWorkerSpeech(unit_dir / "s1")
        key1 = uuid4().hex
        ok1 = await s1.prepare_session(key1, eligible=lambda: True, ttl_seconds=1.5)
        ok1_renew = await s1.prepare_session(key1, eligible=lambda: True, ttl_seconds=1.5)
        req0 = s1.requests[0]
        assert ok1 and ok1_renew and s1.session_warm_ready and s1.starts == 1 and len(s1.requests) == 1
        assert req0["prepare"] is True and req0["prime"] is True and req0["cuda_min_free_mib"] == 3800
        assert "cancel_file" in req0
        # Low-memory watcher immediately releases worker when RAM drops below 8 GiB
        mem_ok.available = 6 * GIB
        await asyncio.sleep(0.18)
        assert not s1.session_warm_ready and s1.worker is None and s1.stops == 1
        await s1.close()
    report["checks"]["unit_prepare_renew_and_low_ram_release"] = {"passed": True}

    # A2: Eligibility flip (e.g. model switched to local Qwen) automatically stops worker on watcher tick
    mem_ok2 = SimpleNamespace(available=16 * GIB, available_pagefile=16 * GIB)
    eligible_flag = {"ok": True}
    with patch("voice_output.memory_sample", return_value=mem_ok2), \
         patch("voice_output.SESSION_MEMORY_CHECK_SECONDS", 0.05):
        s2 = MockWorkerSpeech(unit_dir / "s2")
        ok2 = await s2.prepare_session(uuid4().hex, eligible=lambda: eligible_flag["ok"], ttl_seconds=5.0)
        assert ok2 and s2.session_warm_ready
        eligible_flag["ok"] = False
        await asyncio.sleep(0.18)
        assert not s2.session_warm_ready and s2.worker is None and s2.stops == 1
        await s2.close()
    report["checks"]["unit_ineligible_auto_release"] = {"passed": True}

    # A3: Low VRAM refusal cleans up worker cleanly (0 leaked worker)
    mem_ok3 = SimpleNamespace(available=16 * GIB, available_pagefile=16 * GIB)
    with patch("voice_output.memory_sample", return_value=mem_ok3):
        s3 = MockWorkerSpeech(unit_dir / "s3", refuse_vram=True)
        ok3 = await s3.prepare_session(uuid4().hex, eligible=lambda: True, ttl_seconds=5.0)
        assert not ok3 and not s3.session_warm_ready and s3.worker is None and s3.stops == 1
        await s3.close()
    report["checks"]["unit_vram_refusal_cleanup"] = {"passed": True}

    # A4: Incoming real synthesis during priming yields priming cleanly without killing CUDA weights!
    mem_ok4 = SimpleNamespace(available=16 * GIB, available_pagefile=16 * GIB)
    with patch("voice_output.memory_sample", return_value=mem_ok4):
        s4 = MockWorkerSpeech(unit_dir / "s4", prime_delay=0.45)
        key4 = uuid4().hex
        warm_task = asyncio.create_task(s4.prepare_session(key4, eligible=lambda: True, ttl_seconds=5.0))
        await asyncio.sleep(0.06)
        wav_out = await s4.render("你好世界。", lambda: False)
        warm_ok = await warm_task
        assert wav_out == wav_sample and warm_ok and s4.starts == 1 and s4.stops == 0 and s4.session_warm_ready
        await s4.release_session(key4, force_stop=True)
        assert s4.worker is None and s4.stops == 1
        await s4.close()
    report["checks"]["unit_priming_yield_handover"] = {"passed": True}

    # A5: Low RAM (< 12 GiB) refuses start before spawning worker
    mem_low = SimpleNamespace(available=10 * GIB, available_pagefile=16 * GIB)
    with patch("voice_output.memory_sample", return_value=mem_low):
        s5 = MockWorkerSpeech(unit_dir / "s5")
        ok5 = await s5.prepare_session(uuid4().hex, eligible=lambda: True)
        assert not ok5 and s5.starts == 0 and s5.worker is None
        await s5.close()
    report["checks"]["unit_low_ram_refuses_spawn"] = {"passed": True}

    # --- Part B: New-FUI Browser + HTTP Lifecycle Verification (muted Edge, mocked CUDA worker) ---
    browser_dir = out_dir / "browser-unit"
    browser_dir.mkdir(parents=True, exist_ok=True)
    mem_browser = SimpleNamespace(available=16 * GIB, available_pagefile=16 * GIB)
    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"

    with patch("voice_output.memory_sample", return_value=mem_browser):
        mock_speech = MockWorkerSpeech(browser_dir)
        async with isolated_matrix_server(browser_dir, speech=mock_speech, session_warm=True, cloud_model=True) as (base, moss):
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=str(edge),
                    headless=True,
                    args=["--mute-audio", "--disable-gpu", "--disable-webgl", "--disable-webgl2",
                          "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
                )
                try:
                    await confirm_muted_edge(browser)
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        reduced_motion="reduce",
                        service_workers="block",
                    )
                    await context.add_init_script(build_probe(scene_3d=False, voice_resident=True))
                    page = await context.new_page()
                    page.set_default_timeout(12000)
                    page.on("pageerror", lambda err: report["page_errors"].append(str(err)))

                    await page.goto(base + "/?skipOpening=1&scene=off", wait_until="domcontentloaded")
                    await expect(page.locator("body")).to_have_attribute("data-backend", "live", timeout=10000)
                    await page.wait_for_function(
                        "() => window.fuiSpeechWarmEvents.some(e => e.status === 'ready')",
                        timeout=8000,
                    )
                    assert mock_speech.session_warm_ready and mock_speech.starts == 1

                    # Open settings and check #set-voice-resident toggle + status text
                    await page.locator("#strip-settings").click()
                    await expect(page.locator("#dlg-settings")).to_have_attribute("open", "")
                    assert await page.locator("#set-voice-resident").is_checked()
                    status_ready = (await page.locator("#set-voice-resident-status").text_content() or "").strip()
                    assert "就绪" in status_ready or "ready" in status_ready.lower(), status_ready

                    # Uncheck #set-voice-resident -> immediately releases worker!
                    await page.locator("#set-voice-resident").uncheck()
                    await page.wait_for_function(
                        "() => window.fuiSpeechWarmEvents.at(-1)?.status === 'off'",
                        timeout=5000,
                    )
                    for _ in range(30):
                        if mock_speech.worker is None:
                            break
                        await asyncio.sleep(0.05)
                    assert mock_speech.worker is None and not mock_speech.session_warm_ready
                    assert await page.evaluate("localStorage.getItem('moss-fui.voice-resident')") == "off"

                    # Re-check #set-voice-resident -> re-warms immediately!
                    await page.locator("#set-voice-resident").check()
                    await page.wait_for_function(
                        "() => window.fuiSpeechWarmEvents.at(-1)?.status === 'ready'",
                        timeout=5000,
                    )
                    assert mock_speech.session_warm_ready and mock_speech.worker is not None
                    await page.locator("#dlg-settings [data-dialog-close]").first.click()

                    # Switch backend model to local via API -> immediately releases worker!
                    async with aiohttp.ClientSession() as client:
                        async with client.post(base + "/api/select-model", json={"id": "local"}) as resp:
                            assert resp.status == 200
                    await page.wait_for_function(
                        "() => window.fuiSpeechWarmEvents.at(-1)?.status === 'local'",
                        timeout=5000,
                    )
                    assert mock_speech.worker is None and not mock_speech.session_warm_ready

                    # Switch back to deepseek -> re-warms automatically, then close page -> releases via pagehide!
                    await moss.select_model("deepseek")
                    await page.wait_for_function(
                        "() => window.fuiSpeechWarmEvents.at(-1)?.status === 'ready'",
                        timeout=8000,
                    )
                    assert mock_speech.session_warm_ready
                    await page.close()
                    for _ in range(40):
                        if mock_speech.worker is None:
                            break
                        await asyncio.sleep(0.05)
                    assert mock_speech.worker is None and not mock_speech.session_warm_ready
                    report["checks"]["browser_fui_toggle_local_pagehide"] = {
                        "passed": True,
                        "status_ready_text": status_ready,
                        "starts": mock_speech.starts,
                        "stops": mock_speech.stops,
                    }
                finally:
                    await browser.close()

    report["passed"] = all(c["passed"] for c in report["checks"].values()) and not report["page_errors"]
    return report


async def run_cuda_matrix(out_dir: Path, tag: str, *, cpu_workers: int = 8):
    """Full RTX 3070 Ti CUDA verification matrix: cold, pre-warmed light, 3D WebGL ON, CPU load, and VRAM release."""
    report = {
        "mode": "cuda",
        "tag": tag,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "resources_before": resources_snapshot(),
        "arms": {},
        "page_errors": [],
        "passed": False,
    }
    gpu_before = report["resources_before"]["gpu_free_mib"]
    mem_before = memory_sample()
    if gpu_before < 3800 or min(mem_before.available, mem_before.available_pagefile) < 12 * GIB or other_speaker():
        report["blocked_reason"] = (
            f"RTX 3070 Ti 当前空闲显存 {gpu_before} MiB（需 >= 3800 MiB），"
            f"可用内存 {mem_before.available // (1024**2)} MiB，或已有语音进程占用。"
        )
        return report

    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"

    async def run_arm(
        arm_name: str,
        *,
        session_warm: bool,
        scene_3d: bool,
        cpu_load: bool = False,
        verify_release: bool = False,
        readings_pair: tuple[tuple[str, str], tuple[str, str]],
        include_long: bool = True,
    ):
        arm_dir = out_dir / arm_name
        arm_dir.mkdir(parents=True, exist_ok=True)
        readings = {"battery": readings_pair[0][0], "volume": readings_pair[0][1]}
        arm = {
            "session_warm": session_warm,
            "scene_3d": scene_3d,
            "cpu_load": cpu_load,
            "resources_before": resources_snapshot(),
            "cases": {},
            "page_errors": [],
        }

        async def fake_battery(_intent):
            return readings["battery"]

        async def fake_volume(_intent):
            return readings["volume"]

        launch_args = [
            "--mute-audio",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
        ]
        if not scene_3d:
            launch_args.extend(["--disable-gpu", "--disable-webgl", "--disable-webgl2"])

        with patch("local_overview.device_status", side_effect=fake_battery), \
             patch("local_overview.run_desktop", side_effect=fake_volume):
            speech = MossSpeech(arm_dir, idle_seconds=150, standby_seconds=0)
            async with isolated_matrix_server(
                arm_dir, speech=speech, session_warm=session_warm, cloud_model=True
            ) as (base, moss):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=str(edge), headless=True, args=launch_args
                    )
                    try:
                        await confirm_muted_edge(browser)
                        context = await browser.new_context(
                            viewport={"width": 1440, "height": 900},
                            reduced_motion="reduce",
                            service_workers="block",
                        )
                        await context.add_init_script(build_probe(scene_3d=scene_3d, voice_resident=session_warm))

                        async def route_guard(route):
                            url = route.request.url
                            if url.startswith(("http://", "https://")) and not url.startswith(base + "/"):
                                await route.abort()
                            else:
                                await route.continue_()

                        await context.route("**/*", route_guard)
                        page = await context.new_page()
                        page.set_default_timeout(120000)
                        page.on("pageerror", lambda err: arm["page_errors"].append(str(err)))
                        query = "/?skipOpening=1" if scene_3d else "/?skipOpening=1&scene=off"
                        t_nav = asyncio.get_running_loop().time()
                        await page.goto(base + query, wait_until="domcontentloaded")
                        await expect(page.locator("body")).to_have_attribute("data-backend", "live", timeout=15000)
                        await page.bring_to_front()

                        if scene_3d:
                            await expect(page.locator("#moss-model")).to_have_attribute("data-ready", "true", timeout=45000)
                            arm["scene_3d_ready"] = True

                        if session_warm:
                            await page.wait_for_function(
                                "() => window.fuiSpeechWarmEvents.some(e => e.status === 'ready' || e.status === 'refused')",
                                timeout=60000,
                            )
                            warm_events = await page.evaluate("window.fuiSpeechWarmEvents")
                            arm["warm_events"] = warm_events
                            arm["session_warm_ms"] = round((asyncio.get_running_loop().time() - t_nav) * 1000, 1)
                            assert warm_events[-1]["status"] == "ready", warm_events
                            arm["resources_after_warm"] = resources_snapshot()
                            arm["warm_worker_pids"] = speech_worker_pids()

                        composer = page.locator("#comms-input")
                        send_btn = page.locator("#comms-send")

                        async def run_prompt(case_name: str, text: str, *, timeout_ms: int = 120000):
                            await page.evaluate("window.resetFuiSpeechQA()")
                            await composer.fill(text)
                            await send_btn.click()
                            await page.wait_for_function(
                                "() => window.fuiSpeechQA.events.some(e => e.type === 'end' || e.type === 'error')",
                                timeout=timeout_ms,
                            )
                            trace = await page.evaluate("window.collectFuiSpeechMetrics()")
                            summary = summarize_trace(trace)
                            summary["resources_after"] = resources_snapshot()
                            summary["terminal_event"] = trace["events"][-1]["type"] if trace["events"] else None
                            arm["cases"][case_name] = summary
                            print(
                                f"[{arm_name}:{case_name}] first_pcm_ms={summary['first_pcm_ms']} "
                                f"gaps={summary['schedule_gaps_ms']} gpu_free={summary['resources_after']['gpu_free_mib']}",
                                flush=True,
                            )
                            return summary

                        pool = None
                        burn_futures = []
                        try:
                            if cpu_load:
                                import time as _time
                                pool = ProcessPoolExecutor(max_workers=cpu_workers)
                                stop_at = _time.monotonic() + 75.0
                                burn_futures = [pool.submit(_cpu_burn_worker, stop_at) for _ in range(cpu_workers)]
                                await asyncio.sleep(1.0)
                                arm["cpu_percent_under_load"] = psutil.cpu_percent(interval=0.5)

                            c1 = await run_prompt("first_short_status", "电脑还有多少电，音量多大？", timeout_ms=150000)
                            assert c1["terminal_event"] == "end", c1

                            readings["battery"], readings["volume"] = readings_pair[1]
                            c2 = await run_prompt("second_short_status", "电脑还有多少电，音量多大？", timeout_ms=60000)
                            assert c2["terminal_event"] == "end", c2

                            if include_long:
                                c3 = await run_prompt("long_reading", "请朗读以下文字：" + LONG_SAMPLE, timeout_ms=120000)
                                assert c3["terminal_event"] == "end", c3
                        finally:
                            if pool is not None:
                                pool.shutdown(wait=False, cancel_futures=True)
                                for child in psutil.Process().children(recursive=False):
                                    try:
                                        if child.pid not in speech_worker_pids() and "python" in child.name().lower():
                                            child.terminate()
                                    except psutil.NoSuchProcess:
                                        pass

                        if verify_release:
                            # Test switching to local Qwen releases CUDA speech worker & VRAM immediately
                            async with aiohttp.ClientSession() as client:
                                async with client.post(base + "/api/select-model", json={"id": "local"}) as resp:
                                    assert resp.status == 200
                            for _ in range(50):
                                if not speech_worker_pids():
                                    break
                                await asyncio.sleep(0.1)
                            await asyncio.sleep(0.6)
                            arm["release_on_local_switch"] = {
                                "worker_pids": speech_worker_pids(),
                                "gpu_compute_pids_overlap": sorted(gpu_processes().intersection(arm.get("warm_worker_pids", []))),
                                "resources_after_release": resources_snapshot(),
                            }
                            assert not arm["release_on_local_switch"]["worker_pids"]
                            assert not arm["release_on_local_switch"]["gpu_compute_pids_overlap"]
                    finally:
                        await browser.close()
        await asyncio.sleep(0.8)
        arm["resources_after_close"] = resources_snapshot()
        arm["worker_pids_after_close"] = speech_worker_pids()
        assert not arm["worker_pids_after_close"]
        report["arms"][arm_name] = arm
        report["page_errors"].extend(arm["page_errors"])
        return arm

    # Arm 1: Pure cold baseline (session_warm=False, light scene)
    arm_cold = await run_arm(
        "01_cold_light",
        session_warm=False,
        scene_3d=False,
        include_long=False,
        readings_pair=(
            ("电量 61%（使用电池）。", "系统音量为 43%，当前未静音。"),
            ("电量 63%（使用电池）。", "系统音量为 45%，当前未静音。"),
        ),
    )
    # Arm 2: Foreground pre-warmed (light scene) + long reading + local-model VRAM release proof
    arm_warm_light = await run_arm(
        "02_prewarm_light",
        session_warm=True,
        scene_3d=False,
        verify_release=True,
        readings_pair=(
            ("电量 72%（已接电）。", "系统音量为 51%，当前未静音。"),
            ("电量 74%（已接电）。", "系统音量为 53%，当前未静音。"),
        ),
    )
    # Arm 3: Foreground pre-warmed + REAL 3D 550W WebGL Scene ON + long reading
    arm_warm_3d = await run_arm(
        "03_prewarm_3d_scene",
        session_warm=True,
        scene_3d=True,
        readings_pair=(
            ("电量 81%（已接电）。", "系统音量为 57%，当前未静音。"),
            ("电量 83%（已接电）。", "系统音量为 59%，当前未静音。"),
        ),
    )
    # Arm 4: Foreground pre-warmed + CPU Load (light scene)
    arm_warm_loaded = await run_arm(
        "04_prewarm_cpu_load",
        session_warm=True,
        scene_3d=False,
        cpu_load=True,
        include_long=True,
        readings_pair=(
            ("电量 91%（已接电）。", "系统音量为 62%，当前未静音。"),
            ("电量 93%（已接电）。", "系统音量为 64%，当前未静音。"),
        ),
    )

    report["resources_after"] = resources_snapshot()
    checks = {
        "cold_measured": arm_cold["cases"]["first_short_status"]["first_pcm_ms"] is not None,
        "prewarm_light_first_lt_2500ms": arm_warm_light["cases"]["first_short_status"]["first_pcm_ms"] < 2500,
        "prewarm_light_second_lt_2500ms": arm_warm_light["cases"]["second_short_status"]["first_pcm_ms"] < 2500,
        "prewarm_light_long_lt_2500ms": arm_warm_light["cases"]["long_reading"]["first_pcm_ms"] < 2500,
        "prewarm_light_schedule_gap_lt_25ms": all(g < 25 for g in arm_warm_light["cases"]["long_reading"]["schedule_gaps_ms"]),
        "prewarm_3d_ready": arm_warm_3d.get("scene_3d_ready") is True,
        "prewarm_3d_first_lt_2500ms": arm_warm_3d["cases"]["first_short_status"]["first_pcm_ms"] < 2500,
        "prewarm_3d_long_lt_2500ms": arm_warm_3d["cases"]["long_reading"]["first_pcm_ms"] < 2500,
        "prewarm_loaded_first_lt_3000ms": arm_warm_loaded["cases"]["first_short_status"]["first_pcm_ms"] < 3000,
        "vram_released_on_local_switch": not arm_warm_light["release_on_local_switch"]["worker_pids"],
        "zero_page_errors": not report["page_errors"],
    }
    report["verdict_checks"] = checks
    report["passed"] = all(checks.values())
    return report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("unit", "cuda"), default="unit")
    parser.add_argument("--tag", default="run")
    parser.add_argument("--cpu-workers", type=int, default=8)
    args = parser.parse_args()
    out_dir = ROOT / "runs" / datetime.now().strftime(f"fui-speech-matrix-{args.mode}-{args.tag}-%Y%m%d-%H%M%S-%f")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = (
        await run_unit_suite(out_dir, args.tag)
        if args.mode == "unit"
        else await run_cuda_matrix(out_dir, args.tag, cpu_workers=args.cpu_workers)
    )
    result_path = out_dir / "result.json"
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result_path, "passed=", report["passed"], flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
