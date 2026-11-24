import {createStageAudio} from "/conversation-assets/stage-audio.js?v=fui-signal";
import {createWaveTrace, drawCommunicationWave, sampleTrace} from "/conversation-assets/communication-wave.js";

export function createAudioMonitor(stageCanvas, viewport, reduced) {
  const charts = ["input", "output"].map(name => {
    const canvas = document.getElementById(name + "-fft");
    const chart = {canvas, gfx:canvas.getContext("2d"), trace:createWaveTrace(),
      reading:document.getElementById(name + "-db"), source:null};
    new ResizeObserver(([{contentRect}]) => {
      const scale = Math.min(devicePixelRatio, 2);
      canvas.width = Math.round(contentRect.width * scale);
      canvas.height = Math.round(contentRect.height * scale);
    }).observe(canvas);
    return chart;
  });
  const drawStage = createStageAudio(stageCanvas, viewport);
  const lane = stageCanvas.closest(".stage-audio-lane");
  const modeLabel = document.getElementById("stage-signal-mode");
  const sourceLabel = document.getElementById("stage-signal-source");
  const media = {trace:createWaveTrace(), source:null};
  const stageChannels = [charts[0], charts[1]];
  const sourceNames = {idle:"待机", mic:"麦克风", voice:"MOSS 播报", music:"音乐", video:"视频"};
  const buttons = [...document.querySelectorAll('input[name="monitor-mode"]')];
  const color = [];
  function theme() {
    const style = getComputedStyle(document.documentElement);
    color[0] = style.getPropertyValue("--accent2").trim();
    color[1] = style.getPropertyValue("--signal").trim();
  }
  theme();
  window.addEventListener("moss-theme", theme);
  function select(mode) {
    stageCanvas.dataset.mode = mode;
    modeLabel.textContent = mode === "waveform" ? "通信波形" : "实时频谱";
    buttons.forEach(input => { input.checked = input.value === mode; });
    media.trace.source = null;
    media.trace.peaks.fill(0);
    charts.forEach((chart, index) => {
      chart.trace.source = null;
      chart.trace.peaks.fill(0);
      chart.canvas.setAttribute("aria-label", (index ? "MOSS" : "麦克风") + (mode === "waveform" ? "通信波形" : "实时频谱"));
    });
  }
  const preference = "moss.conversation.monitorMode";
  select(localStorage.getItem(preference) === "waveform" ? "waveform" : "spectrum");
  for (const button of buttons) button.addEventListener("change", () => {
    select(button.value);
    localStorage.setItem(preference, button.value);
  });
  function renderStage() {
    const input = charts[0], voice = charts[1];
    const playing = media.playing;
    const muted = playing && (playing.muted || playing.volume === 0);
    const mediaSource = !muted && media.element === playing ? media.source : null;
    const kind = playing?.tagName === "VIDEO" ? "video" : "music";
    const output = voice.source || mediaSource;
    const active = voice.source ? "voice" : mediaSource ? kind : input.source ? "mic" :
      muted ? "muted" : playing ? "unavailable" : "idle";
    const label = sourceNames[active] || sourceNames[kind] + (muted ? "已静音" : "频谱不可用");
    if (active !== lane.dataset.signal) lane.dataset.signal = active;
    if (sourceLabel.textContent !== label) sourceLabel.textContent = label;
    stageChannels[1] = voice.source ? voice : media;
    drawStage(performance.now(), stageCanvas.dataset.mode, stageChannels,
              input.source, output, color[0], color[1], reduced.matches);
  }
  function sample(index, samples, bins, source, dbfs) {
    const chart = charts[index];
    const {canvas, gfx, reading} = chart;
    Object.assign(chart, {samples, bins, source, dbfs});
    const mode = stageCanvas.dataset.mode;
    const scale = Math.min(devicePixelRatio, 2);
    gfx.clearRect(0, 0, canvas.width, canvas.height);
    reading.hidden = !source;
    if (source) {
      const label = Math.round(dbfs) + " dBFS";
      if (reading.textContent !== label) reading.textContent = label;
    }
    if (mode === "waveform") {
      drawCommunicationWave(chart, source, color[index], scale);
    } else if (source) {
      const count = Math.min(88, Math.max(24, Math.floor(canvas.clientWidth / 4)));
      const binHz = source.context.sampleRate / (2 * bins.length);
      const pitch = canvas.width / count;
      gfx.fillStyle = color[index];
      for (let i = 0; i < count; i++) {
        const first = Math.min(bins.length - 1, Math.floor(80 * 100 ** (i / count) / binHz));
        const end = Math.min(bins.length, Math.max(first + 1, Math.floor(80 * 100 ** ((i + 1) / count) / binHz)));
        let peak = bins[first];
        for (let j = first + 1; j < end; j++) peak = Math.max(peak, bins[j]);
        const amplitude = peak / 255 * canvas.height;
        gfx.fillRect(i * pitch, canvas.height - amplitude, Math.max(1, pitch - scale), amplitude);
      }
    }
    renderStage();
  }
  function sampleMedia(frame) {
    if (!frame.analyser && media.element !== frame.media) return;
    Object.assign(media, {element:frame.analyser ? frame.media : null, source:frame.analyser,
      samples:frame.samples, bins:frame.bins, dbfs:frame.dbfs});
    if (media.source && stageCanvas.dataset.mode === "waveform") sampleTrace(media.trace, media.source, media.samples);
    if (!charts[1].source) renderStage();
  }
  function setMediaPlayback(element) {
    media.playing = element;
    renderStage();
  }
  return {sample, sampleMedia, setMediaPlayback};
}
