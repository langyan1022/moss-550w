"""Compare existing PCM clips locally without synthesizing or playing sound."""

import argparse
import asyncio
import audioop
import hashlib
import io
import json
import math
from pathlib import Path
import re
from statistics import median
import unicodedata
import wave

import numpy as np
from cn2an import an2cn

from voice.spoken_numbers import spoken_numbers
from voice_input import LocalVoice


QUIET_RMS = .004
REVIEW_PAUSE_MS = 120
METRICS = ("duration_ms", "first_audible_ms", "last_audible_ms", "longest_internal_quiet_ms",
           "peak_dbfs", "rms_dbfs", "full_scale_samples", "stereo_side_to_mid_db")


def canonical_text(text):
    text = spoken_numbers(unicodedata.normalize("NFKC", text)).casefold()
    # SenseVoice may write a spoken value as digits followed by the Chinese unit.
    text = re.sub(r"(?<![0-9.])[0-9]{1,3}(?:\.[0-9]{1,3})?(?=(?:点|摄氏度))",
                  lambda value:an2cn(value[0]), text)
    return "".join(char for char in text if char.isalnum())


def db(level):
    return round(20 * math.log10(level), 2) if level > 0 else None


def analyze_audio(data):
    with wave.open(io.BytesIO(data), "rb") as source:
        rate, channels, width = source.getframerate(), source.getnchannels(), source.getsampwidth()
        frames = source.getnframes()
        if width != 2 or channels not in {1, 2} or source.getcomptype() != "NONE" or frames == 0:
            raise ValueError("需要非空的单声道或立体声 PCM16 WAV")
        raw = source.readframes(frames)
    if len(raw) != frames * channels * width:
        raise ValueError("WAV 采样数据不完整")
    pcm = np.frombuffer(raw, dtype="<i2").reshape((frames, channels))
    audio = pcm.astype(np.float64) / 32768
    block = math.ceil(rate / 100)
    starts = np.arange(0, frames, block)
    ends = np.minimum(starts + block, frames)
    energy = np.square(audio).mean(axis=1)
    rms = np.sqrt(np.add.reduceat(energy, starts) / (ends - starts))
    audible = np.flatnonzero(rms > QUIET_RMS)

    def ms(samples):
        return round(samples * 1000 / rate, 2)

    pauses = []
    if len(audible):
        for left, right in zip(audible[:-1], audible[1:]):
            if right > left + 1:
                pauses.append([ms(ends[left]), ms(starts[right]), ms(starts[right] - ends[left])])
    values = {
        "channels": channels, "hz": rate, "frames": frames, "duration_ms": ms(frames),
        "sha256": hashlib.sha256(data).hexdigest(), "pcm_sha256": hashlib.sha256(raw).hexdigest(),
        "peak_dbfs": db(np.abs(audio).max()), "rms_dbfs": db(np.sqrt(energy.mean())),
        "full_scale_samples": int(np.count_nonzero((pcm == -32768) | (pcm == 32767))),
        "first_audible_ms": ms(starts[audible[0]]) if len(audible) else None,
        "last_audible_ms": ms(ends[audible[-1]]) if len(audible) else None,
        "longest_internal_quiet_ms": max((part[2] for part in pauses), default=0) if len(audible) else None,
        "internal_quiet_spans_ms": [part for part in pauses if part[2] >= REVIEW_PAUSE_MS],
    }
    audible_indices = np.flatnonzero(np.any(pcm != 0, axis=1))
    values["digital_zero_lead_ms"] = ms(audible_indices[0]) if len(audible_indices) else ms(frames)
    values["digital_zero_tail_ms"] = ms(frames - audible_indices[-1] - 1) if len(audible_indices) else ms(frames)
    if channels == 2:
        left, right = audio.T
        mid = np.sqrt(np.square(left + right).mean())
        side = np.sqrt(np.square(left - right).mean())
        values["stereo_side_to_mid_db"] = db(side / mid) if mid > 0 else None
    return values, raw


def asr_wav(raw, channels, rate):
    if channels == 2:
        raw = audioop.tomono(raw, 2, .5, .5)
    samples, _ = audioop.ratecv(raw, 2, 1, rate, 16000, None)
    result = io.BytesIO()
    with wave.open(result, "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", ""))
        out.writeframes(samples)
    return result.getvalue()


def source_cases(report_path, pairs):
    cases = {}
    if report_path:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        if not report.get("passed"):
            raise ValueError("合成对照尚未完成")
        for trial in report["rounds"]:
            steps = trial["steps"]
            if steps not in {12, 16}:
                raise ValueError(f"合成对照包含未预期的步数 {steps}")
            name = trial.get("name", "browser")
            request = trial.get("speech_request", {})
            expected = trial.get("text") or request.get("text")
            if not expected or len(trial.get("speech_requests", [request])) != 1:
                raise ValueError("听写对照要求每轮合成相同的一段口播")
            case = cases.setdefault(name, {"expected": expected, "trials": []})
            if case["expected"] != expected:
                raise ValueError(f"{name} 各轮播报文字不同")
            case["trials"].append((steps, report_path.parent / trial["file"], trial.get("sha256")))
    for label, expected, reference, candidate in pairs or []:
        if label in cases:
            raise ValueError(f"样本名称重复 {label}")
        cases[label] = {"expected": expected, "trials": [(16, Path(reference), None), (12, Path(candidate), None)]}
    return cases


def compare_trials(trials, recognized):
    by_steps = {str(steps): [row for row in trials if row["steps"] == steps] for steps in (16, 12)}
    if not all(by_steps.values()):
        raise ValueError("每个样本都需要 16 步和 12 步的音频")
    medians = {steps: {metric: round(median(values), 2) for metric in METRICS
                       if (values := [row[metric] for row in rows if row.get(metric) is not None])}
               for steps, rows in by_steps.items()}
    result = {"medians_by_steps": medians, "candidate_minus_baseline": {
        metric: round(medians["12"][metric] - medians["16"][metric], 2)
        for metric in medians["16"] if metric in medians["12"]},
        "within_steps_pcm_identical": {steps: len({row["pcm_sha256"] for row in rows}) == 1
                                        if len(rows) > 1 else None for steps, rows in by_steps.items()},
    }
    if recognized:
        words = {row["normalized_transcript"] for row in trials}
        result["all_trials_same_nonempty_transcript"] = len(words) == 1 and "" not in words
        result["expected_text_match_by_steps"] = {steps: all(row["matches_expected_text"] for row in rows)
                                                 for steps, rows in by_steps.items()}
        result["required_phrases_present_by_steps"] = {
            steps: all(not row["missing_required"] for row in rows) for steps, rows in by_steps.items()}
    return result


async def inspect(cases, require, measure_only, *, require_by_case=None):
    recognizer = None if measure_only else LocalVoice()
    output = {}
    try:
        for name, case in cases.items():
            expected = canonical_text(case["expected"])
            required_phrases = (require_by_case or {}).get(name, require)
            required = {text: canonical_text(text) for text in required_phrases}
            if any(not term or term not in expected for term in required.values()):
                raise ValueError(f"{name} 的预期原文不包含所指定的核验短语")
            results = []
            for steps, path, expected_hash in case["trials"]:
                audio, raw = analyze_audio(path.read_bytes())
                if expected_hash and audio["sha256"] != expected_hash:
                    raise ValueError(f"合成对照所记录的 SHA-256 不匹配 {path}")
                audio.update(file=str(path.resolve()), steps=steps)
                if recognizer:
                    transcript = await recognizer.transcribe(asr_wav(raw, audio["channels"], audio["hz"]))
                    normalized = canonical_text(transcript)
                    audio.update(transcript=transcript, normalized_transcript=normalized,
                                 matches_expected_text=bool(expected) and normalized == expected,
                                 missing_required=[phrase for phrase, term in required.items() if term not in normalized])
                results.append(audio)
            output[name] = {"expected": case["expected"], "required_phrases": required_phrases, "trials": results,
                            **compare_trials(results, recognizer is not None)}
    finally:
        if recognizer:
            await recognizer.close()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path, help="短句或浏览器步数对照的 result.json")
    parser.add_argument("--pair", nargs=4, action="append", metavar=("LABEL", "TEXT", "WAV16", "WAV12"),
                        help="指定一对 WAV，可重复使用")
    parser.add_argument("--require", action="append", default=[], help="要求在原文和听写中查找的关键短语")
    parser.add_argument("--measure-only", action="store_true", help="仅分析已保存 PCM，跳过 CPU 听写")
    parser.add_argument("--output", type=Path, help="默认写入合成回执旁的 speech-steps-quality.json")
    args = parser.parse_args()
    if not (args.report or args.pair):
        parser.error("需要合成回执或至少一组 --pair")
    if not args.report and not args.output:
        parser.error("直接提供 WAV 时请指定 --output")
    if args.measure_only and args.require:
        parser.error("关键短语核验需要 CPU 听写")
    target = args.output or args.report.with_name("speech-steps-quality.json")
    if args.report and target.resolve() == args.report.resolve():
        parser.error("分析结果不能覆盖合成回执")
    cases = source_cases(args.report, args.pair)
    if not cases or any(target.resolve() == wav.resolve() for case in cases.values() for _, wav, _ in case["trials"]):
        parser.error("请提供有效样本和独立的分析输出路径")
    report = {
        "analysis_completed": True, "synthesis_run": False, "asr_run": not args.measure_only,
        "asr_method": "本机 CPU LocalVoice，立体声等权转单声道，16 kHz" if not args.measure_only else None,
        "energy_window_ms": 10, "quiet_rms_linear": QUIET_RMS, "reported_pauses_min_ms": REVIEW_PAUSE_MS,
        "limits": ["能量间隔按固定阈值计算，人耳感知需另行核对。",
                   "自动听写供字词核对。音色、情绪、自然度与空间感需另行盲听。"],
        "cases": asyncio.run(inspect(cases, args.require, args.measure_only)),
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target.resolve(), flush=True)


if __name__ == "__main__":
    main()
