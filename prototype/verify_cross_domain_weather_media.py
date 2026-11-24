import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from unittest.mock import patch
import wave

import aiohttp
from aiohttp import web

from browser_lab import ROOT
from core import Moss
from local_media.library import MediaLibrary
from model_catalog import SPECS
from serve import application
from verify_context_reply import free_port
from verify_weather_browser_location import sample_forecast
from weather_forecast import format_weather


TOKYO = {"name": "东京", "latitude": 35.68, "longitude": 139.69}
BEIJING = {"name": "北京", "latitude": 39.9, "longitude": 116.4}
NEGATIVE_MEDIA = (
    "先别放一首天空之城", "请别放《东京夜曲》", "不用播一首白帆", "不要播放天气预报",
    "《天空之城》这句歌词是什么意思？", "分析一下《天空之城》的歌名", "你觉得《东京夜曲》好听吗？",
    "把“播放东京夜曲”翻译成英文", "“放一首天空之城”是什么意思？", "第二个", "选第二项", "不要查明天东京天气",
)


def catalog_at(folder):
    music = folder / "music"
    artist = music / "测试歌手"
    artist.mkdir(parents=True)
    for title in ("天空之城", "白帆", "云角", "东京夜曲", "天气预报"):
        with wave.open(str(artist / f"{title}.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(64000))
    catalog = MediaLibrary(folder / "library.sqlite3")
    catalog.configure([{"id": "fixture", "label": "隔离曲库", "kind": "local", "path": str(music)}])
    catalog.scan()
    return catalog.db_path


async def expanded(ask, fresh, moss):
    cross, elliptical = [], []
    for text, title in (
        ("放一首天空之城", "天空之城"), ("给我放首白帆", "白帆"), ("放 一首 云角", "云角"),
        ("帮我播一首天空之城", "天空之城"), ("播放一首天空之城", "天空之城"), ("放一首《东京夜曲》", "东京夜曲"),
        ("播放《天气预报》", "天气预报"), ("来一首白帆", "白帆"), ("我想听《东京夜曲》", "东京夜曲"),
        ("播放天气预报", "天气预报"),
    ):
        key = f"cross_{len(cross)+1:02}"
        await fresh()
        await ask(key + "_seed", "东京今天天气")
        row = await ask(key, text)
        cross.append({"case": key, "ok": row["title"] == title and row["autoplay"] and not row["new_model_fixture_calls"]
                       and row["city"] is None and row["artist"] == "测试歌手"})

    for text, city, period in (
        ("东京今天天气", "东京", "today"), ("东京明天的天气", "东京", "tomorrow"),
        ("后天东京天气", "东京", "day_after_tomorrow"), ("北京今天天气", "北京", "today"),
        ("北京朝阳区的天气", "北京市朝阳区", "current"), ("北京朝阳明天下午天气", "北京市朝阳区", "tomorrow"),
        ("北京市朝阳区后天天气", "北京市朝阳区", "day_after_tomorrow"),
        ("北京朝阳明天会下雨吗，带伞吗", "北京市朝阳区", "tomorrow"),
    ):
        key = f"cross_{len(cross)+1:02}"
        await fresh()
        await ask(key + "_seed", "播放《东京夜曲》")
        row = await ask(key, text)
        cross.append({"case": key, "ok": row["city"] == city and row["period"] == period and not row["new_model_fixture_calls"]
                       and row["title"] is None and not row["autoplay"]})

    for text in NEGATIVE_MEDIA:
        key = f"cross_{len(cross)+1:02}"
        await fresh()
        await ask(key + "_seed_media", "只看看测试歌手的歌，不要播放")
        await ask(key + "_seed_weather", "东京今天天气")
        row = await ask(key, text)
        cross.append({"case": key, "ok": row["city"] is None and row["title"] is None and not row["autoplay"]})

    for text, index in (
        ("第二首", 1), ("播放第二首", 1), ("播放第三首", 2), ("第三首", 2),
        ("试听第二首", 1), ("换成第二首", 1), ("不是第二首，播放第三首", 2), ("先别播放第三首；换成第一首吧", 0),
    ):
        key = f"elliptical_{len(elliptical)+1:02}"
        await fresh()
        listing = await ask(key + "_seed", "只看看测试歌手的歌，不要播放")
        row = await ask(key, text)
        elliptical.append({"case": key, "ok": len(listing["items"]) > index and row["title"] == listing["items"][index]
                           and row["autoplay"] and not row["new_model_fixture_calls"]})

    for text, period, part in (
        ("明天呢", "tomorrow", None), ("那明天呢", "tomorrow", None), ("后天呢", "day_after_tomorrow", None),
        ("那后天呢", "day_after_tomorrow", None), ("明天下雨吗", "tomorrow", None),
        ("明天带伞吗", "tomorrow", None), ("明天下午呢", "tomorrow", "下午"), ("晚上呢", "today", "晚上"),
    ):
        key = f"elliptical_{len(elliptical)+1:02}"
        await fresh()
        await ask(key + "_seed", "东京今天天气")
        row = await ask(key, text)
        elliptical.append({"case": key, "ok": row["city"] == "东京" and row["period"] == period
                           and row["part"] == part and not row["new_model_fixture_calls"]})

    def age_last():
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        turn = moss._get(moss.state()["turns"][-1]["id"])
        moss._update(turn, created_at=old, replied_at=old)

    for aged in (False, True):
        key = f"elliptical_{len(elliptical)+1:02}"
        await fresh()
        await ask(key + "_seed", "东京今天天气")
        if aged:
            age_last()
        await ask(key + "_switch", "播放《天空之城》")
        row = await ask(key, "说回刚才那个地方，后天的天气呢？")
        elliptical.append({"case":key, "ok": row["city"] == "东京" and row["period"] == "day_after_tomorrow"
                           and not row["new_model_fixture_calls"]})

    for aged in (False, True):
        key = f"elliptical_{len(elliptical)+1:02}"
        await fresh()
        await ask(key + "_seed", "只看看测试歌手的歌，不要播放")
        suggestion = await ask(key + "_offer", "你猜一下")
        if aged:
            age_last()
        await ask(key + "_thanks", "谢谢")
        row = await ask(key, "就它")
        ok = (not row["autoplay"] and row["title"] is None) if aged else (
            suggestion["suggested"] and row["title"] == suggestion["suggested"] and row["autoplay"])
        elliptical.append({"case":key, "ok": bool(ok)})
    return cross, elliptical


async def run(observe, matrix):
    folder = ROOT / "runs" / datetime.now().strftime("cross-domain-weather-media-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    result = folder / "result.json"
    print(result, flush=True)
    report = {"passed": False, "model_connections": 0, "model_fixture_calls": [], "cases": {}, "checks": {}}
    os.environ.update(MOSS_MEDIA_DB=str(catalog_at(folder)), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="0")

    async def local_forecast(city, period, place, *, part_of_day=None, question=""):
        resolved = place or {"东京": TOKYO, "北京": BEIJING}[city]
        return format_weather(sample_forecast(), resolved, period, part_of_day, question)

    async def probe_model(**request):
        history = "\n".join(message["content"] for message in request["history"])
        text = request["text"]
        report["model_fixture_calls"].append({"text": text, "tokyo_weather_in_history": "东京" in history,
                                               "tools": [tool["name"] for tool in request["tools"]]})
        if text == "放一首适合入睡的钢琴曲子":
            return await request["dispatch"]("play_song", {"title":"天空之城", "artist":"东京", "play":True})
        if text.startswith("放一首"):
            # Probe whether model-derived location can become an artist on the real tool call.
            return await request["dispatch"]("play_song", {"title": text.removeprefix("放一首"), "artist": "东京", "play": True})
        if text == "推荐一首适合入睡的钢琴曲子":
            return await request["dispatch"]("play_song", {"title":"天空之城", "artist":"东京", "play":False})
        if text in NEGATIVE_MEDIA:
            return await request["dispatch"]("play_song", {"title":"天空之城", "artist":"测试歌手", "play":True})
        return "你是问哪一项？"

    async def reject_model(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    try:
        async with await asyncio.start_server(reject_model, "127.0.0.1", 0) as guard:
            model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            moss = Moss(folder / "data", model_url, "isolated-unused")
            port = free_port()
            app = application(moss, lambda: "standby", folder / "data", port, "isolation", asyncio.Event())
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                with patch("assistant_brain.fetch_weather", local_forecast), patch("assistant_brain.run_holo", probe_model):
                    async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
                        async def ask(key, text):
                            before = len(report["model_fixture_calls"])
                            async with client.post("/api/message", json={"text": text, "chat_id": moss.chat_id}) as response:
                                data = await response.json()
                                assert response.status == 200, data
                            turn_id = data["turns"][-1]["id"]
                            async with asyncio.timeout(15):
                                while data["turns"][-1]["status"] == "thinking":
                                    await asyncio.sleep(.025)
                                    async with client.get("/api/state") as answer:
                                        data = await answer.json()
                            turn = data["turns"][-1]
                            media = turn.get("media") or {}
                            selected = media.get("selected") or {}
                            weather = turn.get("weather") or {}
                            row = {"text": text, "reply": turn["reply"], "source": turn.get("source"),
                                   "title": selected.get("title"), "artist": selected.get("artist"),
                                   "query": media.get("query"), "autoplay": media.get("autoplay", False),
                                   "items": [item["title"] for item in media.get("items", ())],
                                   "city": weather.get("city"), "period": weather.get("period"), "part": weather.get("part_of_day"),
                                   "suggested": (media.get("suggested") or {}).get("title"),
                                   "new_model_fixture_calls": len(report["model_fixture_calls"]) - before}
                            report["cases"][key] = row
                            assert turn["id"] == turn_id and turn["status"] == "replied", turn
                            assert not turn["cloud_requests"]["total"], turn
                            return row

                        async def fresh():
                            async with client.post("/api/new-chat", json={}) as response:
                                assert response.status == 200

                        await ask("weather_first", "东京今天天气")
                        named = await ask("weather_to_full_media", "放一首天空之城")
                        day = await ask("weather_then_media_elliptic", "后天呢")
                        await ask("return_to_weather", "东京的天气")
                        missing = await ask("weather_to_missing_media", "放一首白银没有入库")
                        await ask("weather_before_recommendation", "东京的天气")
                        recommended = await ask("weather_to_recommendation", "推荐一首适合入睡的钢琴曲子")
                        picked = await ask("weather_to_play_recommendation", "放一首适合入睡的钢琴曲子")
                        await fresh()
                        listing = await ask("music_list", "只看看测试歌手的歌，不要播放")
                        second = await ask("music_elliptic", "第二首")
                        next_weather = await ask("music_to_full_weather", "东京今天天气")
                        tomorrow = await ask("weather_elliptic", "那明天呢")
                        ordinal = await ask("music_then_weather_elliptic", "第二个")
                        report["checks"] = {
                            "memo_example": named["title"] == "天空之城" and named["autoplay"] and not named["new_model_fixture_calls"],
                            "missing_title": missing["title"] is None and "东京" not in missing["reply"]
                                             and not missing["new_model_fixture_calls"],
                            "fallback_artist_provenance": recommended["title"] == "天空之城" and not recommended["autoplay"]
                                                         and "东京" not in (recommended["query"] or "")
                                                         and recommended["new_model_fixture_calls"] == 1,
                            "play_recommendation": picked["title"] == "天空之城" and picked["autoplay"]
                                                   and "东京" not in (picked["query"] or ""),
                            "no_cross_domain_weather_action": day["city"] is None and day["title"] is None,
                            "no_cross_domain_media_action": ordinal["title"] is None and not ordinal["autoplay"],
                            "named_weather_after_media": next_weather["city"] == "东京" and tomorrow["city"] == "东京"
                                                         and tomorrow["period"] == "tomorrow" and not tomorrow["new_model_fixture_calls"],
                            "music_followup": len(listing["items"]) >= 2 and second["title"] == listing["items"][1]
                                              and second["autoplay"] and not second["new_model_fixture_calls"],
                        }
                        if matrix:
                            cross, elliptical = await expanded(ask, fresh, moss)
                            report["cross_domain"] = cross
                            report["elliptical"] = elliptical
                            report["checks"]["cross_domain_matrix"] = len(cross) == 30 and all(item["ok"] for item in cross)
                            report["checks"]["elliptical_matrix"] = len(elliptical) == 20 and sum(item["ok"] for item in elliptical) >= 19
                        report["passed"] = not report["model_connections"] and all(report["checks"].values())
            finally:
                await runner.cleanup()
                await moss.close()
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report["passed"], report["checks"], flush=True)
    if not observe:
        assert report["passed"], result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--matrix", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.observe, args.matrix))
