import {sampleMedia} from "/static/agent-media-signal.js?v=fui-signal";

const corsAudioHosts = new Set(['audio-ssl.itunes.apple.com', 'prod-1.storage.jamendo.com']);

export function configureMusicAnalysis(media, value) {
  if (!(window.AudioContext || window.webkitAudioContext) ||
      !(media.captureStream || media.mozCaptureStream)) return false;
  let url;
  try { url = new URL(value, document.baseURI); } catch { return false; }
  if (url.origin === location.origin) return true;
  if (url.protocol !== 'https:' || !corsAudioHosts.has(url.hostname)) return false;
  media.crossOrigin = 'anonymous';
  return true;
}

export function attachMusicSpectrum(media, canvas) {
  const brush = canvas.getContext('2d');
  if (!brush) { canvas.hidden = true; return () => {}; }
  const listeners = new AbortController();
  const signal = listeners.signal;
  let release;
  let visible = true, disabled = false, wanted = false;
  let barColor, peakColor;

  function colors() {
    const theme = getComputedStyle(canvas);
    barColor = theme.getPropertyValue('--edge').trim() || '#278b94';
    peakColor = theme.getPropertyValue('--tick').trim() || '#8edee5';
  }
  function clear() {
    brush.setTransform(1, 0, 0, 1, 0, 0);
    brush.clearRect(0, 0, canvas.width, canvas.height);
  }
  function stop() {
    release?.();
    release = null;
    clear();
  }
  function paint({analyser, bins: levels}) {
    if (!analyser) { clear(); return; }
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    const pixels = Math.round(width * dpr), rows = Math.round(height * dpr);
    if (canvas.width !== pixels || canvas.height !== rows) {
      canvas.width = pixels;
      canvas.height = rows;
    }
    clear();
    brush.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.max(1, Math.min(64, Math.floor(width / 6)));
    const step = width / count, barWidth = Math.max(1, Math.floor(step) - 2);
    const binHz = analyser.context.sampleRate / analyser.fftSize;
    const topHz = Math.min(15000, analyser.context.sampleRate / 2);
    for (let bar = 0; bar < count; bar++) {
      const low = Math.floor(45 * (topHz / 45) ** (bar / count) / binHz);
      const end = Math.min(levels.length, Math.max(low + 1,
        Math.floor(45 * (topHz / 45) ** ((bar + 1) / count) / binHz)));
      let peak = 0;
      for (let bin = low; bin < end; bin++) peak = Math.max(peak, levels[bin]);
      if (!peak) continue;
      const amplitude = Math.ceil(peak / 255 * (height - 2));
      const x = Math.round(bar * step), y = height - amplitude;
      brush.fillStyle = barColor;
      brush.fillRect(x, y, barWidth, amplitude);
      brush.fillStyle = peakColor;
      brush.fillRect(x, y, barWidth, Math.min(2, amplitude));
    }
    for (let y = height - 4; y > 0; y -= 4) brush.clearRect(0, y, width, 1);
  }
  function sync() {
    if (disabled || !visible || document.hidden || media.paused || media.ended) { stop(); return; }
    if (!release) release = sampleMedia(media, paint);
  }

  colors();
  media.addEventListener('play', () => { wanted = true; }, {signal});
  media.addEventListener('playing', sync, {signal});
  for (const event of ['pause', 'ended', 'emptied']) {
    media.addEventListener(event, () => { wanted = false; stop(); }, {signal});
  }
  media.addEventListener('error', event => {
    stop();
    // A regional CDN may omit CORS. Retry the same preview once through native playback.
    if (disabled || media.crossOrigin !== 'anonymous' || !media.getAttribute('src')) return;
    disabled = true;
    canvas.hidden = true;
    event.stopImmediatePropagation();
    const shouldPlay = wanted;
    media.removeAttribute('crossorigin');
    media.load();
    if (shouldPlay) media.play().catch(() => {});
  }, {capture:true, signal});
  document.addEventListener('visibilitychange', sync, {signal});
  window.addEventListener('moss-theme', colors, {signal});
  const observer = new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    sync();
  });
  observer.observe(canvas);
  if (media.readyState >= 3) sync();
  return () => { disabled = true; listeners.abort(); observer.disconnect(); stop(); };
}
