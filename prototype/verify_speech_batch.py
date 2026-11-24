import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

import aiohttp
import psutil

from browser_lab import BrowserServer, ROOT
from verify_speech_delivery import inspect_audio


SAMPLES = {"reply": ["清晨的山风很凉。", "远处传来了海浪的声音。"],
           "verse": ["白云 生远岫。", "暮雨 入寒江。"], "warm": ["歇一会儿吧。", "那我们就慢慢来。"]}
SAMPLES["verse-warm"] = SAMPLES["verse"]


def recognize(directory):
    from verify_verse_asr import compare, hear
    from voice import asr_engine
    results = []
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    try:
        for case in report["requests"]:
            if case["case"] != "two-new":
                continue
            category = case["category"]
            for index, expected in enumerate(SAMPLES[category]):
                path = directory / f"{category}-{index}.wav"
                data = path.read_bytes()
                recognized = hear(data)
                results.append({"file": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                                "expected": expected, "transcript": recognized, "differences": compare(expected, recognized)})
    finally:
        asr_engine.close()
    target = directory / "cpu-asr.json"
    target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)


async def verify(cpu_only, cached_only):
    directory = ROOT / "runs" / datetime.now().strftime("speech-batch-http-%Y%m%d-%H%M%S-%f")
    temporary = directory / "temporary"
    temporary.mkdir(parents=True)
    server = BrowserServer(directory, 18974, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "requests": [], "single_requests": [], "invalid_statuses": []}
    overrides = {"TMP": str(temporary), "TEMP": str(temporary)}
    if cpu_only:
        overrides["CUDA_VISIBLE_DEVICES"] = "-1"
    old_env = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        try:
            await server.start()
        finally:
            for key, original in old_env.items():
                if original is not None:
                    os.environ[key] = original
                else:
                    os.environ.pop(key)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=320)) as client:
            async def batch(label, texts, category, modes):
                start = perf_counter()
                async with client.post(server.url + "/api/speak-batch", json={"texts": texts, "category": category}) as response:
                    assert response.status == 200, (response.status, await response.text())
                    assert response.content_type == "multipart/form-data" and response.headers["X-MOSS-Delivery"] == category
                    mode = response.headers["X-MOSS-Synthesis"]
                    assert mode in modes, mode
                    capable = response.headers["X-MOSS-Batch-Capable"]
                    assert capable in ("0", "1") and (not cpu_only or capable == "0")
                    parts = aiohttp.MultipartReader.from_response(response)
                    data = []
                    for index in range(2):
                        part = await parts.next()
                        assert (part.name, part.filename, part.headers["Content-Type"]) == (str(index), f"{index}.wav", "audio/wav")
                        data.append(bytes(await part.read()))
                    assert await parts.next() is None
                elapsed = round((perf_counter() - start) * 1000)
                durations = [inspect_audio(wav)[0] for wav in data]
                report["requests"].append({"case": label, "category": category, "mode": mode, "batch_capable": capable, "http_ms": elapsed,
                                           "seconds": durations, "sha256": [hashlib.sha256(w).hexdigest() for w in data]})
                return data

            async def single(text, category, expected_capable=None, *, prefer_cached=False):
                body = {"text": text, "category": category, "prefer_cached": prefer_cached}
                async with client.post(server.url + "/api/speak", json=body) as response:
                    assert response.status == 200 and response.content_type == "audio/wav", (response.status, await response.text())
                    capable = response.headers["X-MOSS-Batch-Capable"]
                    assert capable in ("0", "1") and (expected_capable is None or capable == expected_capable)
                    report["single_requests"].append({"category": category, "batch_capable": capable})
                    return await response.read()

            for texts, category in [(["一"], "reply"), (["一", "二", "三"], "reply"), ("一二", "reply"),
                                    ([None, "二"], "reply"), ([" ", "二"], "reply"), (["一" * 161, "二"], "reply"),
                                    (["一" * 120, "二" * 121], "verse"), (["一", "二"], "media"), (["一", "二"], [])]:
                status, _ = await server.post("/api/speak-batch", {"texts": texts, "category": category})
                report["invalid_statuses"].append(status)
                assert status == 400, status

            recorded = ["好，先到这里。", "不客气。"]
            assert await single(recorded[0], "reply", "0") == (ROOT / "voice/lines/done.wav").read_bytes()
            assert await single("媒体开场短音。", "media", "0", prefer_cached=True) == (ROOT / "static/music-ack.wav").read_bytes()
            wavs = await batch("recorded", recorded, "reply", {"cached"})
            assert wavs == [(ROOT / "voice/lines" / name).read_bytes() for name in ("done.wav", "welcome.wav")]
            assert report["requests"][-1]["batch_capable"] == "0"
            if cached_only:
                report["passed"] = True
                return
            for category in (["verse-warm"] if cpu_only else SAMPLES):
                texts = SAMPLES[category]
                wavs = await batch("two-new", texts, category, {"serial"} if cpu_only else {"batch", "serial"})
                assert wavs[0] != wavs[1]
                for index, wav in enumerate(wavs):
                    assert wav == await single(texts[index], category, report["requests"][-1]["batch_capable"])
                    (directory / f"{category}-{index}.wav").write_bytes(wav)
                reversed_wavs = await batch("reverse-replay", texts[::-1], category, {"cached"})
                assert reversed_wavs == wavs[::-1]
                if cpu_only:
                    report.update(passed=True, cuda_visible_devices="-1")
                    return
            mixed = await batch("second-recorded", ["门前的桂花开了。", recorded[0]], "reply", {"single"})
            assert mixed[1] == (ROOT / "voice/lines/done.wav").read_bytes()
            duplicate = await batch("duplicate", ["这是一段新的文字。"] * 2, "reply", {"single"})
            assert duplicate[0] == duplicate[1]

            process = psutil.Process(server.pid)
            def speaker_pids():
                children = process.children(recursive=True)
                return sorted(p.pid for p in children if any(Path(a).name == "managed_worker.py" for a in p.cmdline()))

            before = speaker_pids()
            assert before
            cancel_texts = ["傍晚，河堤边的杨柳迎风摇动。远处传来一阵笛声，散步的人们停下脚步听了一会儿。" * 2,
                            "灯光映在水面上，像一串金色的珠子。天色渐暗，最后一艘渡船向岸边缓缓靠近。" * 2]
            request = asyncio.create_task(client.post(server.url + "/api/speak-batch", json={"texts": cancel_texts, "category": "reply"}))
            async with asyncio.timeout(8):
                while not list(temporary.glob("moss-agent-speech-*")):
                    assert not request.done()
                    await asyncio.sleep(.01)
            active, = temporary.glob("moss-agent-speech-*")
            await asyncio.sleep(.32)
            assert active.exists() and not request.done()
            start = perf_counter()
            request.cancel()
            try:
                await request
            except asyncio.CancelledError:
                pass
            recovery = asyncio.create_task(batch("after-cancel", ["天亮了。", "窗前落着两只鸟。"], "reply", {"batch", "serial"}))
            signalled = False
            async with asyncio.timeout(8):
                while active.exists():
                    signalled |= (active / "request.cancel").exists()
                    await asyncio.sleep(.003)
            cancel_ms = round((perf_counter() - start) * 1000)
            assert signalled, "未观察到 worker 收到取消文件"
            recovered = await recovery
            assert recovered[0] != recovered[1]
            assert before == speaker_pids()
            report["cancellation"] = {"signalled": signalled, "directory_released_ms": cancel_ms,
                                      "same_worker_pids": before, "subsequent_batch_completed": True}
            report["passed"] = True
    finally:
        await server.stop()
        (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(directory, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--cpu-only", action="store_true")
    modes.add_argument("--cached-only", action="store_true")
    modes.add_argument("--recognize", type=Path)
    args = parser.parse_args()
    if args.recognize:
        recognize(args.recognize)
    else:
        asyncio.run(verify(args.cpu_only, args.cached_only))
