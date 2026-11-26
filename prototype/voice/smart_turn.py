"""MOSS Semantic Turn Detection via Pipecat Smart-Turn v3.2 CPU ONNX (~10-45ms, 0 MiB GPU VRAM).

Evaluates prosodic + acoustic + linguistic closure over up to the trailing 8 seconds of
16 kHz mono PCM16 audio so conversational endpointing can commit finished utterances around
~400ms instead of waiting a fixed 1.55s–2.0s silence floor, while holding open during pauses.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import threading
from time import perf_counter

import numpy as np


SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 8
MAX_SAMPLES = SAMPLE_RATE * MAX_AUDIO_SECONDS
N_FFT = 400
HOP_LENGTH = 160
N_MELS = 80
DEFAULT_THRESHOLD = 0.50

_LOCK = threading.Lock()
_SESSION = None
_MODEL_PATH: Path | None = None
_MEL_FB: np.ndarray | None = None
_HANN_WIN: np.ndarray | None = None


def _default_model_candidates() -> tuple[Path, ...]:
    env_path = os.environ.get("MOSS_SMART_TURN_MODEL", "").strip()
    studio = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio"))
    root = Path(__file__).resolve().parent.parent
    cands = []
    if env_path:
        cands.append(Path(env_path))
    cands.extend((
        studio / "models/smart-turn-v3/smart-turn-v3.2-cpu.onnx",
        studio / "models/smart-turn-v3/smart-turn-v3.1-cpu.onnx",
        root / "data/models/smart-turn-v3/smart-turn-v3.2-cpu.onnx",
    ))
    return tuple(cands)


def resolve_smart_turn_model() -> Path | None:
    for path in _default_model_candidates():
        if path.is_file() and path.stat().st_size > 100_000:
            return path
    return None


def _ensure_mel_basis() -> tuple[np.ndarray, np.ndarray]:
    global _MEL_FB, _HANN_WIN
    if _MEL_FB is not None and _HANN_WIN is not None:
        return _MEL_FB, _HANN_WIN

    min_log_hz = 1000.0
    min_log_mel = 15.0
    logstep = math.log(6.4) / 27.0

    def _hz_to_mel(hz):
        hz_arr = np.asarray(hz, dtype=np.float64)
        return np.where(
            hz_arr >= min_log_hz,
            min_log_mel + np.log(np.maximum(hz_arr, 1e-12) / min_log_hz) / logstep,
            3.0 * hz_arr / 200.0,
        )

    def _mel_to_hz(mel):
        mel_arr = np.asarray(mel, dtype=np.float64)
        return np.where(
            mel_arr >= min_log_mel,
            min_log_hz * np.exp(logstep * (mel_arr - min_log_mel)),
            200.0 * mel_arr / 3.0,
        )

    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    mels = np.linspace(_hz_to_mel(0.0), _hz_to_mel(SAMPLE_RATE / 2.0), N_MELS + 2)
    pts = _mel_to_hz(mels)
    diffs = np.diff(pts)
    ramps = pts[None, :] - freqs[:, None]
    weights = np.zeros((N_MELS, len(freqs)), dtype=np.float32)
    for i in range(N_MELS):
        lower = -ramps[:, i] / diffs[i]
        upper = ramps[:, i + 2] / diffs[i + 1]
        weights[i] = np.maximum(0.0, np.minimum(lower, upper))
    enorm = 2.0 / (pts[2:N_MELS + 2] - pts[:N_MELS])
    weights *= enorm[:, None]
    _MEL_FB = weights
    _HANN_WIN = np.hanning(N_FFT + 1)[:-1].astype(np.float32)
    return _MEL_FB, _HANN_WIN


def extract_whisper_log_mel(audio_f32: np.ndarray) -> np.ndarray:
    """Compute OpenAI Whisper / Smart-Turn v3 `(1, 80, 800)` float32 log-mel spectrogram."""
    samples = np.asarray(audio_f32, dtype=np.float32).reshape(-1)
    if len(samples) > MAX_SAMPLES:
        samples = samples[-MAX_SAMPLES:]
    elif len(samples) < MAX_SAMPLES:
        # Left-pad silence so the latest speech pause aligns cleanly at the right edge (t = 8s).
        samples = np.pad(samples, (MAX_SAMPLES - len(samples), 0))
    fb, win = _ensure_mel_basis()
    padded = np.pad(samples, N_FFT // 2, mode="reflect")
    n_frames = 1 + (len(padded) - N_FFT) // HOP_LENGTH
    frames = np.lib.stride_tricks.as_strided(
        padded,
        shape=(n_frames, N_FFT),
        strides=(padded.strides[0] * HOP_LENGTH, padded.strides[0]),
    )
    stft = np.fft.rfft(frames * win, n=N_FFT, axis=1)
    power = np.abs(stft[:, :N_FFT // 2 + 1]) ** 2
    mel = (fb @ power.T)[:, :-1]
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec[None, :, :].astype(np.float32)


def _get_session():
    global _SESSION, _MODEL_PATH
    with _LOCK:
        if _SESSION is not None:
            return _SESSION, _MODEL_PATH
        model_path = resolve_smart_turn_model()
        if model_path is None:
            return None, None
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        _SESSION = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        _MODEL_PATH = model_path
        _ensure_mel_basis()
        return _SESSION, _MODEL_PATH


def predict_turn_completion(pcm_or_samples: bytes | bytearray | np.ndarray, *, threshold: float | None = None) -> dict:
    """Predict whether the speaker has semantically completed their turn."""
    thresh = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    env_thresh = os.environ.get("MOSS_SMART_TURN_THRESHOLD")
    if threshold is None and env_thresh:
        try:
            thresh = max(0.15, min(0.90, float(env_thresh)))
        except ValueError:
            pass

    sess, model_path = _get_session()
    if sess is None:
        return {
            "available": False,
            "complete": False,
            "probability": 0.5,
            "inference_ms": 0.0,
            "model": "unavailable",
        }

    if isinstance(pcm_or_samples, (bytes, bytearray, memoryview)):
        raw = bytes(pcm_or_samples)
        if len(raw) % 2:
            raw = raw[:-1]
        if len(raw) < 1600:  # < 50ms audio
            return {
                "available": True,
                "complete": False,
                "probability": 0.0,
                "inference_ms": 0.0,
                "model": model_path.stem if model_path else "smart-turn-v3",
            }
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        samples = np.asarray(pcm_or_samples, dtype=np.float32)

    t0 = perf_counter()
    feats = extract_whisper_log_mel(samples)
    logits = sess.run(None, {"input_features": feats})[0]
    logit = float(logits[0][0])
    prob = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
    ms = round((perf_counter() - t0) * 1000.0, 2)
    return {
        "available": True,
        "complete": prob >= thresh,
        "probability": round(prob, 4),
        "inference_ms": ms,
        "model": model_path.stem if model_path else "smart-turn-v3",
    }
