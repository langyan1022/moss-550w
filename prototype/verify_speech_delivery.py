import asyncio
import audioop
from datetime import datetime
import hashlib
import io
import json
import wave

import aiohttp
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import send


def inspect_audio(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.getnframes()
        pcm = wav.readframes(frames)
    assert frames > rate // 3 and width == 2 and audioop.rms(pcm, width) > 50
    if channels == 2:
        pcm = audioop.tomono(pcm, width, .5, .5)
    samples, _ = audioop.ratecv(pcm, width, 1, rate, 16000, None)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as dest:
        dest.setparams((1, 2, 16000, 0, "NONE", "NONE"))
        dest.writeframes(samples)
    return round(frames / rate, 3), stream.getvalue()


async def check():
    folder = ROOT / "runs" / datetime.now().strftime("speech-delivery-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18568)
    report = {"passed":False, "audio":{}, "browser":[]}
    await server.start()
    sentence = "我在，你慢慢说。"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=200)) as http:
            async def synth(text, category, cached=False):
                async with http.post(server.url + "/api/speak", json={"text":text, "category":category, "prefer_cached":cached}) as response:
                    data = await response.read()
                    assert response.status == 200, data[:250]
                    assert response.headers["X-MOSS-Delivery"] == category
                    return data, response.headers.get("X-MOSS-Announcement")

            async def recognize(pcm):
                async with http.post(server.url + "/api/transcribe", data=pcm, headers={"Content-Type":"audio/wav"}) as response:
                    response.raise_for_status()
                    return (await response.json())["text"]

            for category in ("reply","warm","confirm","notice","media"):
                audio, cue = await synth(sentence, category)
                seconds, speech = inspect_audio(audio)
                digest = hashlib.sha256(audio).hexdigest()
                assert not cue and digest not in [entry["sha256"] for entry in report["audio"].values()]
                assert (await synth(sentence, category))[0] == audio
                asr = await recognize(speech) if category in {"reply","warm"} else None
                if category in {"reply","warm"}:
                    assert "我在" in asr and "慢慢" in asr, asr
                report["audio"][category] = {"seconds":seconds, "sha256":digest, "asr":asr}
                (folder / (category + ".wav")).write_bytes(audio)
                print(category, seconds, asr, flush=True)
            assert (await synth(sentence,"media",True))[1] is None
            bundled, label = await synth("一个尚未缓存的曲名。","media",True)
            assert label == "short" and bundled == (ROOT/"static/music-ack.wav").read_bytes()
            cue_seconds, cue_pcm = inspect_audio(bundled)
            assert "播放" in await recognize(cue_pcm)
            report["bundled_cue_seconds"] = cue_seconds
            cue, _ = await synth("为你播放。","media")
            seconds, speech = inspect_audio(cue)
            cue_text = await recognize(speech)
            assert "播放" in cue_text, cue_text
            report["short_cue"] = {"seconds":seconds, "asr":cue_text}
            (folder / "music-ack.wav").write_bytes(cue)
            async with http.post(server.url + "/api/speak", json={"text":sentence,"category":"unknown"}) as response:
                assert response.status == 400
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                page.set_default_timeout(180000)
                await page.add_init_script("window.voices=[]; addEventListener('moss-speech-start',e=>voices.push(e.detail))")
                await page.goto(server.url + "/?chat=1")
                checks = await page.evaluate("""async () => {
                  const {speechDelivery,expressiveText}=await import('/static/speech-delivery.js');
                  return [speechDelivery({status:'pending'}).category,
                    speechDelivery({status:'error'}).category,
                    speechDelivery({user:'请保存草稿，内容是我今天有点累',source:'memory',reply:'已记住'}).category,
                    speechDelivery({user:'我今天不累',reply:'收到。'}).category,
                    speechDelivery({user:'我今天有点累',reply:'要休息一会儿吗？'}).category,
                    expressiveText('哈哈，好呀。',speechDelivery({user:'笑一声',reply:'哈哈，好呀。'}).expression)]
                }""")
                assert checks == ["confirm","notice","reply","reply","warm","[laughter] 好呀。"], checks
                for prompt, category in [("找本地视频 没有这条短片82961","notice"),("我今天有点累，请只说一句话。","warm")]:
                    answer = await send(page,server,prompt)
                    turn = (await server.state())["turns"][-1]
                    await page.wait_for_function("id=>voices.some(v=>v.id===id)",arg=turn["id"])
                    voice = await page.evaluate("id=>voices.find(v=>v.id===id)",turn["id"])
                    assert voice["category"] == category and voice["seconds"] > 0
                    report["browser"].append({"user":prompt,"answer":answer,"speech":voice})
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(check())
