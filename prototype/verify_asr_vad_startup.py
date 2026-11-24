import asyncio
import audioop
from contextlib import suppress
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from unittest.mock import patch
import wave

import psutil

from voice_input import LocalVoice


ROOT = Path(__file__).resolve().parent
WAKE = ROOT / "runs/voice-wake-20261121-080850-467228"
CONTROLS = ROOT / "runs/voice-20261120-123436"
INTERRUPT = ROOT / "runs/voice-turn-taking-20261121-162000-209694"
ECHO = ROOT / "runs/voice-echo-20261121-163301-672709"
ENGLISH = ROOT.parent / "alternatives/moshi-duplex/runs/replay-fixture"
MODE_ENV = "MOSS_ASR_LEAF_VAD"
VAD_MARKERS = {"0": "ASR VAD | faster_whisper.vad", "1": "ASR VAD | asr_silero_vad"}
MODE_ORDER = ("0", "1", "1", "0")
SAMPLE_RATE = 16000
MIB = 2**20
MIN_START_MIB = 5120
MIN_RUN_MIB = 4096


def load_fixtures():
    recordings = (
        ("mandarin_command", WAKE / "command.wav", 1),
        ("mandarin_wake", WAKE / "wake.wav", 1),
        ("mandarin_inline", WAKE / "inline.wav", 1),
        ("mandarin_background", WAKE / "background.wav", 1),
        ("mandarin_low_gain", WAKE / "command.wav", .05),
        ("mandarin_very_low_gain", WAKE / "command.wav", .025),
        ("silence", CONTROLS / "silence.wav", 1),
        ("music_0", CONTROLS / "music-0.wav", 1),
        ("music_30", CONTROLS / "music-30.wav", 1),
        ("music_120", CONTROLS / "music-120.wav", 1),
        ("english_opening", ENGLISH / "opening.wav", 1),
        ("english_interruption", ENGLISH / "interrupt.wav", 1),
        ("english_backchannel", ENGLISH / "backchannel.wav", 1),
        ("mandarin_interruption", INTERRUPT / "stop.wav", 1),
        ("mandarin_backchannel", INTERRUPT / "backchannel.wav", 1),
        ("mandarin_double_backchannel", INTERRUPT / "double_backchannel.wav", 1),
        ("mandarin_with_pause", INTERRUPT / "mic-stop.wav", 1),
        ("echo_only", ECHO / "mic-echo_only.wav", 1),
        ("quiet_with_echo", ECHO / "mic-quiet_with_echo.wav", 1),
    )
    fixtures = {}
    for name, source, gain in recordings:
        with wave.open(str(source), "rb") as original:
            rate = original.getframerate()
            assert (original.getnchannels(), original.getsampwidth(), original.getcomptype()) == (1, 2, "NONE"), source
            assert original.getnframes() <= 30 * rate, source
            pcm = original.readframes(original.getnframes())
        if rate != SAMPLE_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, SAMPLE_RATE, None)
        if gain != 1:
            pcm = audioop.mul(pcm, 2, gain)
        assert 2000 <= len(pcm) // 2 <= 30 * SAMPLE_RATE, source
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as output:
                output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
                output.writeframes(pcm)
            binary = buffer.getvalue()
        fixtures[name] = {
            "source": str(source), "source_rate_hz": rate, "gain": gain,
            "duration_s": round(len(pcm) / (2 * SAMPLE_RATE), 4),
            "wav_sha256": hashlib.sha256(binary).hexdigest(),
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "binary": binary,
        }
    return fixtures


async def monitor_resources(voice, task, finished, resources):
    while not finished.is_set():
        free = psutil.virtual_memory().available // MIB
        resources["min_free_mib"] = min(resources["min_free_mib"], free)
        if free < MIN_RUN_MIB:
            resources["stopped"] = f"可用物理内存 {free} MiB"
            task.cancel()
            return
        if voice.proc is not None:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                manager = psutil.Process(voice.proc.pid)
                for child in [manager, *manager.children(recursive=True)]:
                    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        memory = child.memory_info()
                        peak = getattr(memory, "peak_wset", memory.rss) / MIB
                        resources["peak_worker_working_set_mib"] = max(
                            resources["peak_worker_working_set_mib"], round(peak, 1)
                        )
        with suppress(TimeoutError):
            await asyncio.wait_for(finished.wait(), .25)


async def measure(run_dir, index, mode, fixtures, record):
    free = psutil.virtual_memory().available // MIB
    if free < MIN_START_MIB:
        raise RuntimeError(f"未启动 ASR 对照，可用物理内存 {free} MiB")
    resources = record["resources"] = {"start_free_mib": free, "min_free_mib": free, "peak_worker_working_set_mib": 0}
    log_path = run_dir / f"{index}-vad-{mode}.stderr.log"
    record["stderr_log"] = log_path.name
    voice = LocalVoice()
    create_subprocess = asyncio.create_subprocess_exec
    finished = asyncio.Event()
    monitor = asyncio.create_task(monitor_resources(voice, asyncio.current_task(), finished, resources))
    with log_path.open("wb") as diagnostics:
        async def create_asr_worker(*args, **kwargs):
            if any(Path(str(arg)).name == "asr_engine.py" for arg in args):
                kwargs["stderr"] = diagnostics
            return await create_subprocess(*args, **kwargs)

        try:
            with patch.dict(os.environ, {MODE_ENV: mode}), patch.object(asyncio, "create_subprocess_exec", create_asr_worker):
                started = perf_counter()
                await voice.prepare()
                record["prepare_to_ready_ms"] = round((perf_counter() - started) * 1000, 1)
                worker = voice.proc
                assert worker is not None and worker.returncode is None
                record["worker_pid"] = worker.pid
                for name, fixture in fixtures.items():
                    started = perf_counter()
                    text = await voice.transcribe(fixture["binary"])
                    record["samples"][name] = {"text": text, "ms": round((perf_counter() - started) * 1000, 1)}
                    assert voice.proc is worker and worker.returncode is None, name
        finally:
            finished.set()
            await monitor
            await voice.close()
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    record["vad_backend"] = [line for line in lines if line.startswith("ASR VAD | ")]
    record["recognizer"] = [line for line in lines if line.startswith("ASR ready | ")]
    assert record["vad_backend"] == [VAD_MARKERS[mode]], record["vad_backend"]
    assert len(record["recognizer"]) == 1 and record["recognizer"][0].endswith("CPU"), record["recognizer"]


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("asr-vad-startup-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    report = {"passed": False, "device": "cpu", "cloud_requests": 0, "mode_order": MODE_ORDER, "arms": []}
    print(folder, flush=True)
    try:
        os.environ.update(CUDA_VISIBLE_DEVICES="-1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        fixtures = load_fixtures()
        report["fixtures"] = {
            key: {field: value for field, value in entry.items() if field != "binary"}
            for key, entry in fixtures.items()
        }
        for index, mode in enumerate(MODE_ORDER, 1):
            arm = {"mode": mode, "samples": {}}
            report["arms"].append(arm)
            await measure(folder, index, mode, fixtures, arm)
            print(index, arm["vad_backend"][0], arm["prepare_to_ready_ms"], arm["resources"], flush=True)

        mismatches = {}
        for name in fixtures:
            outputs = [arm["samples"][name]["text"] for arm in report["arms"]]
            if len(set(outputs)) != 1:
                mismatches[name] = outputs
        report["transcript_mismatches"] = mismatches
        assert not mismatches, mismatches
        a, b, c, d = (arm["prepare_to_ready_ms"] for arm in report["arms"])
        report["ready_baseline_median_ms"] = round(median((a, d)), 1)
        report["ready_candidate_median_ms"] = round(median((b, c)), 1)
        report["ready_save_median_ms"] = round(median((a - b, d - c)), 1)
        report["ready_save_by_pair_ms"] = [round(a - b, 1), round(d - c, 1)]
        assert report["arms"][0]["samples"]["silence"]["text"] == ""
        assert report["arms"][0]["samples"]["mandarin_command"]["text"].rstrip("。！？!?") == "你在吗"
        assert "媒体库" in report["arms"][0]["samples"]["mandarin_background"]["text"]
        assert len({tuple(arm["recognizer"]) for arm in report["arms"]}) == 1
        report["passed"] = True
    except (Exception, asyncio.CancelledError) as error:
        report["error"] = repr(error)
        raise
    finally:
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
