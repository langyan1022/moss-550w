import argparse
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import numpy as np
import sherpa_onnx
import soundfile as sf


ROOT = Path(__file__).resolve().parent
sys.path.append(str(ROOT / "runs" / "praat-verse-assessment"))
import parselmouth


def characters(text):
    return "".join(re.findall(r"[\u3400-\u9fff]", text))


def token_positions(expected, result):
    tokens = [(c, round(float(t), 3)) for word, t in zip(result.tokens, result.timestamps, strict=True)
              for c in characters(word)]
    heard = "".join(c for c, _ in tokens)
    anchors = [None] * len(expected)
    changes = []
    for operation, i, j, a, b in SequenceMatcher(None, expected, heard, autojunk=False).get_opcodes():
        if operation != "equal":
            changes.append({"operation": operation, "expected": expected[i:j], "asr": heard[a:b],
                            "first_recognized_anchor_s": tokens[a][1] if a < len(tokens) else None})
        if operation == "equal" or (operation == "replace" and j-i == b-a):
            for k in range(j-i):
                anchors[i+k] = {"s": tokens[a+k][1], "asr": tokens[a+k][0], "match": operation == "equal"}
    return heard, anchors, changes


def acoustic_edges(samples, rate):
    hop = round(rate * .01)
    frames = samples[:len(samples)//hop*hop].reshape(-1, hop)
    db = 10*np.log10(np.maximum(1e-12, np.mean(frames.astype("float64")**2, axis=1)))
    gate = max(-55, float(np.percentile(db, 90)) - 32)
    active = np.flatnonzero(db >= gate)
    if not len(active):
        raise ValueError("没有可测的有效声音")
    edges = np.flatnonzero(np.diff(np.r_[False, db < gate, False]))
    gaps = [(a*.01, b*.01) for a, b in zip(edges[::2], edges[1::2]) if b-a >= 6 and a > 0 and b < len(db)]
    return float(active[0])*.01, (float(active[-1])+1)*.01, gaps, gate


def boundary(left, right, gaps):
    if left is None or right is None:
        return None
    a, b = left["s"], right["s"]
    candidates = [(start,end) for start,end in gaps
                  if a < (start+end)/2 < b and start >= a-.06 and end <= b+.06]
    if candidates:
        begin, end = max(candidates, key=lambda gap: gap[1]-gap[0])
        return {"end": round(begin,3), "next": round(end,3), "quiet_ms": round(1000*(end-begin)),
                "method":"ctc_energy", "anchors_matched":left["match"] and right["match"]}
    middle = round((a+b)/2,3)
    return {"end":middle, "next":middle, "quiet_ms":None, "method":"ctc_midpoint",
            "anchors_matched":left["match"] and right["match"]}


def assess(path, annotated, recognizer):
    parts = [[characters(part) for part in line.split("/") if characters(part)]
             for line in re.split(r"[，,。！？!?；;\n]+", annotated) if characters(line)]
    expected = "".join("".join(line) for line in parts)
    samples, rate = sf.read(path, dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, mono)
    recognizer.decode_stream(stream)
    heard, anchors, changes = token_positions(expected, stream.result)
    first, last, gaps, gate = acoustic_edges(mono, rate)
    pitch = parselmouth.Sound(mono, sampling_frequency=rate).to_pitch_ac(
        time_step=.01, pitch_floor=55, pitch_ceiling=420, voicing_threshold=.5)
    times, hz = pitch.xs(), pitch.selected_array["frequency"]
    narrow = parselmouth.Sound(mono, sampling_frequency=rate).to_pitch_ac(
        time_step=.01, pitch_floor=55, pitch_ceiling=300, voicing_threshold=.5)
    narrow_times, narrow_hz = narrow.xs(), narrow.selected_array["frequency"]
    edges = {0: {"next": first}, len(anchors): {"end": last}}
    def measure(start, end):
        for i in (start, end):
            if i not in edges:
                edges[i] = boundary(anchors[i-1], anchors[i], gaps)
        left, right = edges[start] or {}, edges[end] or {}
        t0, t1 = left.get("next"), right.get("end")
        row = {"text":expected[start:end], "from_s":t0, "to_s":t1,
               "next_boundary":right if end < len(anchors) else None,
               "matched_characters":sum(bool(a and a["match"]) for a in anchors[start:end])}
        if t0 is not None and t1 is not None and t1 > t0:
            voiced = hz[(times >= t0) & (times < t1) & (hz > 0)]
            row.update(duration_ms=round(1000*(t1-t0)), voiced_frames=len(voiced),
                       f0_hz_p10_p50_p90=[round(float(v),1) for v in np.percentile(voiced,[10,50,90])]
                       if len(voiced) >= 5 else None)
            narrow_voiced = narrow_hz[(narrow_times >= t0) & (narrow_times < t1) & (narrow_hz > 0)]
            if len(narrow_voiced) >= 5 and len(voiced) >= 5:
                row["f0_300hz_ceiling_median"] = round(float(np.median(narrow_voiced)), 1)
                row["f0_ceiling_sensitive"] = bool(abs(12*np.log2(np.median(voiced)/np.median(narrow_voiced))) > 2)
        return row

    cursor = 0
    lines = []
    for line in parts:
        end = cursor + sum(len(beat) for beat in line)
        row = measure(cursor, end)
        row["beats"] = []
        for beat in line:
            row["beats"].append(measure(cursor,cursor+len(beat)))
            cursor += len(beat)
        lines.append(row)
    return {"file":str(path.resolve()), "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "duration_s":round(len(mono)/rate,3), "trailing_quiet_ms":round(1000*(len(mono)/rate-last)),
            "quiet_gate_dbfs":round(gate,1), "asr":heard, "changes":changes,
            "tokens":[{"char":c, "anchor":a} for c,a in zip(expected, anchors, strict=True)], "lines":lines}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True, help="使用标点划分半句，可用 / 标出每顿")
    parser.add_argument("--a", required=True, type=Path)
    parser.add_argument("--b", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not characters(args.text):
        parser.error("需要提供朗诵原文")
    model = Path(os.getenv("VOICESTUDIO_ROOT", "E:/VoiceStudio")) / "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(model/"model.int8.onnx"),
        tokens=str(model/"tokens.txt"), language="zh", use_itn=False, provider="cpu", num_threads=2)
    a, b = [assess(wav, args.text, recognizer) for wav in (args.a, args.b)]
    comparisons = []
    for ra, rb in zip(a["lines"], b["lines"], strict=True):
        comparison = {"text":ra["text"]}
        for field in ("duration_ms", "f0_hz_p10_p50_p90"):
            va, vb = ra.get(field), rb.get(field)
            if va is not None and vb is not None:
                if isinstance(va,list):
                    stable = ra.get("f0_ceiling_sensitive") is False and rb.get("f0_ceiling_sensitive") is False
                    comparison["delta_f0_median_hz"] = round(vb[1]-va[1], 1) if stable else None
                else:
                    comparison["delta_duration_ms"] = vb-va
        pa, pb = ((r.get("next_boundary") or {}).get("quiet_ms") for r in (ra,rb))
        comparison["delta_pause_ms"] = pb-pa if pa is not None and pb is not None else None
        comparisons.append(comparison)
    result = {"method":"SenseVoice int8 CPU 逐字 CTC 锚点约 60 ms，波形能量以 10 ms 测量。连续低于门限 60 ms 才报告停顿；其他边界使用相邻识别锚点的中点。"
              "Praat AC 每 10 ms 估计基频，仅统计有声帧。汉字识别差异需要复听，基频和时长仅比较同一段文字。",
              "input":args.text, "praat":parselmouth.PRAAT_VERSION, "asr":sherpa_onnx.__version__,
              "a":a, "b":b, "b_minus_a":comparisons}
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output, comparisons, flush=True)


if __name__ == "__main__":
    main()
