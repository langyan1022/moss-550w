import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, open_browser
from verify_memory import send


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("media-browser-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18248, model_url="http://127.0.0.1:18278/v1")
    report = {"passed": False, "turns": []}
    try:
        await server.start()
        async with async_playwright() as p:
            browser = await open_browser(p)
            page = await browser.new_page()
            await page.goto(server.url + "?chat=1")
            async def ask(text):
                reply = await send(page, server, text)
                turn = (await server.state())["turns"][-1]
                assert turn["source"] == "media"
                evidence = {"text": text, "reply": reply, "online_calls": turn["media"]["online_calls"]}
                evidence["selected_id"] = (turn["media"].get("selected") or {}).get("media_id")
                report["turns"].append(evidence)
                assert evidence["online_calls"] == 0
                return turn["media"]
            await ask("列出媒体库")
            first = await ask("我想听刀郎的歌")
            item = first["selected"]
            assert item["kind"] == "audio" and "刀郎" in (item["title"] + item["artist"] + item["path"])
            audio = page.get_by_test_id("media-audio").last
            await audio.wait_for(state="attached")
            await page.wait_for_function("""()=>[...document.querySelectorAll('.media-audio')].some(
                audio => !audio.paused && !audio.muted && audio.currentTime>.2 && audio.duration>1)""")
            played = await audio.evaluate("""audio=>({position:audio.currentTime,duration:audio.duration,
                                                      ready_state:audio.readyState,muted:audio.muted})""")
            report["browser_audio"] = played
            link = server.url + "/media/" + item["media_id"]
            r = await page.request.get(link, headers={"Range": "bytes=0-95"})
            prefix = await r.body()
            with Path(item["path"]).open("rb") as fp:
                assert prefix == fp.read(96)
            assert r.status == 206 and r.headers["content-range"].startswith("bytes 0-95/")
            assert (await page.request.get(link, headers={"Range": "bytes=999999999999-"})).status == 416
            assert (await page.request.get(server.url + "/media/" + "0" * 32)).status == 404
            assert (await page.request.get(server.url + "/media/relations.sqlite3")).status == 404
            report["range"] = {"status": r.status, "content_range": r.headers["content-range"],
                               "mime": r.headers["content-type"], "verified_sha256_first_96": hashlib.sha256(prefix).hexdigest()}
            second = await ask("那就放这个片段")
            assert second["playback"]["status"] == "played" and second["selected"]["media_id"] == item["media_id"]
            await ask(r"G:\音乐 里边有")
            last = await ask("检索到就直接放把，无论什么")
            assert last["playback"]["status"] == "played"
            assert last["selected"]["media_id"] == item["media_id"]
            await page.screenshot(path=str(folder / "media-chat.png"), full_page=True)
            results = await ask("帮我找刀郎音乐")
            assert results["items"] and results["items"][0]["kind"] == "audio"
            picked = await ask("预览第 2 条")
            assert picked["selected"]["media_id"] == results["items"][1]["media_id"]
            image = await ask("只查本地，搜索图片 不存在的测试封面987")
            assert image["query"] == "不存在的测试封面987"
            video = await ask("帮我找视频 不存在的测试片987")
            assert video["query"] == "不存在的测试片987"
            await browser.close()
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
