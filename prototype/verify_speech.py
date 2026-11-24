import asyncio
import audioop
from datetime import datetime
import io
import json
from time import perf_counter
import wave

import aiohttp
from browser_lab import BrowserServer, ROOT


async def main():
    out = ROOT / "runs" / datetime.now().strftime("speech-%Y%m%d-%H%M%S")
    server = BrowserServer(out, 18348)
    await server.start()
    result = {"passed": False}
    try:
        async with aiohttp.ClientSession(trust_env=False) as http:
            for attempt in ("cold", "cached"):
                started = perf_counter()
                async with http.post(server.url + "/api/speak", json={"text": "为你播放刀郎的《冲动的惩罚》。"}) as resp:
                    audio = await resp.read()
                    assert resp.status == 200, audio[:400]
                    result[attempt] = {"ms": round(1000 * (perf_counter() - started)), "voice": resp.headers["X-MOSS-Voice"]}
            with wave.open(io.BytesIO(audio), "rb") as wav:
                ch, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
                pcm = wav.readframes(frames)
            result.update(channels=ch, rate=rate, seconds=round(frames/rate, 3), rms=audioop.rms(pcm, width))
            assert frames > rate and result["rms"] > 100
            (out / "announcement.wav").write_bytes(audio)
            mono = audioop.tomono(pcm, width, .5, .5) if ch == 2 else pcm
            pcm16, _ = audioop.ratecv(mono, width, 1, rate, 16000, None)
            stream = io.BytesIO()
            with wave.open(stream, "wb") as control:
                control.setparams((1, 2, 16000, 0, "NONE", "NONE"))
                control.writeframes(pcm16)
            async with http.post(server.url + "/api/transcribe", data=stream.getvalue(),
                                 headers={"Content-Type":"audio/wav"}) as resp:
                result["asr"] = (await resp.json())["text"]
            assert "冲动" in result["asr"] and "惩罚" in result["asr"]
            result["passed"] = True
    finally:
        await server.stop()
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(out, result, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
