import argparse
import asyncio
from collections import OrderedDict
import gzip
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from time import perf_counter
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from pydantic import ValidationError
import msvcrt
import win32api
import win32con
import win32job

from assistant_notes import NoteConflict, NoteUpdate, NoteWrite
from core import Moss, WINDOW_IMAGE_PROVIDER
from context_tools import MAX_CAPTURE_BYTES, recognize_capture
from device_status import DeviceStatusCache
from jev_handoff import DRAFT_HANDOFF_ENABLED
from mcp_configured import MCP_DATA
from runtime_log import BackendLogHub, RuntimeLog
from voice_input import LocalVoice
from voice_output import MossSpeech, SPEECH_RATES, SpeechInputError
from wake_handoff import BrowserWake
from weather_current import CurrentWeatherCache
from weather_location import browser_position
from city_catalog import city_catalog, selected_city
from window_image import IMAGE_MAX_JSON, prepare_window_image
from doc_extract import IMAGE_EXTS, extract_document_bytes
from brain_iot_routes import install_brain_iot_routes
from online_music_routes import install_online_music_routes


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from run_lab import ENGINE, WEIGHTS

CUDA_ENGINE = ENGINE.parent.parent / "llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.69.0" / ENGINE.name
VULKAN_ENGINE = ENGINE.parent.parent / "llama.cpp-win-x86_64-vulkan-avx2-2.69.0" / ENGINE.name
QWEN_ENGINES = {"cuda": CUDA_ENGINE, "vulkan": VULKAN_ENGINE, "cpu": ENGINE}


def local_port(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))


@asynccontextmanager
async def engine_process(port, log_dir, backend, *, vulkan_device=None):
    url = f"http://127.0.0.1:{port}"
    model = "moss-chat-qwen35-4b"
    slots = log_dir / "slots"
    slots.mkdir(exist_ok=True)
    if type(backend) is bool:
        backend = "cuda" if backend else "cpu"
    executable = QWEN_ENGINES[backend]
    if not executable.is_file():
        raise FileNotFoundError(f"找不到本机 Qwen 的 {backend} 后端。")
    cmd = [str(executable), "-m", str(WEIGHTS), "--alias", model, "--host", "127.0.0.1",
           "--port", str(port), "--parallel", "1", "--ctx-size", "4096",
           "--threads", "8", "--threads-batch", "8", "--reasoning", "off", "--jinja",
           "--cache-prompt", "--cache-ram", "0", "--slot-save-path", str(slots)]
    device = None
    if backend != "cpu":
        device = "CUDA0" if backend == "cuda" else vulkan_device or "Vulkan0"
        cmd.extend(["--device", device, "--fit", "on", "--fit-target", "1024",
                    "--sleep-idle-seconds", "90", "--verbosity", "4"])
    else:
        cmd.extend(["--n-gpu-layers", "0"])
    # 主进程被强制退出时，Windows 同时释放专用推理进程。
    group = win32job.CreateJobObject(None, "MossQwen-" + secrets.token_hex(16))
    policy = win32job.QueryInformationJobObject(group, win32job.JobObjectExtendedLimitInformation)
    policy["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(group, win32job.JobObjectExtendedLimitInformation, policy)
    log_path = log_dir / "qwen.log"
    with log_path.open("ab") as log:
        log.write(f"\nMOSS Qwen backend={backend}, requested_device={device or 'none'}\n".encode("utf-8"))
        log.flush()
        log_start = log.tell()
        proc = None
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
            try:
                win32job.AssignProcessToJobObject(group, handle)
            finally:
                handle.Close()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as client:
                async with asyncio.timeout(100):
                    while True:
                        if proc.poll() is not None:
                            raise RuntimeError(f"Qwen 退出，详见 {log_dir / 'qwen.log'}")
                        try:
                            async with client.get(url + "/health") as response:
                                if response.status == 200 and (await response.json()).get("status") == "ok":
                                    break
                        except (aiohttp.ClientConnectionError, TimeoutError):
                            pass
                        await asyncio.sleep(.35)
            if device:
                with log_path.open("rb") as output:
                    output.seek(log_start)
                    startup_log = output.read()
                layers = re.search(rb"offloaded\s+(\d+)/(\d+)\s+layers to GPU", startup_log)
                device_log = re.search(rb"\busing device\s+" + re.escape(device.encode("ascii")) + rb"(?=\s|\()", startup_log)
                if not device_log or not layers or int(layers[1]) < 1:
                    raise RuntimeError(f"Qwen 日志未确认 {device} 已加载模型层，详见 {log_path}。")
            yield url + "/v1", model, backend
        finally:
            try:
                if proc is not None:
                    if proc.poll() is None:
                        proc.terminate()
                    await asyncio.to_thread(proc.wait, 15)
            finally:
                group.Close()


@asynccontextmanager
async def engine(port, log_dir, *, backend="auto", vulkan_device=None, allow_cpu=False):
    local_port(port)
    candidates = ("cuda", "vulkan", "cpu") if allow_cpu else ("cuda", "vulkan")
    if backend != "auto":
        candidates = (backend,)
    async with AsyncExitStack() as stack:
        for candidate in candidates:
            try:
                ready = await stack.enter_async_context(engine_process(port, log_dir, candidate, vulkan_device=vulkan_device))
            except (OSError, RuntimeError, TimeoutError) as exc:
                if candidate == candidates[-1]:
                    raise RuntimeError(f"本机 Qwen 未能使用 {candidate} 启动。详见 {log_dir / 'qwen.log'}，或在设置中使用 DeepSeek。") from exc
            else:
                yield ready
                return


class _StatePayload:
    def __init__(self, moss, speech_prepare, speech_mic_warm=False, speech_mic_warm_on_activity=False,
                 speech_session_warm=False):
        self.moss = moss
        self.speech_prepare = speech_prepare
        self.speech_mic_warm = speech_mic_warm
        self.speech_mic_warm_on_activity = speech_mic_warm_on_activity
        self.speech_session_warm = speech_session_warm
        self._caches = OrderedDict()

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _entry(self, owner):
        entry = self._caches.get(owner)
        if entry is None:
            entry = {"stamp": None, "rows": {}, "turns": b"", "metadata": None, "body": b""}
            self._caches[owner] = entry
            while len(self._caches) > 32:
                self._caches.popitem(last=False)
        else:
            self._caches.move_to_end(owner)
        return entry

    def _refresh_turns(self, entry, chat, owner):
        db = self.moss.db
        # total_changes sees this connection; data_version sees commits by other connections.
        stamp = (owner, chat, db.total_changes, db.execute("PRAGMA data_version").fetchone()[0])
        if stamp == entry["stamp"]:
            return
        previous = entry["rows"] if entry["stamp"] and entry["stamp"][:2] == (owner, chat) else {}
        rows = {}
        for key, doc, counts in self.moss._turn_rows(chat):
            cached = previous.get(key)
            if cached is None or cached[:2] != (doc, counts):
                turn = self.moss._public_turn(doc, counts, chat_id=chat)
                cached = (doc, counts, self.encode(turn), bool(turn.get("transport")))
            rows[key] = cached
        entry["rows"] = rows
        entry["turns"] = b",".join(row[2] for row in rows.values())
        entry["metadata"] = None
        entry["stamp"] = stamp

    def read(self, transport_owner=None, owner_code=None):
        owner = self.moss.normalize_owner(owner_code)
        entry = self._entry(owner)
        # Expiration can change the visible memories without advancing the revision.
        snapshot = self.moss._state_metadata(owner_code=owner)
        snapshot["speech_prepare"] = self.speech_prepare
        snapshot["speech_mic_warm"] = self.speech_mic_warm
        snapshot["speech_mic_warm_on_activity"] = self.speech_mic_warm_on_activity
        snapshot["speech_session_warm"] = self.speech_session_warm
        snapshot["jev_handoff"] = DRAFT_HANDOFF_ENABLED
        chat_id = snapshot["chat_id"]
        self._refresh_turns(entry, chat_id, owner)
        rows = entry["rows"]
        owned = rows.get(transport_owner)
        if owned and owned[3]:
            # Encode the owner-only token separately, so public SSE and GET cannot reuse it.
            owned_turn = self.encode(self.moss._public_turn(owned[0], owned[1], transport_owner, chat_id=chat_id))
            turns = b",".join(owned_turn if key == transport_owner else row[2] for key, row in rows.items())
            body = self.encode(snapshot)[:-1] + b',"turns":[' + turns + b"]}"
            return snapshot["revision"], body
        if entry["metadata"] != snapshot:
            entry["metadata"] = snapshot
            entry["body"] = self.encode(snapshot)[:-1] + b',"turns":[' + entry["turns"] + b"]}"
        return snapshot["revision"], entry["body"]


def application(moss, local_engine, data, port, token, stopped, *, qwen_backend="auto", vulkan_device=None, allow_cpu=False,
                speech_idle_seconds=150, speech_standby_seconds=0, default_mic_warm_mode="0",
                default_session_warm="0", fui_public=None, lan_hosts=(), tls_port=0):
    voice = LocalVoice()
    speech = MossSpeech(data, idle_seconds=speech_idle_seconds, standby_seconds=speech_standby_seconds)
    device_status = DeviceStatusCache()
    current_weather = CurrentWeatherCache()
    handoffs = moss.jev_handoffs
    subscribers = set()
    backend_log = BackendLogHub(data)
    moss.rsi.on_event = backend_log.emit
    moss.sag.on_event = backend_log.emit
    moss.pulse.on_log = backend_log.emit
    moss.consolidator.on_log = backend_log.emit
    moss.jobs.on_log = backend_log.emit
    moss.iot.on_log = backend_log.emit
    runtime = RuntimeLog(moss.db, on_emit=backend_log.emit, has_observers=lambda: bool(backend_log.subscribers))
    lan_host_list = [h.strip() for h in (lan_hosts or ()) if h and h.strip() not in {"127.0.0.1", "localhost", "0.0.0.0"}]
    listen_summary = ", ".join(["127.0.0.1", *lan_host_list])
    backend_log.emit(f"MOSS 550W backend listening {listen_summary}:{port} model={moss.model_id}", source="SYS", level="ok")
    wake = BrowserWake(token)
    log_tail_task = None
    pulse_task = None
    last_dev_log = [0.0]
    last_auto_dream = [0.0]
    boot_monotonic = perf_counter()

    async def tail_logs_loop():
        slot = 0
        last_pulse = 0.0
        last_hw_refresh = 0.0
        cached_hw = None
        try:
            while not stopped.is_set():
                if backend_log.subscribers:
                    backend_log.poll_files()
                    now_t = perf_counter()
                    if now_t - last_hw_refresh >= 3.6 or cached_hw is None:
                        last_hw_refresh = now_t
                        try:
                            cached_hw = await device_status.read()
                        except Exception:
                            pass
                    if now_t - last_pulse >= 1.1:
                        last_pulse = now_t
                        hw = cached_hw or {}
                        up_s = int(max(0.0, now_t - boot_monotonic))
                        hh, rem = divmod(up_s, 3600)
                        mm, ss = divmod(rem, 60)
                        if slot == 0:
                            cpu_p = hw.get("cpu_percent")
                            cpu_t = (hw.get("cpu") or {}).get("temperature_c")
                            gpu = hw.get("gpu") or {}
                            gpu_p = gpu.get("percent")
                            gpu_t = gpu.get("temperature_c")
                            c_str = f"CPU {cpu_p:.0f}%" if isinstance(cpu_p, (int, float)) else "CPU --"
                            if isinstance(cpu_t, (int, float)):
                                c_str += f"/{cpu_t:.0f}C"
                            g_str = f"GPU {gpu_p:.0f}%" if isinstance(gpu_p, (int, float)) else "GPU --"
                            if isinstance(gpu_t, (int, float)):
                                g_str += f"/{gpu_t:.0f}C"
                            lvl = "warn" if (isinstance(cpu_t, (int, float)) and cpu_t >= 82) or (isinstance(gpu_t, (int, float)) and gpu_t >= 82) else "info"
                            backend_log.emit(f"BUS·THERM | {c_str} · {g_str} · LINK OK", source="DEV", level=lvl, write_file=False)
                        elif slot == 1:
                            mem = hw.get("memory") or {}
                            disk = hw.get("disk") or {}
                            bat = hw.get("battery") or {}
                            r_str = (f"RAM {mem['available']/(1024**3):.1f}/{mem['total']/(1024**3):.0f}G"
                                     if mem.get("available") is not None and mem.get("total") else "RAM --")
                            d_str = (f"DISK {disk.get('drive','C:')} {disk['free']/(1024**3):.0f}G FREE"
                                     if disk.get("free") is not None else "DISK --")
                            p_str = "PWR AC" if bat.get("ac") or bat.get("present") is False else f"BAT {bat.get('percent', 0):.0f}%"
                            backend_log.emit(f"BUS·MEMIO | {r_str} · {d_str} · {p_str}", source="DEV", level="info", write_file=False)
                        elif slot == 2:
                            phase = "THINKING" if moss._has_active_turn() else "STANDBY"
                            try:
                                row_c = moss.db.execute("SELECT COUNT(*) FROM turns WHERE chat_id=?", (moss.chat_id,)).fetchone()
                                turns_n = int(row_c[0]) if row_c else 0
                            except Exception:
                                turns_n = 0
                            backend_log.emit(
                                f"CORE·550W | {moss.model_name} [{phase}] · T={turns_n:02d} · SSE={len(backend_log.subscribers)} · UP {hh:02d}:{mm:02d}:{ss:02d}",
                                source="CORE", level="ok" if phase == "THINKING" else "info", write_file=False,
                            )
                        else:
                            try:
                                sag_s = moss.sag.status_snapshot()
                                rsi_s = moss.rsi.status_snapshot()
                                ha_live = bool(moss.iot.config.get("ha_enabled") and (moss.iot.config.get("ha_url") or moss.iot.config.get("ha_base_url")) and moss.iot.config.get("ha_token"))
                                ha_mode = "LIVE" if ha_live else "DRY"
                                hw_seq = int(getattr(moss.iot, "hw_seq", 0))
                                backend_log.emit(
                                    f"MATX·BRAIN | SAG {sag_s['event_count']}N/{sag_s['entity_count']}E · RSI {rsi_s['active_count']}R · HA:{ha_mode} · HW#{hw_seq:03d}",
                                    source="BRAIN", level="info", write_file=False,
                                )
                            except Exception:
                                pass
                        slot = (slot + 1) % 4
                moss.jobs.poll_watchers()
                await asyncio.sleep(0.45)
        except asyncio.CancelledError:
            pass

    async def proactive_heartbeat_loop():
        try:
            while not stopped.is_set():
                await asyncio.sleep(30.0)
                if stopped.is_set():
                    break
                busy = moss._has_active_turn()
                if not busy:
                    try:
                        dev_sample = await device_status.read() if (subscribers or backend_log.subscribers) else None
                    except Exception:
                        dev_sample = None
                    if moss.pulse.config.get("enabled"):
                        moss.pulse.evaluate_tick(device_sample=dev_sample, is_busy=busy)
                    # v5.5: Evaluate due recurring tasks / daily briefings independently of ad-hoc nudge toggle
                    for rtask in moss.routines.pop_due_tasks():
                        try:
                            kind = rtask.get("action_kind")
                            if kind == "briefing":
                                wm_snap = moss.consolidator.latest_snapshot()
                                brief = moss.pulse.generate_briefing(
                                    device_sample=dev_sample,
                                    timers_count=len(moss.timers.snapshot()),
                                    notes_count=len((moss.notes.page("", archived=False, before=0, limit=50)).get("items") or []),
                                    rsi_active=moss.rsi.status_snapshot()["active_count"],
                                    sag_events=moss.sag.status_snapshot()["event_count"],
                                    world_model=(wm_snap or {}).get("world_model"),
                                )
                                moss.pulse._emit_alert(
                                    kind="routine_briefing",
                                    level="info",
                                    title=f"例行早报 · {rtask['name']}",
                                    body=brief["text"],
                                    dedup_key=f"routine:{rtask['id']}",
                                    cooldown_sec=540.0,
                                    bypass_budget=True,
                                )
                            elif kind == "dream":
                                res = moss.consolidator.run_consolidation(trigger="routine")
                                moss.pulse._emit_alert(
                                    kind="routine_dream",
                                    level="info",
                                    title=f"定期深度整理 · {rtask['name']}",
                                    body=res["summary"],
                                    dedup_key=f"routine:{rtask['id']}",
                                    cooldown_sec=540.0,
                                    bypass_budget=True,
                                )
                            else:
                                moss.pulse._emit_alert(
                                    kind="routine",
                                    level="info",
                                    title=f"例行任务 · {rtask['name']}",
                                    body=rtask.get("payload") or rtask["name"],
                                    dedup_key=f"routine:{rtask['id']}",
                                    cooldown_sec=540.0,
                                    bypass_budget=True,
                                )
                        except Exception as exc:
                            backend_log.emit(f"Routine dispatch error ({rtask.get('id')}): {exc}", source="PULSE", level="warn", write_file=False)
                    if not moss.pulse.config.get("enabled"):
                        continue
                    # Sleep-time consolidation when idle >= 15 min (max once per hour)
                    idle_sec = perf_counter() - getattr(moss, "_last_activity", perf_counter())
                    now_t = perf_counter()
                    if idle_sec >= 900 and (last_auto_dream[0] == 0.0 or now_t - last_auto_dream[0] >= 3600):
                        last_auto_dream[0] = now_t
                        try:
                            moss.consolidator.run_consolidation(trigger="idle_sleep")
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass

    async def start_log_tail(app):
        nonlocal log_tail_task, pulse_task
        log_tail_task = asyncio.create_task(tail_logs_loop())
        pulse_task = asyncio.create_task(proactive_heartbeat_loop())

    async def stop_log_tail(app):
        for tsk in (log_tail_task, pulse_task):
            if tsk is not None:
                tsk.cancel()
                try:
                    await tsk
                except asyncio.CancelledError:
                    pass
    accepted_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *(f"{h}:{port}" for h in lan_host_list)}
    if tls_port:
        accepted_hosts.update({f"127.0.0.1:{tls_port}", f"localhost:{tls_port}", *(f"{h}:{tls_port}" for h in lan_host_list)})
    speech_prepare = os.environ.get("MOSS_SPEECH_PREPARE", "1") == "1"
    mic_warm_mode = os.environ.get("MOSS_SPEECH_MIC_WARM", default_mic_warm_mode)
    speech_mic_warm = speech_prepare and mic_warm_mode in {"1", "activity"}
    speech_mic_warm_on_activity = speech_mic_warm and mic_warm_mode == "activity"
    speech_session_warm = speech_prepare and os.environ.get("MOSS_SPEECH_SESSION_WARM", default_session_warm) == "1"
    payloads = _StatePayload(moss, speech_prepare, speech_mic_warm, speech_mic_warm_on_activity, speech_session_warm)

    def can_warm_microphone():
        return moss.model_id != "local" and local_engine() == "standby"

    def changed():
        runtime.capture()
        for signal in subscribers:
            signal.set()

    moss.on_change = changed

    def _req_owner(request):
        ident = request.get("moss_identity")
        if isinstance(ident, dict) and ident.get("code"):
            return moss.normalize_owner(ident["code"])
        auth_check = request.app.get("moss_auth_check")
        if auth_check:
            ok, resolved = auth_check(request)
            if ok and isinstance(resolved, dict) and resolved.get("code"):
                return moss.normalize_owner(resolved["code"])
        return moss.DEFAULT_OWNER

    def state_response(*, transport_owner=None, owner_code=None, headers=None):
        _, body = payloads.read(transport_owner, owner_code=owner_code)
        return web.Response(body=body, content_type="application/json", charset="utf-8", headers=headers)

    _gzip_static_cache = OrderedDict()
    _GZIP_CACHE_LIMIT = 32
    _GZIP_EXTS = (".js", ".mjs", ".css", ".svg", ".json", ".html")
    _GZIP_MIME = {"application/json", "text/plain", "text/markdown", "text/html",
                  "application/javascript", "text/javascript", "text/css", "image/svg+xml"}

    def _cached_gzip_file(file_path: Path):
        st = file_path.stat()
        if st.st_size < 4096:
            return None
        key = (str(file_path), st.st_mtime_ns, st.st_size)
        hit = _gzip_static_cache.get(key)
        if hit is not None:
            _gzip_static_cache.move_to_end(key)
            return hit
        gz = gzip.compress(file_path.read_bytes(), compresslevel=6)
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}-gz"'
        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        entry = (gz, etag, ctype)
        _gzip_static_cache[key] = entry
        while len(_gzip_static_cache) > _GZIP_CACHE_LIMIT:
            _gzip_static_cache.popitem(last=False)
        return entry

    @web.middleware
    async def local_requests(request, handler):
        origin = request.headers.get("Origin")
        if request.host not in accepted_hosts or (origin and origin != f"{request.scheme}://{request.host}"):
            raise web.HTTPForbidden()
        auth_check = request.app.get("moss_auth_check")
        is_internal_callback = (
            request.remote in {"127.0.0.1", "::1"}
            and request.path in {"/api/timer-notification", "/api/shutdown", "/api/wake/activate", "/api/wake/status"}
        )
        needs_auth = (
            (request.path.startswith(("/api/", "/media/", "/drafts/"))
             or request.path in {"/conversation/runtime-log", "/fui/local-weather"})
            and request.path not in {"/api/health", "/api/auth/status", "/api/auth/verify", "/api/auth/logout"}
            and not is_internal_callback
        )
        if auth_check and needs_auth:
            authed, ident = auth_check(request)
            if not authed:
                return web.json_response({"error": "需要输入 4 位 550W 授权码验证身份", "auth_required": True},
                                         status=401, headers={"Cache-Control": "no-store"})
            request["moss_identity"] = ident
            # CS-only system administration guard: C operators may only personalize badge/location/spectrum/tone
            is_cs = ident.get("role") == "CS"
            if not is_cs:
                p = request.path
                admin_get_blocked = (
                    request.method == "GET"
                    and p in {"/api/online-music/config", "/api/console-settings/providers", "/api/console-settings/library", "/api/iot/config"}
                )
                admin_write_blocked = request.method in {"POST", "DELETE", "PUT"} and (
                    p in {
                        "/api/select-model", "/api/console-settings/providers", "/api/console-settings/library",
                        "/api/online-music/config", "/api/online-music/ping",
                        "/api/iot/config", "/api/ha/service", "/api/hardware/moss/command",
                        "/api/hardware/moss/telemetry", "/api/proactive/config", "/api/watchers",
                        "/api/skills", "/api/sag/ingest", "/api/jobs", "/api/rsi/iterate",
                        "/api/brain/consolidate", "/api/soul/profile", "/api/soul/beliefs",
                        "/api/soul/distill", "/api/routines", "/api/auth/config", "/api/auth/users",
                        "/api/shutdown", "/api/approve", "/api/context/jev-handoff",
                        "/api/context/vision-to-sag", "/api/prepare-speech",
                    }
                    or p.startswith((
                        "/api/watchers/", "/api/rsi/lessons/", "/api/skills/", "/api/auth/users/",
                        "/api/soul/profile/", "/api/soul/beliefs/", "/api/routines/", "/api/proactive/alerts/",
                    ))
                )
                if admin_get_blocked or admin_write_blocked:
                    return web.json_response(
                        {"error": "当前为 C 级普通权限，仅可设置工牌、定位、频谱及陪伴语气；该系统配置需 CS 管理员授权。",
                         "required_role": "CS", "role": ident.get("role", "C")},
                        status=403, headers={"Cache-Control": "no-store"},
                    )
        expected_type = {"/api/transcribe": "audio/wav", "/api/context/ocr": "image/png"}.get(request.path, "application/json")
        if request.method == "POST" and request.content_type != expected_type:
            raise web.HTTPUnsupportedMediaType()
        started = perf_counter()
        path = request.path
        should_log = (path.startswith(("/api/", "/media/")) and path not in {
            "/api/events", "/api/runtime-log/stream", "/api/device-status", "/api/health",
        })
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            if should_log:
                ms = (perf_counter() - started) * 1000
                backend_log.emit(f"{request.method} {path} -> {exc.status} ({ms:.0f}ms)",
                                 source="HTTP", level="warn" if exc.status < 500 else "crit", error=exc.status >= 400)
            raise
        if (request.method in {"GET", "HEAD"} and getattr(response, "status", 0) == 200
                and "Cache-Control" not in response.headers
                and path.startswith("/conversation-assets/")
                and (path == "/conversation-assets/moss-render.js"
                     or path.endswith((".glb", ".bin", ".wasm", ".task", ".json", ".png", ".jpg", ".webp", ".woff2")))):
            response.headers["Cache-Control"] = "public, max-age=86400"
        # Lightweight Gzip compression for large JS/CSS/SVG/JSON responses when client supports gzip
        accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
        if (accepts_gzip and request.method == "GET" and getattr(response, "status", 0) == 200
                and "Range" not in request.headers and "Content-Encoding" not in response.headers):
            if isinstance(response, web.FileResponse) and path.endswith(_GZIP_EXTS):
                fpath = getattr(response, "_path", None)
                if isinstance(fpath, Path) and fpath.is_file():
                    cached = _cached_gzip_file(fpath)
                    if cached is not None:
                        gz_bytes, etag, ctype = cached
                        out_headers = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding", "ETag": etag}
                        if "Cache-Control" in response.headers:
                            out_headers["Cache-Control"] = response.headers["Cache-Control"]
                        if request.headers.get("If-None-Match") == etag:
                            response = web.Response(status=304, headers=out_headers)
                        else:
                            response = web.Response(body=gz_bytes, content_type=ctype, headers=out_headers)
            elif (type(response) is web.Response and response.body and len(response.body) >= 2048
                  and response.content_type in _GZIP_MIME):
                response.enable_compression(web.ContentCoding.gzip)
                response.headers["Vary"] = "Accept-Encoding"
        if should_log:
            ms = (perf_counter() - started) * 1000
            status = getattr(response, "status", 200)
            extra = ""
            if path in {"/api/speak", "/api/speak-batch"}:
                st = response.headers.get("Server-Timing", "")
                extra = f" [{st}]" if st else ""
            backend_log.emit(f"{request.method} {path} {status} ({ms:.0f}ms){extra}",
                             source="TTS" if "speak" in path else "ASR" if "transcribe" in path else "MEDIA" if path.startswith("/media/") else "HTTP",
                             level="ok" if status < 300 else "warn" if status < 500 else "crit",
                             error=status >= 400)
        return response

    async def state(request):
        return state_response(owner_code=_req_owner(request), headers={"Cache-Control": "no-store"})

    async def jev_handoff(request):
        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        if not DRAFT_HANDOFF_ENABLED:
            return web.json_response({"status":"refused", "error":"本机草稿窗不可用。"}, status=503)
        try:
            result = await handoffs.submit(await request.json())
        except ValueError as error:
            return web.json_response({"status":"refused", "error":str(error)}, status=400)
        code = {"filled":200, "refused":409, "uncertain":503}[result["status"]]
        return web.json_response(result, status=code, headers={"Cache-Control":"no-store"})

    async def close_handoffs(app):
        await handoffs.close()

    async def health(request):
        return web.json_response({"app": "moss-local-chat", "pid": os.getpid(), "model": moss.model_name, "model_id": moss.model_id,
                                  "personality_id": moss.personality_id,
                                  "agent_mode": "holojarvis", "local_engine": local_engine(),
                                  "qwen_backend": qwen_backend, "vulkan_device": vulkan_device, "allow_cpu": allow_cpu,
                                  "speech_standby_seconds": speech_standby_seconds,
                                  "speech_session_warm": speech_session_warm,
                                  "speech_session_warm_ready": bool(getattr(speech, "session_warm_ready", False)),
                                  "memory_backend": "mem0" if moss.index else "sqlite",
                                  "memory_url": moss.index.url if moss.index else None})

    async def device_readings(request):
        sample = await device_status.read()
        now_t = perf_counter()
        if backend_log.subscribers and now_t - last_dev_log[0] >= 4.5:
            last_dev_log[0] = now_t
            cpu = sample.get("cpu_percent")
            gpu = sample.get("gpu") or {}
            mem = sample.get("memory") or {}
            parts = []
            if cpu is not None:
                parts.append(f"CPU {cpu:.0f}%")
            if gpu.get("percent") is not None:
                gt = f"/{gpu['temperature_c']:.0f}C" if gpu.get("temperature_c") is not None else ""
                parts.append(f"GPU {gpu['percent']:.0f}%{gt}")
            if mem.get("available") is not None and mem.get("total"):
                parts.append(f"RAM {mem['available']/(1024**3):.1f}/{mem['total']/(1024**3):.0f}G")
            if parts:
                backend_log.emit(" · ".join(parts), source="DEV", level="info", write_file=False)
        return web.json_response(sample, headers={"Cache-Control": "no-store"})

    def configured_position(identifier):
        entry = selected_city(identifier)
        if not entry:
            raise ValueError("找不到所选城市，请重新选择。")
        return {"name": entry["name"], "latitude": entry["latitude"], "longitude": entry["longitude"],
                "kind": "configured", "parent": entry["city"] or entry["province"]}

    async def cities(request):
        if "id" not in request.query:
            return web.json_response(city_catalog())
        entry = selected_city(request.query["id"])
        if not entry:
            return web.json_response({"error": "找不到所选城市。"}, status=404)
        return web.json_response({"city": entry})

    async def weather_readings(request):
        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        headers = {"Cache-Control": "no-store"}
        try:
            body = await request.json()
            city_request = isinstance(body, dict) and body.keys() == {"city_id"}
            location = configured_position(body["city_id"]) if city_request else browser_position(body)
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400, headers=headers)
        try:
            reading = await current_weather.read(location)
        except (aiohttp.ClientError, TimeoutError, ValueError, OverflowError):
            return web.json_response({"error":"当前位置天气暂时无法读取。"}, status=502, headers=headers)
        if city_request:
            reading = {**reading, "city_id": body["city_id"]}
        return web.json_response(reading, headers=headers)

    async def models(request):
        return web.json_response({"models": moss.model_list(), "selected": moss.model_id})

    async def personality_endpoint(request):
        from personality import personality_list
        owner = _req_owner(request)
        if request.method == "GET":
            return web.json_response({"selected": moss.owner_personality(owner), "personalities": personality_list()},
                                     headers={"Cache-Control": "no-store"})
        try:
            body = await request.json()
            preset_id = body.get("id") if isinstance(body, dict) else None
            await moss.select_personality(preset_id, owner_code=owner)
        except (ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return state_response(owner_code=owner)

    async def ocr(request):
        try:
            data = await request.clone(client_max_size=MAX_CAPTURE_BYTES).read()
            return web.json_response({"text": await recognize_capture(data)}, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400)
        except (OSError, RuntimeError, TimeoutError):
            return web.json_response({"error": "本机没有完成文字识别。"}, status=503)

    async def image_question(request):
        owner = _req_owner(request)
        try:
            body = await request.clone(client_max_size=IMAGE_MAX_JSON).json()
            if not isinstance(body, dict) or body.get("model_id") != WINDOW_IMAGE_PROVIDER:
                raise ValueError("请核对窗口画面的百炼提交目标。")
            chat_id, source_id, text = (body.get(key) for key in ("chat_id", "turn_id", "text"))
            if not all(isinstance(value, str) and value.strip() for value in (chat_id, source_id, text)):
                raise ValueError("请提供窗口画面来源和问题。")
            if len(text) > 1200 or len(text.encode("utf-8")) > 2700:
                raise ValueError("请输入一条不超过 1200 个字符、2700 字节的问题。")
            moss.require_chat_owner(chat_id, owner)
            if chat_id != moss.active_chat_for(owner) or not moss.model_available(WINDOW_IMAGE_PROVIDER):
                raise ValueError("请打开当前对话并核对百炼模型是否可用。")
            image, size = await asyncio.to_thread(prepare_window_image, body.get("image"))
            if not await moss.message(text, chat_id, image_context=(source_id, image, size), owner_code=owner):
                raise ValueError("请输入针对画面的问题。")
            return state_response(owner_code=owner, headers={"Cache-Control": "no-store"})
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "截图超出 8 MiB，请选择较小的窗口。"}, status=413)
        except PermissionError as error:
            return web.json_response({"error": str(error)}, status=403)
        except (ValueError, KeyError) as error:
            return web.json_response({"error": str(error)}, status=409)

    async def timer_dismiss(request):
        try:
            body = await request.json()
            with moss.db:
                moss.timers.dismiss(body.get("id") if isinstance(body, dict) else None)
                moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
            moss._notify()
            await moss.sync_reminders()
            return web.json_response({"ok": True})
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400)

    async def _read_json_dict(request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("请求体必须是合法 JSON。") from exc
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return body

    async def timer_notification(request):
        try:
            body = await _read_json_dict(request)
        except ValueError:
            raise web.HTTPBadRequest()
        supplied = body.get("token")
        if not isinstance(supplied, str) or not supplied.isascii() or not hmac.compare_digest(supplied, token):
            raise web.HTTPForbidden()
        key, delivered = body.get("id"), body.get("delivered", False)
        generation = body.get("generation", 0)
        if (type(key) is not int or not 0 < key < 2**63 or type(delivered) is not bool
                or type(generation) is not int or not 0 <= generation < 2**63):
            raise web.HTTPBadRequest()
        with moss.db:
            item = moss.timers.notification(key, delivered=delivered, generation=generation)
        return web.json_response({"timer": item})

    async def notes(request):
        try:
            if request.method == "GET":
                archive = request.query.get("archived", "0")
                before = request.query.get("before", "0")
                if archive not in {"0", "1"} or not before.isascii() or not before.isdecimal() or len(before) > 19:
                    raise ValueError("便签列表参数无效。")
                cursor = int(before)
                if cursor >= 2**63:
                    raise ValueError("便签列表参数无效。")
                result = moss.notes.page(request.query.get("q", ""), archived=archive == "1", before=cursor)
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            body = await request.json()
            note_id = request.match_info.get("note_id")
            with moss.db:
                if note_id:
                    key = int(note_id)
                    if not 0 < key < 2**63:
                        raise ValueError("找不到这条便签。")
                    item = moss.notes.update(key, NoteUpdate.model_validate(body))
                else:
                    item = moss.notes.add(NoteWrite.model_validate(body).text)
            try:
                if isinstance(body, dict) and note_id and body.get("action") == "archive":
                    moss.sag.delete_by_source_ref("note", f"note:{item['id']}")
                else:
                    moss.sag.upsert_event(content=item["text"], title=item["text"][:40], source="note", source_ref=f"note:{item['id']}")
            except Exception:
                pass
            return web.json_response({"item": item}, status=200 if note_id else 201)
        except ValidationError:
            return web.json_response({"error": "请提供有效的便签和操作，正文可填写 1 至 1200 个字符。"}, status=400)
        except NoteConflict as error:
            return web.json_response({"error": str(error)}, status=409)
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400)

    async def prepare_voice(request):
        try:
            await voice.prepare()
            return web.Response(status=204)
        except (ValueError, TimeoutError, OSError) as error:
            return web.json_response({"error": str(error) or "本机语音识别准备超时"}, status=503)

    async def transcribe(request):
        try:
            preview = request.headers.get("X-MOSS-ASR-Preview") == "1"
            text = await voice.transcribe(await request.read(), preview=preview)
            return web.json_response({"text": text})
        except (ValueError, TimeoutError, OSError) as error:
            return web.json_response({"error": str(error) or "本机语音识别超时"}, status=422)

    async def stop_speech(app):
        await speech.close()

    async def close_voice(app):
        await voice.close()

    async def prepare_speech(request):
        try:
            body = await request.json()
            if body == {}:
                if speech_prepare:
                    await speech.prepare_imports()
            elif isinstance(body, dict) and set(body) in ({"mic"}, {"mic", "release"}):
                key = body["mic"]
                if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key)
                        or ("release" in body and body["release"] is not True)):
                    raise ValueError("无效的麦克风语音准备请求")
                if speech_mic_warm:
                    if body.get("release"):
                        await speech.release_microphone(key)
                    elif not speech_mic_warm_on_activity or request.headers.get("X-MOSS-Voice-Activity") == "1":
                        ready = await speech.prepare_microphone(key, eligible=can_warm_microphone,
                                                                prime=not speech_mic_warm_on_activity)
                        if speech_mic_warm_on_activity:
                            return web.Response(status=204, headers={"X-MOSS-Mic-Prepared": "1" if ready else "0"})
            elif isinstance(body, dict) and set(body) in ({"session"}, {"session", "release"}):
                key = body["session"]
                if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key)
                        or ("release" in body and body["release"] is not True)):
                    raise ValueError("无效的会话语音预热请求")
                if body.get("release"):
                    if hasattr(speech, "release_session"):
                        await speech.release_session(key, force_stop=True)
                elif speech_session_warm and hasattr(speech, "prepare_session"):
                    ready = await speech.prepare_session(key, eligible=can_warm_microphone, prime=True)
                    return web.Response(status=204, headers={"X-MOSS-Session-Prepared": "1" if ready else "0"})
                else:
                    return web.Response(status=204, headers={"X-MOSS-Session-Prepared": "0"})
            else:
                raise ValueError("语音进程准备请求只接受空对象、麦克风标识或会话标识")
            return web.Response(status=204)
        except (ValueError, UnicodeDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (OSError, RuntimeError):
            return web.json_response({"error": "未能提前启动语音进程"}, status=503)

    async def speak(request):
        try:
            body = await request.json()
            transport = request.transport
            if not isinstance(body, dict):
                raise SpeechInputError("需要播报文本")
            category = body.get("category", "reply")
            if not isinstance(category, str) or category not in SPEECH_RATES:
                return web.json_response({"error": "未知播报语气"}, status=400)
            room_amount = body.get("room_amount", 100)
            if type(room_amount) is not int or not 0 <= room_amount <= 150:
                raise SpeechInputError("空间感需为 0 至 150 的整数百分比")
            room = {"room_amount": room_amount} if room_amount != 100 else {}
            headers = {"Cache-Control": "no-store", "X-MOSS-Voice": "OmniVoice", "X-MOSS-Delivery": category}
            def disconnected():
                return transport is None or transport.is_closing()

            if request.path == "/api/speak-batch":
                waves, mode = await speech.render_batch(body.get("texts"), disconnected, category=category, **room)
                multipart = aiohttp.MultipartWriter("form-data")
                for index, data in enumerate(waves):
                    part = multipart.append(data, {"Content-Type": "audio/wav"})
                    part.set_content_disposition("form-data", name=str(index), filename=f"{index}.wav")
                headers["X-MOSS-Synthesis"] = mode
                headers["X-MOSS-Batch-Capable"] = "1" if speech.batch_capable else "0"
                return web.Response(body=multipart, headers=headers)
            timings = {}
            binary = await speech.render(body.get("text"), disconnected, category=category,
                                         cached_only=category == "media" and body.get("prefer_cached") is True,
                                         timings=timings, **room)
            headers["Server-Timing"] = ", ".join(f"{name};dur={duration:.1f}" for name, duration in timings.items())
            headers["X-MOSS-Batch-Capable"] = "1" if speech.batch_capable else "0"
            if binary is None:
                headers.update({"Content-Type": "audio/wav", "X-MOSS-Announcement": "short"})
                return web.FileResponse(ROOT / "static/music-ack.wav", headers=headers)
            return web.Response(body=binary, content_type="audio/wav",
                                headers=headers)
        except (SpeechInputError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except ConnectionAbortedError:
            return web.Response(status=204)
        except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
            return web.json_response({"error": str(exc) or "本机语音合成超时"}, status=503)

    async def action(request):
        transport_owner = None
        owner = _req_owner(request)
        try:
            body = await request.json()
            verb = request.match_info["verb"]
            if not isinstance(body, dict):
                raise ValueError("需要 JSON 对象")
            if verb == "new-chat":
                await moss.new_chat(owner_code=owner)
            elif verb == "transport-result":
                moss.finish_transport(body, owner_code=owner)
            elif verb in {"media-select", "media-event"}:
                chat, turn, media = (body.get(k) for k in ("chat_id", "turn_id", "media_id"))
                if not all(isinstance(k, str) and k for k in (chat, turn, media)):
                    raise ValueError("缺少对话、轮次或媒体标识")
                moss.require_chat_owner(chat, owner)
                if verb == "media-select":
                    await moss.select_media(chat, turn, media, body.get("index"), owner_code=owner)
                else:
                    event, play_id = body.get("event"), body.get("play_id")
                    recover = body.get("recover") is True and event == "failed"
                    if recover and "position_s" in body:
                        def connected():
                            transport = request.transport
                            return transport is not None and not transport.is_closing()
                        if chat == moss.active_chat_for(owner) and await moss.resume_failed_track(chat, turn, media, play_id, body["position_s"], connected):
                            return state_response(owner_code=owner)
                        return web.json_response({"ok": True})
                    moss.record_playback(chat, turn, media, event, play_id, body.get("started_at_us"), owner_code=owner)
                    if recover:
                        if await moss.recover_failed_track(chat, turn, media):
                            return state_response(owner_code=owner)
                    return web.json_response({"ok": True})
            else:
                fields = {"message": "text", "approve": "turn_id", "cancel": "turn_id", "select-model": "id",
                          "remember": "text", "forget": "id", "switch-chat": "id", "select-personality": "id"}
                arg = body.get(fields[verb])
                if not isinstance(arg, str) or not arg.strip():
                    raise ValueError("缺少文字或操作标识")
                if verb == "message" and not body.get("chat_id"):
                    if any(field in body for field in ("reply_context_turn", "reply_context_goal", "reply_context_request_id")):
                        raise ValueError("请指定拟回复所属的对话。")
                    if "window_context" in body:
                        raise ValueError("请指定窗口文字所属的对话。")
                if verb == "message" and "window_context" in body and not isinstance(body["window_context"], dict):
                    raise ValueError("请提供有效的窗口文字来源。")
                position = (browser_position(body["browser_location"])
                            if verb == "message" and "browser_location" in body else None)
                configured = (configured_position(body["preferred_city_id"])
                              if verb == "message" and "preferred_city_id" in body else None)
                if verb == "message" and body.get("chat_id"):
                    transport_owner = await moss.message(arg, chat_id=body["chat_id"], player=body.get("player"),
                                                         transport_request_id=body.get("transport_request_id"),
                                                         queued_player=body.get("queued_player"),
                                                         reply_context_turn=body.get("reply_context_turn"),
                                                         reply_context_goal=body.get("reply_context_goal"),
                                                         reply_context_request_id=body.get("reply_context_request_id"),
                                                         window_context=body.get("window_context"), browser_location=position,
                                                         configured_location=configured, owner_code=owner)
                elif verb == "message":
                    await moss.message(arg, browser_location=position, configured_location=configured, owner_code=owner)
                elif verb in {"approve", "cancel"}:
                    draft_only = body.get("pending_draft_only") is True
                    calendar_only = body.get("pending_calendar_only") is True
                    if draft_only and calendar_only:
                        raise ValueError("语音决定只能对应一项待确认操作。")
                    if "conversation_only" in body:
                        chat_id = body.get("chat_id")
                        if (verb != "cancel" or set(body) != {"turn_id", "chat_id", "conversation_only"}
                                or body["conversation_only"] is not True or not isinstance(chat_id, str) or not chat_id):
                            raise ValueError("请指定当前对话及要结束的回复轮次。")
                        await moss.cancel(arg, conversation_only=True, chat_id=chat_id, owner_code=owner)
                    else:
                        await getattr(moss, verb)(arg, pending_draft_only=draft_only, pending_calendar_only=calendar_only, owner_code=owner)
                elif verb in {"remember", "forget", "switch-chat", "select-personality"}:
                    await getattr(moss, verb.replace("-", "_"))(arg, owner_code=owner)
                else:
                    if verb == "select-model" and arg == "local":
                        await speech.release_microphone()
                        if hasattr(speech, "release_session"):
                            await speech.release_session(force_stop=True)
                        if hasattr(speech, "idle"):
                            await speech.idle()
                    await getattr(moss, verb.replace("-", "_"))(arg)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return state_response(transport_owner=transport_owner, owner_code=owner)

    async def backend_log_stream(request):
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8",
                                             "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await stream.prepare(request)
        signal = asyncio.Event()
        backend_log.subscribers.add(signal)
        cursor = backend_log.attach(request.headers.get("Last-Event-ID", ""))
        try:
            while not stopped.is_set():
                signal.clear()
                for cursor, frame in backend_log.pending(cursor):
                    await stream.write(frame)
                try:
                    await asyncio.wait_for(signal.wait(), 15)
                except TimeoutError:
                    await stream.write(b": keep-alive\n\n")
        except (ConnectionError, aiohttp.ClientConnectionError):
            pass
        finally:
            backend_log.subscribers.discard(signal)
        return stream

    async def runtime_log_source(request):
        return web.json_response(backend_log.source_info(), headers={"Cache-Control": "no-store"})

    async def mobile_page(request):
        if fui_public is not None:
            mfile = fui_public / "mobile.html"
            if mfile.is_file():
                return web.FileResponse(mfile, headers={"Cache-Control": "no-cache"})
        raise web.HTTPNotFound()

    async def runtime_stream(request):
        referer_path = urlsplit(request.headers.get("Referer", "")).path
        if request.query.get("mode") == "tail" or (fui_public is not None and referer_path == "/"):
            return await backend_log_stream(request)
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8",
                                             "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await stream.prepare(request)
        signal = asyncio.Event()
        cursor = runtime.attach(request.headers.get("Last-Event-ID", ""))
        subscribers.add(signal)
        try:
            while not stopped.is_set():
                signal.clear()
                for cursor, frame in runtime.pending(cursor):
                    await stream.write(frame)
                try:
                    await asyncio.wait_for(signal.wait(), 15)
                except TimeoutError:
                    await stream.write(b": keep-alive\n\n")
        except (ConnectionError, aiohttp.ClientConnectionError):
            pass
        finally:
            runtime.detach()
            subscribers.discard(signal)
        return stream

    async def event_stream(request):
        stream_owner = _req_owner(request)
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8",
                                             "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await stream.prepare(request)
        signal = asyncio.Event()
        subscribers.add(signal)
        wake_notifications = (
            request.path == "/api/events"
            and request.query.get("wake") == "1"
            and stream_owner == moss.DEFAULT_OWNER
        )
        if wake_notifications:
            wake.pages[signal] = request.transport
        previous_wake = None
        auth_check = request.app.get("moss_auth_check")
        try:
            while not stopped.is_set():
                signal.clear()
                if auth_check:
                    authed, cur_ident = auth_check(request)
                    cur_owner = moss.normalize_owner((cur_ident or {}).get("code"))
                    if not authed or cur_owner != stream_owner:
                        await stream.write(b"event: auth-required\ndata: {}\n\n")
                        break
                offer = wake.offer if wake_notifications else None
                if offer and not offer.shown.is_set() and offer.ticket != previous_wake:
                    await stream.write(f"event: wake-offer\ndata: {offer.ticket}\n\n".encode("ascii"))
                    previous_wake = offer.ticket
                revision, payload = payloads.read(owner_code=stream_owner)
                await stream.write(f"id: {revision}\nevent: state\ndata: ".encode("ascii") + payload + b"\n\n")
                try:
                    await asyncio.wait_for(signal.wait(), 15)
                except TimeoutError:
                    await stream.write(b": keep-alive\n\n")
        except (ConnectionError, aiohttp.ClientConnectionError):
            pass
        finally:
            subscribers.discard(signal)
            wake.pages.pop(signal, None)
        return stream

    async def file(request):
        owner = _req_owner(request)
        name = request.match_info["name"]
        if Path(name).name != name:
            raise web.HTTPNotFound()
        turn_id = Path(name).stem
        owner_row = moss.db.execute(
            "SELECT c.owner_code FROM turns t JOIN chats c ON c.id = t.chat_id WHERE t.id=?",
            (turn_id,),
        ).fetchone()
        if owner_row and moss.normalize_owner(owner_row[0]) != owner:
            raise web.HTTPForbidden()
        try:
            content = moss.read_draft(name)
        except (KeyError, ValueError, FileNotFoundError):
            raise web.HTTPNotFound()
        return web.Response(body=content, content_type="text/plain",
                            charset="utf-8", headers={"X-Content-Type-Options": "nosniff"})

    async def shutdown(request):
        if request.remote not in {"127.0.0.1", "::1"}:
            raise web.HTTPForbidden()
        try:
            body = await _read_json_dict(request)
        except ValueError:
            raise web.HTTPBadRequest()
        if not hmac.compare_digest(str(body.get("token", "")), token):
            raise web.HTTPForbidden()
        asyncio.get_running_loop().call_soon(stopped.set)
        return web.json_response({"stopping": True})

    async def index(request):
        if fui_public is not None and request.path == "/":
            return web.FileResponse(fui_public / "index.html", headers={"Cache-Control": "no-cache"})
        return web.FileResponse(ROOT / "static/conversation.html", headers={"Cache-Control": "no-cache"})

    async def status_page(request):
        if fui_public is not None:
            return web.FileResponse(fui_public / "status.html", headers={"Cache-Control": "no-cache"})
        return web.FileResponse(ROOT / "static/device-status.html", headers={"Cache-Control": "no-cache"})

    async def model_view(request):
        return web.FileResponse(ROOT / "static/conversation-assets/moss-view.js",
                                headers={"Cache-Control": "no-cache"})

    async def chats_search(request):
        owner = _req_owner(request)
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response({"query": "", "hits": []}, headers={"Cache-Control": "no-store"})
        needle = q.casefold()
        rows = moss.db.execute(
            "SELECT t.id, t.chat_id, c.title, t.doc FROM turns t "
            "JOIN chats c ON c.id = t.chat_id WHERE COALESCE(c.owner_code,'MOSS')=? "
            "ORDER BY t.rowid DESC LIMIT 600",
            (owner,),
        ).fetchall()
        hits = []
        matched_chats: set[str] = set()
        title_fallback: dict[str, dict] = {}
        for turn_id, cid, title, raw_doc in rows:
            try:
                doc = json.loads(raw_doc)
            except ValueError:
                continue
            user_txt = str(doc.get("user") or "")
            reply_txt = str(doc.get("reply") or "")
            title_txt = str(title or "")
            matched_role = ""
            snippet = ""
            for role_name, field in (("user", user_txt), ("assistant", reply_txt)):
                pos = field.casefold().find(needle)
                if pos != -1:
                    matched_role = role_name
                    lo = max(0, pos - 24)
                    hi = min(len(field), pos + len(q) + 56)
                    prefix = "…" if lo > 0 else ""
                    suffix = "…" if hi < len(field) else ""
                    snippet = prefix + field[lo:hi].replace("\n", " ") + suffix
                    break
            if matched_role:
                matched_chats.add(cid)
                hits.append({
                    "chat_id": cid,
                    "chat_title": title_txt or cid,
                    "turn_id": turn_id,
                    "created_at": doc.get("created_at") or "",
                    "role": matched_role,
                    "snippet": snippet,
                    "user": user_txt[:120],
                })
                if len(hits) >= 40:
                    break
            elif cid not in title_fallback and needle in title_txt.casefold():
                pos = title_txt.casefold().find(needle)
                lo = max(0, pos - 24)
                hi = min(len(title_txt), pos + len(q) + 56)
                prefix = "…" if lo > 0 else ""
                suffix = "…" if hi < len(title_txt) else ""
                title_fallback[cid] = {
                    "chat_id": cid,
                    "chat_title": title_txt or cid,
                    "turn_id": turn_id,
                    "created_at": doc.get("created_at") or "",
                    "role": "title",
                    "snippet": prefix + title_txt[lo:hi].replace("\n", " ") + suffix,
                    "user": user_txt[:120],
                }
        for cid, entry in title_fallback.items():
            if len(hits) >= 40:
                break
            if cid not in matched_chats:
                hits.append(entry)
        return web.json_response({"query": q, "hits": hits}, headers={"Cache-Control": "no-store"})

    async def chat_export(request):
        owner = _req_owner(request)
        cid = request.match_info["chat_id"]
        chat_row = moss.db.execute("SELECT id, title, created_at, owner_code FROM chats WHERE id=?", (cid,)).fetchone()
        if not chat_row:
            raise web.HTTPNotFound()
        _, title, created_at, chat_owner = chat_row
        if moss.normalize_owner(chat_owner) != owner:
            return web.json_response({"error": "无权导出其他授权人员的对话记录。"}, status=403)
        op_label = "OP-01" if owner == moss.DEFAULT_OWNER else f"OP-{owner}"
        turn_rows = moss.db.execute("SELECT doc FROM turns WHERE chat_id=? ORDER BY rowid ASC", (cid,)).fetchall()
        lines = [
            f"# MOSS 550W 对话记录 · {title or cid}",
            "",
            f"- 会话编号：`{cid}`",
            f"- 所属人员：`{owner}`",
            f"- 创建时间：`{created_at}`",
            f"- 轮次数量：`{len(turn_rows)}`",
            "",
            "---",
            "",
        ]
        for idx, (raw_doc,) in enumerate(turn_rows, 1):
            try:
                doc = json.loads(raw_doc)
            except ValueError:
                continue
            ts = str(doc.get("created_at") or "")[:19].replace("T", " ")
            lines.append(f"## #{idx:02d} · {ts}")
            lines.append("")
            lines.append(f"**{op_label}**：{doc.get('user') or ''}")
            lines.append("")
            body = doc.get("reply") or doc.get("error") or f"({doc.get('status') or 'pending'})"
            lines.append(f"**MOSS**：{body}")
            lines.append("")
        md = "\n".join(lines).encode("utf-8")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", cid)[:24] or "chat"
        disp = f'attachment; filename="moss-chat-{safe_id}.md"; filename*=UTF-8\'\'{quote(f"moss-chat-{safe_id}.md")}'
        return web.Response(
            body=md,
            content_type="text/markdown",
            charset="utf-8",
            headers={"Cache-Control": "no-store", "Content-Disposition": disp, "X-Content-Type-Options": "nosniff"},
        )

    async def chat_attach(request):
        """Accept pasted screenshots (Ctrl+V) or dragged documents (PDF/DOCX/XLSX/CSV/text/code) directly from composer."""
        import base64
        import io
        from PIL import Image

        owner = _req_owner(request)
        try:
            body = await request.clone(client_max_size=16 * 1024 * 1024).json()
            if not isinstance(body, dict):
                raise ValueError("需要 JSON 对象")
            name = Path(str(body.get("name") or "attachment.bin")).name
            mime = str(body.get("mime") or "").lower()
            prompt = str(body.get("prompt") or "").strip()
            chat_id = str(body.get("chat_id") or moss.active_chat_for(owner))
            moss.require_chat_owner(chat_id, owner)
            if chat_id != moss.active_chat_for(owner):
                raise ValueError("当前对话已切换，请刷新后在当前对话重新发送附件。")
            raw_b64 = str(body.get("data_b64") or "").strip()
            if not raw_b64:
                raise ValueError("附件内容为空")
            if "," in raw_b64 and raw_b64.startswith("data:"):
                raw_b64 = raw_b64.split(",", 1)[1]
            raw = base64.b64decode(raw_b64)
            ext = Path(name).suffix.lower()
            is_image = mime.startswith("image/") or ext in IMAGE_EXTS
            if is_image:
                def _to_png(blob: bytes):
                    with Image.open(io.BytesIO(blob)) as im:
                        im = im.convert("RGB")
                        im.thumbnail((2048, 2048))
                        buf = io.BytesIO()
                        im.save(buf, format="PNG")
                        return buf.getvalue(), im.size
                png_bytes, size = await asyncio.to_thread(_to_png, raw)
                q = prompt or f"请解读这张图片（{name}）中的核心信息。"
                if len(q) > 1200:
                    q = q[:1200]
                if moss.model_available(WINDOW_IMAGE_PROVIDER):
                    await moss.message(q, chat_id, image_context=("composer_attach", png_bytes, size), owner_code=owner)
                else:
                    ocr_text = (await recognize_capture(png_bytes)).strip()
                    combined = f"{q}\n\n【本机 OCR 识别自图片 {name}（百炼视觉未配置，仅提取文字）】\n{ocr_text or '（未识别到文字）'}"
                    await moss.message(combined[:1150], chat_id, owner_code=owner)
                return state_response(owner_code=owner, headers={"Cache-Control": "no-store"})

            extracted = await asyncio.to_thread(extract_document_bytes, name, raw, max_chars=8000)
            q = prompt or f"请总结附件《{extracted['name']}》的关键要点。"
            sag_note = ""
            if owner == moss.DEFAULT_OWNER:
                # Keep user message concise while injecting extracted content via SAG + message text safely
                ev = moss.sag.upsert_event(
                    content=f"附件《{extracted['name']}》：\n{extracted['text']}",
                    title=f"附件：{extracted['name']}",
                    source="attach",
                )
                sag_note = f"，已同步至 SAG E#{ev['id']}"
            preview_budget = max(200, 1080 - len(q) - len(extracted["name"]) - 120)
            snippet = extracted["text"][:preview_budget]
            msg = (
                f"{q}\n"
                f"【附件《{extracted['name']}》已解析（{extracted['bytes']}B{sag_note}，以下为只读正文摘录）】\n"
                f"{snippet}"
            )
            await moss.message(msg[:1190], chat_id, owner_code=owner)
            return state_response(owner_code=owner, headers={"Cache-Control": "no-store"})
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "附件超出 15 MB 上限。"}, status=413)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (OSError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc) or "附件格式无法识别或已损坏。"}, status=400)

    app = web.Application(middlewares=[local_requests], client_max_size=16 * 1024 * 1024)
    app.on_startup.append(start_log_tail)
    app.on_shutdown.append(stop_log_tail)
    app.on_shutdown.append(close_handoffs)
    app.on_shutdown.append(stop_speech)
    app.on_cleanup.append(close_voice)
    app.add_routes([web.get("/", index), web.get("/api/health", health), web.get("/api/state", state),
                    web.get("/status", status_page), web.get("/status.html", status_page),
                    web.get("/mobile", mobile_page), web.get("/mobile.html", mobile_page),
                    web.get("/api/device-status", device_readings),
                    web.get("/api/runtime-log/stream", backend_log_stream),
                    web.get("/api/runtime-log/source", runtime_log_source),
                    web.get("/api/cities", cities), web.post("/api/weather/current", weather_readings),
                    web.post("/api/context/ocr", ocr), web.post("/api/context/image-question", image_question),
                    web.post("/api/context/jev-handoff", jev_handoff),
                    web.post("/api/timer-dismiss", timer_dismiss),
                    web.post("/api/timer-notification", timer_notification),
                    web.get("/api/notes", notes), web.post("/api/notes", notes),
                    web.post(r"/api/notes/{note_id:[0-9]{1,19}}", notes),
                    web.get("/api/chats/search", chats_search),
                    web.get(r"/api/chats/{chat_id:[A-Za-z0-9_-]{1,64}}/export", chat_export),
                    web.post("/api/chat/attach", chat_attach),
                    web.get("/api/events", event_stream), web.post("/api/shutdown", shutdown),
                    web.post(r"/api/wake/{wake_action:activate|status|claim|shown}", wake.handle),
                    web.get("/api/models", models), web.get("/api/personality", personality_endpoint),
                    web.post("/api/personality", personality_endpoint), web.post("/api/prepare-voice", prepare_voice),
                    web.post("/api/transcribe", transcribe), web.post("/api/speak", speak),
                    web.post("/api/speak-batch", speak), web.post("/api/prepare-speech", prepare_speech),
                    web.post("/api/{verb:message|approve|cancel|remember|forget|new-chat|switch-chat|select-model|select-personality|media-select|media-event|transport-result}", action),
                    web.get("/drafts/{name}", file), web.get("/media/{media_id}", moss.media.stream),
                    web.get("/conversation", index), web.get("/conversation/runtime-log", runtime_stream),
                    web.get("/conversation-assets/moss-view.js", model_view),
                    web.static("/conversation-assets", ROOT / "static/conversation-assets"),
                    web.static("/static", ROOT / "static")])
    install_brain_iot_routes(
        app,
        moss,
        stopped,
        device_status=device_status,
        prepare_window_image=prepare_window_image,
        recognize_capture=recognize_capture,
        image_max_json=IMAGE_MAX_JSON,
        window_image_provider=WINDOW_IMAGE_PROVIDER,
        read_json_dict=_read_json_dict,
        lan_host_list=lan_host_list,
        port=port,
        tls_port=tls_port,
    )
    install_online_music_routes(app, moss)
    if fui_public is not None:
        from console_settings import install_console_settings
        from fui_routes import install_fui
        install_fui(app, fui_public)
        install_console_settings(app, moss)
    return app


def _ensure_lan_tls_cert(data: Path, lan_hosts: list[str]) -> tuple[Path, Path]:
    """Generate or reuse a 365-day self-signed X.509 v3 SAN cert for localhost + LAN IPv4s."""
    import ipaddress
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    tls_dir = data / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tls_dir / "moss-lan.crt"
    key_path = tls_dir / "moss-lan.key"
    san_meta = tls_dir / "moss-lan.san.json"
    desired_ips = sorted({"127.0.0.1", *(h for h in lan_hosts if h and h != "0.0.0.0")})

    if cert_path.is_file() and key_path.is_file() and san_meta.is_file():
        try:
            meta = json.loads(san_meta.read_text(encoding="utf-8"))
            cert_obj = x509.load_pem_x509_certificate(cert_path.read_bytes())
            exp = getattr(cert_obj, "not_valid_after_utc", None) or cert_obj.not_valid_after.replace(tzinfo=timezone.utc)
            if meta.get("ips") == desired_ips and exp > datetime.now(timezone.utc) + timedelta(days=30):
                return cert_path, key_path
        except Exception:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cn = lan_hosts[0] if lan_hosts else "127.0.0.1"
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MOSS 550W Local Console"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])
    san_entries = [x509.DNSName("localhost")]
    for ip_str in desired_ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
        except ValueError:
            san_entries.append(x509.DNSName(ip_str))

    now_utc = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_utc - timedelta(minutes=5))
        .not_valid_after(now_utc + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    san_meta.write_text(json.dumps({"ips": desired_ips, "created_at": now_utc.isoformat()}, indent=2), encoding="utf-8")
    return cert_path, key_path


async def run(args):
    data = args.data.resolve()
    data.mkdir(exist_ok=True, parents=True)
    lan_hosts = [h.strip() for h in (args.lan_host or "").split(",") if h.strip() and h.strip() not in {"127.0.0.1", "localhost"}]
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY"), "127.0.0.1", "localhost", *lan_hosts]))
    local_port(args.port)
    stopped = asyncio.Event()
    async with AsyncExitStack() as stack:
        stack.callback(MCP_DATA.reset, MCP_DATA.set(data))
        lock = stack.enter_context((data / "owner.lock").open("a+b"))
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        local_engine = "external" if args.model_url else "standby"
        local_lock = asyncio.Lock()
        async def start_local():
            nonlocal local_engine
            async with local_lock:
                if local_engine == "standby":
                    _, _, local_engine = await stack.enter_async_context(engine(
                        args.engine_port, data, backend=args.qwen_backend, vulkan_device=args.vulkan_device,
                        allow_cpu=args.allow_cpu))

        if args.model_url:
            if urlsplit(args.model_url).hostname not in {"127.0.0.1", "localhost"}:
                raise ValueError("测试模型必须在本机")
            base, model = args.model_url.rstrip("/"), args.model
        else:
            base, model = f"http://127.0.0.1:{args.engine_port}/v1", "moss-chat-qwen35-4b"
        moss = Moss(data, base, model, write_delay=args.write_delay, memory_url=args.memory_url,
                    prefer_cloud=not args.model_url, start_local=start_local,
                    draft_model_reply=args.draft_model_reply)
        stack.push_async_callback(moss.close)
        token = secrets.token_urlsafe(32)
        public = ROOT / "fui/public"
        isolated_fui = os.environ.get("MOSS_ISOLATED_FUI") == "1"
        new_frontend = public if (data == ROOT / "data" or isolated_fui) and (public / "index.html").is_file() else None
        tls_port = int(args.tls_port or 0)
        if tls_port:
            local_port(tls_port)
        app = application(moss, lambda: local_engine, data, args.port, token, stopped,
                          qwen_backend=args.qwen_backend, vulkan_device=args.vulkan_device, allow_cpu=args.allow_cpu,
                          default_mic_warm_mode="activity", default_session_warm="0" if isolated_fui else "1",
                          fui_public=new_frontend,
                          speech_standby_seconds=0 if isolated_fui else (90 if os.environ.get("MOSS_SPEECH_STANDBY", "1") == "1" else 0),
                          lan_hosts=lan_hosts, tls_port=tls_port)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        bound_lan = []
        bound_tls = []
        ssl_ctx = None
        if tls_port:
            import ssl
            cert_path, key_path = _ensure_lan_tls_cert(data, lan_hosts)
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(str(cert_path), str(key_path))
            await web.TCPSite(runner, "127.0.0.1", tls_port, ssl_context=ssl_ctx).start()
        for h in lan_hosts:
            if h == "0.0.0.0":
                continue
            try:
                await web.TCPSite(runner, h, args.port).start()
                bound_lan.append(h)
            except OSError as exc:
                print(f"WARN: LAN HTTP bind failed on {h}:{args.port}: {exc}", flush=True)
            if ssl_ctx is not None:
                try:
                    await web.TCPSite(runner, h, tls_port, ssl_context=ssl_ctx).start()
                    bound_tls.append(h)
                except OSError as exc:
                    print(f"WARN: LAN HTTPS bind failed on {h}:{tls_port}: {exc}", flush=True)
        if not isolated_fui:
            moss.timers.enable_windows(data, args.port, startup=args)
            if moss.timers.windows:
                moss.timers.windows.save_startup()
        owner = data / "instance.json"
        temp = data / "instance.tmp"
        temp.write_text(json.dumps({
            "port": args.port, "tls_port": tls_port, "pid": os.getpid(),
            "token": token, "lan_hosts": bound_lan, "tls_hosts": bound_tls,
        }), encoding="utf-8")
        temp.replace(owner)
        stack.callback(owner.unlink, missing_ok=True)
        await moss.sync_reminders()
        lan_note = ("  LAN: " + ", ".join(f"http://{h}:{args.port}/" for h in bound_lan)) if bound_lan else ""
        tls_note = ("  HTTPS: " + ", ".join(f"https://{h}:{tls_port}/" for h in bound_tls)) if bound_tls else ""
        print(f"MOSS http://127.0.0.1:{args.port}/{lan_note}{tls_note}", flush=True)
        await stopped.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18221)
    parser.add_argument("--lan-host", default=os.environ.get("MOSS_LAN_HOST", ""),
                        help="逗号分隔的局域网监听 IPv4（例如 192.168.1.100），与 127.0.0.1 并行监听")
    parser.add_argument("--tls-port", type=int, default=int(os.environ.get("MOSS_TLS_PORT", "0") or 0),
                        help="可选局域网 HTTPS 端口（例如 18443），自动签发本地 SAN 证书以解锁麦克风/摄像头")
    parser.add_argument("--engine-port", type=int, default=18222)
    parser.add_argument("--qwen-backend", choices=("auto", "cuda", "vulkan", "cpu"), default="auto")
    parser.add_argument("--vulkan-device", help="以 llama-server --list-devices 输出的 Vulkan<N> 指定显卡")
    parser.add_argument("--allow-cpu", action="store_true", help="允许自动模式在显卡均不可用时使用 CPU")
    parser.add_argument("--model-url")
    parser.add_argument("--memory-url")
    parser.add_argument("--model", default="moss-chat-qwen35-4b")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--write-delay", type=float, default=0)
    parser.add_argument("--draft-model-reply", action="store_true",
                        help="草稿保存后调用模型生成答复，用于对比回执耗时")
    args = parser.parse_args()
    if args.vulkan_device and (args.qwen_backend not in {"auto", "vulkan"}
                              or not re.fullmatch(r"Vulkan[0-9]+", args.vulkan_device)):
        parser.error("--vulkan-device 需要使用 Vulkan<N>，并配合 auto 或 vulkan 后端。")
    if args.allow_cpu and args.qwen_backend != "auto":
        parser.error("--allow-cpu 仅用于自动选择后端。")
    asyncio.run(run(args))
