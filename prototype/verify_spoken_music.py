"""Compare browser playback timing with identical cached MOSS audio."""

import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch
from urllib.parse import urlsplit
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import DEFAULT_DB
from model_catalog import SPECS
from verify_context_reply import free_port
from voice_output import MossSpeech


QUERY = "我想听刀郎的歌"
CAPTION = "为你播放刀郎的《冲动的惩罚》。"
ARCHIVED_VOICE = (ROOT / "runs/spoken-music-20261120-133310/data/voice-announcements"
                  / "fa96fa2810ff8c1d4494da55e51e4ff909297b7c5ae38ff00985571d970a98f6.wav")


async def main(args):
    folder = ROOT / "runs" / datetime.now().strftime("spoken-music-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result_path = folder / "result.json"
    catalog = folder / "local-catalog.sqlite3"
    with closing(sqlite3.connect(DEFAULT_DB.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(catalog)) as copy:
            source.backup(copy)
    print(result_path, flush=True)
    report = {"passed":False, "cached":args.warm, "baseline":bool(args.baseline),
              "trials":[], "model_connections":0, "outside_requests":[], "page_errors":[]}

    async def blocked_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def trial(browser, server):
        page = await browser.new_page(viewport={"width":1440, "height":900}, reduced_motion="reduce")
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        speech = []

        async def guard(route):
            url = urlsplit(route.request.url)
            if url.scheme in {"http", "https"} and url.netloc != f"127.0.0.1:{server.port}":
                report["outside_requests"].append(url.netloc)
                await route.abort()
            elif args.baseline and url.path in ("/static/agent-projection.js", "/static/agent-speech.js"):
                await route.fulfill(path=args.baseline / Path(url.path).name, content_type="text/javascript")
            elif url.path == "/api/speak":
                body = route.request.post_data_json
                speech.append(body)
                if body.get("category") == "media" and body.get("prefer_cached") is True:
                    await route.continue_()
                else:
                    await route.fulfill(status=204)
            else:
                await route.continue_()

        try:
            await page.route("**/*", guard)
            await page.add_init_script("""window.voiceEvents = [];
              for (const event of ['moss-speech-start','moss-speech-end','moss-media-start']) {
                addEventListener(event, ({detail}) => voiceEvents.push({
                  event, ms:performance.now() - submitted, id:detail.id || detail.turnId,
                  short:detail.shortCue === true,
                  volume:[...document.querySelectorAll('.media-audio')].find(a=>!a.paused)?.volume ?? null
                }));
              }""")
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            await page.get_by_test_id("composer").fill(QUERY)
            await page.evaluate("window.submitted = performance.now()")
            await page.get_by_test_id("send").click()
            await page.wait_for_function("voiceEvents.length >= 3")
            events = await page.evaluate("voiceEvents")
            row = (await server.state())["turns"][-1]
            assert row["media"]["play_mode"] == "track" and row["media"]["selected"]["source_kind"] == "local", row
            assert row["media"]["selected"]["title"] == "冲动的惩罚" and row["cloud_requests"]["total"] == 0, row
            assert len(events) == 3 and all(item["id"] == row["id"] for item in events), events
            by_event = {item["event"]:item for item in events}
            start, end, music = (by_event[key] for key in ("moss-speech-start", "moss-speech-end", "moss-media-start"))
            assert start["short"] is not args.warm, events
            assert speech[0]["text"] == CAPTION and speech[0]["prefer_cached"] is True, speech
            if args.warm:
                assert len(speech) == 1, speech
                assert end["ms"] - start["ms"] >= report["wave_seconds"] * 900
            if args.baseline and args.warm:
                assert start["ms"] < end["ms"] <= music["ms"], events
            else:
                assert start["ms"] <= music["ms"] < end["ms"] and 0 < music["volume"] <= .15, events
            await page.wait_for_function("""id=>{const a=document.querySelector('.media-audio[data-audio-turn="'+id+'"]');
                return a && !a.paused && a.volume===1 && a.currentTime>.35;}""", arg=row["id"])
            assert (await server.state())["turns"][-1]["browser_playback"]["event"] == "started"
            assert await page.locator("#tools-panel").is_visible()
            await page.screenshot(path=folder / f'music-{len(report["trials"])+1}.png')
            await page.reload(wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            assert not await page.evaluate("voiceEvents.length")
            return {"events":events, "song_start_ms":round(music["ms"], 1),
                    "after_voice_start_ms":round(music["ms"]-start["ms"], 1)}
        finally:
            await page.close()

    async with await asyncio.start_server(blocked_model, "127.0.0.1", 0) as sink:
        model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
        env = {"LOCALAPPDATA":str(folder / "profile"), "MOSS_PROVIDER_DIR":str(folder / "providers"), "MOSS_MEDIA_DB":str(catalog),
               "MOSS_WINDOWS_REMINDERS":"0", "MOSS_MEM0_ENABLED":"0", "MOSS_KEV_ENABLED":"0",
               "MOSS_SPEECH_PREPARE":"0", "MOSS_SPEECH_STANDBY":"0"}
        for _, prefixes, _, _ in SPECS.values():
            for prefix in prefixes:
                env[prefix + "_BASE_URL"] = model_url
        with patch.dict(os.environ, env):
            server = BrowserServer(folder, free_port(), model_url=model_url)
            try:
                if args.warm:
                    with wave.open(str(args.cache_file), "rb") as source:
                        report["wave_seconds"] = source.getnframes() / source.getframerate()
                    report["wave_sha256"] = hashlib.sha256(args.cache_file.read_bytes()).hexdigest()
                    assert report["wave_seconds"] > 1
                    speech = MossSpeech(server.data)
                    target = speech._announcement_path(speech._spoken(CAPTION, "media"))
                    target.parent.mkdir(parents=True)
                    shutil.copyfile(args.cache_file, target)
                await server.start()
                async with async_playwright() as playwright:
                    browser = await open_browser(playwright)
                    try:
                        await confirm_muted_edge(browser)
                        for _ in range(args.repeats):
                            assert (await server.post("/api/new-chat", {}))[0] == 200
                            report["trials"].append(await trial(browser, server))
                    finally:
                        await browser.close()
                assert not any(report[key] for key in ("outside_requests", "page_errors", "model_connections")), report
                report["passed"] = True
            finally:
                await server.stop()
                result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(result_path, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warm", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--cache-file", type=Path, default=ARCHIVED_VOICE)
    parser.add_argument("--repeats", type=int, choices=range(1,6), default=3)
    asyncio.run(main(parser.parse_args()))
