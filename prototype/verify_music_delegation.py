import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
import sqlite3
from time import perf_counter, time_ns
from unittest.mock import patch
from uuid import uuid4
import wave

import aiohttp
from aiohttp import web
from mutagen.id3 import TIT2, TPE1
from mutagen.wave import WAVE

from browser_lab import ROOT
from core import Moss
from local_media.library import DEFAULT_DB, MediaLibrary, song_title_key
from media_chat import MediaChat
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_weather_browser_location import sample_forecast
from weather_forecast import format_weather


WEATHER = "今天河南郑州中原区的天气怎么样"
GENERIC = "来一首适合此时此刻听的歌吧"
PREFERRED = "按我的音乐偏好放一首"
ARTIST = "刀郎的吧"
CORRECTION = "刀郎的是随便一首歌曲"


def catalog_at(folder, actual_index):
    path = folder / "catalog.sqlite3"
    if actual_index:
        with closing(sqlite3.connect(DEFAULT_DB.as_uri() + "?mode=ro", uri=True, timeout=5)) as source, \
                closing(sqlite3.connect(path)) as isolated:
            source.backup(isolated)
        return path
    directory = folder / "music"
    for artist, titles in (("刀郎", ("西海情歌", "手心里的温柔", "冲动的惩罚", "吻别")),
                           ("张学友", ("刀郎", "吻别", "一路上有你", "歌吧")),
                           ("一棵小葱", ("西楼别序",))):
        root = directory / artist
        root.mkdir(parents=True)
        for title in titles:
            track_path = root / (title + ".wav")
            with wave.open(str(track_path), "wb") as file:
                file.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
                file.writeframes(bytes(64_000))
            if artist != "一棵小葱":
                track = WAVE(track_path)
                track.add_tags()
                track.tags.add(TIT2(encoding=3, text=title))
                track.tags.add(TPE1(encoding=3, text=artist))
                track.save()
    library = MediaLibrary(path)
    library.configure([{"id": "input", "label": "隔离曲库", "kind": "local", "path": str(directory)}])
    library.scan()
    return path


async def verify_contextual_music(ask, playable, new_chat, client, report):
    await new_chat()
    failed = await playable(GENERIC, record_event="failed")
    blocked = await playable(GENERIC, record_event="blocked")
    first = await playable(GENERIC, record_started=True)
    assert not any(row["memory_ids"] for row in (failed, blocked, first))
    assert all(row["source_kind"] == "local" for row in (failed, blocked, first))
    assert failed["media_id"] == blocked["media_id"] == first["media_id"], (failed, blocked, first)
    recent = [first]
    for _ in range(3):
        row = await playable(GENERIC, record_started=True)
        assert not row["memory_ids"] and row["source_kind"] == "local", row
        assert song_title_key(row["title"]) not in {song_title_key(previous["title"]) for previous in recent}, row
        recent.append(row)

    await new_chat()
    saved, _ = await ask("记住，我听歌偏爱张学友")
    assert saved["source"] == "memory" and saved["memory_id"], saved
    preference_id = saved["memory_id"]
    preferred = await playable(GENERIC)
    assert preferred["artist"] == "张学友" and preferred["memory_ids"] == [preference_id], preferred
    def choose_different_artist(matches):
        matches.sort(key=lambda match: match[1]["artist"] != "刀郎")

    with patch("local_media.library.shuffle", side_effect=choose_different_artist):
        random_pick = await playable("随机放首歌")
    assert random_pick["artist"] == "刀郎" and not random_pick["memory_ids"], random_pick
    override = await playable(ARTIST, "刀郎")
    assert not override["memory_ids"], override
    async with client.get("/api/state") as response:
        state = await response.json()
    assert any(memory["id"] == preference_id and memory["text"] == "我听歌偏爱张学友" for memory in state["memories"]), state["memories"]

    await new_chat()
    revised, _ = await ask("记住：我的音乐偏好是张学友的歌")
    assert revised["source"] == "memory" and revised["memory_id"] == preference_id, revised
    decoy = await playable("播放刀郎的《吻别》", title="吻别")
    assert decoy["artist"] == "刀郎" and not decoy["memory_ids"], decoy
    for title in ("一路上有你", "刀郎", "歌吧"):
        played = await playable(f"播放张学友的《{title}》", title=title, record_started=True)
        assert played["artist"] == "张学友" and not played["memory_ids"], played
    exact = await playable(PREFERRED, title="吻别")
    assert exact["artist"] == "张学友" and exact["memory_ids"] == [preference_id], exact
    assert exact["media_id"] != decoy["media_id"], (exact, decoy)

    for text in ("分析一下“按我的音乐偏好放一首”这句话", "我朋友想按他的音乐偏好放一首", "先不要按我的音乐偏好放一首"):
        _, no_play = await ask(text)
        assert not no_play["autoplay"] and not no_play["memory_ids"], no_play

    await new_chat()
    for text in ("记住：我朋友听歌偏爱张学友", "记住：‘我听歌偏爱张学友’是一句示例"):
        saved_elsewhere, row = await ask(text)
        assert saved_elsewhere["source"] == "memory" and not row["autoplay"], row
        assert saved_elsewhere["memory_id"] != preference_id, saved_elsewhere
    before_forget = await playable(GENERIC)
    assert before_forget["memory_ids"] == [preference_id] and before_forget["artist"] == "张学友", before_forget
    _, listed = await ask("第一首叫什么，谁唱的？")
    assert not listed["autoplay"] and listed["memory_ids"] == [preference_id], listed
    async with client.post("/api/forget", json={"id": preference_id}) as response:
        assert response.ok
        remaining = (await response.json())["memories"]
        assert len(remaining) == 2 and all(item["id"] != preference_id for item in remaining), remaining
    _, old_list = await ask("直接放吧")
    assert not old_list["autoplay"] and not old_list["memory_ids"], old_list
    neutral = await playable(GENERIC)
    assert not neutral["memory_ids"] and neutral["source_kind"] == "local" and neutral["artist"] == first["artist"], neutral
    assert "偏好" not in neutral["reply"], neutral

    report["contextual_music"] = {"plain_after_failure_reused":first["media_id"], "recent_titles":[row["title"] for row in recent],
                                  "preference_id":preference_id, "preferred_version":exact["media_id"],
                                  "same_title_decoy":decoy["media_id"], "neutral_after_forget":neutral["media_id"]}


async def run(actual_index):
    suffix = "source-index" if actual_index else "fixture"
    folder = ROOT / "runs" / (datetime.now().strftime("music-delegation-%Y%m%d-%H%M%S-%f-") + suffix)
    folder.mkdir()
    result = folder / "result.json"
    print(result, flush=True)
    report = {"passed": False, "catalog": "read-only source index snapshot" if actual_index else "fixture",
              "rows": [], "model_connections": 0, "model_fallback": [], "local_search_ms": []}
    os.environ.update(MOSS_MEDIA_DB=str(catalog_at(folder, actual_index)), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0")

    async def weather(_city, period, place, *, part_of_day=None, question=""):
        assert place and "中原区" in place["name"]
        return format_weather(sample_forecast(), place, period, part_of_day, question)

    async def no_model(**request):
        report["model_fallback"].append(request["text"])
        return "还需要指定对象。"

    local_search = MediaChat._local_search

    def timed_search(media, query, *args, **kwargs):
        started = perf_counter()
        try:
            return local_search(media, query, *args, **kwargs)
        finally:
            report["local_search_ms"].append(round((perf_counter() - started) * 1000, 1))

    async def block_cloud(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with await asyncio.start_server(block_cloud, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            moss = Moss(folder / "data", model_url, "isolated-unused")
            port = free_port()
            app = application(moss, lambda: "standby", folder / "data", port, "isolated", asyncio.Event())
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                with patch("assistant_brain.fetch_weather", weather), patch("assistant_brain.run_holo", no_model), \
                        patch.object(MediaChat, "_local_search", timed_search):
                    async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
                        async def ask(text):
                            before = len(report["model_fallback"])
                            before_search = len(report["local_search_ms"])
                            started = perf_counter()
                            async with client.post("/api/message", json={"text": text, "chat_id": moss.chat_id}) as response:
                                state = await response.json()
                                assert response.ok, state
                            turn_id = state["turns"][-1]["id"]
                            async with asyncio.timeout(10):
                                while state["turns"][-1]["status"] == "thinking":
                                    await asyncio.sleep(.01)
                                    async with client.get("/api/state") as response:
                                        state = await response.json()
                            turn = state["turns"][-1]
                            assert turn["id"] == turn_id and turn["status"] == "replied", turn
                            assert not turn["cloud_requests"]["total"]
                            media = turn.get("media") or {}
                            selected = media.get("selected") or {}
                            row = {"text": text, "reply": turn["reply"], "source": turn["source"],
                                   "title": selected.get("title"), "artist": selected.get("artist"), "online": media.get("online_calls"),
                                   "source_kind": selected.get("source_kind"), "media_id": selected.get("media_id"),
                                   "memory_ids": turn.get("memory_ids", []),
                                   "autoplay": media.get("autoplay"), "results": len(media.get("items", ())),
                                   "model_fallbacks": len(report["model_fallback"]) - before,
                                   "search_ms": report["local_search_ms"][before_search:],
                                   "elapsed_ms": round((perf_counter() - started) * 1000, 1)}
                            report["rows"].append(row)
                            return turn, row

                        async def playback_event(turn, item, event):
                            payload = {"chat_id": turn["chat_id"], "turn_id": turn["id"], "media_id": item["media_id"],
                                       "event": event}
                            if event == "started":
                                payload.update(play_id=uuid4().hex, started_at_us=time_ns() // 1000)
                            async with client.post("/api/media-event", json=payload) as response:
                                assert response.ok and (await response.json())["ok"], payload
                            async with client.get("/api/state") as response:
                                state = await response.json()
                            receipt = next(t["browser_playback"] for t in state["turns"] if t["id"] == turn["id"])
                            assert receipt["event"] == event and receipt["media_id"] == item["media_id"], receipt
                            return receipt

                        async def playable(text, artist=None, title=None, *, record_started=False, record_event=None):
                            turn, row = await ask(text)
                            assert row["autoplay"] and row["results"] and row["online"] == row["model_fallbacks"] == 0, row
                            media = turn["media"]
                            selected = media["selected"]
                            assert media["play_mode"] == "track" and selected["title"] in media["announcement"]
                            assert selected["title"] in turn["reply"] and not turn.get("weather"), row
                            if title is None:
                                assert row["source"] == "media", row
                            if artist:
                                assert row["artist"] == artist and media["query"] == artist, row
                                assert all(item["artist"] == artist for item in media["items"]), row
                            if title:
                                assert row["title"] == title, row
                            async with client.get("/media/" + selected["media_id"], headers={"Range":"bytes=0-127"}) as stream:
                                assert stream.status == 206 and len(await stream.read()) == 128
                                row["range_status"] = stream.status
                            event = "started" if record_started else record_event
                            if event:
                                row["test_playback_receipt"] = await playback_event(turn, selected, event)
                            return row

                        async def new_chat():
                            async with client.post("/api/new-chat", json={}) as response:
                                assert response.ok

                        recent_weather, _ = await ask(WEATHER)
                        assert recent_weather["weather"]["location_kind"] == "district"
                        await playable(GENERIC)
                        await playable(ARTIST, "刀郎")
                        await playable(CORRECTION, "刀郎")
                        for text in ("给我挑一首现在适合听的歌", "来一首歌吧", "放一首适合眼下听的音乐"):
                            await playable(text)
                        await playable("就张学友的", "张学友")
                        await playable("给我来一首刀郎的吧", "刀郎")
                        if not actual_index:
                            await playable("播放《刀郎》", title="刀郎")

                        for text in ("先别来一首适合此时此刻听的歌吧", "歌词里的“来一首适合现在听的歌”怎么解释？",
                                     "不想听一首刀郎的", "讨论一下刀郎的音乐风格", "刀郎的还是张学友的？"):
                            _, row = await ask(text)
                            assert not row["autoplay"], row
                        await new_chat()
                        await ask(WEATHER)
                        _, row = await ask("刀郎的吧")
                        assert not row["autoplay"] and not row["title"], row
                        warm = await playable(GENERIC)
                        report["warm_no_media_context"] = warm

                        for text in ("换首歌", "换一首", GENERIC):
                            await new_chat()
                            prior = await playable("播放《冲动的惩罚》", title="冲动的惩罚", record_started=True)
                            changed = await playable(text, record_started=True)
                            assert song_title_key(changed["title"]) != song_title_key(prior["title"]), changed
                            assert changed["test_playback_receipt"]["media_id"] != prior["test_playback_receipt"]["media_id"]

                        await new_chat()
                        await playable("播放《冲动的惩罚》", title="冲动的惩罚", record_started=True)
                        _, bounded = await ask("第7首")
                        assert bounded["autoplay"] is False and "候选列表只有" in bounded["reply"], bounded
                        assert bounded["model_fallbacks"] == bounded["online"] == 0, bounded

                        await new_chat()
                        only = await playable("播放一棵小葱的歌吧", artist="一棵小葱", record_started=True)
                        again = await playable("再来一首", artist="一棵小葱", record_started=True)
                        assert again["test_playback_receipt"]["media_id"] == only["test_playback_receipt"]["media_id"], again
                        _, still_scoped = await ask("换一首")
                        assert still_scoped["autoplay"] is False and "没有其他可播放" in still_scoped["reply"], still_scoped
                        assert still_scoped["model_fallbacks"] == still_scoped["online"] == 0, still_scoped

                        for text in ("放点音乐吧", "放点歌听听", "来一首歌吧", "来一首音乐"):
                            await new_chat()
                            await playable(text, record_started=True)
                        if not actual_index:
                            await new_chat()
                            await playable("播放《歌吧》", title="歌吧", record_started=True)
                            await playable("播放张学友的《歌吧》", title="歌吧", record_started=True)
                            _, missing = await ask("播放一棵小葱的《歌吧》")
                            assert not missing["autoplay"] and "一棵小葱" in missing["reply"], missing

                        await new_chat()
                        target = "手心里的温柔" if actual_index else "吻别"
                        catalog_counts = []

                        def shuffle_catalog(matches):
                            catalog_counts.append(len(matches))
                            matches.sort(key=lambda match: match[1]["title"] != target)

                        with patch("local_media.library.shuffle", side_effect=shuffle_catalog):
                            picked = await playable("随机放首歌", record_started=True)
                        assert len(catalog_counts) == 1 and catalog_counts[0] > 6, catalog_counts
                        assert picked["title"] == target, picked
                        report["random_catalog_candidate_count"] = catalog_counts[0]

                        await new_chat()
                        prior = await playable("播放刀郎的歌吧", artist="刀郎", record_started=True)
                        expected = []

                        def shuffle_artist(tracks):
                            alternatives = [item["title"] for item in tracks
                                            if song_title_key(item["title"]) != song_title_key(prior["title"])]
                            assert len(alternatives) > 1 and all(item["artist"] == "刀郎" for item in tracks), tracks
                            tracks.reverse()
                            expected.append(alternatives[-1])

                        with patch("media_chat.shuffle", side_effect=shuffle_artist), \
                                patch("local_media.library.shuffle", side_effect=AssertionError("Artist selection escaped its list")):
                            picked = await playable("随机放首歌", artist="刀郎", record_started=True)
                        assert len(expected) == 1 and picked["title"] == expected[0], picked
                        assert picked["test_playback_receipt"]["media_id"] != prior["test_playback_receipt"]["media_id"]
                        if not actual_index:
                            await verify_contextual_music(ask, playable, new_chat, client, report)
                        assert not report["model_connections"]
                        report["passed"] = True
            finally:
                await runner.cleanup()
                await moss.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--actual-index", action="store_true", help="Use a SQLite snapshot with local library paths")
    asyncio.run(run(parser.parse_args().actual_index))
