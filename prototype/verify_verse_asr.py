import argparse
from difflib import SequenceMatcher
import hashlib
import io
import json
from pathlib import Path
import re
import wave

import numpy as np

from verify_speech_delivery import inspect_audio
from voice import asr_engine


def compare(expected, transcript):
    source, heard = ("".join(re.findall(r"[\u3400-\u9fff]", text)) for text in (expected, transcript))
    return [{"operation":op, "expected":source[i:j], "transcript":heard[k:l]}
            for op, i, j, k, l in SequenceMatcher(None, source, heard, autojunk=False).get_opcodes()
            if op != "equal"]


def hear(data):
    _, pcm = inspect_audio(data)
    return asr_engine.transcribe(pcm)


def recognize_long_sample(path, expected):
    audio = path.read_bytes()
    _, data = inspect_audio(audio)
    with wave.open(io.BytesIO(data), "rb") as source:
        rate = source.getframerate()
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    # 每次转写限 30 秒，在中间区域挑选能量最低的 100 毫秒作为切分点。
    step = rate // 10
    bounds = [0]
    while len(pcm) - bounds[-1] > 30 * rate:
        first = bounds[-1] + 13 * rate
        last = min(bounds[-1] + 24 * rate, len(pcm) - 3 * rate)
        position = min(range(first, last, step), key=lambda start: np.mean(abs(pcm[start:start+step].astype(float))))
        bounds.append(position + step // 2)
    bounds.append(len(pcm))
    chunks = []
    try:
        for start, end in zip(bounds, bounds[1:]):
            stream = io.BytesIO()
            with wave.open(stream, "wb") as dest:
                dest.setparams((1, 2, rate, end - start, "NONE", "NONE"))
                dest.writeframes(pcm[start:end].tobytes())
            chunks.append({"start_seconds": start / rate, "end_seconds": end / rate,
                           "transcript": asr_engine.transcribe(stream.getvalue())})
    finally:
        asr_engine.close()
    transcript = "".join(part["transcript"] for part in chunks)
    report = {"sha256": hashlib.sha256(audio).hexdigest(), "expected": expected, "transcript": transcript,
              "chunks": chunks, "diff": compare(expected, transcript)}
    target = path.with_name(path.stem + "-asr.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target, flush=True)


def recognize(folder):
    run = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    report = {}
    try:
        for poem, modes in run["acoustic"].items():
            for mode, audio in modes.items():
                path = folder / audio["file"]
                original = path.read_bytes()
                expected = "".join(s["caption"] for s in run["plans"][poem][mode])
                text = hear(original)
                spoken = "".join(p["spoken"] for p in run["plans"][poem][mode])
                row = {"sha256":hashlib.sha256(original).hexdigest(), "asr_input_hz":16000,
                       "expected_caption":expected, "expected_spoken":spoken, "transcript":text, "diff":compare(spoken, text)}
                requests = [r for r in run["requests"] if r["category"] == mode and r["poem"] == poem]
                if len(requests) > 1:
                    pieces = []
                    with wave.open(io.BytesIO(original), "rb") as source:
                        for req in requests:
                            segment = io.BytesIO()
                            with wave.open(segment, "wb") as sample:
                                sample.setparams(source.getparams())
                                sample.writeframes(source.readframes(req["samples"]))
                            heard = hear(segment.getvalue())
                            pieces.append({"expected_spoken":req["text"], "frames":req["samples"],
                                           "transcript":heard, "diff":compare(req["text"], heard)})
                        assert source.tell() == source.getnframes()
                    row["decoded_at_actual_http_chunk_boundaries"] = pieces
                assert original == path.read_bytes()
                report[path.name] = row
        for credit in (*run.get("credit_audio", {}).values(), *run.get("ci_audio", {}).values()):
            path = folder / credit["file"]
            data = path.read_bytes()
            assert hashlib.sha256(data).hexdigest() == credit["sha256"]
            heard = hear(data)
            report[path.name] = {"sha256":credit["sha256"], "expected_caption":credit["caption"],
                                 "expected_spoken":credit["spoken"], "transcript":heard, "diff":compare(credit["spoken"], heard)}
    finally:
        asr_engine.close()
    target = folder / "cpu-asr.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--text", type=Path, help="转写指定 WAV，用同一份原文核对")
    args = parser.parse_args()
    if args.text:
        recognize_long_sample(args.path, args.text.read_text(encoding="utf-8").strip())
    else:
        recognize(args.path)
