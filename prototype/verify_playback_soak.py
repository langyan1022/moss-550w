import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median
import sqlite3
from time import perf_counter
import traceback
from urllib.parse import urlsplit

import psutil
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from local_media.library import DEFAULT_DB
from verify_memory import send


SONGS = (("刘德华", "冰雨"), ("刀郎", "西海情歌"))
TIMING = """
document.addEventListener('submit', e => {
  if (!e.isTrusted || !e.target.matches('#composer, #compose')) return;
  performance.clearResourceTimings();
  const sample = window.soakLast = {start:performance.now(), old:new Set(
    [...document.querySelectorAll('[data-turn-id]')].map(n=>n.dataset.turnId))};
  const observer = new MutationObserver(() => {
    const turn = [...document.querySelectorAll('article[data-turn-id]')].find(n=>!sample.old.has(n.dataset.turnId));
    const node = turn?.querySelector('[data-testid="final"],[data-testid="open-media"]');
    if (!node || !node.getClientRects().length || !node.textContent.trim()) return;
    sample.turn = turn.dataset.turnId;
    requestAnimationFrame(()=>requestAnimationFrame(()=>{sample.visible=performance.now()}));
    observer.disconnect();
  });
  observer.observe(document.body,{childList:true,subtree:true,characterData:true});
}, true);
document.addEventListener('playing',e=>{
  const s=window.soakLast, id=e.target.dataset?.audioTurn;
  if(s && id && !s.old.has(id) && s.playing===undefined) {s.playing=performance.now();s.audioTurn=id}
},true);
window.soakMetrics = id => {
  const s=window.soakLast;
  if(!s?.visible || !s.playing || s.turn!==id || s.audioTurn!==id) return null;
  const entries=performance.getEntriesByType('resource').filter(e=>e.startTime>=s.start);
  const message=entries.find(e=>e.name.endsWith('/api/message')&&e.responseStatus===200);
  const receipt=entries.find(e=>e.name.endsWith('/api/media-event')&&e.responseStatus===200);
  if(!message || !receipt) return null;
  return {submit_to_message_http_ms:message.responseEnd-s.start,
          submit_to_visible_result_ms:s.visible-s.start,submit_to_playing_ms:s.playing-s.start,
          submit_to_started_receipt_http_ms:receipt.responseEnd-s.start};
};
"""
AUDIO_STATE = """id => {
  const all = [...document.querySelectorAll('.media-audio')];
  const node = all.find(n=>n.dataset.audioTurn===id);
  if (!node) return null;
  return {turn:id, media:node.dataset.mediaId, src:node.currentSrc, position:node.currentTime,
    duration:Number.isFinite(node.duration)?node.duration:null, paused:node.paused, ended:node.ended,
    error:node.error?.code??null, rate:node.playbackRate, active:all.filter(n=>!n.paused&&!n.ended).length};
}"""


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stats(values):
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "p50": round(median(ordered), 2),
            "p95": round(ordered[math.ceil(len(ordered)*.95)-1], 2), "max": round(max(ordered), 2)}


def resources(pid):
    root = psutil.Process(pid)
    rss = seconds = processes = 0
    for proc in [root, *root.children(recursive=True)]:
        try:
            with proc.oneshot():
                mem = proc.memory_info().rss
                cpu = proc.cpu_times()
            rss += mem
            seconds += cpu.user + cpu.system
            processes += 1
        except psutil.NoSuchProcess:
            continue
    return {"processes": processes, "summed_rss_mib": round(rss/1048576, 2), "live_cpu_seconds": round(seconds, 2)}


def atomic_report(path, report):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class PlaybackRun:
    def __init__(self, args, folder):
        self.args = args
        self.folder = folder
        self.server = BrowserServer(folder, args.port, model_url="http://127.0.0.1:9/v1")
        self.start = None
        self.timings = defaultdict(list)
        self.player_timings = defaultdict(lambda:defaultdict(list))
        self.http = Counter()
        self.pos_delta = 0.
        self.samples = self.ends = self.switches = self.poll_gaps = 0
        self.rss = defaultdict(list)
        self.high_cpu_since = None
        self.report = {"started_at": stamp(), "status": "starting", "requested_seconds": args.seconds,
                       "scene":args.scene, "edge_rss_limit_mib":args.edge_rss_limit_mib, "scene_requests":[],
                       "min_available_mib":args.min_available_mib, "max_system_cpu_percent":args.max_system_cpu_percent,
                       "switch_seconds": args.switch_seconds, "sample_seconds": args.sample_seconds,
                       "natural_every": args.natural_every, "model_url": self.server.model_url, "page_errors": []}
        self.files = {}

    def record(self, stream, item):
        self.files[stream].write(json.dumps({"at":stamp(), **item}, ensure_ascii=False) + "\n")
        self.files[stream].flush()

    def checkpoint(self):
        self.report.update(wall_seconds=round(perf_counter()-self.start, 2) if self.start else 0,
                           observed_forward_audio_seconds=round(self.pos_delta, 2), poll_samples=self.samples,
                           natural_ends=self.ends, track_switches=self.switches, long_poll_gaps=self.poll_gaps,
                           timings_ms={name:stats(values) for name,values in self.timings.items()},
                           browser_timings_ms={group:{name:stats(values) for name,values in columns.items()}
                                               for group,columns in self.player_timings.items()},
                           http=dict(self.http),
                           rss_mib={name:{"first":v[0],"max":max(v),"last":v[-1]} for name,v in self.rss.items()})
        atomic_report(self.folder/"result.json", self.report)

    def on_response(self, response):
        path = urlsplit(response.url).path
        if path.endswith(("/moss-view.js","/moss-render.js","/earth-view.js",".glb")):
            self.report["scene_requests"].append(path)
        if not (path.startswith("/media/") or path in {"/api/message","/api/media-event","/api/transport-result"}):
            return
        endpoint = "/media/file" if path.startswith("/media/") else path
        key = f"{response.request.method} {endpoint} {response.status}"
        self.http[key] += 1
        payload = response.request.post_data_json if response.request.method == "POST" else {}
        self.record("http", {"path":path, "status":response.status, "event":payload.get("event"),
                             "turn":payload.get("turn_id"), "media":payload.get("media_id")})
        if response.status >= 400:
            self.report["page_errors"].append(key)

    async def read_audio(self):
        return await self.page.evaluate(AUDIO_STATE, self.active_turn)

    async def wait_playing(self):
        async with asyncio.timeout(20):
            while True:
                audio = await self.read_audio()
                if audio and not audio["paused"] and audio["position"] >= .25:
                    assert audio["active"] == 1 and audio["rate"] == 1 and not audio["error"], audio
                    assert audio["src"].endswith("/media/"+self.media_id), audio
                    return audio
                await asyncio.sleep(.08)

    async def select(self, number):
        artist, title = SONGS[number % len(SONGS)]
        command = artist + "的" + title
        before = perf_counter()
        reply = await send(self.page, self.server, command)
        reply_ms = (perf_counter()-before)*1000
        turn = (await self.server.state())["turns"][-1]
        selected = turn["media"]["selected"]
        assert turn["model_id"] == "local" and title in reply, (command, reply)
        assert turn["source"] == "media" or "play_song" in (turn.get("holo_tools") or []), turn["source"]
        assert selected["media_id"] in self.allowed[number % len(SONGS)], selected
        if number:
            assert selected["media_id"] != self.media_id
            self.switches += 1
        self.active_turn, self.media_id = turn["id"], selected["media_id"]
        audio = await self.wait_playing()
        playing_ms = (perf_counter()-before)*1000
        async with asyncio.timeout(8):
            while True:
                receipt = (await self.server.state())["turns"][-1].get("browser_playback") or {}
                if receipt.get("event") == "started" and receipt.get("media_id") == self.media_id:
                    break
                await asyncio.sleep(.05)
        self.timings["track_action_to_visible_reply"].append(reply_ms)
        self.timings["track_action_to_verified_250ms"].append(playing_ms)
        await self.page.wait_for_function("id=>Boolean(window.soakMetrics(id))", arg=turn["id"],timeout=8000)
        browser_times = await self.page.evaluate("id=>window.soakMetrics(id)", turn["id"])
        group = "cold_session" if number == 0 else "warm_session"
        for column, value in browser_times.items():
            self.player_timings[group][column].append(value)
        self.record("actions", {"command":command, "turn":turn["id"], "source":turn["source"],
                                "tools":turn.get("holo_tools"), "reply":reply,
                                "reply_ms":round(reply_ms,2), "started_ms":round(playing_ms,2),
                                "group":group, "browser_timings_ms":browser_times,
                                "source_drive":"G:", "receipt":receipt, "audio":audio})
        return audio

    async def control(self, command, expected):
        before = perf_counter()
        reply = await send(self.page, self.server, command)
        ms = (perf_counter()-before)*1000
        turn = (await self.server.state())["turns"][-1]
        receipt = turn.get("transport") or {}
        assert turn["source"] == "transport" and receipt["event"] == expected, (command, reply, receipt)
        assert receipt["target_turn"] == self.active_turn and receipt["media_id"] == self.media_id, receipt
        if expected == "playing":
            audio = await self.wait_playing()
        else:
            audio = await self.read_audio()
            assert audio["active"] == 0 and audio["paused"], audio
            if expected == "stopped":
                assert audio["position"] < .12, audio
            await asyncio.sleep(1)
            after = await self.read_audio()
            assert after["active"] == 0 and abs(after["position"] - audio["position"]) < .05, after
        self.timings["control_"+expected+"_action_to_visible_reply"].append(ms)
        self.record("actions", {"command":command, "turn":turn["id"], "source":turn["source"],
                                "reply":reply, "reply_ms":round(ms,2), "receipt":receipt, "audio":audio})
        return audio

    async def sample(self, previous, last_tick):
        audio = await self.read_audio()
        position = audio["position"]
        assert audio["media"] == self.media_id and audio["rate"] == 1 and not audio["error"], audio
        assert (audio["ended"] and audio["active"] == 0) or (not audio["paused"] and audio["active"] == 1), audio
        delta = position - previous["position"]
        elapsed = perf_counter()-last_tick
        assert delta >= 0 and (audio["ended"] or delta > .05), (audio, previous, elapsed)
        if elapsed > self.args.sample_seconds*2 + 5:
            self.poll_gaps += 1
        self.pos_delta += delta
        self.samples += 1
        self.record("samples", {"sample":self.samples, "elapsed_s":round(elapsed,2),
                                "advance_s":round(delta,2), **audio})
        return audio

    async def resource_sample(self):
        data = {"web":resources(self.server.pid), "edge":resources(self.edge_pid)}
        data["web_tcp"] = dict(Counter(conn.status for conn in psutil.Process(self.server.pid).net_connections(kind="tcp4")))
        data["available_mib"] = round(psutil.virtual_memory().available / 1048576)
        data["system_cpu_percent"] = psutil.cpu_percent()
        data["dom"] = await self.page.evaluate("""() => {
            const players = [...document.querySelectorAll('.media-audio')];
            return {nodes:document.querySelectorAll('*').length, turns:document.querySelectorAll('.turn').length,
                    audio:players.length, buffered_players:players.filter(p=>p.buffered.length>0).length,
                    playing:players.filter(p=>!p.paused && !p.ended).length};
        }""")
        for name in ("web", "edge"):
            self.rss[name].append(data[name]["summed_rss_mib"])
        self.record("resources", data)
        if self.args.edge_rss_limit_mib and data["edge"]["summed_rss_mib"] > self.args.edge_rss_limit_mib:
            raise RuntimeError(f"独立 Edge 内存达到 {data['edge']['summed_rss_mib']} MiB，本轮已停止。")
        if self.args.min_available_mib and data["available_mib"] < self.args.min_available_mib:
            raise RuntimeError(f"系统可用内存降至 {data['available_mib']} MiB，本轮已停止。")
        if self.args.max_system_cpu_percent and data["system_cpu_percent"] > self.args.max_system_cpu_percent:
            now = perf_counter()
            self.high_cpu_since = self.high_cpu_since or now
            if now - self.high_cpu_since >= 60:
                raise RuntimeError("系统 CPU 持续一分钟超过本轮压力上限，本轮已停止。")
        else:
            self.high_cpu_since = None
        if self.args.scene == "light" and not self.report.get("loading_scene"):
            assert not self.report["scene_requests"] and not self.page.workers, self.report["scene_requests"]

    async def load_scene_while_playing(self):
        audio = await self.read_audio()
        start = await self.page.evaluate("performance.timeOrigin")
        await self.page.locator(".preferences summary").click()
        self.report["loading_scene"] = True
        await self.page.locator("#scene-mode-link").click()
        async with asyncio.timeout(90):
            while not await self.page.locator("#moss-model[data-ready='true']").count():
                await self.resource_sample()
                await asyncio.sleep(2)
        after = await self.read_audio()
        assert (await self.page.evaluate("performance.timeOrigin") == start and audio["src"] == after["src"]
                and after["turn"] == audio["turn"] and after["position"] > audio["position"] and not after["paused"])
        assert self.page.workers and any(path.endswith("/moss-view.js") for path in self.report["scene_requests"])
        renderer = await self.page.locator("#moss-model").get_attribute("data-rendering")
        self.report["scene_during_playback"] = {"same_document":True, "same_track":True,
                                                "from_seconds":audio["position"], "to_seconds":after["position"],
                                                "model_ready":True, "rendering":renderer}
        observation_start = tick = perf_counter()
        observe_until = observation_start + self.args.scene_observe_seconds
        while tick < observe_until and not after["ended"]:
            await asyncio.sleep(min(self.args.sample_seconds, observe_until - tick))
            after = await self.sample(after, tick)
            tick = perf_counter()
            await self.resource_sample()
        self.report["scene_during_playback"].update(observed_seconds=round(tick - observation_start, 2),
                                                    position_after_observing=after["position"])

    async def check_parked_track(self, old):
        selector = f'.media-audio[data-audio-turn="{old["turn"]}"]'
        await self.page.wait_for_function("""s => {
            const node = document.querySelector(s);
            return node?.paused && !node.buffered.length && node.readyState === 0;
        }""", arg=selector, timeout=6000)
        saved = old["position"]
        await self.page.locator(f'article[data-turn-id="{old["turn"]}"]').get_by_test_id("open-media").click()
        await self.page.wait_for_function("""([s,seconds]) => {
            const node = document.querySelector(s);
            return node?.paused && node.readyState >= 1 && node.currentTime >= seconds - .15;
        }""", arg=[selector,saved], timeout=10000)
        restored = await self.page.locator(selector).evaluate("n => n.currentTime")
        assert (await self.read_audio())["active"] == 1
        await self.page.locator(f'article[data-turn-id="{self.active_turn}"]').get_by_test_id("open-media").click()
        self.report["reopened_history"] = {"seconds_before_switch":saved, "seconds_after_reopen":restored,
                                           "released_buffer":True, "current_song_playing":True}

    async def exercise(self, browser):
        cdp = await browser.new_browser_cdp_session()
        try:
            info = await cdp.send("SystemInfo.getProcessInfo")
        finally:
            await cdp.detach()
        self.edge_pid = next(proc["id"] for proc in info["processInfo"] if proc["type"] == "browser")
        self.page = await browser.new_page(viewport={"width":1280,"height":800},reduced_motion="reduce")
        await self.page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
        await self.page.add_init_script(TIMING)
        self.page.on("pageerror", lambda e: self.report["page_errors"].append(str(e)))
        self.page.on("response", self.on_response)
        path = "/?chat=1&scene=off" if self.args.scene == "light" else "/?chat=1"
        await self.page.goto(self.server.url + path, wait_until="domcontentloaded")
        await self.page.locator(".connection.online").wait_for()
        if self.args.scene == "light":
            await self.page.locator("main[data-light-scene]").wait_for()
            assert await self.page.locator("#scene-mode-link").get_attribute("href") == "/?chat=1"
            assert not await self.page.locator(".model-loading").is_visible()
        assert (await self.server.state())["model_id"] == "local"
        self.report.update(status="running", edge_pid=self.edge_pid, server_pid=self.server.pid,
                           browser_version=browser.version, browser_muted=True, speech_enabled=False)
        await self.resource_sample()
        self.start = perf_counter()
        number = 0
        while perf_counter()-self.start < self.args.seconds:
            old = await self.read_audio() if number == 1 else None
            previous = await self.select(number)
            if old and not old["ended"]:
                await self.check_parked_track(old)
            if number == 0 and self.args.scene == "light":
                await self.page.wait_for_selector(".stage-audio-lane[data-signal='music']", timeout=8000)
                await self.page.screenshot(path=self.folder/"light-scene-playing.png", timeout=5000)
            if number % 3 == 1:
                await self.control("暂停音乐", "paused")
                previous = await self.control("继续播放音乐", "playing")
            if number % 5 == 2:
                await self.control("停止播放音乐", "stopped")
                previous = await self.control("继续播放音乐", "playing")
            self.checkpoint()
            natural = self.args.natural_every and (number+1) % self.args.natural_every == 0
            track_start = tick = perf_counter()
            while perf_counter()-self.start < self.args.seconds:
                await asyncio.sleep(self.args.sample_seconds)
                previous = await self.sample(previous, tick)
                tick = perf_counter()
                if previous["ended"]:
                    self.ends += 1
                    self.record("actions", {"natural_end":True,"audio":previous})
                    break
                assert not self.report["page_errors"], self.report["page_errors"]
                if self.samples % 6 == 0:
                    await self.resource_sample()
                    self.checkpoint()
                if not natural and tick-track_start >= self.args.switch_seconds:
                    break
            number += 1
        if previous["ended"]:
            await self.select(number)
        if self.args.load_scene_at_end:
            await self.load_scene_while_playing()
        await self.control("停止播放音乐", "stopped")
        await self.resource_sample()
        assert not self.report["page_errors"]
        assert self.samples and self.http["POST /api/media-event 200"] and self.http["POST /api/transport-result 200"]
        assert self.http["GET /media/file 206"] or self.http["GET /media/file 200"]
        self.report.update(status="passed", finished_at=stamp())

    async def run(self):
        self.folder.mkdir(parents=True, exist_ok=False)
        print(self.folder, flush=True)
        self.report["source_sha256"] = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ("verify_playback_soak.py","media_chat.py","media_transport.py","static/app.js","static/agent-transport.js",
             "static/conversation.html","static/agent-terminal.js","static/agent-terminal.css","static/agent-projection.js")}
        with closing(sqlite3.connect(DEFAULT_DB.as_uri()+"?mode=ro", uri=True)) as src, closing(sqlite3.connect(self.folder/"catalog.sqlite3")) as dest:
            src.backup(dest)
            self.allowed = []
            for artist, title in SONGS:
                found = dest.execute("SELECT media_id,artist,path FROM media WHERE kind='audio' AND title IN (?,?)",
                                     (title, title+"(Live)")).fetchall()
                ids = {key for key,credit,path in found if Path(path).drive.upper()=="G:" and Path(path).is_file()
                       and (artist==credit or artist in Path(path).parts)}
                assert ids, (artist,title)
                self.allowed.append(ids)
        os.environ["MOSS_MEDIA_DB"] = str(self.folder/"catalog.sqlite3")
        os.environ["MOSS_KEV_ENABLED"] = "0"
        for stream in ("http","actions","samples","resources"):
            self.files[stream] = (self.folder/(stream+".jsonl")).open("x",encoding="utf-8")
        self.checkpoint()
        try:
            await self.server.start()
            async with async_playwright() as pw:
                browser = await open_browser(pw)
                try:
                    await confirm_muted_edge(browser)
                    await self.exercise(browser)
                except Exception:
                    if getattr(self, "page", None) and not self.page.is_closed():
                        await self.page.screenshot(path=self.folder/"failure.png", timeout=5000)
                        self.report["failure_screen"] = (await self.page.locator("body").inner_text())[-1600:]
                    raise
                finally:
                    await browser.close()
        except BaseException:
            self.report.update(status="failed", error=traceback.format_exc(), finished_at=stamp())
            raise
        finally:
            self.checkpoint()
            await self.server.stop()
            for file in self.files.values():
                file.close()
        print(self.folder, self.report["status"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=7200)
    parser.add_argument("--scene", choices=("light","full"), default="light")
    parser.add_argument("--load-scene-at-end", action="store_true", help="在最后一曲播放中载入三维终端，验收页面和播放进度")
    parser.add_argument("--scene-observe-seconds", type=float, default=0, help="三维载入后继续观测播放和进程内存的秒数")
    parser.add_argument("--edge-rss-limit-mib", type=int, help="独立 Edge 进程树工作集之和的中止阈值")
    parser.add_argument("--min-available-mib", type=int, help="系统可用内存的中止阈值")
    parser.add_argument("--max-system-cpu-percent", type=float, help="持续一分钟的系统 CPU 占用中止阈值")
    parser.add_argument("--switch-seconds", type=float, default=95)
    parser.add_argument("--sample-seconds", type=float, default=5)
    parser.add_argument("--natural-every", type=int, default=5)
    parser.add_argument("--port", type=int, default=18976)
    args = parser.parse_args()
    if args.load_scene_at_end and args.scene != "light":
        parser.error("载入三维场景需要从轻量对话开始")
    if args.scene_observe_seconds < 0 or (args.scene_observe_seconds and not args.load_scene_at_end):
        parser.error("三维观测时长须为非负数，启用时需要 --load-scene-at-end")
    if min(args.seconds,args.switch_seconds,args.sample_seconds) <= 0 or args.natural_every < 0:
        parser.error("运行、切歌和采样时长必须为正数，自然结束的间隔不能为负数")
    run_folder = ROOT/"runs"/datetime.now().strftime("playback-soak-%Y%m%d-%H%M%S-%f")
    asyncio.run(PlaybackRun(args,run_folder).run())
