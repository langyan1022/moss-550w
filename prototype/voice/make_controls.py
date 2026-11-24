import math
from pathlib import Path
import sys

import numpy as np
from scipy.signal import resample_poly
import soundfile as sf


folder = Path(sys.argv[1])
path = Path(r"G:\音乐\1-流行\西海情歌 - 刀郎.flac")
sf.write(folder / "silence.wav", np.zeros(48000, dtype="float32"), 16000, subtype="PCM_16")
with sf.SoundFile(path) as stream:
    for offset in (0, 30, 120):
        stream.seek(offset * stream.samplerate)
        audio = stream.read(3 * stream.samplerate, always_2d=True, dtype="float32").mean(axis=1)
        factor = math.gcd(stream.samplerate, 16000)
        samples = resample_poly(audio, 16000 // factor, stream.samplerate // factor)
        sf.write(folder / f"music-{offset}.wav", samples, 16000, subtype="PCM_16")
