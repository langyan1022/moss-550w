import asyncio
from datetime import datetime
import hashlib
import json
from time import perf_counter

import aiohttp
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_speech_delivery import inspect_audio
from voice_output import MossSpeech, RECORDED_LINES


TIMING = """
window.displayed = {};
window.utterances = {};
new MutationObserver(() => {
  for (const row of document.querySelectorAll('.turn')) {
    const id = row.dataset.turnId;
    const reply = row.querySelector('[data-testid=final], [data-testid=approve]');
    if (id && reply?.getClientRects().length && !(id in displayed)) displayed[id] = performance.now();
  }
}).observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
addEventListener('moss-speech-start', e => {
  utterances[e.detail.id] = {...e.detail, started:performance.now()};
});
addEventListener('moss-speech-end', e => {
  if (utterances[e.detail.id]) utterances[e.detail.id].ended = true;
});
"""


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("recorded-lines-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18592)
    report = {"passed": False, "browser": [], "audio": []}
    await server.start()
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script(TIMING)
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                for prompt, category, spoken in (
                    ("在吗", "warm", "我在。"),
                    ("谢谢", "reply", "不客气。"),
                    ("算了", "reply", "好，先到这里。"),
                    ("保存草稿，内容是明天清点书架。", "confirm", "草稿准备好了，现在保存吗？"),
                ):
                    count = len((await server.state())["turns"])
                    await page.get_by_test_id("composer").fill(prompt)
                    await page.get_by_test_id("send").click()
                    row = page.get_by_test_id("turn").nth(count)
                    await row.get_by_test_id("approve" if category == "confirm" else "final").wait_for(timeout=90000)
                    turn_id = await row.get_attribute("data-turn-id")
                    await page.wait_for_function("id => utterances[id]?.ended && displayed[id]", arg=turn_id)
                    voice = await page.evaluate("""id => ({...utterances[id], text_to_speech_ms:
                            Math.round(utterances[id].started - displayed[id])})""", turn_id)
                    assert voice["category"] == category and voice["text"] == spoken and not voice["shortCue"], voice
                    report["browser"].append({"user": prompt, "spoken": spoken, "category": category,
                                              "text_to_speech_ms": voice["text_to_speech_ms"]})
            finally:
                await browser.close()
        assert not (server.data / "voice-prompts").exists(), "固定句触发了语音模型初始化"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=160)) as client:
            for (category, text), filename in RECORDED_LINES.items():
                start = perf_counter()
                async with client.post(server.url + "/api/speak", json={"text": text, "category": category}) as resp:
                    resp.raise_for_status()
                    audio = await resp.read()
                    assert resp.headers["X-MOSS-Delivery"] == category
                elapsed = round(1000 * (perf_counter() - start), 1)
                assert audio == (ROOT / "voice/lines" / filename).read_bytes()
                seconds, sample = inspect_audio(audio)
                asr = None
                if text not in {"[sigh]", "[laughter]"}:
                    async with client.post(server.url + "/api/transcribe", data=sample,
                                           headers={"Content-Type": "audio/wav"}) as resp:
                        resp.raise_for_status()
                        asr = (await resp.json())["text"]
                    def words(sentence):
                        return "".join(c for c in sentence if c.isalnum())
                    assert words(asr) == words(text), (text, asr)
                report["audio"].append({"text": text, "category": category, "asr": asr, "seconds": seconds,
                                        "request_ms": elapsed, "bytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest()})
        speech = MossSpeech(server.data)
        assert await speech.render("我在。", lambda: False, category="reply", cached_only=True) is None
        assert await speech.render("我在！", lambda: False, category="warm", cached_only=True) is None
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
