import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

from cognitive_utils import attach_kill_on_close_job, close_job_handle
from voice_asr_correct import correct_asr_transcript


UNUSED_WORKER_SECONDS = 45


class LocalVoice:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.proc = None
        self._job_handle = None
        self.used = False
        self.closing = False
        self.expiry = None
        self.root = Path(__file__).resolve().parent
        self.python = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio")) / "project/.venv/Scripts/python.exe"

    @staticmethod
    def _terminate_proc(proc):
        if not proc or proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass

    @asynccontextmanager
    async def _worker(self):
        async with self.lock:
            try:
                async with asyncio.timeout(100):
                    if self.closing:
                        raise ValueError("本机语音输入服务正在关闭")
                    if self.proc is None or self.proc.returncode is not None:
                        self.used = False
                        if not self.python.is_file():
                            raise ValueError("找不到本机 VoiceStudio 语音环境")
                        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
                               "OPENBLAS_NUM_THREADS": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                        proc = await asyncio.create_subprocess_exec(
                            str(self.python), "-B", "-u", str(self.root / "voice/asr_engine.py"), "--worker",
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, cwd=str(self.root),
                            stderr=subprocess.DEVNULL if sys.stderr is None else None,
                            env=env, creationflags=subprocess.CREATE_NO_WINDOW)
                        if self.closing:
                            self._terminate_proc(proc)
                            try:
                                await asyncio.wait_for(proc.wait(), 2)
                            except (TimeoutError, asyncio.CancelledError):
                                try:
                                    proc.kill()
                                except OSError:
                                    pass
                            raise ValueError("本机语音输入服务正在关闭")
                        self.proc = proc
                        prev_job, self._job_handle = self._job_handle, attach_kill_on_close_job(getattr(proc, "pid", None))
                        close_job_handle(prev_job)
                        if (await self.proc.stdout.readline()).strip() != b"ready":
                            raise ValueError("本机语音模型未就绪")
                    yield self.proc
            except (TimeoutError, OSError, ValueError, asyncio.CancelledError):
                await self._stop()
                raise

    async def prepare(self):
        async with self._worker() as proc:
            self._schedule_unused_expiry(proc)

    def _schedule_unused_expiry(self, proc):
        if not self.used:
            self._cancel_expiry()
            self.expiry = asyncio.create_task(self._expire_unused(proc))

    def _cancel_expiry(self):
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None

    async def _expire_unused(self, proc):
        # 保留完整的三十秒录音时间，给尚未提交过录音的进程留出回收窗口。
        await asyncio.sleep(UNUSED_WORKER_SECONDS)
        async with self.lock:
            if self.proc is proc and not self.used:
                self.expiry = None
                await self._stop()

    async def transcribe(self, binary, *, preview=False):
        try:
            with wave.open(io.BytesIO(binary), "rb") as wav:
                if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, 16000, 2, "NONE"):
                    raise ValueError("请使用 16 kHz 单声道 PCM16 WAV")
                count = wav.getnframes()
                if not 2000 <= count <= 480000:
                    raise ValueError("录音长度应在八分之一秒至三十秒之间")
                pcm = wav.readframes(count)
        except (EOFError, wave.Error) as exc:
            raise ValueError("无效的 WAV 录音") from exc
        if len(pcm) != 2 * count:
            raise ValueError("录音内容不完整")
        if not any(pcm):
            return ""
        if preview and (self.lock.locked() or not self.proc or self.proc.returncode is not None):
            return None
        async with self._worker() as proc:
            if preview:
                self._schedule_unused_expiry(proc)
            else:
                self.used = True
                self._cancel_expiry()
            proc.stdin.write(base64.b64encode(pcm) + b"\n")
            await proc.stdin.drain()
            line = await proc.stdout.readline()
            if not line:
                raise ValueError("本机语音进程意外退出")
            value = json.loads(line)
            if not isinstance(value, str):
                raise ValueError("本机语音模型返回了无效结果")
            if not any(char.isalnum() for char in value):
                return ""
            return value if preview else correct_asr_transcript(value)

    async def stream_chunk(self, session_id: str, pcm_bytes: bytes) -> dict:
        """Append 16 kHz PCM16 frame bytes into an incremental recognizer session buffer."""
        if not isinstance(pcm_bytes, (bytes, bytearray)) or not pcm_bytes:
            return {"buffered_bytes": 0}
        if len(pcm_bytes) > 640_000:
            raise ValueError("音频分片过大")
        if len(pcm_bytes) % 2:
            pcm_bytes = bytes(pcm_bytes[:-1])
        async with self._worker() as proc:
            self.used = True
            self._cancel_expiry()
            payload = {"op": "stream_chunk", "session": session_id[:64], "b64": base64.b64encode(pcm_bytes).decode("ascii")}
            proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            await proc.stdin.drain()
            line = await proc.stdout.readline()
            if not line:
                raise ValueError("本机语音进程意外退出")
            return json.loads(line)

    async def stream_commit(self, session_id: str, *, preview: bool = False) -> str:
        """Decode accumulated streamed PCM16 chunks for a session and reset buffer."""
        if preview and (self.closing or not self.proc or self.proc.returncode is not None):
            return ""
        async with self._worker() as proc:
            if not preview:
                self.used = True
                self._cancel_expiry()
            payload = {"op": "stream_commit", "session": session_id[:64], "preview": bool(preview)}
            proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            await proc.stdin.drain()
            line = await proc.stdout.readline()
            if not line:
                raise ValueError("本机语音进程意外退出")
            value = json.loads(line)
            if not isinstance(value, str) or not any(char.isalnum() for char in value):
                return ""
            return value if preview else correct_asr_transcript(value)

    async def stream_reset(self, session_id: str) -> None:
        if self.closing or not self.proc or self.proc.returncode is not None:
            return
        async with self.lock:
            if self.closing or not self.proc or self.proc.returncode is not None:
                return
            payload = {"op": "stream_reset", "session": session_id[:64]}
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
                await self.proc.stdin.drain()
                line = await asyncio.wait_for(self.proc.stdout.readline(), 2.0)
                if not line:
                    await self._stop()
            except (TimeoutError, OSError):
                await self._stop()

    async def close(self):
        self.closing = True
        self._cancel_expiry()
        # Wake any in-flight _worker() blocked on stdout/startup immediately instead of waiting 100s on lock.
        self._terminate_proc(self.proc)
        async with self.lock:
            await self._stop()

    async def _stop(self):
        self._cancel_expiry()
        proc, self.proc = self.proc, None
        job_handle, self._job_handle = self._job_handle, None
        try:
            if proc and proc.returncode is None:
                try:
                    if proc.stdin and not proc.stdin.is_closing():
                        proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.wait(), 3)
                    except TimeoutError:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), 2)
                        except TimeoutError:
                            proc.kill()
                            await proc.wait()
                except asyncio.CancelledError:
                    self._terminate_proc(proc)
                    raise
                finally:
                    if proc.returncode is None:
                        self._terminate_proc(proc)
        finally:
            close_job_handle(job_handle)
