import asyncio
from array import array
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import wave

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from verify_legacy_music import SPECTRUM
from verify_memory import send


def fixtures(folder):
    catalog = folder / "library"
    audio = catalog / "频谱乐手"
    audio.mkdir(parents=True)
    wave_file = audio / "稳定音阶.wav"
    with wave.open(str(wave_file), "wb") as out:
        out.setparams((1, 2, 48000, 0, "NONE", ""))
        second = array("h", (int(6000 * math.sin(i * 2 * math.pi * 440 / 48000)) for i in range(48000)))
        out.writeframes(second.tobytes() * 90)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages"
        ffmpeg = next(packages.glob("Gyan.FFmpeg*/**/ffmpeg.exe"))
    subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=128x96:r=12", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                    "-t", "40", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(catalog / "频谱有声视频.mp4")],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")
    shutil.copyfile(ROOT / "static/conversation-assets/moss-opening.mp4", catalog / "频谱无声视频.mp4")
    db = folder / "library.sqlite3"
    library = MediaLibrary(db)
    library.configure([{"id":"test", "label":"测试媒体", "kind":"local", "path":str(catalog)}])
    library.scan()
    return db


async def capture_player(page, folder, filename, *, kind):
    panel = page.get_by_test_id("media-projection")
    await panel.wait_for(state="visible")
    controls = ["player-toggle", "player-seek", "player-mute", "player-volume"]
    if kind == "video":
        controls.append("player-fullscreen")
    for control in controls:
        await panel.locator(f".{control}").wait_for(state="visible")

    layout = await panel.evaluate("""(panel, controls) => {
      const bounds = panel.getBoundingClientRect();
      const inspected = [panel, ...panel.querySelectorAll('.projection-heading,.projection-body,#tool-player,.player-controls')];
      const widths = inspected.map(el => ({name:el.id || el.className, client:el.clientWidth, scroll:el.scrollWidth}));
      const buttons = controls.map(name => {
        const el = panel.querySelector('.' + name);
        const rect = el.getBoundingClientRect();
        return {name, left:rect.left, right:rect.right, width:rect.width, disabled:el.disabled};
      });
      return {viewport:[innerWidth,innerHeight], pageWidth:document.documentElement.scrollWidth,
              panel:[bounds.left,bounds.right], widths, buttons,
              visualFocusOrder:getComputedStyle(panel).readingFlow === 'flex-visual'};
    }""", controls)
    assert layout["pageWidth"] <= layout["viewport"][0] + 1, layout
    assert layout["panel"][0] >= -1 and layout["panel"][1] <= layout["viewport"][0] + 1, layout
    assert all(item["scroll"] <= item["client"] + 1 for item in layout["widths"]), layout
    assert all(item["width"] > 0 and not item["disabled"] and item["left"] >= layout["panel"][0] - 1
               and item["right"] <= layout["panel"][1] + 1 for item in layout["buttons"]), layout

    await panel.locator("#tools-pin").focus()
    reached = []
    for _ in range(16):
        await page.keyboard.press("Tab")
        focused = await panel.evaluate("""(panel, controls) => {
          const el = document.activeElement;
          if (!panel.contains(el)) return null;
          if (el.closest('.media-choice')) return 'media-choice';
          return controls.find(name => el.classList.contains(name)) || el.id || '';
        }""", controls)
        if focused is None:
            break
        reached.append(focused)
        if set(controls + ["media-choice"]) <= set(reached):
            break
    assert set(controls + ["media-choice"]) <= set(reached), reached
    if layout["visualFocusOrder"]:
        assert reached.index("player-toggle") < reached.index("media-choice"), reached

    for control in ("player-toggle", "player-mute", "player-fullscreen"):
        if control in controls:
            await panel.locator(f".{control}").click(trial=True)
    await page.evaluate("document.activeElement?.blur()")
    await panel.screenshot(path=folder / filename, animations="disabled")
    return {"screenshot": filename, "layout": layout, "keyboard_order": reached,
            "buttons_actionable": [name for name in controls if name not in {"player-seek", "player-volume"}]}


async def test(page, server, folder, result):
    cdp = await page.context.new_cdp_session(page)
    nodes, connections = [], []
    cdp.on("WebAudio.audioNodeCreated", lambda event: nodes.append(event["node"]))
    cdp.on("WebAudio.nodesConnected", lambda event: connections.append(event))
    await cdp.send("WebAudio.enable")
    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
    await page.goto(server.url + "/?chat=1")
    # Read the same real analyser as the stage and the floating player's spectrum.
    await page.evaluate("""async () => {
      const {monitorMediaSamples} = await import('/static/agent-media-signal.js');
      window.signalProbe = {seq:0, clears:0};
      monitorMediaSamples(({media, samples, bins, analyser, dbfs}) => {
        const probe = signalProbe;
        if (!analyser) { probe.clears++; probe.frame=null; return; }
        const peak=bins.indexOf(Math.max(...bins));
        probe.frame={kind:media.localName, id:media.dataset.mediaId, hz:peak*analyser.context.sampleRate/analyser.fftSize,
                     time:media.currentTime, dbfs, sampleCount:samples.length, binCount:bins.length};
        probe.seq++;
      });
    }""")
    await send(page, server, "播放频谱乐手的歌")
    await page.wait_for_function("signalProbe.frame?.kind==='audio' && Math.abs(signalProbe.frame.hz-440)<25")
    audio = page.locator("audio.media-audio").last
    await audio.evaluate("a=>{a.volume=1}")
    await page.wait_for_function("signalProbe.frame?.dbfs>-19 && signalProbe.frame.dbfs<-16")
    result["audio"] = await page.evaluate("signalProbe.frame")
    await page.wait_for_function(SPECTRUM)
    outputs = {node["nodeId"] for node in nodes if node["nodeType"] == "AudioDestination"}
    result["audio_graph"] = {"analysers": sum(node["nodeType"] == "Analyser" for node in nodes),
                             "connections_to_speaker": sum(edge["destinationId"] in outputs for edge in connections)}
    assert result["audio_graph"] == {"analysers": 1, "connections_to_speaker": 0}
    result["player_ui"] = {"audio":await capture_player(page, folder, "桌面音乐投影.png", kind="audio")}
    await audio.evaluate("a=>{a.volume=.5}")
    await page.wait_for_function("signalProbe.frame?.dbfs>-25 && signalProbe.frame.dbfs<-23")
    result["half_volume"] = await page.evaluate("signalProbe.frame")
    await audio.evaluate("a=>a.muted=true")
    await page.wait_for_function("signalProbe.frame?.dbfs<=-95")
    await audio.evaluate("a=>a.muted=false")
    await page.wait_for_function("signalProbe.frame?.dbfs>-25")
    await page.locator("#tools-close").click()
    await page.get_by_test_id("media-projection").wait_for(state="hidden")
    await audio.evaluate("a=>a.play()")
    sequence = await page.evaluate("signalProbe.seq")
    await page.wait_for_function("n=>signalProbe.seq>n+4 && signalProbe.frame?.time>.25", arg=sequence)
    result["closed_panel"] = await page.evaluate("signalProbe.frame")
    await audio.evaluate("a=>a.pause()")
    await page.wait_for_function("signalProbe.frame===null")
    await audio.evaluate("a=>a.play()")
    await page.wait_for_function("signalProbe.frame?.kind==='audio' && Math.abs(signalProbe.frame.hz-440)<25")
    await send(page, server, "播放视频 频谱有声视频")
    await page.wait_for_function("signalProbe.frame?.kind==='video' && Math.abs(signalProbe.frame.hz-880)<25")
    result["video"] = await page.evaluate("signalProbe.frame")
    assert await audio.evaluate("a=>a.paused")
    result["player_ui"]["video"] = await capture_player(page, folder, "桌面视频投影.png", kind="video")
    await send(page, server, "播放视频 频谱无声视频")
    await page.wait_for_function("document.querySelector('#tool-player video')?.currentTime>.25")
    assert await page.evaluate("signalProbe.frame===null || signalProbe.frame.dbfs<=-95")
    result["silent_video"] = await page.evaluate("({time:document.querySelector('#tool-player video').currentTime,frame:signalProbe.frame})")
    await page.set_viewport_size({"width":390, "height":844})
    result["player_ui"]["narrow_video"] = await capture_player(page, folder, "窄屏视频投影.png", kind="video")
    await cdp.detach()


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("media-signal-%Y%m%d-%H%M%S-%f")
    database = fixtures(folder)
    server = BrowserServer(folder, 18792, model_url="http://127.0.0.1:18789/v1")
    old = os.environ.get("MOSS_MEDIA_DB")
    result = {"passed": False}
    try:
        os.environ["MOSS_MEDIA_DB"] = str(database)
        await server.start()
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                await test(page, server, folder, result)
                result["passed"] = True
            finally:
                await browser.close()
    finally:
        if old is None:
            os.environ.pop("MOSS_MEDIA_DB", None)
        else:
            os.environ["MOSS_MEDIA_DB"] = old
        await server.stop()
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, result["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
