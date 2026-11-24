"""Replay the desktop transcript, measuring default music and announcement separately."""

import argparse
import asyncio
from contextlib import AsyncExitStack, closing
from datetime import datetime
import json
import os
import re
import sqlite3
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import DEFAULT_DB
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_local_overview import QUESTION, check_live_values, independent_readings
from verify_local_overview_browser import inspect_panel


PROJECTION_FILES = frozenset(("agent-speech.js", "agent-projection.js"))
TRACK_NUMBER_PREFIX = re.compile(r"^\s*[0-9０-９]{1,3}\s*[.．、)）_–—-]\s*\S")
DEFAULT_MUSIC_REQUEST = "来一首适合此时此刻听的歌吧"


async def run(*, baseline=None, music_only=False, measure_only=False, music_preferences=False):
    folder = ROOT / "runs" / datetime.now().strftime("recent-human-flow-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    evidence = folder / "result.json"
    print(evidence, flush=True)
    report = {"passed": False, "turns": [], "model_connections": 0, "page_errors": [], "outside": [],
              "baseline": bool(baseline), "uncached_media_requests": [], "status_speech_fixture_requests": [],
              "music_only": music_only, "measure_only": measure_only, "music_preferences": music_preferences,
              "screenshots": []}
    catalog = folder / "local-catalog.sqlite3"
    with closing(sqlite3.connect(DEFAULT_DB.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(catalog)) as isolated:
            original.backup(isolated)

    async def guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def ask(page, server, text):
        before = {item["id"] for item in (await server.state())["turns"]}
        await page.get_by_test_id("composer").fill(text)
        await page.evaluate("flowSubmitted = performance.now()")
        start = perf_counter()
        await page.get_by_test_id("send").click()
        async with asyncio.timeout(20):
            while True:
                state = await server.state()
                turn = next((row for row in state["turns"] if row["id"] not in before and row["user"] == text), None)
                if turn and turn["status"] not in {"thinking", "saving"}:
                    break
                await asyncio.sleep(.03)
        row = {"text": text, "source": turn["source"], "reply": turn.get("reply"),
               "decision_ms": round((perf_counter() - start) * 1000, 1),
               "model_calls": turn.get("cloud_requests", {}).get("total")}
        report["turns"].append(row)
        assert turn["status"] == "replied" and row["model_calls"] == 0, turn
        return turn, row, start

    async def capture(page, name):
        await page.screenshot(path=folder / name, animations="disabled")
        report["screenshots"].append(name)

    async def wait_for_playback(server, turn_id):
        async with asyncio.timeout(30):
            while True:
                current = next(item for item in (await server.state())["turns"] if item["id"] == turn_id)
                receipt = current.get("browser_playback") or {}
                if receipt.get("event") == "started":
                    return receipt
                assert receipt.get("event") not in {"failed", "blocked"}, receipt
                await asyncio.sleep(.05)

    async def preference_flow(page, server):
        attribution = "按你保存的音乐偏好"
        preference = "我听歌偏爱张学友"
        saved, _, _ = await ask(page, server, "记住，" + preference)
        assert saved["source"] == "memory" and saved.get("memory_id")
        memory_id = saved["memory_id"]
        stored = (await server.state())["memories"]
        assert [(row["id"], row["text"]) for row in stored] == [(memory_id, preference)], stored

        async def play(text, *, artist=None, attributed=False):
            turn, row, start = await ask(page, server, text)
            media = turn.get("media") or {}
            song = media.get("selected") or {}
            assert turn["source"] == "media" and media.get("autoplay") and media.get("online_calls") == 0, turn
            assert song.get("media_id") and song["source_kind"] == "local", song
            if artist:
                assert song["artist"] == artist, song
            with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
                path, indexed_artist = db.execute("SELECT path, artist FROM media WHERE media_id=?",
                                                  (song["media_id"],)).fetchone()
            assert Path(path).drive.casefold() == "g:", path
            if attributed:
                if song.get("artist_source") == "folder":
                    assert not indexed_artist.strip() and artist in Path(path).parent.parts, song
                else:
                    assert indexed_artist == artist, song
                assert memory_id in (turn.get("memory_ids") or ()), turn
                assert turn["reply"].startswith(attribution), turn["reply"]
            else:
                assert attribution not in turn["reply"], turn["reply"]
            assert song["title"] in turn["reply"] and "？" not in turn["reply"].splitlines()[0], turn["reply"]
            receipt = await wait_for_playback(server, turn["id"])
            start_ms = round((perf_counter() - start) * 1000, 1)
            assert receipt["media_id"] == song["media_id"] and receipt.get("play_id") and receipt.get("at"), receipt
            progress = await page.wait_for_function("""({turnId, mediaId}) => {
                const node = [...document.querySelectorAll('audio.media-audio')].find(audio =>
                    audio.dataset.audioTurn === turnId && audio.dataset.mediaId === mediaId);
                if (!node || node.paused || node.currentTime <= .12 || node.readyState < 2) return null;
                return {path:new URL(node.currentSrc).pathname, seconds:node.currentTime};
            }""", arg={"turnId": turn["id"], "mediaId": song["media_id"]}, timeout=15000)
            audio = await progress.json_value()
            assert audio["path"] == "/media/" + song["media_id"], audio
            visible = await page.locator(f'article[data-turn-id="{turn["id"]}"]').get_by_test_id("final").inner_text()
            assert visible.strip() == turn["reply"].strip(), (visible, turn["reply"])
            row.update(song=song["title"], artist=song["artist"], indexed_artist=indexed_artist,
                       artist_source=song.get("artist_source") or ("tag" if indexed_artist else None),
                       attributed=attributed, persisted_receipt=receipt, audio=audio,
                       verified_start_ms=start_ms)
            return turn, receipt

        personalized, first = await play(DEFAULT_MUSIC_REQUEST, artist="张学友", attributed=True)
        _, corrected = await play("刀郎的吧", artist="刀郎")
        assert corrected["media_id"] != first["media_id"]
        assert (await server.state())["memories"] == stored
        removed, _, _ = await ask(page, server, "撤回记忆：" + preference)
        assert removed["source"] == "memory" and "已删除" in removed["reply"], removed
        assert not (await server.state())["memories"]
        unpersonalized, _ = await play(DEFAULT_MUSIC_REQUEST)
        assert memory_id not in (unpersonalized.get("memory_ids") or ()) and not (await server.state())["memories"]
        report["preference_flow"] = {"memory_id": memory_id, "personalized_turn": personalized["id"],
                                     "after_removal_turn": unpersonalized["id"], "override_kept_memory": True}

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "LOCALAPPDATA": str(folder / "profile"), "MOSS_MEDIA_DB": str(catalog),
                "MOSS_PROVIDER_DIR": str(folder / "providers"), "MOSS_MEM0_ENABLED": "0",
                "MOSS_WINDOWS_REMINDERS": "0", "MOSS_KEV_ENABLED": "0", "MOSS_SPEECH_PREPARE": "0",
                "MOSS_SPEECH_STANDBY": "0",
            }))
            sink = await stack.enter_async_context(await asyncio.start_server(guard, "127.0.0.1", 0))
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            server = BrowserServer(folder, free_port(), model_url=model_url)
            moss = Moss(server.data, model_url, "unused", prefer_cloud=False)
            stack.push_async_callback(moss.close)
            runner = web.AppRunner(application(moss, lambda: "standby", server.data, server.port, "recent-qa", asyncio.Event()),
                                   access_log=None, shutdown_timeout=1)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "127.0.0.1", server.port).start()
            playwright = await stack.enter_async_context(async_playwright())
            browser = await open_browser(playwright)
            stack.push_async_callback(browser.close)
            await confirm_muted_edge(browser)
            context = await browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            await context.grant_permissions([], origin=server.url)
            await context.add_init_script("""window.flowSubmitted = 0;
                window.flowEvents = [];
                window.flowPlayCalls = [];
                const play = HTMLMediaElement.prototype.play;
                HTMLMediaElement.prototype.play = function (...args) {
                  if (this.dataset.audioTurn) flowPlayCalls.push({id: this.dataset.audioTurn,
                    ms: performance.now() - flowSubmitted, volume: this.volume});
                  return play.apply(this, args);
                };
                for (const name of ['moss-media-start', 'moss-speech-start', 'moss-speech-end', 'moss-speech-error']) {
                  addEventListener(name, ({detail}) => flowEvents.push({
                    name, id: detail.turnId || detail.id, ms: performance.now() - flowSubmitted,
                    short: detail.shortCue || false,
                    volume: Array.from(document.querySelectorAll('.media-audio'))
                        .find(audio => !audio.paused)?.volume ?? null,
                  }));
                }""")
            page = await context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            media_requests, media_responses = [], []

            def record_media_request(request):
                if urlsplit(request.url).path.startswith("/media/"):
                    media_requests.append((request.url, perf_counter(), request.headers.get("range")))

            def record_media_response(response):
                if urlsplit(response.url).path.startswith("/media/"):
                    media_responses.append((response.url, perf_counter(), response.status, response.headers.get("content-type")))

            page.on("request", record_media_request)
            page.on("response", record_media_response)

            async def local(route):
                url = urlsplit(route.request.url)
                if url.scheme in {"http", "https"} and url.netloc != f"127.0.0.1:{server.port}":
                    report["outside"].append(url.netloc)
                    await route.abort()
                    return
                filename = url.path.removeprefix("/static/")
                if baseline and filename in PROJECTION_FILES:
                    await route.fulfill(path=baseline / filename, content_type="text/javascript")
                    return
                if url.path == "/api/speak":
                    body = route.request.post_data_json
                    if body["category"] == "media" and body.get("prefer_cached") is True:
                        await route.continue_()
                    elif body["category"] == "media":
                        report["uncached_media_requests"].append(body["text"])
                        await route.fulfill(status=204)
                    else:
                        report["status_speech_fixture_requests"].append(body["text"])
                        await route.fulfill(path=ROOT / "static/music-ack.wav", content_type="audio/wav")
                    return
                await route.continue_()

            await page.route("**/*", local)
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            if music_preferences:
                await preference_flow(page, server)
                assert not report["model_connections"] and not report["page_errors"] and not report["outside"], report
                report["passed"] = True
                return
            weather_panel = page.get_by_test_id("weather-panel")
            if not music_only:
                weather, _, _ = await ask(page, server, "今天天气怎么样")
                assert not weather.get("weather") and "城市" in weather["reply"], weather
                location, _, _ = await ask(page, server, "我当前的定位点是在哪里，你不知道么")
                assert not location.get("weather") and "定位" in location["reply"], location
                resolved, row, _ = await ask(page, server, "河南郑州市中原区")
                assert resolved.get("weather", {}).get("location_kind") == "district" and "郑州市中原区" in resolved["reply"], resolved
                await weather_panel.wait_for()
                assert "郑州市中原区" in (await weather_panel.inner_text())
                assert await page.locator("dialog[open]").count() == 1
                row["popup"] = "weather"
                await capture(page, "01-区县天气.png")
                await page.keyboard.press("Escape")
                await weather_panel.wait_for(state="hidden")

                began, before_status = datetime.now(), independent_readings()
                status, row, _ = await ask(page, server, QUESTION)
                check_live_values(status["local_overview"], began, datetime.now(), before_status, independent_readings())
                row["popup"] = await inspect_panel(page, status)
                assert not await weather_panel.is_visible() and await page.locator("dialog[open]").count() == 1
                await capture(page, "02-本机三项读数.png")
                await page.wait_for_function("id=>flowEvents.some(e=>e.id===id&&e.name==='moss-speech-end')", arg=status["id"])
                row["speech_fixture"] = await page.evaluate("id=>flowEvents.filter(e=>e.id===id)", status["id"])
                assert [e["name"] for e in row["speech_fixture"]] == ["moss-speech-start", "moss-speech-end"]
                await page.keyboard.press("Escape")
                await page.get_by_test_id("local-status-panel").wait_for(state="hidden")

            for text, artist in ((DEFAULT_MUSIC_REQUEST, None),
                                 ("刀郎的吧", "刀郎"), ("刀郎的是随便一首歌曲", "刀郎")):
                turn, row, start = await ask(page, server, text)
                media = turn.get("media", {})
                song = media.get("selected") or {}
                assert song.get("media_id") and media.get("autoplay") and media.get("online_calls") == 0, turn
                if artist:
                    assert song.get("artist") == artist, song
                receipt = await wait_for_playback(server, turn["id"])
                assert receipt["media_id"] == song["media_id"] and receipt.get("play_id"), receipt
                row.update(song=song["title"], artist=song.get("artist"),
                           source_kind=song["source_kind"], playback_event=receipt["event"],
                           verified_start_ms=round((perf_counter() - start) * 1000, 1))
                await page.wait_for_function("id=>flowEvents.some(e=>e.id===id&&e.name==='moss-speech-end')", arg=turn["id"])
                events = row["events"] = await page.evaluate("id=>flowEvents.filter(e=>e.id===id)", turn["id"])
                assert sorted(e["name"] for e in events) == ["moss-media-start", "moss-speech-end", "moss-speech-start"], events
                speech_start = next(e for e in events if e["name"] == "moss-speech-start")
                speech_end = next(e for e in events if e["name"] == "moss-speech-end")
                music_start = next(e for e in events if e["name"] == "moss-media-start")
                play_calls = await page.evaluate("id=>flowPlayCalls.filter(e=>e.id===id)", turn["id"])
                assert len(play_calls) == 1, play_calls
                play_call = play_calls[0]
                row.update(music_start_ms=round(music_start["ms"], 1), cue_start_ms=round(speech_start["ms"], 1),
                           speech_end_ms=round(speech_end["ms"], 1), play_call=play_call)
                if artist is None:
                    media_url = server.url + "/media/" + song["media_id"]
                    request = next(entry for entry in media_requests if entry[0] == media_url and entry[1] >= start)
                    response = next(entry for entry in media_responses if entry[0] == media_url and entry[1] >= request[1])
                    row["first_media_open"] = {"request_ms": round((request[1] - start) * 1000, 1), "range": request[2],
                                               "response_ms": round((response[1] - start) * 1000, 1), "status": response[2],
                                               "content_type": response[3]}
                    assert response[2] in {200, 206}, row
                    panel = page.locator(f'#tools-panel[data-turn-id="{turn["id"]}"]')
                    heading = panel.locator(".player-heading")
                    await heading.wait_for()
                    player_title = await heading.locator("strong").inner_text()
                    player_artists = await heading.locator(".player-artist").all_text_contents()
                    selected_row = panel.locator('#tool-results .media-choice[aria-current="true"]')
                    list_title = await selected_row.locator("button > span").inner_text()
                    list_detail = await selected_row.locator("small").inner_text()
                    chat_reply = await page.locator(f'article[data-turn-id="{turn["id"]}"]').get_by_test_id("final").inner_text()
                    row["display"] = {"player_title": player_title, "player_artist": "".join(player_artists),
                                      "list_title": list_title, "list_detail": list_detail, "reply_first_line": chat_reply.splitlines()[0]}
                    row["clean_credited_pick"] = bool(song.get("artist", "").strip() and not TRACK_NUMBER_PREFIX.match(song["title"]))
                    assert player_title == list_title == song["title"] and song["title"] in chat_reply, row
                    await capture(page, "03-默认选曲.png")
                    if not baseline and not measure_only:
                        assert row["clean_credited_pick"], row
                        assert player_artists == [song["artist"]] and song["artist"] in list_detail and song["artist"] in chat_reply, row
                assert speech_start["short"] and speech_end["ms"] - speech_start["ms"] > 1100, events
                if baseline:
                    assert speech_end["ms"] < music_start["ms"], events
                else:
                    # Data can arrive after the cue. Playback must be requested during it at reduced volume.
                    assert play_call["ms"] < speech_end["ms"] and 0 < play_call["volume"] <= .15, row
                    if music_start["ms"] < speech_end["ms"]:
                        assert 0 < music_start["volume"] <= .15, row
                    else:
                        assert music_start["volume"] == 1, row
                    assert await page.evaluate("""id=>document.querySelector(
                        '.media-audio[data-audio-turn="'+id+'"]').volume===1""", turn["id"])
            assert len(report["uncached_media_requests"]) == 3, report["uncached_media_requests"]
            if not music_only:
                original, _, _ = await ask(page, server, "播放《冲动的惩罚》")
                original_receipt = await wait_for_playback(server, original["id"])
                replacement, row, started = await ask(page, server, "换首歌")
                replacement_receipt = await wait_for_playback(server, replacement["id"])
                assert original_receipt["media_id"] != replacement_receipt["media_id"]
                named = original["media"]["selected"]
                chosen = replacement["media"]["selected"]
                assert named["title"] == "冲动的惩罚" and chosen["title"] != named["title"]
                assert chosen["source_kind"] in {"local", "nas"} and replacement["media"]["online_calls"] == 0
                row.update(song=chosen["title"], artist=chosen.get("artist"), replaced=named["title"],
                           verified_start_ms=round((perf_counter()-started)*1000, 1))
            assert not report["model_connections"] and not report["page_errors"] and not report["outside"], report
            report["passed"] = True
    finally:
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(evidence, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--music-only", action="store_true")
    parser.add_argument("--music-preferences", action="store_true", help="用 G 盘复验保存、临时覆盖和撤回歌手偏好")
    parser.add_argument("--measure-only", action="store_true", help="Report default-pick quality without failing older selection code")
    args = parser.parse_args()
    asyncio.run(run(baseline=args.baseline, music_only=args.music_only, measure_only=args.measure_only,
                    music_preferences=args.music_preferences))
