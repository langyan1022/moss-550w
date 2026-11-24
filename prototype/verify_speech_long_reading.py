import asyncio
from datetime import datetime
import json
from statistics import median

import psutil
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_context_reply import free_port
from verify_speech_prepare_ab import free_gpu, other_speaker


TEXT = (ROOT / "voice/long-reading-sample.txt").read_text(encoding="utf-8").strip()
PROMPT = "请朗读以下文字：" + TEXT
PROBE = """
const trace = {submitted:null, clips:[], events:[]};
const buffers = new Map();
document.addEventListener('submit', event => {
  if (event.target.id === 'composer') trace.submitted = performance.now();
}, true);
for (const type of ['pending', 'start', 'end', 'error']) {
  window.addEventListener('moss-speech-' + type, ({detail}) => {
    trace.events.push({type, ...detail, at:performance.now()});
  });
}
const originalStart = AudioBufferSourceNode.prototype.start;
const originalStop = AudioBufferSourceNode.prototype.stop;
AudioBufferSourceNode.prototype.start = function(when=0, offset=0, duration) {
  const context = this.context;
  const start = Math.max(when, context.currentTime);
  const scheduled = performance.now();
  const output = context.getOutputTimestamp();
  originalStart.apply(this, arguments);
  const span = duration ?? this.buffer.duration - offset;
  trace.clips.push({start, scheduled, output, offset, end:start+span, stops:[]});
  buffers.set(this, trace.clips.at(-1));
};
AudioBufferSourceNode.prototype.stop = function(when=0) {
  buffers.get(this)?.stops.push(Math.max(when, this.context.currentTime));
  return originalStop.apply(this, arguments);
};
window.collectReadingMetrics = () => {
  for (const [source, clip] of buffers) {
    const audio = source.buffer, rate = audio.sampleRate, frames = Math.round(rate / 100);
    const channels = Array.from({length:audio.numberOfChannels}, (_, n) => audio.getChannelData(n));
    if (clip.stops.length) clip.end = Math.min(clip.end, clip.stops.at(-1));
    const begin = Math.round(clip.offset * rate);
    const finish = Math.min(audio.length, begin + Math.round((clip.end - clip.start) * rate));
    clip.first = null;
    clip.last = null;
    for (let i=begin; i+frames<=finish; i+=frames) {
      if (!channels.some(channel => {
        let squares = 0;
        for (let n=i; n<i+frames; n++) squares += channel[n] ** 2;
        return squares / frames >= 10 ** (-45 / 10);
      })) continue;
      clip.first ??= clip.start + (i-begin) / rate;
      clip.last = clip.start + (i+frames-begin) / rate;
    }
  }
  return trace;
};
window.readingFinished = () => trace.events.some(row => ['end', 'error'].includes(row.type));
"""


async def measure(page, server, report):
    speech_responses = []
    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    page.on("response", lambda item: speech_responses.append(item) if item.url in {
        server.url + "/api/speak", server.url + "/api/speak-batch"} else None)
    await page.add_init_script(PROBE)
    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.get_by_test_id("composer").fill(PROMPT)
    await page.get_by_test_id("send").click()
    await page.wait_for_function("readingFinished()", timeout=185000)
    trace = await page.evaluate("collectReadingMetrics()")
    report["trace"] = trace
    assert trace["submitted"] is not None and trace["events"][-1]["type"] == "end", trace["events"]
    turn, = (await server.state())["turns"]
    assert turn["source"] == "narration" and turn["reply"] == TEXT
    assert not report["page_errors"] and all(response.ok for response in speech_responses)
    report["responses"] = [{"path": row.url.removeprefix(server.url), "request": row.request.post_data_json,
                            "timing_ms": row.request.timing} for row in speech_responses]
    clips = trace["clips"]
    starts = [row for row in trace["events"] if row["type"] == "start"]
    assert len(clips) == len(starts) >= 3 and all(row["first"] is not None for row in clips)
    assert "".join("".join(row["text"].split()) for row in starts) == "".join(TEXT.split())
    first = clips[0]
    output = first["output"]
    # 时钟从音频输出设备返回。静音浏览器记录调度时间和有效音频，无实体麦克风采样。
    if output["performanceTime"] > 0 and output["contextTime"] <= first["start"]:
        voice_ms = (first["first"] - output["contextTime"]) * 1000 + output["performanceTime"]
        clock = "web_audio_output_timestamp"
    else:
        voice_ms = (first["first"] - first["start"]) * 1000 + first["scheduled"]
        clock = "web_audio_schedule_estimate"
    report.update(first_active_ms=round(voice_ms - trace["submitted"], 1), first_active_clock=clock,
                  schedule_gaps_ms=[round(1000 * (b["start"] - a["end"]), 1) for a, b in zip(clips, clips[1:])],
                  voice_gaps_ms=[round(1000 * (b["first"] - a["last"]), 1) for a, b in zip(clips, clips[1:])],
                  play_to_end_ms=round(trace["events"][-1]["at"] - trace["submitted"], 1))


async def main():
    available_mib = psutil.virtual_memory().available // 2**20
    gpu_mib = free_gpu()
    if available_mib < 8192 or gpu_mib < 3800 or other_speaker():
        raise RuntimeError(f"朗读环境仍有其他语音进程或资源不足。可用内存 {available_mib} MiB，显存 {gpu_mib} MiB。")
    folder = ROOT / "runs" / datetime.now().strftime("speech-long-reading-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "text": TEXT, "page_errors": [], "start_free_gpu_mib": gpu_mib,
              "min_ram_available_mib": available_mib, "voice_worker_peak_rss_mib": 0, "voice_worker_seen": False}
    print(folder, flush=True)

    async def watch(task):
        while not task.done():
            free = psutil.virtual_memory().available // 2**20
            report["min_ram_available_mib"] = min(free, report["min_ram_available_mib"])
            for process in psutil.Process(server.pid).children(recursive=True):
                try:
                    if any(a.endswith("managed_worker.py") for a in process.cmdline()):
                        report["voice_worker_seen"] = True
                        peak = process.memory_info().peak_wset // 2**20
                        report["voice_worker_peak_rss_mib"] = max(peak, report["voice_worker_peak_rss_mib"])
                except psutil.NoSuchProcess:
                    continue
            if free < 4608:
                report["stop_reason"] = "可用内存不足 4.5 GiB"
                task.cancel()
                return
            await asyncio.sleep(.35)

    try:
        await server.start()
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                job = asyncio.create_task(measure(page, server, report))
                async with asyncio.TaskGroup() as group:
                    group.create_task(watch(job))
                    await job
            finally:
                await browser.close()
        assert report["voice_worker_seen"] and report["voice_worker_peak_rss_mib"] >= 256, "未观测到真正运行语音推理的进程"
        report["median_voice_gap_ms"] = median(report["voice_gaps_ms"])
        report["passed"] = True
    finally:
        try:
            await server.stop()
        finally:
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
