import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from time import perf_counter

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_memory import send
from verify_subtitle_search import nas_source


def render_video(ffmpeg, folder, path, tracks):
    args = [str(ffmpeg), "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=12"]
    for index, (line, language) in enumerate(tracks):
        script = folder / f"track-{index}.srt"
        script.write_text("1\n00:00:01,625 --> 00:00:03,500\n" + line + "\n", encoding="utf-8")
        args.extend(["-i", str(script)])
    args.extend(["-map", "0:v"])
    for index, (_, language) in enumerate(tracks):
        args.extend(["-map", str(index+1) + ":s", f"-metadata:s:s:{index}", "language=" + language])
    codec = "mov_text" if path.suffix == ".mp4" else "ass"
    args.extend(["-t", "5", "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    args.extend(["-c:s", codec, str(path)])
    subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8")


async def browser_check(server, folder, media_id):
    await server.start()
    try:
        async with async_playwright() as pw:
            edge = await open_browser(pw)
            try:
                await confirm_muted_edge(edge)
                page = await edge.new_page(reduced_motion="reduce")
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                reply = await send(page, server, "搜台词：磁带已经倒好了")
                assert "磁带已经倒好了" in reply
                turn = (await server.state())["turns"][-1]
                item, = turn["media"]["items"]
                assert item["media_id"] == media_id and turn["media"]["online_calls"] == 0
                await page.get_by_test_id("media-projection").get_by_test_id("track-choice").first.click()
                player = page.locator("#tool-player video")
                await page.wait_for_function("""() => { const v=document.querySelector('#tool-player video');
                   return v && !v.paused && v.currentTime > 1.7 && v.currentTime < 3.5; }""")
                playing = await player.evaluate("v=>v.currentTime")
                await page.wait_for_function("() => document.querySelector('#tool-player video')?.paused")
                stopped = await player.evaluate("v=>v.currentTime")
                assert 3.38 <= stopped <= 3.9, stopped
                current = (await server.state())["turns"][-1]
                assert current["browser_playback"]["media_id"] == media_id
                await page.screenshot(path=folder / "embedded-subtitle.png")
                return {"browser_playing_at_s":playing, "browser_paused_at_s":stopped}
            finally:
                await edge.close()
    finally:
        await server.stop()


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("embedded-subtitles-%Y%m%d-%H%M%S-%f")
    local, remote = folder / "local", folder / "remote"
    local.mkdir(parents=True)
    remote.mkdir()
    ffmpeg = shutil.which("ffmpeg") or next((Path(os.environ["LOCALAPPDATA"]) /
                                          "Microsoft/WinGet/Packages").glob("Gyan.FFmpeg*/**/ffmpeg.exe"))
    main = local / "磁带实验.mp4"
    render_video(ffmpeg, folder, main, [("<i>磁带已经倒好了。</i>", "zho"), ("The tape is ready.", "eng")])
    shutil.copyfile(main, remote / "磁带实验副本.mp4")
    render_video(ffmpeg, folder, remote / "舞台灯光.mkv", [("台上的灯熄灭了。", "zho")])
    index = folder / "catalog.sqlite3"
    os.environ["MOSS_MEDIA_DB"] = str(index)
    os.environ["MOSS_SAMPLE_USER"] = "moss-media-test"
    os.environ["MOSS_SAMPLE_PASSWORD"] = secrets.token_hex(16)
    library = MediaLibrary(index)
    report = {"passed":False}
    try:
        with nas_source(remote, folder, "舞台灯光.mkv") as source:
            library.configure([{"id":"disk", "label":"磁盘", "kind":"local", "path":str(local)},
                               {**source, "label":"回环共享"}])
            started = perf_counter()
            state = library.scan()
            assert all(item["state"] == "ready" for item in state), state
            report["first_scan_ms"] = round((perf_counter() - started)*1000)
            searched = library.search_subtitles("磁带已经倒好了")
            first, = searched["items"]
            cue = first["subtitle"]
            assert first["source"] == "disk" and searched["consulted_sources"] == ["disk"]
            assert cue["text"] == "磁带已经倒好了。" and (cue["start_s"], cue["end_s"]) == (1.625, 3.5), cue
            assert library.subtitle_available(first["media_id"], cue)
            english, = library.search_subtitles("The tape is ready")["items"]
            assert english["media_id"] == first["media_id"] and english["subtitle"]["text"] == "The tape is ready."
            mkv, = library.search_subtitles("台上的灯熄灭了")["items"]
            assert mkv["source"] == "nas" and mkv["subtitle"]["start_s"] == 1.625
            with closing(library.connect()) as db:
                count, = db.execute("SELECT count(*) FROM embedded_subtitle_scans").fetchone()
            assert count == 3
            report.update(disk_mp4=cue, english_track=english["subtitle"], nas_mkv=mkv["subtitle"],
                          embedded_video_count=count)
            server = BrowserServer(folder / "app", 18683, model_url="http://127.0.0.1:9/v1")
            report.update(await browser_check(server, folder, first["media_id"]))
            render_video(ffmpeg, folder, main, [("新的磁带内容。", "zho")])
            assert not library.subtitle_available(first["media_id"], cue)
            old, = library.search_subtitles("磁带已经倒好了")["items"]
            assert old["source"] == "nas"
            assert not library.search_subtitles("新的磁带内容")["items"]
            assert all(item["state"] == "ready" for item in library.scan())
            new, = library.search_subtitles("新的磁带内容")["items"]
            assert new["media_id"] == first["media_id"] and new["source"] == "disk"
            started = perf_counter()
            library.scan()
            report["same_files_scan_ms"] = round((perf_counter() - started)*1000)
            report.update(replaced_video_verified=True, original_queried_from_nas=True, passed=True)
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())
