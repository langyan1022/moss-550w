const taps = new WeakMap();

function createAnalysisContext() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (typeof AudioContext.prototype.setSinkId === "function") {
    try {
      // A silent graph with a fixed sample rate avoids opening or querying the output device.
      return new AudioContext({sampleRate: 48000, sinkId: {type: "none"}});
    } catch { /* The browser may restrict audio sink selection. */ }
  }
  return new AudioContext();
}

// Each media element keeps its native speaker output. Only the captured copy feeds Web Audio.
function createTap(media) {
  const listeners = new Set();
  const lifecycle = new AbortController();
  const options = {signal: lifecycle.signal};
  let context, stream, source, gain, analyser, frame;
  let samples, bins, next = 0, started = false;

  function publish(dbfs) {
    const value = {media, samples, bins, analyser, dbfs};
    for (const listener of listeners) listener(value);
  }
  function stop() {
    const hadSignal = Boolean(analyser);
    cancelAnimationFrame(frame);
    source?.disconnect();
    gain?.disconnect();
    analyser?.disconnect();
    stream?.removeEventListener("addtrack", sync);
    stream?.getTracks().forEach(track => track.stop());
    if (context && context.state !== "closed") context.close().catch(() => {});
    context = stream = source = gain = analyser = samples = bins = null;
    next = 0;
    if (hadSignal) publish(-Infinity);
  }
  function tick(time) {
    frame = requestAnimationFrame(tick);
    if (time < next || context.state !== "running") return;
    next = time + 32;
    analyser.getFloatTimeDomainData(samples);
    analyser.getByteFrequencyData(bins);
    let power = 0;
    for (const value of samples) power += value * value;
    publish(10 * Math.log10(Math.max(1e-10, power / samples.length)));
  }
  function volume() {
    if (gain) gain.gain.value = media.volume;
  }
  function resume() {
    if (context?.state === "suspended") context.resume().catch(() => {});
  }
  function sync() {
    if (!listeners.size || !started || media.paused || media.ended || media.muted || media.volume === 0 || document.hidden) {
      stop();
      return;
    }
    if (context) { resume(); return; }
    try {
      if (!stream) {
        stream = (media.captureStream || media.mozCaptureStream).call(media);
        stream.addEventListener("addtrack", sync, {once: true});
      }
      const track = stream.getAudioTracks().find(track => track.readyState === "live");
      if (!track) return;
      context = createAnalysisContext();
      source = context.createMediaStreamSource(new MediaStream([track]));
      gain = context.createGain();
      volume();
      analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      analyser.minDecibels = -84;
      analyser.maxDecibels = -22;
      analyser.smoothingTimeConstant = .65;
      source.connect(gain).connect(analyser);
      samples = new Float32Array(analyser.fftSize);
      bins = new Uint8Array(analyser.frequencyBinCount);
      resume();
      frame = requestAnimationFrame(tick);
    } catch { stop(); }
  }
  started = !media.paused && !media.ended && media.readyState >= 3;
  media.addEventListener("playing", () => { started = true; sync(); }, options);
  for (const event of ["pause", "ended", "emptied", "error", "waiting"]) {
    media.addEventListener(event, () => { started = false; stop(); }, options);
  }
  media.addEventListener("volumechange", () => { volume(); sync(); }, options);
  document.addEventListener("visibilitychange", sync, options);
  document.addEventListener("pointerdown", resume, {capture:true, ...options});
  document.addEventListener("keydown", resume, {capture:true, ...options});
  return {listeners, sync, dispose() { lifecycle.abort(); stop(); }};
}

export function sampleMedia(media, listener) {
  if (!(window.AudioContext || window.webkitAudioContext) || !(media.captureStream || media.mozCaptureStream)) return () => {};
  let tap = taps.get(media);
  if (!tap) { tap = createTap(media); taps.set(media, tap); }
  tap.listeners.add(listener);
  tap.sync();
  return () => {
    if (!tap.listeners.delete(listener) || tap.listeners.size) return;
    taps.delete(media);
    tap.dispose();
  };
}

export function monitorMediaSamples(onFrame, onPlayback) {
  let owner, release, lifecycle;
  function clear() {
    if (!owner) return;
    const previous = owner;
    owner = null;
    lifecycle.abort();
    release?.();
    release = null;
    onFrame({media:previous, analyser:null, samples:null, bins:null, dbfs:-Infinity});
    onPlayback(null);
  }
  function playing(event) {
    const media = event.target;
    if (!(media instanceof HTMLMediaElement) || !media.isConnected || !media.classList.contains("media-audio") ||
        media.paused || media.ended || media === owner) return;
    clear();
    owner = media;
    lifecycle = new AbortController();
    const options = {signal:lifecycle.signal};
    for (const name of ["pause", "ended", "emptied", "error", "waiting"])
      media.addEventListener(name, clear, options);
    media.addEventListener("volumechange", () => onPlayback(media), options);
    onPlayback(media);
    release = sampleMedia(media, value => {
      if (owner !== media) return;
      if (media.isConnected) onFrame(value);
      else clear();
    });
  }
  document.addEventListener("playing", playing, true);
  const active = [...document.querySelectorAll(".media-audio")].find(media => !media.paused && media.readyState >= 3);
  if (active) playing({target:active});
  return () => { document.removeEventListener("playing", playing, true); clear(); };
}
