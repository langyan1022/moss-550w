"""Add short, centered reflections to a local OmniVoice WAV."""
import shutil
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile


def _gated_power(samples, rate):
    """K-weighted, gated power for matching mono and stereo program loudness."""
    a = 10 ** (4 / 40)
    omega = 2 * np.pi * 1500 / rate
    c = np.cos(omega)
    t = np.sqrt(2 * a) * np.sin(omega)
    b = a * np.array([a + 1 + (a - 1) * c + t, -2 * (a - 1 + (a + 1) * c),
                      a + 1 + (a - 1) * c - t])
    den = [a + 1 - (a - 1) * c + t, 2 * (a - 1 - (a + 1) * c), a + 1 - (a - 1) * c - t]
    weighted = signal.lfilter(b, den, samples, axis=0)
    # 38 Hz second-order high-pass with Q=0.5.
    k = np.tan(np.pi * 38 / rate)
    weighted = signal.lfilter([1, -2, 1], [(1+k)**2, 2*(k*k-1), (1-k)**2], weighted, axis=0)
    power = np.square(weighted).sum(axis=1)
    window = round(0.4 * rate)
    if len(power) < window:
        return float(power.mean())
    running = np.r_[0, np.cumsum(power)]
    block_start = np.arange(0, len(power) - window + 1, round(0.1 * rate))
    blocks = (running[block_start + window] - running[block_start]) / window
    audible = blocks[blocks > 10 ** ((-70 + 0.691) / 10)]
    if not audible.size:
        return float(power.mean())
    return float(audible[audible > 0.1 * audible.mean()].mean())


@lru_cache(maxsize=4)
def _reflections(rate):
    """Synthetic stereo impulse response. Silence at the input produces silence."""
    length = round(0.42 * rate)
    middle = np.zeros(length)
    side = np.zeros(length)
    for delay, gain in [(0.013, 0.19), (0.022, 0.13), (0.034, -0.10), (0.048, 0.08)]:
        middle[round(delay * rate)] = gain
    side[round(0.019 * rate)] = 0.0055
    side[round(0.037 * rate)] = -0.005

    rng = np.random.default_rng(550)
    start = round(0.031 * rate)
    t = np.arange(length - start) / rate
    envelope = np.minimum(t / 0.009, 1) * 0.001 ** (t / 0.36)
    for impulse, level in [(middle, 0.20), (side, 0.0055)]:
        diffuse = rng.standard_normal(len(t)) * envelope
        impulse[start:] += level * diffuse / np.linalg.norm(diffuse)
    band = signal.butter(2, [135, min(4600, 0.43 * rate)], fs=rate, btype="bandpass", output="sos")
    return signal.sosfilt(band, middle), signal.sosfilt(band, side)


def apply_room(source: Path, target: Path, amount: float = 1.0):
    """Render a PCM WAV. 0 keeps the source bytes, 1 is the light preset, 2 the maximum."""
    source, target = Path(source), Path(target)
    if not 0 <= amount <= 2:
        raise ValueError("amount must be between 0 and 2")
    if amount == 0:
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return

    rate, raw = wavfile.read(source)
    audio = raw.astype(np.float64)
    if raw.dtype.kind in "iu":
        scale = 2 ** (8 * raw.dtype.itemsize - 1)
        audio = (audio - (scale if raw.dtype.kind == "u" else 0)) / scale
    if audio.ndim == 1:
        audio = audio[:, None]
    if not len(audio) or audio.shape[1] not in (1, 2) or rate < 8000:
        raise ValueError("Expected a nonempty mono or stereo WAV sampled at 8 kHz or above")

    middle_ir, side_ir = _reflections(rate)
    mono = audio.mean(axis=1)
    middle = signal.oaconvolve(mono, middle_ir)
    side = signal.oaconvolve(mono, side_ir)
    stereo = amount * np.column_stack((middle + side, middle - side))
    stereo[:len(audio)] += audio
    # Compare how both files play through a stereo output, where mono is sent to both sides.
    source_power = _gated_power(audio, rate) * (2 if audio.shape[1] == 1 else 1)
    output_power = _gated_power(stereo, rate)
    if output_power > 0:
        stereo *= np.sqrt(source_power / output_power)
    peak = np.max(np.abs(stereo))
    if peak > 0.98:
        stereo *= 0.98 / peak
    wavfile.write(target, rate, np.rint(stereo * 32767).astype("<i2"))
