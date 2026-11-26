import atexit
import base64
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

import numpy as np


STUDIO = Path(os.getenv("VOICESTUDIO_ROOT", r"E:\VoiceStudio"))
SAMPLE_RATE = 16000
MAX_SECONDS = 30
MAX_WAV_BYTES = SAMPLE_RATE * MAX_SECONDS * 2 + 4096
_lock = threading.Lock()
_process = None


def _read_pcm(wav_bytes):
    if not 44 <= len(wav_bytes) <= MAX_WAV_BYTES:
        raise ValueError("请提供 30 秒以内的 WAV 录音。")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (
                SAMPLE_RATE, 1, 2, "NONE"
            ):
                raise ValueError("录音格式应为 16 kHz 单声道 PCM16 WAV。")
            frames = wav.getnframes()
            if not SAMPLE_RATE // 8 <= frames <= SAMPLE_RATE * MAX_SECONDS:
                raise ValueError("录音过短或超过 30 秒。")
            pcm = wav.readframes(frames)
    except (wave.Error, EOFError, struct.error, RuntimeError) as error:
        raise ValueError("WAV 录音格式无效。") from error
    if len(pcm) != frames * 2:
        raise ValueError("录音数据不完整。")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    # 用 20 毫秒检查短句，避免前后停顿影响对轻声说话的判断。
    blocks = samples[: len(samples) // 320 * 320].reshape(-1, 320)
    if blocks.std(axis=1).max() < 30:
        raise ValueError("没有检测到声音，请靠近麦克风再说一次。")
    return pcm


def _stop():
    global _process
    process, _process = _process, None
    if process is None:
        return
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    finally:
        if getattr(process, "stdout", None) is not None:
            process.stdout.close()


def close():
    with _lock:
        _stop()


def _ensure_recognizer():
    global _process
    if _process is None or _process.poll() is not None:
        _stop()
        _process = subprocess.Popen(
            [STUDIO / "project/.venv/Scripts/python.exe", Path(__file__).resolve(), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONUTF8": "1",
                 "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            if _process.stdout.readline().rstrip() != b"ready":
                raise RuntimeError("本机语音识别模型初始化失败，请查看服务日志。")
        except (OSError, RuntimeError):
            _stop()
            raise
    return _process


def prepare():
    with _lock:
        _ensure_recognizer()


def transcribe(wav_bytes: bytes, *, preview: bool = False) -> str | None:
    pcm = _read_pcm(wav_bytes)
    if not _lock.acquire(blocking=not preview):
        return None
    try:
        process = _ensure_recognizer()
        try:
            process.stdin.write(base64.b64encode(pcm) + b"\n")
            process.stdin.flush()
            reply = process.stdout.readline()
            if not reply:
                raise RuntimeError("本机语音识别进程已退出，请查看服务日志。")
            text = json.loads(reply.decode("utf-8"))
        except (OSError, RuntimeError, ValueError):
            _stop()
            raise
    finally:
        _lock.release()
    if not text:
        raise ValueError("没有识别到语音，请再说一次。")
    return text


def _load_recognizer(models, sdk):
    # 仅在识别进程启动时核对本地 k2-fsa 发布的中英粤日韩 int8 权重和词表。
    sense = models / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    if sense.is_dir() and hasattr(sdk.OfflineRecognizer, "from_sense_voice"):
        try:
            for name, digest in (
                ("model.int8.onnx","c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"),
                ("tokens.txt","f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"),
            ):
                with (sense / name).open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                        raise ValueError(f"{name} 校验未通过")
            recognizer = sdk.OfflineRecognizer.from_sense_voice(
                model=str(sense / "model.int8.onnx"), tokens=str(sense / "tokens.txt"),
                language="auto", use_itn=True, provider="cpu", num_threads=3,
            )
            print("ASR ready | SenseVoice Small int8 | CPU", file=sys.stderr, flush=True)
            return recognizer
        except (OSError, RuntimeError, ValueError) as error:
            print(f"ASR | SenseVoice 无法载入：{error}，切换到 Whisper。", file=sys.stderr, flush=True)

    cache = models / "models--csukuangfj--sherpa-onnx-whisper-tiny"
    model = cache / "snapshots" / (cache / "refs/main").read_text(encoding="utf-8").strip()
    recognizer = sdk.OfflineRecognizer.from_whisper(
        encoder=str(model / "tiny-encoder.int8.onnx"), decoder=str(model / "tiny-decoder.int8.onnx"),
        tokens=str(model / "tiny-tokens.txt"), language="zh", task="transcribe",
        provider="cpu", num_threads=3,
    )
    print("ASR ready | Whisper tiny int8 | CPU", file=sys.stderr, flush=True)
    return recognizer


def _spoken_text(result, clear_voice):
    text = result.text.strip()
    if not any(c.isalnum() for c in text):
        return ""
    event = result.event.strip("<|> ").casefold()
    lang = result.lang.strip("<|> ").casefold()
    if lang == "nospeech" or event in {"bgm", "sing", "applause", "laughter", "cry", "sneeze", "breath", "cough", "nospeech"}:
        return ""
    # A few uncertain frames can make the decoder emit a perfectly plausible
    # English interjection. Confirm these acoustically, including with Whisper
    # where event and language tags may be empty.
    if len(text) <= 32 and text.isascii() and 0 < len(re.findall(r"[a-zA-Z]+", text)) <= 3 and not clear_voice():
        return ""
    return text


def _vad_backend():
    if os.environ.get("MOSS_ASR_LEAF_VAD", "1") == "1":
        import asr_silero_vad

        if asr_silero_vad.available():
            return asr_silero_vad

    from faster_whisper import vad

    return vad


def _worker():
    from time import monotonic as _mono
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    import sherpa_onnx

    backend = _vad_backend()
    recognizer = _load_recognizer(STUDIO / "models", sherpa_onnx)
    vad = backend.VadOptions(threshold=0.45, min_speech_duration_ms=64, min_silence_duration_ms=160, speech_pad_ms=64)
    confirmed_vad = backend.VadOptions(threshold=0.72, min_speech_duration_ms=96, min_silence_duration_ms=96, speech_pad_ms=0)
    backend.get_vad_model()
    stream_buffers: dict[str, tuple[bytearray, float]] = {}
    max_pcm_bytes = SAMPLE_RATE * MAX_SECONDS * 2
    print(f"ASR VAD | {backend.__name__}", file=sys.stderr, flush=True)
    print("ready", file=output, flush=True)
    for line in sys.stdin.buffer:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"{"):
            try:
                cmd = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                print('""', file=output, flush=True)
                continue
            op = cmd.get("op")
            sid = str(cmd.get("session") or "default")[:64]
            now_m = _mono()
            stale = [k for k, (_, ts) in stream_buffers.items() if now_m - ts > 300.0]
            for k in stale:
                stream_buffers.pop(k, None)
            if op == "stream_reset":
                stream_buffers.pop(sid, None)
                print('""', file=output, flush=True)
                continue
            if op == "stream_chunk":
                try:
                    chunk = base64.b64decode(cmd.get("b64") or "", validate=True)
                except (ValueError, TypeError):
                    buf_prev = stream_buffers.get(sid)
                    print(json.dumps({"buffered_bytes": len(buf_prev[0]) if buf_prev else 0, "error": "invalid_audio_b64"}), file=output, flush=True)
                    continue
                if sid not in stream_buffers and len(stream_buffers) >= 16:
                    oldest_k = min(stream_buffers.items(), key=lambda kv: kv[1][1])[0]
                    stream_buffers.pop(oldest_k, None)
                buf, _ = stream_buffers.get(sid, (bytearray(), now_m))
                remain = max_pcm_bytes - len(buf)
                if remain > 0 and chunk:
                    buf.extend(chunk[:remain])
                stream_buffers[sid] = (buf, now_m)
                print(json.dumps({"buffered_bytes": len(buf)}), file=output, flush=True)
                continue
            if op == "stream_commit":
                is_preview = bool(cmd.get("preview"))
                entry = stream_buffers.get(sid) if is_preview else stream_buffers.pop(sid, None)
                pcm = bytes(entry[0]) if entry else b""
                if len(pcm) % 2:
                    pcm = pcm[:-1]
                if not pcm:
                    print('""', file=output, flush=True)
                    continue
            else:
                print('""', file=output, flush=True)
                continue
        else:
            try:
                pcm = base64.b64decode(stripped, validate=True)
            except (ValueError, TypeError):
                print('""', file=output, flush=True)
                continue
            if len(pcm) % 2:
                pcm = pcm[:-1]
        if len(pcm) < 2:
            print('""', file=output, flush=True)
            continue
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        # Silero 只判定是否有人声，保留整句原始采样供识别器处理轻声和短答。
        gain = min(6.0, 0.20 / max(np.abs(samples).max(), 0.001))
        normalized = samples * max(gain, 1.0)
        if not backend.get_speech_timestamps(normalized, vad, sampling_rate=SAMPLE_RATE):
            print('""', file=output, flush=True)
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        recognizer.decode_stream(stream)
        text = _spoken_text(stream.result, lambda: backend.get_speech_timestamps(normalized, confirmed_vad, sampling_rate=SAMPLE_RATE))
        print(json.dumps(text, ensure_ascii=False), file=output, flush=True)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
else:
    atexit.register(close)
