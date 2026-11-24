import asyncio
from datetime import datetime
import json
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_artist_request import OBSERVER
from verify_memory import send


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("media-conversation-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18746, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "cases": [], "errors": []}
    await server.start()
    try:
        assert (await server.state())["model_id"] == "local"
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.add_init_script(OBSERVER)
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.wait_for_function("Boolean(window.mossTransport)")
                selected_id, selected_title = None, None
                catalog = []
                prompts = [
                    ("我想听刘德华的歌", "first"), ("第二首", "second"), ("这首叫什么", "title"),
                    ("暂停", "pause"), ("继续", "resume"), ("不是第二首，播放第三首", "next"),
                    ("别放这首，换下一首", "next"), ("换一首", "next"), ("播放下一首", "next"),
                    ("不是第三首，别放下一首", "no_switch"),
                    ("只看看刀郎的歌，不要播放", "browse"), ("这首是谁唱的？", "artist")
                ]
                for prompt, action in prompts:
                    started = perf_counter()
                    case = {"prompt": prompt}
                    report["cases"].append(case)
                    try:
                        answer = await send(page, server, prompt)
                    except RuntimeError as error:
                        case["error"] = str(error)
                        raise
                    turn = (await server.state())["turns"][-1]
                    media = turn.get("media") or {}
                    item = media.get("selected") or {}
                    case.update(answer=answer, source=turn.get("source"), ms=round((perf_counter()-started)*1000))
                    if action == "first":
                        catalog = media["items"]
                    if action in {"first", "second", "next"} and item.get("media_id"):
                        await page.wait_for_function(
                            "id=>[...document.querySelectorAll('.media-audio')].some(a=>a.dataset.mediaId===id&&!a.paused&&a.currentTime>.1)",
                            arg=item["media_id"], timeout=15000)
                    if action == "second":
                        previous_turn = turn["id"]
                        previous_audio = page.locator(f'[data-audio-turn="{previous_turn}"]')
                        await previous_audio.evaluate("a=>a.currentTime=17")
                        await page.wait_for_function("id=>{const a=document.querySelector('[data-audio-turn=\"'+id+'\"]');return !a.seeking&&a.currentTime>=17}", arg=previous_turn)
                    players = await page.locator("audio.media-audio").evaluate_all(
                        "aa=>aa.filter(a=>a.currentTime>0).map(a=>({id:a.dataset.mediaId,paused:a.paused,time:a.currentTime}))")
                    here = next((a for a in players if a["id"] == selected_id), {})
                    if action in {"first", "second", "next"}:
                        index = next((i for i, r in enumerate(catalog) if r["media_id"] == selected_id), 0)
                        target = catalog[0 if action == "first" else 1 if action == "second" else (index + 1) % len(catalog)]
                        case["ok"] = (item.get("media_id") == target["media_id"] and any(
                            a["id"] == target["media_id"] and not a["paused"] for a in players))
                        if item:
                            selected_id, selected_title = item["media_id"], item["title"]
                    elif action == "title":
                        case["ok"] = bool(selected_title and selected_title in answer and here and not here["paused"])
                    elif action in {"pause", "resume"}:
                        case["ok"] = (turn.get("transport", {}).get("event") == ("paused" if action == "pause" else "playing")
                                      and here.get("paused") is (action == "pause"))
                    elif action == "browse":
                        case["ok"] = bool(here and not here["paused"] and not media.get("autoplay"))
                    elif action == "no_switch":
                        case["ok"] = bool(here and not here["paused"] and turn.get("media_declined")
                                          and not media.get("autoplay") and not turn.get("transport"))
                    else:
                        case["ok"] = bool("刘德华" in answer and here and not here["paused"])
                    case.update(player=players, selected=selected_title)
                    case["visible_ms"] = await page.evaluate("attempts.at(-1)?.reply_ms")
                    print(prompt, case["source"], answer[:85], case["ok"], flush=True)
                report["message_network"] = await page.evaluate("""performance.getEntriesByType('resource')
                    .filter(r => r.name.endsWith('/api/message')).map(r => ({
                    wait:Math.round(r.requestStart-r.fetchStart), server:Math.round(r.responseStart-r.requestStart),
                    download:Math.round(r.responseEnd-r.responseStart)}))""")
                assert len(report["message_network"]) == len(prompts)
                assert max(request["wait"] for request in report["message_network"]) < 5000, report["message_network"]
                paused_position = await previous_audio.evaluate("a=>a.paused && Number(a.dataset.parkedTime ?? a.currentTime)")
                assert paused_position >= 17, paused_position
                await page.locator(f'[data-turn-id="{previous_turn}"] .projection-link').click()
                await page.locator("#tool-player .player-toggle").click()
                await page.wait_for_function(
                    "id=>{const a=document.querySelector('[data-audio-turn=\"'+id+'\"]');return !a.paused&&a.currentTime>=17}",
                    arg=previous_turn, timeout=10000)
                report["historical_replay"] = {
                    "paused_at": paused_position, "resumed_at": await previous_audio.evaluate("a=>a.currentTime")}
                report["passed"] = not report["errors"] and all(case["ok"] for case in report["cases"])
            finally:
                await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)
    assert report["passed"]


if __name__ == "__main__":
    asyncio.run(main())
