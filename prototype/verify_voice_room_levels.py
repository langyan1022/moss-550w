"""Check the real room DSP and speech cache without loading a speech model."""

import argparse
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from io import BytesIO, StringIO
import json
from math import pi, sin
import os
from pathlib import Path
import struct
import subprocess
import sys
from time import perf_counter
import traceback
from types import SimpleNamespace
import wave


ROOT = Path(__file__).resolve().parent
RATE = 24000
LEVELS = (0, 50, 100, 150)


def make_dry_voice():
    frames = [round(11000 * sin(2 * pi * 219 * i / RATE) * (1 - i / 6000)) for i in range(6000)]
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return output.getvalue()


def check_worker(folder):
    from voice.omni_room import apply_room

    worker_file = ROOT / "voice/clone_omnivoice.py"
    tree = ast.parse(worker_file.read_text(encoding="utf-8"))
    loop = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "worker_loop")
    module = compile(ast.Module(body=[loop], type_ignores=[]), str(worker_file), "exec")
    dry = make_dry_voice()
    (folder / "dry.wav").write_bytes(dry)
    apply_room(folder / "dry.wav", folder / "previous-default.wav", 1)

    def run(args, _session):
        for _, output in args.line:
            Path(output).write_bytes(dry)
        return {}

    requests = []
    for case, settings in [(str(level), {"room_amount":level}) for level in LEVELS] + [
        ("legacy-true", {"room":True}), ("legacy-false", {"room":False}), ("invalid", {"room_amount":True})
    ]:
        requests.append({"request_id":case, "text":"样本", "device":"cpu", "steps":16,
                         "output":str(folder / (case + ".wav")), **settings})
    output = StringIO()
    namespace = {"os":os, "sys":SimpleNamespace(stdin=[json.dumps(row) for row in requests]), "json":json,
                 "Path":Path, "perf_counter":perf_counter, "ThreadPoolExecutor":ThreadPoolExecutor,
                 "load_room_renderer":lambda: (apply_room, 0.0), "run":run, "reply_stream":output,
                 "traceback":traceback}
    exec(module, namespace)
    namespace["worker_loop"](SimpleNamespace(), SimpleNamespace(device="cpu"))
    replies = [json.loads(row) for row in output.getvalue().splitlines()]
    assert [row["code"] for row in replies] == [0] * 6 + [1], replies
    assert (folder / "0.wav").read_bytes() == (folder / "legacy-false.wav").read_bytes() == dry
    assert (folder / "100.wav").read_bytes() == (folder / "legacy-true.wav").read_bytes() == (
        folder / "previous-default.wav").read_bytes()
    assert "room_s" not in replies[0]["info"] and "room_s" in replies[1]["info"]
    hashes = {str(level):sha256((folder / f"{level}.wav").read_bytes()).hexdigest() for level in LEVELS}
    assert len(set(hashes.values())) == len(LEVELS), hashes
    channels = {}
    for level in LEVELS:
        with wave.open(str(folder / f"{level}.wav"), "rb") as wav:
            channels[str(level)] = wav.getnchannels()
    assert list(channels.values()) == [1, 2, 2, 2], channels
    print(json.dumps({"room_hashes":hashes, "channels":channels, "default_matches":True, "dry_matches":True}))


async def check_cache(folder):
    from voice_output import MossSpeech, SPEECH_RATES, SPEECH_STEPS, SpeechInputError

    class FixtureSpeech(MossSpeech):
        def __init__(self):
            data = folder / "runtime"
            data.mkdir()
            super().__init__(data)
            self.studio = folder / "models-fixture"
            model_ref = self.studio / "models/models--k2-fsa--OmniVoice/refs/main"
            model_ref.parent.mkdir(parents=True)
            model_ref.write_text("offline-test-model", encoding="utf-8")
            self.calls = []

        async def _start(self):
            self.worker = SimpleNamespace(returncode=None)

        def _release_after(self, *_args, **_kwargs):
            pass

        async def _worker_reply(self, req, *_args, **_kwargs):
            level = req["room_amount"]
            assert req["room"] is (level > 0), req
            self.calls.append(level)
            for _, output in req.get("lines") or [(req["text"], req["output"])]:
                Path(output).write_bytes((folder / f"{level}.wav").read_bytes())
            info = {"mode":"fixture", "imports_s":0, "load_seconds":0, "generation_seconds":0,
                    "load_durations_s":{}, "batch_capable":True}
            if level:
                info["room_s"] = .02
            return {"code":0, "info":info}

    speech = FixtureSpeech()
    online = lambda: False
    sentence = "空间感测试。"
    for level in LEVELS:
        timings = {}
        rendered = await speech.render(sentence, online, timings=timings, room_amount=level)
        assert rendered == (folder / f"{level}.wav").read_bytes()
        assert timings["mix"] == (20 if level else 0), timings
        assert await speech.render(sentence, online, room_amount=level) == rendered
    assert speech.calls == list(LEVELS)
    assert await speech.render(sentence, online) == (folder / "100.wav").read_bytes()
    assert await speech.render("不客气。", online, room_amount=0) == (speech.root / "lines/welcome.wav").read_bytes()
    assert await speech.render("不客气。", online, room_amount=150) == (speech.root / "lines/welcome.wav").read_bytes()
    assert speech.calls == list(LEVELS)

    media_line = "为你播放《空间》。"
    default = await speech.render(media_line, online, category="media")
    options = {"category":"media", "room_amount":50}
    assert await speech.render(media_line, online, cached_only=True, **options) is None
    custom = await speech.render(media_line, online, **options)
    assert custom != default
    spoken_media = speech._spoken(media_line, "media")
    fingerprint = ["offline-test-model", spoken_media, f"steps{SPEECH_STEPS}-room1", str(SPEECH_RATES["media"])]
    fingerprint.extend(str((speech.root / name).stat().st_mtime_ns) for name in
                       ("clone_omnivoice.py", "reference.wav", "pronunciations.json", "omni_room.py"))
    assert speech._announcement_path(spoken_media).stem == sha256("\0".join(fingerprint).encode("utf-8")).hexdigest()
    assert speech._announcement_path(spoken_media) != speech._announcement_path(spoken_media, 50)
    speech.lines.clear()
    assert await speech.render(media_line, online, cached_only=True, **options) == custom
    assert await speech.render(media_line, online, category="media", cached_only=True) == default
    batch, _ = await speech.render_batch(["上半段。", "下半段。"], online, room_amount=150)
    assert batch == [(folder / "150.wav").read_bytes()] * 2
    for bad in (None, True, -1, 151, 100.0, "100"):
        try:
            await speech.render(sentence, online, room_amount=bad)
        except SpeechInputError:
            continue
        raise AssertionError(f"unexpected setting accepted: {bad!r}")
    assert speech.calls == [*LEVELS, 100, 50, 150], speech.calls
    return {"worker_amounts":speech.calls, "cache_by_room":True, "recorded_unchanged":True,
            "media_miss_returns_none":True, "batch_room_amount":150, "invalid_inputs_rejected":6}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        check_worker(args.worker)
        return

    folder = ROOT / "runs" / datetime.now().strftime("voice-room-levels-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    voice_python = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio")) / "project/.venv/Scripts/python.exe"
    dsp = subprocess.run([voice_python, "-X", "utf8", "-B", __file__, "--worker", folder], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "OPENBLAS_NUM_THREADS":"1", "OMP_NUM_THREADS":"1"})
    if dsp.returncode:
        raise RuntimeError(dsp.stderr)
    result = {"passed":True, "dsp":json.loads(dsp.stdout), "controller":asyncio.run(check_cache(folder))}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(folder, result["passed"])


if __name__ == "__main__":
    main()
