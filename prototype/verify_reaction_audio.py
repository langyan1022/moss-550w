import asyncio
import argparse
from array import array
from datetime import datetime
import hashlib
from io import BytesIO
import json
from math import sqrt
import os
from time import perf_counter
import wave

import aiohttp

from browser_lab import BrowserServer, ROOT


CASES = (("帮我叹口气", "唉。", "sigh"), ("轻轻笑一下", "哈哈。", "laughter"))


async def spoken(turn):
    module = (ROOT / "static/speech-delivery.js").as_uri()
    code = """const {speechDelivery,expressiveText}=await import(process.argv[1]);
const result=speechDelivery(JSON.parse(process.argv[2]));
process.stdout.write(JSON.stringify({text:expressiveText(result.text,result.expression),category:result.category}));"""
    proc = await asyncio.create_subprocess_exec("node", "--input-type=module", "-e", code, module,
                                                json.dumps(turn, ensure_ascii=False), stdout=asyncio.subprocess.PIPE)
    data, _ = await proc.communicate()
    if proc.returncode:
        raise ValueError("读取页面语气转换失败")
    return json.loads(data.decode("utf-8"))


async def main(*, expect_cache):
    folder = ROOT / "runs" / datetime.now().strftime("reaction-audio-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "model_connections": 0, "expected_cache": expect_cache, "cases": []}
    os.environ["MOSS_WINDOWS_REMINDERS"] = "0"

    async def reject(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    sink = await asyncio.start_server(reject, "127.0.0.1", 0)
    port = sink.sockets[0].getsockname()[1]
    test = BrowserServer(folder, 18972, model_url=f"http://127.0.0.1:{port}/v1")
    try:
        async with sink:
            await test.start()
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=120)) as client:
                for prompt, caption, reaction in CASES:
                    start = perf_counter()
                    status, result = await test.post("/api/message", {"text": prompt})
                    elapsed = (perf_counter() - start) * 1000
                    assert status == 200
                    turn = result["turns"][-1]
                    assert turn["source"] == "smalltalk" and turn["reply"] == caption and turn["status"] == "replied"
                    payload = await spoken(turn)
                    assert payload == {"category": "reply", "text": f"[{reaction}]"}
                    start = perf_counter()
                    async with client.post(test.url + "/api/speak", json=payload) as response:
                        assert response.status == 200 and response.headers["X-MOSS-Voice"] == "OmniVoice", await response.text()
                        data = await response.read()
                    latency = perf_counter() - start
                    with wave.open(BytesIO(data)) as wav:
                        assert wav.getsampwidth() == 2
                        rate = wav.getframerate()
                        channels = wav.getnchannels()
                        count = wav.getnframes()
                        samples = array("h")
                        samples.frombytes(wav.readframes(count))
                        peak = max(map(abs, samples), default=0) / 32768
                        rms = sqrt(sum(value * value for value in samples) / len(samples)) / 32768
                    assert count / rate >= .15 and peak > .005 and rms > .0008, (reaction, peak, rms, count, rate)
                    filename = reaction + ".wav"
                    if expect_cache:
                        assert data == (ROOT / "voice/lines" / filename).read_bytes(), "固定拟声没有使用已验收录音"
                        assert latency < 1.5, latency
                    (folder / filename).write_bytes(data)
                    report["cases"].append({"text": prompt, "caption": caption, "tag": payload["text"],
                                            "file": filename, "sha256": hashlib.sha256(data).hexdigest(),
                                            "receipt_ms": round(elapsed, 2), "wav_seconds": round(count/rate, 3),
                                            "synthesis_seconds": round(latency, 3), "channels": channels,
                                            "sample_rate": rate, "peak": round(peak, 4), "rms": round(rms, 4)})
            assert report["model_connections"] == 0
            assert report["cases"][0]["sha256"] != report["cases"][1]["sha256"]
            report["passed"] = True
    finally:
        await test.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-cache", action="store_true")
    asyncio.run(main(expect_cache=parser.parse_args().expect_cache))
