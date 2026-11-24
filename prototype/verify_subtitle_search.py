import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from unittest.mock import patch
import wave

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from core import Moss
from local_media.library import MediaLibrary
from serve import application
from verify_context_reply import free_port
from verify_media_stream import share
from verify_memory import send


LOCAL = """1
00:00:00,250 --> 00:00:01,500
收到坐标，准备出发。

2
00:00:02,250 --> 00:00:04,500
第二组收到坐标。
"""
NAS = """WEBVTT

00:00.250 --> 00:01.500
收到坐标，准备出发。

00:04.900 --> 00:06.300 align:start
<v 导航>远岸应答。</v>
"""
BOUNDARIES = """1
00:00:06,100 --> 00:00:09,500
片尾校验。

2
01:00:00,013 --> 01:00:00,500
越界校验。
"""


@contextmanager
def nas_source(remote, folder, sample="远岸记录.mp4"):
    unc = Path("//localhost/" + remote.drive[0] + "$").joinpath(*remote.parts[1:])
    if (unc / sample).is_file():
        yield {"id":"nas", "kind":"nas", "path":str(unc)}
    else:
        with share(remote, folder) as instance:
            yield {"id":"nas", "kind":"nas", "path":r"\\127.0.0.1\MEDIA",
                   "smb":{"port":instance["port"],"username_env":"MOSS_SAMPLE_USER",
                          "password_env":"MOSS_SAMPLE_PASSWORD"}}


def fixture(folder):
    local, remote = folder / "local", folder / "remote"
    local.mkdir()
    remote.mkdir()
    master = LOCAL + "\n3\n00:00:04,900 --> 00:00:06,300\n远岸应答。\n"
    (folder / "burn.srt").write_text(master, encoding="utf-8")
    executable = shutil.which("ffmpeg")
    if not executable:
        packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages"
        executable = next(packages.glob("Gyan.FFmpeg*/**/ffmpeg.exe"))
    video = local / "航行记录.mp4"
    subprocess.run([str(executable), "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=navy:s=320x180:r=12", "-vf", "subtitles=burn.srt:force_style='FontName=Microsoft YaHei'",
                    "-t", "7", "-filter_threads", "1", "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(video)], cwd=folder, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    shutil.copyfile(video, remote / "远岸记录.mp4")
    sub = local / "航行记录.zh-Hans.srt"
    sub.write_text(LOCAL, encoding="gb18030")
    (local / "航行记录.en.srt").write_text(BOUNDARIES, encoding="utf-8")
    (remote / "远岸记录.vtt").write_text(NAS, encoding="utf-8-sig")
    return local, remote, sub


async def browser_test(server, report, folder):
    await server.start()
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                page.set_default_timeout(18000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                await page.locator(".connection.online").wait_for()
                for query, prompt, selection, index, start, end, source in [
                    ("收到坐标", "搜台词：收到坐标", "第二段", 2, 2.25, 4.5, "disk"),
                    ("收到坐标", None, "上一段", 1, .25, 1.5, "disk"),
                    ("收到坐标", None, "下一个片段", 2, 2.25, 4.5, "disk"),
                    ("远岸应答", "搜台词：远岸应答", None, 1, 4.9, 6.3, "nas"),
                    ("片尾校验", "哪部电影里说过‘片尾校验’？", None, 1, 6.1, 7, "disk")
                ]:
                    if prompt:
                        reply = await send(page, server, prompt)
                        results = (await server.state())["turns"][-1]
                        items = results["media"]["items"]
                        assert results["source"] == "media" and results["media"]["online_calls"] == 0
                        assert not results["media"]["autoplay"] and query in reply
                    assert items[index-1]["source"] == source
                    panel = page.get_by_test_id("media-projection")
                    choices = panel.get_by_test_id("track-choice")
                    target = choices.nth(index-1)
                    await target.wait_for()
                    assert query in await target.inner_text()
                    if selection:
                        await send(page, server, selection)
                    else:
                        async with page.expect_response(lambda r:r.url.endswith("/api/media-select")) as clicked:
                            await target.click()
                        response = await clicked.value
                        assert response.ok and response.request.post_data_json["index"] == index
                    video = panel.locator("video")
                    await page.wait_for_function("""([lo,hi])=>{const v=document.querySelector('#tool-player video');
                       return v&&!v.paused&&v.currentTime>lo+.06&&v.currentTime<hi}""", arg=[start,end])
                    actual = await video.evaluate("v=>({time:v.currentTime,duration:v.duration,src:v.currentSrc})")
                    await page.wait_for_function("()=>document.querySelector('#tool-player video')?.paused")
                    stopped = await video.evaluate("v=>v.currentTime")
                    assert end-.12 <= stopped <= end+.45, stopped
                    chosen = (await server.state())["turns"][-1]
                    assert chosen["source"] == "media" and not chosen["cloud_requests"]["total"]
                    assert chosen["media"]["selected"]["subtitle"]["start_s"] == start
                    assert chosen["browser_playback"]["event"] == "started"
                    assert chosen["browser_playback"]["media_id"] == items[index-1]["media_id"]
                    assert await panel.locator('[aria-current="true"]').count() == 1
                    report["playback"].append({"query":query,"command":selection,"source":source,"index":index,"start":start,
                                               "end":end,"actual":actual,"paused_at":stopped,
                                               "browser_receipt":chosen["browser_playback"]})
                await page.screenshot(path=folder/"subtitle-match.png")
                reply = await send(page, server, "搜台词：越界校验")
                target = choices.first
                assert "1:00:00.013" in reply and "1:00:00.013" in await target.inner_text()
                await target.click()
                await page.wait_for_function("()=>document.getElementById('projection-transport').textContent.includes('超出')")
                for _ in range(40):
                    last = (await server.state())["turns"][-1]
                    if (last.get("browser_playback") or {}).get("event") == "failed":
                        break
                    await asyncio.sleep(.1)
                assert last["browser_playback"]["event"] == "failed"
                report["out_of_range"] = last["browser_playback"]
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        await server.stop()


async def subtitle_turn(server, endpoint, payload):
    status, state = await server.post(endpoint, payload)
    assert status == 200, (status, state)
    turn = state["turns"][-1]
    async with asyncio.timeout(15):
        while turn["status"] == "thinking":
            await asyncio.sleep(.05)
            turn = (await server.state())["turns"][-1]
    assert turn["status"] == "replied" and turn["source"] == "media", turn
    assert turn["media"]["online_calls"] == 0, turn
    return turn


async def stale_subtitle_cases(server, library, local, sidecar, report):
    async def search(query):
        return await subtitle_turn(server, "/api/message", {"text":f"搜台词：{query}"})

    async def reject_selection(original, item):
        turn = await subtitle_turn(server, "/api/media-select", {
            "chat_id":original["chat_id"], "turn_id":original["id"], "media_id":item["media_id"], "index":1,
        })
        assert "更新媒体索引" in turn["reply"], turn
        media = turn["media"]
        assert not media["autoplay"] and not media.get("playback") and not turn.get("browser_playback"), turn
        return {"reply":turn["reply"], "autoplay":media["autoplay"]}

    await server.start()
    try:
        original = await search("收到坐标")
        old = original["media"]["items"][0]
        assert old["source"] == "disk" and old["subtitle"]["start_s"] == .25, old
        sidecar.write_text(LOCAL.replace("收到坐标", "新的航线").replace("00:00:00,250", "00:00:01,250"),
                           encoding="utf-8")

        found = library.search_subtitles("收到坐标")
        report["edited_sidecar_lookup"] = {"sources":[item["source"] for item in found["items"]],
                                             "unavailable":found["unavailable_sources"]}
        assert found["consulted_sources"] == ["disk","nas"] and [item["source"] for item in found["items"]] == ["nas"], found
        assert any(source["id"] == "disk" and source["state"] == "stale" for source in found["unavailable_sources"]), found
        assert not library.subtitle_available(old["media_id"], old["subtitle"])
        report["edited_sidecar_old_selection"] = await reject_selection(original, old)

        assert all(s["state"] == "ready" for s in library.scan())
        updated = await search("新的航线")
        new = updated["media"]["items"][0]
        assert new["source"] == "disk" and new["subtitle"]["start_s"] == 1.25, new
        legacy = {key:value for key, value in new["subtitle"].items() if key != "version"}
        assert library.subtitle_available(new["media_id"], new["subtitle"])
        assert library.subtitle_available(new["media_id"], legacy)
        report["updated_cue"] = {"source":new["source"], "start_s":new["subtitle"]["start_s"]}

        video = local / "航行记录.mp4"
        replacement = local.parent / "replacement.mp4"
        # Replace the indexed file with a valid MP4 whose size differs by one padding box.
        replacement.write_bytes(video.read_bytes() + b"\x00\x00\x00\x08free")
        replacement.replace(video)

        found = library.search_subtitles("新的航线")
        report["replaced_video_lookup"] = {"items":len(found["items"]), "unavailable":found["unavailable_sources"]}
        assert not found["items"], found
        assert any(source["id"] == "disk" and source["state"] == "stale" for source in found["unavailable_sources"]), found
        assert not library.subtitle_available(new["media_id"], new["subtitle"])
        assert not library.subtitle_available(new["media_id"], legacy)
        report["replaced_video_old_selection"] = await reject_selection(updated, new)
        assert all(s["state"] == "ready" for s in library.scan())
        refreshed = library.search_subtitles("新的航线")["items"]
        assert len(refreshed) == 2 and refreshed[0]["subtitle"]["start_s"] == 1.25, refreshed
        assert library.subtitle_available(refreshed[0]["media_id"], refreshed[0]["subtitle"])
        assert not library.subtitle_available(new["media_id"], new["subtitle"])
        report["old_selection_after_rescan"] = await reject_selection(updated, new)
        sidecar.rename(sidecar.with_name("不匹配影片.zh.srt"))
        library.scan()
        assert not library.search_subtitles("新的航线")["items"]
    finally:
        await server.stop()


async def followup_cases(folder, local, sidecar, report):
    for title in ("隔离曲目一", "隔离曲目二"):
        with wave.open(str(local / f"{title}.wav"), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(64000))
    library = MediaLibrary(folder / "catalog.sqlite3")
    library.configure([{"id":"disk", "kind":"local", "path":str(local)}])
    assert library.scan()[0]["state"] == "ready"
    os.environ.update(MOSS_MEDIA_DB=str(library.db_path), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      MOSS_MEM0_ENABLED="0", MOSS_KEV_ENABLED="0", MOSS_WINDOWS_REMINDERS="0")
    report["followups"] = results = {}
    model_calls = 0

    async def answer_without_tools(**_request):
        nonlocal model_calls
        model_calls += 1
        return "这是隔离对话回执。"

    moss = Moss(folder / "data", "http://127.0.0.1:9/v1", "unused", prefer_cloud=False)
    port = free_port()
    app = application(moss, lambda: "standby", folder / "data", port, "isolation", asyncio.Event())
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        with patch("assistant_brain.run_holo", answer_without_tools):
            async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                async def ask(key, text):
                    before = model_calls
                    async with client.post("/api/message", json={"text":text, "chat_id":moss.chat_id}) as response:
                        state = await response.json()
                        assert response.status == 200, state
                    turn_id = state["turns"][-1]["id"]
                    async with asyncio.timeout(15):
                        while state["turns"][-1]["status"] == "thinking":
                            await asyncio.sleep(.025)
                            async with client.get("/api/state") as response:
                                state = await response.json()
                    turn = state["turns"][-1]
                    assert turn["id"] == turn_id and turn["status"] == "replied", turn
                    media = turn.get("media") or {}
                    selected = media.get("selected") or {}
                    results[key] = {"text":text, "source":turn.get("source"), "reply":turn["reply"],
                                    "autoplay":media.get("autoplay", False), "kind":selected.get("kind"),
                                    "media_id":selected.get("media_id"), "items":len(media.get("items", ())),
                                    "start_s":(media.get("playback") or {}).get("clip_start_s"),
                                    "item_s":(selected.get("subtitle") or {}).get("start_s"),
                                    "model_calls":model_calls-before}
                    assert turn["cloud_requests"]["total"] == 0 and not media.get("online_calls"), turn
                    return results[key]

                for key, text in (
                    ("search", "搜台词：收到坐标"), ("first", "下一段"), ("next", "下一个片段"),
                    ("end", "播放下一段"), ("previous", "上一段"), ("beginning", "上一段"),
                    ("ordinal", "第二段"), ("preview", "预览第一段"), ("question", "第二段是在几秒？"),
                    ("topic_seed", "搜台词：收到坐标"), ("other_topic", "现在几点"),
                    ("topic_stale", "下一个片段"), ("still_stale", "播放第二段"),
                    ("switch_seed", "搜台词：收到坐标"),
                ):
                    await ask(key, text)
                async with client.post("/api/new-chat", json={}) as response:
                    assert response.status == 200
                await ask("other_chat", "播放下一段")
                await ask("file_seed", "搜台词：收到坐标")
                sidecar.write_text(LOCAL.replace("收到坐标", "航行结束"), encoding="utf-8")
                await ask("file_stale", "第二段")
                await ask("audio_seed", "搜索歌曲：隔离曲目")
                await ask("audio", "第二首")
    finally:
        await runner.cleanup()
        await moss.close()

    assert results["search"]["items"] == 2 and not results["search"]["autoplay"], results["search"]
    for key, start in (("first", .25), ("next", 2.25), ("previous", .25), ("ordinal", 2.25)):
        row = results[key]
        assert (row["autoplay"] and row["source"] == "media" and row["kind"] == "video" and
                row["start_s"] == start and not row["model_calls"]), (key, row)
    assert len({results[key]["media_id"] for key in ("first", "next", "previous", "ordinal")}) == 1
    for key, bound in (("end", "最后一段"), ("beginning", "第一段")):
        assert bound in results[key]["reply"] and not results[key]["autoplay"], (key, results[key])
    assert results["preview"]["item_s"] == .25 and not results["preview"]["autoplay"]
    for key in ("question", "topic_stale", "still_stale", "other_chat", "file_stale"):
        assert not results[key]["autoplay"] and results[key]["start_s"] is None, (key, results[key])
    assert "更新媒体索引" in results["file_stale"]["reply"] and not results["file_stale"]["model_calls"]
    assert results["audio"]["kind"] == "audio" and results["audio"]["autoplay"], results["audio"]


async def run(stale_only, followups_only=False):
    folder = ROOT / "runs" / datetime.now().strftime("subtitle-search-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    print(folder, flush=True)
    local, remote, sidecar = fixture(folder)
    if followups_only:
        report = {"passed":False}
        try:
            await followup_cases(folder, local, sidecar, report)
            report["passed"] = True
        finally:
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"])
        return
    os.environ.update(MOSS_SAMPLE_USER="moss-subtitle", MOSS_SAMPLE_PASSWORD=secrets.token_urlsafe(18),
                      MOSS_MEDIA_DB=str(folder/"catalog.sqlite3"), MOSS_KEV_ENABLED="0", MOSS_WINDOWS_REMINDERS="0")
    library = MediaLibrary(folder/"catalog.sqlite3")
    report = {"passed":False, "playback":[]}
    server = BrowserServer(folder, 18871, model_url="http://127.0.0.1:9/v1")
    try:
        with nas_source(remote, folder) as source:
            report["smb_path"] = source["path"]
            library.configure([{"id":"disk","kind":"local","path":str(local)}, source])
            assert all(s["state"]=="unindexed" for s in library.search_subtitles("坐标")["unavailable_sources"])
            assert all(s["state"]=="ready" for s in library.scan())
            found = library.search_subtitles("收到，坐标")
            assert found["consulted_sources"] == ["disk"] and len(found["items"]) == 2
            assert len({v["media_id"] for v in found["items"]}) == 1
            assert library.search_subtitles("远岸")["consulted_sources"] == ["disk","nas"]
            assert not library.search_subtitles("没有这句")["items"]
            old = found["items"][0]
            assert library.subtitle_available(old["media_id"], old["subtitle"])
            if not stale_only:
                await browser_test(server, report, folder)
            await stale_subtitle_cases(server, library, local, sidecar, report)
        moved = remote.rename(folder / "remote-disconnected")
        try:
            offline = library.search_subtitles("远岸")
            assert not offline["items"] and "nas" in [s["id"] for s in offline["unavailable_sources"]]
        finally:
            moved.rename(remote)
        report.update(passed=True, offline=offline["unavailable_sources"], updated_and_renamed=True)
    finally:
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(folder, report["passed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify subtitle index, media selection and clip playback.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stale-only", action="store_true", help="Run the file replacement cases without Edge.")
    mode.add_argument("--followups-only", action="store_true", help="Verify natural subtitle navigation without Edge.")
    args = parser.parse_args()
    asyncio.run(run(args.stale_only, args.followups_only))
