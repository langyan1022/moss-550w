import asyncio
from collections import OrderedDict
from contextlib import suppress
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from time import perf_counter
from uuid import uuid4
import wave

import win32job

from device_status import GIB, memory_sample
from voice.pronunciation import apply_pronunciations
from voice.spoken_numbers import spoken_numbers

SPEECH_RATES = {"reply": 1.15, "media": 1.18, "confirm": 1.10, "notice": 1.08,
                "warm": 1.05, "verse": 0.90, "verse-warm": 0.90}
SPEECH_STEPS = 12
LINE_CACHE_LIMIT = 36
STATUS_REPLY_CACHE_LIMIT = 32
STATUS_VOICE_FILES = ("reference.wav", "pronunciation.py", "pronunciations.json", "spoken_numbers.py",
                      "managed_worker.py", "clone_omnivoice.py", "omni_room.py", "weight_snapshot.py",
                      "gpu-weight-registration.json", "stock_voice_prompt.py",
                      "reference-seeds/registration.json", "reference-seeds/neutral-cpu.pt")
_DIGIT = r"[一二三四五六七八九]"
_LEVEL = rf"百分之(?:零|一百|{_DIGIT}?十{_DIGIT}?|{_DIGIT})"
_SUPPLY = r"(?:已接电(?:，正在充电)?|未接电|使用电池)"
_BATTERY = rf"电量 ?{_LEVEL}(?:，{_SUPPLY}|（{_SUPPLY}）)。"
_VOLUME = rf"系统音量(?:为 ?)?{_LEVEL}(?:，(?:当前)?[已未]静音)?。"
STATUS_REPLY = re.compile(rf"(?:{_BATTERY}(?:\n?{_VOLUME})?|{_VOLUME}(?:\n?{_BATTERY})?)")
MIC_WARM_SECONDS = 25
MIC_ACTIVITY_MIN_AVAILABLE = 12 * GIB
MIC_CUDA_MIN_FREE_MIB = 3800
SESSION_WARM_SECONDS = 300
SESSION_MIN_AVAILABLE = 12 * GIB
SESSION_CUDA_MIN_FREE_MIB = 3800
SESSION_MEMORY_CHECK_SECONDS = 5
IMPORT_PREPARE_SECONDS = 12
STANDBY_MEMORY_CHECK_SECONDS = 5
STANDBY_MIN_AVAILABLE = 8 * GIB
STANDBY_IMPORT_RESERVE = 3 * GIB
RECORDED_LINES = {
    ("warm", "我在。"): "here.wav",
    ("reply", "你好。"): "hello.wav",
    ("reply", "早上好。"): "good-morning.wav",
    ("reply", "早安。"): "morning-peace.wav",
    ("reply", "中午好。"): "good-noon.wav",
    ("reply", "下午好。"): "good-afternoon.wav",
    ("reply", "晚上好。"): "good-evening.wav",
    ("reply", "晚安。"): "good-night.wav",
    ("reply", "不客气。"): "welcome.wav",
    ("reply", "好，先到这里。"): "done.wav",
    ("reply", "[sigh]"): "sigh.wav",
    ("reply", "[laughter]"): "laughter.wav",
    ("confirm", "草稿准备好了，现在保存吗？"): "confirm-draft.wav",
    ("reply", "已保存。"): "saved-draft.wav",
    ("notice", "这次没能完成。详情在对话里。"): "action-incomplete.wav",
    ("reply", "文件已归类，位置在对话里。"): "file-organized.wav",
    ("reply", "便签在对话里。"): "note-shown.wav",
    ("reply", "好的。"): "acknowledge.wav",
}

class SpeechInputError(ValueError):
    pass


class MossSpeech:
    def __init__(self, data, *, idle_seconds=150, standby_seconds=0):
        self.root = Path(__file__).resolve().parent / "voice"
        self.studio = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio"))
        self.cache = data / "voice-prompts"
        self.announcements = data / "voice-announcements"
        self.status_replies = data / "voice-status-replies"
        self._status_fingerprint = None
        self._status_worker_stamp = None
        self.lock = asyncio.Lock()
        self.worker = None
        self.worker_group = None
        self._batch_capable = False
        self.closing = False
        self.release = None
        self.idle_seconds = idle_seconds
        self.standby_seconds = standby_seconds
        self.lines = OrderedDict()
        self._used_worker = False
        self._mic_key = self._mic_timer = self._mic_task = None
        self._closed_mics = OrderedDict()
        self._session_key = self._session_task = self._session_eligible = self._session_cancel = None
        self._session_ttl = SESSION_WARM_SECONDS
        self._session_warm_ready = False
        self._closed_sessions = OrderedDict()

    @property
    def batch_capable(self):
        return bool(self._batch_capable and self.worker and self.worker.returncode is None and not self.closing)

    @property
    def session_warm_ready(self):
        return bool(self._session_warm_ready and self.worker and self.worker.returncode is None and not self.closing)

    async def prepare_imports(self):
        if self.closing or self.lock.locked():
            return
        async with self.lock:
            if self.worker and self.worker.returncode is None:
                return
            memory = memory_sample()
            if min(memory.available, memory.available_pagefile) < 8 * GIB:
                return
            try:
                # 启动同一个进程并让它等待读取第一行；此处不发送加载权重或生成语音的命令。
                await self._start()
            except (OSError, asyncio.CancelledError):
                await self._stop()
                raise
            if not self.closing:
                self._release_after(IMPORT_PREPARE_SECONDS)

    async def prepare_microphone(self, key, *, eligible, prime=True):
        if self.session_warm_ready and eligible():
            return True
        if (self._mic_task and not self._mic_task.done()):
            if self._mic_key == key and not self.closing and eligible():
                try:
                    return await asyncio.shield(self._mic_task)
                except asyncio.CancelledError:
                    if not self._mic_task or not self._mic_task.cancelled():
                        raise
            return False
        if (self.closing or self.lock.locked() or self._used_worker or self._mic_key
                or self._session_key or key in self._closed_mics or not eligible()):
            return False
        memory = memory_sample()
        minimum = 8 * GIB if prime else MIC_ACTIVITY_MIN_AVAILABLE
        if min(memory.available, memory.available_pagefile) < minimum:
            return False
        self._mic_key = key
        loop = asyncio.get_running_loop()
        self._mic_timer = loop.call_later(
            MIC_WARM_SECONDS, lambda: loop.create_task(self.release_microphone(key)))
        task = self._mic_task = loop.create_task(self._load_for_microphone(key, eligible, prime))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            return False

    async def _load_for_microphone(self, key, eligible, prime):
        async with self.lock:
            if self._mic_key != key or self.closing:
                return False
            ready = False
            imports_only = False
            try:
                if not eligible():
                    return False
                self._cancel_release()
                await self._start()
                with tempfile.TemporaryDirectory(prefix="moss-mic-warm-") as folder:
                    request = {"request_id": uuid4().hex, "device": "cuda", "steps": SPEECH_STEPS,
                               "prepare": True, "prime": prime, "cuda_min_free_mib": MIC_CUDA_MIN_FREE_MIB}
                    result = await self._worker_reply(request, Path(folder) / "cancel", lambda: False,
                                                      timeout=MIC_WARM_SECONDS)
                ready = result["code"] == 0 and result["info"].get("device") == "cuda"
                imports_only = not prime and result.get("preload_refused") is True
                if ready:
                    self._batch_capable = result["info"].get("batch_capable") is True
            except (RuntimeError, OSError, TimeoutError):
                pass
            finally:
                if not ready:
                    if self._mic_key == key:
                        self._forget_microphone()
                    if imports_only and not self.closing:
                        self._release_after(IMPORT_PREPARE_SECONDS)
                    else:
                        await self._stop()
            return ready

    def _forget_microphone(self):
        self._mic_key = None
        if self._mic_timer:
            self._mic_timer.cancel()
            self._mic_timer = None

    async def release_microphone(self, key=None):
        key = self._mic_key if key is None else key
        if key is None:
            return
        # A page can close while its start request is still arriving on another connection.
        self._closed_mics[key] = None
        self._closed_mics.move_to_end(key)
        if len(self._closed_mics) > 32:
            self._closed_mics.popitem(last=False)
        if key != self._mic_key:
            return
        self._forget_microphone()
        task, self._mic_task = self._mic_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.lock:
            if not self._mic_key and not self._used_worker and not self._session_key:
                await self._stop()

    def _forget_session(self):
        self._session_key = None
        self._session_eligible = None
        self._session_warm_ready = False

    async def prepare_session(self, key, *, eligible, ttl_seconds=SESSION_WARM_SECONDS, prime=True):
        if self.closing or key in self._closed_sessions or not eligible():
            return False
        if (self._session_key == key and self._session_warm_ready
                and self.worker and self.worker.returncode is None):
            self._session_eligible = eligible
            self._session_ttl = ttl_seconds
            if not self.lock.locked():
                self._watch_session(asyncio.get_running_loop().time() + ttl_seconds)
            return True
        if (self._session_key is None and self._mic_key is None and not self.lock.locked()
                and self._used_worker and self._batch_capable
                and self.worker and self.worker.returncode is None):
            self._session_key = key
            self._session_eligible = eligible
            self._session_ttl = ttl_seconds
            self._session_warm_ready = True
            self._watch_session(asyncio.get_running_loop().time() + ttl_seconds)
            return True
        if self._session_task and not self._session_task.done():
            if self._session_key == key:
                try:
                    return await asyncio.shield(self._session_task)
                except asyncio.CancelledError:
                    if not self._session_task or not self._session_task.cancelled():
                        raise
            return False
        if (self.lock.locked() or self._mic_key
                or (self._session_key is not None and self._session_key != key)):
            return False
        memory = memory_sample()
        if min(memory.available, memory.available_pagefile) < SESSION_MIN_AVAILABLE:
            return False
        self._session_key = key
        self._session_eligible = eligible
        self._session_ttl = ttl_seconds
        self._session_warm_ready = False
        loop = asyncio.get_running_loop()
        task = self._session_task = loop.create_task(
            self._load_for_session(key, eligible, ttl_seconds, prime))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            return False

    async def _load_for_session(self, key, eligible, ttl_seconds, prime):
        async with self.lock:
            if self._session_key != key or self.closing or not eligible():
                if self._session_key == key:
                    self._forget_session()
                return False
            ready = False
            try:
                self._cancel_release()
                await self._start()
                with tempfile.TemporaryDirectory(prefix="moss-session-warm-") as folder:
                    cancel = Path(folder) / "cancel"
                    self._session_cancel = cancel
                    request = {"request_id": uuid4().hex, "cancel_file": str(cancel),
                               "device": "cuda", "steps": SPEECH_STEPS, "prepare": True,
                               "prime": prime, "cuda_min_free_mib": SESSION_CUDA_MIN_FREE_MIB}
                    result = await self._worker_reply(
                        request, cancel, lambda: self._session_key != key or self.closing, timeout=45)
                ready = (result["code"] == 0 and result["info"].get("device") == "cuda"
                         and self._session_key == key and not self.closing and eligible())
                if ready:
                    self._batch_capable = result["info"].get("batch_capable") is True
                    self._session_warm_ready = True
                    self._watch_session(asyncio.get_running_loop().time() + ttl_seconds)
            except (RuntimeError, OSError, TimeoutError, ConnectionAbortedError):
                pass
            finally:
                self._session_cancel = None
                if self._session_task is asyncio.current_task():
                    self._session_task = None
                if not ready:
                    if self._session_key == key:
                        self._forget_session()
                    await self._stop()
            return ready

    async def release_session(self, key=None, *, force_stop=False):
        target = self._session_key if key is None else key
        if target is not None:
            self._closed_sessions[target] = None
            self._closed_sessions.move_to_end(target)
            if len(self._closed_sessions) > 32:
                self._closed_sessions.popitem(last=False)
            if target != self._session_key and not force_stop:
                return
            if target == self._session_key:
                self._forget_session()
        elif not force_stop:
            return
        if self._session_cancel:
            with suppress(OSError):
                self._session_cancel.touch()
        task, self._session_task = self._session_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.lock:
            if force_stop or (not self._session_key and not self._mic_key and not self._used_worker):
                self._cancel_release()
                await self._stop()

    def _cancel_release(self):
        if self.release:
            self.release.cancel()
            self.release = None

    def _release_after(self, seconds, *, standby=False, standby_deadline=None, session_deadline=None):
        self._cancel_release()
        loop = asyncio.get_running_loop()
        timer = loop.call_later(seconds, lambda: loop.create_task(
            self._release_if_due(timer, standby, standby_deadline, session_deadline)))
        self.release = timer

    @staticmethod
    def _standby_has_memory(minimum=STANDBY_MIN_AVAILABLE):
        try:
            memory = memory_sample()
        except OSError:
            return False
        return min(memory.available, memory.available_pagefile) >= minimum

    def _watch_standby(self, deadline):
        delay = max(0, min(STANDBY_MEMORY_CHECK_SECONDS, deadline - asyncio.get_running_loop().time()))
        self._release_after(delay, standby_deadline=deadline)

    def _watch_session(self, deadline):
        delay = max(0, min(SESSION_MEMORY_CHECK_SECONDS, deadline - asyncio.get_running_loop().time()))
        self._release_after(delay, session_deadline=deadline)

    async def _release_if_due(self, timer, standby, standby_deadline, session_deadline=None):
        async with self.lock:
            if self.release is not timer or self.closing:
                return
            self.release = None
            if session_deadline is not None:
                if (self._session_key and self._session_warm_ready
                        and asyncio.get_running_loop().time() < session_deadline
                        and self.worker and self.worker.returncode is None
                        and self._standby_has_memory()
                        and (self._session_eligible is None or self._session_eligible())):
                    self._watch_session(session_deadline)
                    return
                self._forget_session()
            if (standby_deadline is not None and asyncio.get_running_loop().time() < standby_deadline
                    and self.worker and self.worker.returncode is None and self._standby_has_memory()):
                self._watch_standby(standby_deadline)
                return
            await self._stop()
            if self.closing or not standby or not self.standby_seconds:
                return
            if not self._standby_has_memory(STANDBY_MIN_AVAILABLE + STANDBY_IMPORT_RESERVE):
                return
            try:
                # 先结束用过 CUDA 的进程，再让新进程只导入模块等待读取请求。
                await self._start()
            except OSError:
                await self._stop()
                raise
            if not self.closing:
                self._watch_standby(asyncio.get_running_loop().time() + self.standby_seconds)

    async def _start(self):
        if self.closing:
            raise ConnectionAbortedError()
        if self.worker and self.worker.returncode is None:
            return
        self._batch_capable = False
        models = self.studio / "models/models--k2-fsa--OmniVoice"
        self._status_worker_stamp = self._status_voice_stamp()
        snapshot = (models / "refs/main").read_text(encoding="utf-8").strip()
        if self.worker_group:
            self.worker_group.Close()
        name = "MossSpeech-" + uuid4().hex
        self.worker_group = win32job.CreateJobObject(None, name)
        limits = win32job.QueryInformationJobObject(self.worker_group, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.worker_group, win32job.JobObjectExtendedLimitInformation, limits)
        self.worker = await asyncio.create_subprocess_exec(
            str(self.studio / "project/.venv/Scripts/python.exe"), "-B", "-u", str(self.root / "managed_worker.py"),
            "--worker", "--model", str(models / "snapshots" / snapshot), "--reference", str(self.root / "reference.wav"),
            "--transcript", "感谢您十七年来的辛苦付出，恭喜您，明天可以离港回家了。",
            "--prompt-cache", str(self.cache), cwd=self.root, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL if sys.stderr is None else None,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "MOSS_SPEECH_JOB": name, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                 "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        if self.closing:
            await self._stop()
            raise ConnectionAbortedError()

    def _announcement_path(self, text, room_amount=100):
        models = self.studio / "models/models--k2-fsa--OmniVoice/refs/main"
        room_version = f"steps{SPEECH_STEPS}-room1" if room_amount == 100 else f"steps{SPEECH_STEPS}-room-percent{room_amount}"
        version = [models.read_text(encoding="utf-8").strip(), text, room_version, str(SPEECH_RATES["media"])]
        for name in ("clone_omnivoice.py", "reference.wav", "pronunciations.json", "omni_room.py"):
            version.append(str((self.root / name).stat().st_mtime_ns))
        digest = hashlib.sha256("\0".join(version).encode("utf-8")).hexdigest()
        return self.announcements / (digest + ".wav")

    def _status_voice_stamp(self):
        files = (self.studio / "models/models--k2-fsa--OmniVoice/refs/main",
                 *(self.root / name for name in STATUS_VOICE_FILES))
        if registration := os.environ.get("MOSS_OMNIVOICE_REGISTRATION"):
            files += (Path(registration),)
        neutral_cuda = self.root / "reference-seeds/neutral-cuda.pt"
        if neutral_cuda.is_file():
            files += (neutral_cuda,)
        try:
            return tuple((path, info.st_size, info.st_mtime_ns) for path in files for info in (path.stat(),))
        except OSError:
            return None

    def _status_cache_path(self, key):
        category, text, room_amount = key
        if category != "reply" or not STATUS_REPLY.fullmatch(text):
            return None, None
        stamp = self._status_voice_stamp()
        if stamp is None:
            return None, None
        if not self._status_fingerprint or self._status_fingerprint[0] != stamp:
            fingerprint = hashlib.sha256()
            try:
                for path, _, _ in stamp:
                    fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
            except OSError:
                return None, None
            if self._status_voice_stamp() != stamp:
                return None, None
            self._status_fingerprint = stamp, fingerprint.hexdigest()
        material = ("status-reply-v1", self._status_fingerprint[1], category, text, str(room_amount),
                    str(SPEECH_STEPS), str(SPEECH_RATES[category]),
                    os.environ.get("MOSS_OMNIVOICE_REGISTERED_REFERENCE", "1"))
        digest = hashlib.sha256("\0".join(material).encode("utf-8")).hexdigest()
        return self.status_replies / (digest + ".wav"), stamp

    @staticmethod
    def _valid_status_wave(data):
        if len(data) <= 44 or len(data) != int.from_bytes(data[4:8], "little") + 8:
            return False
        try:
            with wave.open(BytesIO(data), "rb") as audio:
                frames = audio.getnframes()
                return frames > 0 and len(audio.readframes(frames)) == frames * audio.getnchannels() * audio.getsampwidth()
        except (EOFError, wave.Error):
            return False

    def _spoken(self, text, category):
        if not isinstance(text, str) or not text.strip() or len(text) > 220:
            raise SpeechInputError("每次播报需要 1 至 220 个字符")
        text = apply_pronunciations(text.strip(), model="omnivoice")
        return text if category == "media" else spoken_numbers(text)

    def _cached(self, key):
        status_path, _ = self._status_cache_path(key)
        cache_key = status_path if status_path else key
        if cache_key in self.lines:
            self.lines.move_to_end(cache_key)
            return self.lines[cache_key]
        category, text, room_amount = key
        if name := RECORDED_LINES.get((category, text)):
            try:
                return (self.root / "lines" / name).read_bytes()
            except FileNotFoundError:
                pass
        if category == "media":
            path = self._announcement_path(text, room_amount)
            if path.is_file():
                return path.read_bytes()
        if status_path:
            with suppress(OSError):
                data = status_path.read_bytes()
                if self._valid_status_wave(data):
                    self._remember(cache_key, data)
                    return data
        return None

    def _store(self, key, data, *, persist_status=True):
        category, text, room_amount = key
        status_path, voice_stamp = self._status_cache_path(key)
        if category == "media":
            path = self._announcement_path(text, room_amount)
            self.announcements.mkdir(exist_ok=True)
            staging = path.with_suffix(".partial")
            staging.write_bytes(data)
            staging.replace(path)
            saved = sorted(self.announcements.glob("*.wav"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            for old in saved[48:]:
                old.unlink(missing_ok=True)
        if status_path:
            # A running engine with an older voice snapshot cannot create entries for a newer one.
            if self.worker is not None and self._status_worker_stamp != voice_stamp:
                return
            if not self._valid_status_wave(data):
                return
        if status_path and persist_status:
            with suppress(OSError):
                self.status_replies.mkdir(exist_ok=True)
                staging = self.status_replies / (uuid4().hex + ".partial")
                try:
                    staging.write_bytes(data)
                    staging.replace(status_path)
                finally:
                    staging.unlink(missing_ok=True)
                saved = sorted(self.status_replies.glob("*.wav"), key=lambda p:p.stat().st_mtime_ns, reverse=True)
                for old in saved[STATUS_REPLY_CACHE_LIMIT:]:
                    old.unlink(missing_ok=True)
        self._remember(status_path if status_path else key, data)

    def _remember(self, key, data):
        self.lines[key] = data
        self.lines.move_to_end(key)
        while len(self.lines) > LINE_CACHE_LIMIT:
            self.lines.popitem(last=False)

    async def render(self, text, disconnected, category="reply", cached_only=False, *, timings=None, room_amount=100):
        started = perf_counter()
        data, mode = await self._render_many([self._spoken(text, category)], category, disconnected, cached_only, timings,
                                             room_amount=room_amount)
        if timings is not None:
            elapsed = (perf_counter() - started) * 1000
            timings["tts"] = elapsed
            if mode == "cached":
                timings["cache"] = elapsed
        return data[0]

    async def render_batch(self, texts, disconnected, category="reply", *, room_amount=100):
        if category not in ("reply", "warm", "verse", "verse-warm"):
            raise SpeechInputError("成组播报支持 reply、warm、verse 和 verse-warm")
        if (not isinstance(texts, list) or len(texts) != 2
                or any(not isinstance(t, str) or not t.strip() or len(t) > 160 for t in texts)
                or sum(map(len, texts)) > 240):
            raise SpeechInputError("需要两个非空文本段，每段最多 160 个字符，合计最多 240 个字符")
        return await self._render_many([self._spoken(t, category) for t in texts], category, disconnected, False,
                                       room_amount=room_amount)

    async def _render_many(self, texts, category, disconnected, cached_only, timings=None, *, room_amount=100):
        if type(room_amount) is not int or not 0 <= room_amount <= 150:
            raise SpeechInputError("空间感需要为 0 到 150 之间的整数百分比")
        if self.closing or disconnected():
            raise ConnectionAbortedError()
        keys = [(category, text, room_amount) for text in texts]
        ready = {key: self._cached(key) for key in dict.fromkeys(keys)}
        if cached_only or all(data is not None for data in ready.values()):
            return [ready[key] for key in keys], "cached"
        if self._mic_key:
            # From now on the real synthesis request, rather than the mic, owns the worker.
            self._forget_microphone()
            self._release_after(MIC_WARM_SECONDS)
        if self._session_cancel:
            # Yield background kernel priming immediately so the real reply takes the warm CUDA weights.
            with suppress(OSError):
                self._session_cancel.touch()
        queued = perf_counter()
        async with self.lock:
            if timings is not None:
                timings["queue"] = (perf_counter() - queued) * 1000
            if self.closing or disconnected():
                raise ConnectionAbortedError()
            for key, data in ready.items():
                if data is None:
                    ready[key] = self._cached(key)
            missing = [key for key, data in ready.items() if data is None]
            if not missing:
                return [ready[key] for key in keys], "cached"
            self._cancel_release()
            generated = False
            try:
                spawning = not self.worker or self.worker.returncode is not None
                started = perf_counter()
                await self._start()
                if timings is not None and spawning:
                    timings["spawn"] = (perf_counter() - started) * 1000
                with tempfile.TemporaryDirectory(prefix="moss-agent-speech-") as folder:
                    paths = [Path(folder) / f"{i}.wav" for i in range(len(missing))]
                    cancel = Path(folder) / "request.cancel"
                    req = {"request_id": uuid4().hex, "cancel_file": str(cancel),
                           "device": "auto", "steps": SPEECH_STEPS, "room": room_amount > 0, "room_amount": room_amount,
                           "speed": SPEECH_RATES[category],
                           "warm_voice": category in ("warm", "verse-warm"),
                           "flow_smooth": category not in ("verse", "verse-warm")}
                    if len(missing) == 1:
                        req.update(text=missing[0][1], output=str(paths[0]))
                    else:
                        req["lines"] = [[key[1], str(path)] for key, path in zip(missing, paths)]
                    started = perf_counter()
                    reply = await self._worker_reply(req, cancel, disconnected, timeout=150 * len(missing))
                    self._batch_capable = reply["code"] == 0 and reply["info"].get("batch_capable") is True
                    if reply["code"] == 130 or disconnected():
                        raise ConnectionAbortedError()
                    if reply["code"] != 0:
                        raise RuntimeError("本机 MOSS 音色合成失败，请重新播放")
                    if timings is not None:
                        info = reply["info"]
                        timings["worker"] = (perf_counter() - started) * 1000
                        # imports 是该进程历史导入时间。预导入的部分可能早于当前请求。
                        for source, name in (("imports_s", "imports"), ("load_seconds", "load"),
                                             ("generation_seconds", "generate")):
                            timings[name] = info[source] * 1000
                        timings["mix"] = info.get("room_s", 0) * 1000
                        for source, name in (("room_import_s", "room_import"), ("room_wait_s", "room_wait")):
                            if source in info:
                                timings[name] = info[source] * 1000
                        for source, name in (("load_checkpoint_and_codec_s", "checkpoint"),
                                             ("place_generator_s", "place_model"), ("reference_prompt_s", "reference"),
                                             ("place_decoder_s", "place_codec")):
                            if source in info["load_durations_s"]:
                                timings[name] = info["load_durations_s"][source] * 1000
                    output = [path.read_bytes() for path in paths]
                    if any(len(data) < 45 or data[:4] != b"RIFF" or data[8:12] != b"WAVE" for data in output):
                        raise RuntimeError("MOSS 语音进程没有输出有效音频")
                    for key, data in zip(missing, output):
                        ready[key] = data
                        self._store(key, data, persist_status=reply["info"].get("fallback") is None)
                    generated = True
                    self._used_worker = True
                    return [ready[key] for key in keys], reply["info"]["mode"]
            finally:
                if not self.closing:
                    if (self._session_key and self.worker and self.worker.returncode is None
                            and self._batch_capable
                            and (self._session_eligible is None or self._session_eligible())):
                        self._session_warm_ready = True
                        self._watch_session(asyncio.get_running_loop().time() + max(self.idle_seconds, self._session_ttl))
                    else:
                        self._release_after(self.idle_seconds, standby=generated)

    async def _worker_reply(self, req, cancel, disconnected, timeout):
        try:
            self.worker.stdin.write(json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n")
            await self.worker.stdin.drain()
            async with asyncio.timeout(timeout):
                reading = asyncio.create_task(self.worker.stdout.readline())
                try:
                    while not reading.done():
                        await asyncio.wait({reading}, timeout=.2)
                        if disconnected() and not cancel.exists():
                            cancel.touch()
                    line = reading.result()
                finally:
                    if not reading.done():
                        reading.cancel()
                        await asyncio.gather(reading, return_exceptions=True)
            if self.closing:
                raise ConnectionAbortedError()
            if not line:
                await self._stop()
                raise RuntimeError("本机 MOSS 语音进程已退出")
            try:
                reply = json.loads(line)
                if reply.get("request_id") != req["request_id"]:
                    raise ValueError("请求编号不一致")
            except (ValueError, AttributeError) as exc:
                await self._stop()
                raise RuntimeError("语音进程返回的请求编号无效") from exc
            return reply
        except (TimeoutError, asyncio.CancelledError, asyncio.LimitOverrunError, OSError):
            # 确保进程退出后再释放其输出目录，防止超时后的旧回执被后续请求消费。
            await self._stop()
            raise

    async def _stop(self):
        worker, self.worker = self.worker, None
        self._batch_capable = False
        self._used_worker = False
        self._session_warm_ready = False
        if self.worker_group:
            self.worker_group.Close()
            self.worker_group = None
        if worker and worker.returncode is None:
            with suppress(OSError):
                worker.stdin.close()
            try:
                await asyncio.wait_for(worker.wait(), 3)
            except TimeoutError:
                with suppress(OSError):
                    worker.terminate()
                try:
                    await asyncio.wait_for(worker.wait(), 2)
                except TimeoutError:
                    with suppress(OSError):
                        worker.kill()
                    await worker.wait()
            except asyncio.CancelledError:
                with suppress(OSError):
                    worker.terminate()
                raise
            finally:
                if worker.returncode is None:
                    with suppress(OSError):
                        worker.terminate()

    async def idle(self):
        async with self.lock:
            await self._stop()

    async def close(self):
        self.closing = True
        self._cancel_release()
        self._forget_microphone()
        self._forget_session()
        if self._session_cancel:
            with suppress(OSError):
                self._session_cancel.touch()
        for pending in (self._mic_task, self._session_task):
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        self._mic_task = self._session_task = None
        # 先结束属于本服务的推理作业，正在等待回复的 HTTP 请求才会释放锁。
        if self.worker_group:
            self.worker_group.Close()
            self.worker_group = None
        await self.idle()
