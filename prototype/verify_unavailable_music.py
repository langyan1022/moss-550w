import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from media_chat import MediaLibrary
from verify_memory import send


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("unavailable-track-%Y%m%d-%H%M%S-%f")
    library_root = folder / "music" / "测试歌手"
    library_root.mkdir(parents=True)
    for title in ("白昼", "晨光", "夜航", "远山"):
        shutil.copyfile(ROOT / "static/music-ack.wav", library_root / (title + ".wav"))
    db = folder / "catalog.sqlite3"
    library = MediaLibrary(db)
    library.configure([{"id": "disk", "kind": "local", "path": str(library_root.parent)}])
    library.scan()
    server = BrowserServer(folder, 18489, model_url="http://127.0.0.1:18278/v1")
    original = os.environ.get("MOSS_MEDIA_DB")
    os.environ["MOSS_MEDIA_DB"] = str(db)
    try:
        await server.start()
    finally:
        if original is None:
            del os.environ["MOSS_MEDIA_DB"]
        else:
            os.environ["MOSS_MEDIA_DB"] = original
    report = {"passed": False, "cases": []}
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            await confirm_muted_edge(browser)
            page = await browser.new_page(reduced_motion="reduce")
            page.set_default_timeout(15000)
            await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
            await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")

            async def ask(text):
                start = perf_counter()
                await send(page, server, text)
                turn = (await server.state())["turns"][-1]
                media = turn["media"]
                assert turn["status"] == "replied" and turn["source"] == "media" and media["online_calls"] == 0
                result = {"user": text, "reply": turn["reply"], "autoplay": media["autoplay"]}
                report["cases"].append(result)
                if media["autoplay"]:
                    async with asyncio.timeout(8):
                        while True:
                            receipt = (await server.state())["turns"][-1].get("browser_playback") or {}
                            if receipt.get("event") == "started":
                                break
                            await asyncio.sleep(.03)
                    selected = media["selected"]
                    assert receipt["media_id"] == selected["media_id"]
                    result.update(played=selected["title"], receipt_ms=round((perf_counter() - start) * 1000))
                return media

            items = (await ask("只看看测试歌手的歌"))["items"]
            assert len(items) == 4
            first, second, third, fourth = items

            def take_offline(item):
                path = Path(item["path"]).resolve()
                assert path.is_relative_to(library_root.resolve())
                path.rename(path.with_suffix(".offline"))

            take_offline(first)
            exact = await ask("播放第一首")
            assert not exact["autoplay"] and exact["selected"] is None
            assert first["title"] in report["cases"][-1]["reply"]
            chosen = await ask("你帮我选一首吧")
            assert chosen["autoplay"] and chosen["selected"]["media_id"] == second["media_id"], report
            take_offline(third)
            changed = await ask("换一个")
            assert changed["autoplay"] and changed["selected"]["media_id"] == fourth["media_id"], report
            take_offline(second)
            other = await ask("换一个")
            assert not other["autoplay"] and "没有其他可播放" in report["cases"][-1]["reply"]
            single = await ask("再换一首测试歌手的歌")
            assert not single["autoplay"] and not (await server.state())["turns"][-1].get("browser_playback")
            take_offline(fourth)
            empty = await ask("你来决定")
            assert not empty["autoplay"] and not (await server.state())["turns"][-1].get("browser_playback")
            turns = (await server.state())["turns"]
            assert sum((t.get("browser_playback") or {}).get("event") == "started" for t in turns) == 2
            report["passed"] = True
            await browser.close()
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder, flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
