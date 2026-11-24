/**
 * Wandering Earth 2 Robot Dog BenBen (笨笨) — Smart Context-Aware Animated OLED Dot-Matrix Faceplate.
 * Renders a crisp 44x22 discrete LED/OLED dot-matrix grid with 12 animated expressions driven
 * automatically by live MOSS runtime state (mic, thinking, speaking, music, hardware alerts,
 * time-of-day) and conversation sentiment, plus interactive click-to-pet / expression preview.
 */
import { t } from './i18n.js';

const COLS = 44;
const ROWS = 22;

const OLED_PALETTES = {
  'mono-white':     { primary: '#ffffff', dim: '#9aa4b2', top: '#ffffff', accent: '#ff9a3c', alert: '#ff4d3d', off: 'rgba(255,255,255,0.045)' },
  'ssd-dual':       { primary: '#5ce6ff', dim: '#2b7b92', top: '#ffd54a', accent: '#ffd54a', alert: '#ff5c4d', off: 'rgba(92,230,255,0.05)' },
  'ice-cyan':       { primary: '#6bebff', dim: '#2f8fa4', top: '#a8f4ff', accent: '#ffaa44', alert: '#ff5247', off: 'rgba(107,235,255,0.05)' },
  'amber':          { primary: '#ffbe4f', dim: '#9a6b21', top: '#ffe08a', accent: '#ffffff', alert: '#ff4d3d', off: 'rgba(255,190,79,0.05)' },
  'phosphor-green': { primary: '#64ff9e', dim: '#24804a', top: '#b2ffd0', accent: '#ffd54a', alert: '#ff5c4d', off: 'rgba(100,255,158,0.05)' },
  'moss-red':       { primary: '#ff5c4d', dim: '#8c2820', top: '#ff9b8a', accent: '#ffd54a', alert: '#ffffff', off: 'rgba(255,92,77,0.05)' },
};

const MOOD_META = {
  idle:      { code: 'M-01', state: 'data' },
  listening: { code: 'M-02', state: 'crit' },
  thinking:  { code: 'M-03', state: 'data' },
  speaking:  { code: 'M-04', state: 'ok' },
  music:     { code: 'M-05', state: 'ok' },
  happy:     { code: 'M-06', state: 'ok' },
  comfort:   { code: 'M-07', state: 'data' },
  grieved:   { code: 'M-08', state: 'warn' },
  alert:     { code: 'M-09', state: 'crit' },
  sleepy:    { code: 'M-10', state: 'idle' },
  surprised: { code: 'M-11', state: 'warn' },
  proud:     { code: 'M-12', state: 'ok' },
};

const PREVIEW_CYCLE = ['happy', 'proud', 'music', 'grieved', 'comfort', 'surprised', 'thinking', 'sleepy', 'alert', 'idle'];

const RE_GRIEVED = /笨蛋|真笨|好笨|蠢|傻|搞错|答错|错了|气死|不行|胡说|瞎编|编造|怎么回事|不智能/u;
const RE_COMFORT = /好累|太累|累死|疲惫|烦死|好烦|难过|委屈|加班|焦虑|睡不着|撑不住|难受|崩溃|压力大|头疼|辛苦/u;
const RE_SURPRISED = /卧槽|震惊|天哪|离谱|居然|真的吗|哇塞|哇哦|不可思议/u;
const RE_HAPPY = /哈哈|嘿嘿|厉害|真棒|谢谢|感谢|夸|有用|可爱|笑死|好耶|太强|牛逼|真牛|干得好|喜欢|不错|漂亮|满分/u;

function createGrid() {
  return new Uint8Array(COLS * ROWS);
}

// Grid cell values: 0 = off, 1 = primary bright, 2 = dim, 3 = accent (amber/gold), 4 = alert (red), 5 = top-zone
function plot(g, x, y, v = 1) {
  const xi = Math.round(x);
  const yi = Math.round(y);
  if (xi >= 0 && xi < COLS && yi >= 0 && yi < ROWS) g[yi * COLS + xi] = v;
}

function rect(g, x, y, w, h, v = 1) {
  for (let dy = 0; dy < h; dy += 1) {
    for (let dx = 0; dx < w; dx += 1) plot(g, x + dx, y + dy, v);
  }
}

function roundedEye(g, cx, cy, rw, rh, v = 1) {
  for (let dy = -rh; dy <= rh; dy += 1) {
    for (let dx = -rw; dx <= rw; dx += 1) {
      if (Math.abs(dx) === rw && Math.abs(dy) === rh) continue;
      plot(g, cx + dx, cy + dy, v);
    }
  }
}

function drawArcEye(g, cx, cy, radius, flipDown = false, v = 1) {
  for (let dx = -radius; dx <= radius; dx += 1) {
    const norm = dx / Math.max(1, radius);
    const dy = Math.round((1 - norm * norm) * 2.6) * (flipDown ? 1 : -1);
    plot(g, cx + dx, cy + dy, v);
    plot(g, cx + dx, cy + dy + (flipDown ? -1 : 1), v);
  }
}

function drawSmile(g, cx, cy, halfW = 3, v = 1) {
  for (let dx = -halfW; dx <= halfW; dx += 1) {
    const lift = Math.abs(dx) === halfW ? -1 : 0;
    plot(g, cx + dx, cy + lift, v);
  }
}

function drawStar(g, cx, cy, phase, v = 3) {
  plot(g, cx, cy, v);
  if (phase) {
    plot(g, cx - 1, cy, v);
    plot(g, cx + 1, cy, v);
    plot(g, cx, cy - 1, v);
    plot(g, cx, cy + 1, v);
  }
}

function drawTinyZ(g, x, y, big, v = 2) {
  if (big) {
    rect(g, x, y, 4, 1, v);
    plot(g, x + 2, y + 1, v);
    plot(g, x + 1, y + 2, v);
    rect(g, x, y + 3, 4, 1, v);
  } else {
    rect(g, x, y, 3, 1, v);
    plot(g, x + 1, y + 1, v);
    rect(g, x, y + 2, 3, 1, v);
  }
}

function drawNote(g, x, y, v = 3) {
  plot(g, x, y + 3, v);
  plot(g, x + 1, y + 3, v);
  plot(g, x + 1, y + 2, v);
  plot(g, x + 1, y + 1, v);
  plot(g, x + 1, y, v);
  plot(g, x + 2, y, v);
  plot(g, x + 3, y + 1, v);
}

function drawHeart(g, cx, cy, beat, v = 3) {
  const s = beat ? 1 : 0;
  plot(g, cx - 1, cy - 1 - s, v);
  plot(g, cx + 1, cy - 1 - s, v);
  rect(g, cx - 2, cy - s, 5, 1, v);
  rect(g, cx - 1, cy + 1 - s, 3, 1, v);
  plot(g, cx, cy + 2 - s, v);
}

function composeFrame(mood, sec, audioLevel) {
  const g = createGrid();
  const lx = 13;
  const rx = 30;
  const ey = 10;

  // Top telemetry scan dot (subtle hardware alive beat)
  const scanX = Math.floor((sec * 7) % (COLS - 6)) + 3;
  plot(g, scanX, 1, 5);
  plot(g, Math.max(2, scanX - 1), 1, 2);

  if (mood === 'idle') {
    const cycle = sec % 3.8;
    const blink = cycle > 3.58 && cycle < 3.74;
    const glance = Math.sin(sec * 0.7) > 0.75 ? 2 : Math.sin(sec * 0.7) < -0.75 ? -2 : 0;
    const bob = Math.sin(sec * 1.9) > 0.65 ? -1 : 0;
    if (blink) {
      rect(g, lx - 3 + glance, ey + bob, 7, 1, 1);
      rect(g, rx - 3 + glance, ey + bob, 7, 1, 1);
    } else {
      roundedEye(g, lx + glance, ey + bob, 3, 3, 1);
      roundedEye(g, rx + glance, ey + bob, 3, 3, 1);
      // Dark pupil glint inside eyes for BenBen charm
      plot(g, lx + glance - 1, ey + bob - 1, 0);
      plot(g, rx + glance - 1, ey + bob - 1, 0);
    }
    drawSmile(g, 22 + Math.round(glance * 0.4), 17, 2, 1);
    return g;
  }

  if (mood === 'listening') {
    const pulse = Math.round((Math.sin(sec * 8) * 0.5 + 0.5) * 2 + audioLevel * 5);
    // Perked ear chevrons
    plot(g, 6, 3, 3); plot(g, 5, 4, 3); plot(g, 7, 4, 3);
    plot(g, 37, 3, 3); plot(g, 36, 4, 3); plot(g, 38, 4, 3);
    roundedEye(g, lx, ey - 1, 3, 4, 1);
    roundedEye(g, rx, ey - 1, 3, 4, 1);
    plot(g, lx - 1, ey - 3, 0);
    plot(g, rx - 1, ey - 3, 0);
    // Cheek audio wave bars
    for (let i = 0; i < 3; i += 1) {
      const h = 1 + ((pulse + i * 2) % 5);
      rect(g, 2 + i * 2, 14 - h, 1, h * 2, 3);
      rect(g, 41 - i * 2, 14 - h, 1, h * 2, 3);
    }
    rect(g, 20, 17, 4, 1, 1);
    return g;
  }

  if (mood === 'thinking') {
    const phase = Math.floor(sec * 10) % 8;
    const ring = [[-3, -2], [0, -3], [3, -2], [3, 1], [0, 3], [-3, 1], [-3, -1], [1, -3]];
    for (const cx of [lx, rx]) {
      for (let i = 0; i < ring.length; i += 1) {
        const [dx, dy] = ring[i];
        const dist = (i - phase + ring.length) % ring.length;
        const v = dist === 0 ? 3 : dist <= 3 ? 1 : 2;
        plot(g, cx + dx, ey + dy, v);
        plot(g, cx + dx + (dx >= 0 ? -1 : 1), ey + dy, dist <= 2 ? 1 : 2);
      }
      plot(g, cx, ey, phase % 2 === 0 ? 3 : 1);
    }
    // Streaming dot-matrix compute bar
    const bar = Math.floor(sec * 12) % 12;
    for (let i = 0; i < 12; i += 1) {
      const v = i === bar || i === (bar + 1) % 12 ? 3 : i === (bar + 2) % 12 ? 1 : 2;
      plot(g, 16 + i, 17, v);
    }
    return g;
  }

  if (mood === 'speaking') {
    const mouthAmp = Math.max(1, Math.round(1.5 + Math.abs(Math.sin(sec * 11)) * 2.5 + audioLevel * 3));
    drawArcEye(g, lx, ey, 3, false, 1);
    drawArcEye(g, rx, ey, 3, false, 1);
    // Dynamic 7-column voice mouth waveform
    for (let dx = -3; dx <= 3; dx += 1) {
      const wave = Math.max(1, Math.round(mouthAmp * (1 - Math.abs(dx) * 0.22) * (0.65 + 0.35 * Math.sin(sec * 15 + dx))));
      rect(g, 22 + dx * 2, 16 - Math.floor(wave / 2), 1, wave, Math.abs(dx) <= 1 ? 3 : 1);
    }
    return g;
  }

  if (mood === 'music') {
    const sway = Math.round(Math.sin(sec * 4.5) * 2);
    const bounce = Math.abs(Math.sin(sec * 4.5)) > 0.55 ? -1 : 0;
    drawArcEye(g, lx + sway, ey + bounce, 3, false, 1);
    drawArcEye(g, rx + sway, ey + bounce, 3, false, 1);
    drawSmile(g, 22 + sway, 15 + bounce, 3, 1);
    // Floating musical notes
    const noteY = 3 + (Math.floor(sec * 4) % 3);
    drawNote(g, 3 + ((Math.floor(sec * 2)) % 2), noteY, 3);
    drawNote(g, 37, 5 - (Math.floor(sec * 4) % 3), 3);
    // Bottom equalizer dots
    for (let i = 0; i < 8; i += 1) {
      const h = 1 + Math.round((Math.sin(sec * 9 + i * 1.1) * 0.5 + 0.5) * 2);
      rect(g, 11 + i * 3, 21 - h, 2, h, i % 2 === 0 ? 1 : 2);
    }
    return g;
  }

  if (mood === 'happy') {
    const bounce = Math.sin(sec * 6) > 0.3 ? -1 : 0;
    drawArcEye(g, lx, ey + bounce, 4, false, 1);
    drawArcEye(g, rx, ey + bounce, 4, false, 1);
    // Warm amber cheek dots (palette-safe orange/amber)
    rect(g, 6, 13 + bounce, 3, 1, 3);
    rect(g, 35, 13 + bounce, 3, 1, 3);
    // Grinning mouth
    drawSmile(g, 22, 16 + bounce, 4, 1);
    rect(g, 20, 17 + bounce, 5, 1, 1);
    // Twinkling stars
    const tw = Math.floor(sec * 4) % 2 === 0;
    drawStar(g, 5, 5, tw, 3);
    drawStar(g, 38, 5, !tw, 3);
    return g;
  }

  if (mood === 'comfort') {
    const breathe = Math.sin(sec * 2.2) > 0.2 ? 0 : 1;
    // Soft sympathetic arched eyes
    for (let dx = -3; dx <= 3; dx += 1) {
      const slopeL = Math.round(dx * 0.45);
      const slopeR = Math.round(-dx * 0.45);
      plot(g, lx + dx, ey - 1 + slopeL, 1);
      plot(g, lx + dx, ey + slopeL, 1);
      plot(g, rx + dx, ey - 1 + slopeR, 1);
      plot(g, rx + dx, ey + slopeR, 1);
    }
    rect(g, 7, 14, 2, 1, 3);
    rect(g, 35, 14, 2, 1, 3);
    drawHeart(g, 22, 16, breathe === 0, 3);
    return g;
  }

  if (mood === 'grieved') {
    // Classic Wandering Earth 2 sad BenBen eyes (/ \) + animated falling teardrop!
    const quiver = Math.sin(sec * 14) > 0.6 ? 1 : 0;
    for (let i = -3; i <= 3; i += 1) {
      plot(g, lx + i, ey - Math.round(i * 0.55), 1);
      plot(g, lx + i, ey - Math.round(i * 0.55) + 1, 1);
      plot(g, rx + i, ey + Math.round(i * 0.55), 1);
      plot(g, rx + i, ey + Math.round(i * 0.55) + 1, 1);
    }
    // Falling animated glowing pixel teardrop
    const dropY = 12 + Math.floor((sec * 4.2) % 8);
    plot(g, rx + 3, dropY, 3);
    if (dropY > 12) plot(g, rx + 3, dropY - 1, 2);
    // Quivering tiny wobbly mouth
    for (let dx = -3; dx <= 3; dx += 1) {
      plot(g, 22 + dx, 17 + ((dx + quiver) % 2 === 0 ? 0 : 1), 1);
    }
    return g;
  }

  if (mood === 'alert') {
    const flash = Math.floor(sec * 6) % 2 === 0;
    const shake = flash ? 1 : -1;
    // Hollow wide alert eyes
    for (const cx of [lx + shake, rx + shake]) {
      for (let dy = -3; dy <= 3; dy += 1) {
        for (let dx = -3; dx <= 3; dx += 1) {
          if (Math.abs(dx) === 3 && Math.abs(dy) === 3) continue;
          if (Math.abs(dx) >= 2 || Math.abs(dy) >= 2) plot(g, cx + dx, ey + dy, 4);
        }
      }
      plot(g, cx, ey, flash ? 1 : 4);
    }
    // Flashing side exclamation beacons
    if (flash) {
      rect(g, 4, 6, 2, 5, 4); plot(g, 4, 13, 4); plot(g, 5, 13, 4);
      rect(g, 38, 6, 2, 5, 4); plot(g, 38, 13, 4); plot(g, 39, 13, 4);
    }
    rect(g, 19, 17, 6, 1, 4);
    return g;
  }

  if (mood === 'sleepy') {
    const breath = Math.sin(sec * 1.4) > 0 ? 1 : 0;
    rect(g, lx - 3, ey + breath, 7, 2, 1);
    rect(g, rx - 3, ey + breath, 7, 2, 1);
    rect(g, 20, 16 + breath, 4, 1, 2);
    // Floating animated Z z pixels
    const rise = Math.floor((sec * 2.4) % 5);
    drawTinyZ(g, 35, 8 - rise, true, 3);
    drawTinyZ(g, 39, 12 - ((rise + 2) % 5), false, 2);
    return g;
  }

  if (mood === 'surprised') {
    const pop = Math.floor(sec * 8) % 2;
    roundedEye(g, lx, ey, 4, 4, 1);
    roundedEye(g, rx, ey, 4, 4, 1);
    rect(g, lx - 1, ey - 1, 3, 3, 0);
    rect(g, rx - 1, ey - 1, 3, 3, 0);
    plot(g, lx + pop, ey, 3);
    plot(g, rx + pop, ey, 3);
    // Open O mouth
    rect(g, 20, 16, 4, 3, 1);
    rect(g, 21, 17, 2, 1, 0);
    // Shock ticks
    plot(g, 4, 4 + pop, 3); plot(g, 5, 5 + pop, 3);
    plot(g, 39, 4 + pop, 3); plot(g, 38, 5 + pop, 3);
    return g;
  }

  // proud (M-12): Winking smug BenBen + animated wagging tail indicator!
  const wag = Math.round(Math.sin(sec * 10) * 5);
  drawArcEye(g, lx, ey, 3, false, 1);
  // Crisp winking '>' right eye
  for (let i = 0; i <= 3; i += 1) {
    plot(g, rx - 2 + i, ey - 3 + i, 1);
    plot(g, rx - 2 + i, ey + 3 - i, 1);
    plot(g, rx - 1 + i, ey - 3 + i, 1);
    plot(g, rx - 1 + i, ey + 3 - i, 1);
  }
  drawSmile(g, 22, 15, 3, 1);
  drawStar(g, 38, 5, Math.floor(sec * 5) % 2 === 0, 3);
  // Animated wagging pixel tail along bottom bar
  rect(g, 20 + wag, 19, 4, 2, 3);
  rect(g, 21 + Math.round(wag * 0.5), 20, 2, 1, 1);
  return g;
}

export function createBenBenController() {
  const box = document.getElementById('benben-box');
  const canvas = document.getElementById('benben-canvas');
  const chipEl = document.getElementById('benben-mood-chip');
  const modeTagEl = document.getElementById('benben-mode-tag');
  const quoteEl = document.getElementById('benben-quote');
  const codeEl = document.getElementById('benben-code');
  if (!canvas || !box) {
    return {
      updateContext() {},
      setAudioLevel() {},
      setOledPreset() {},
      setFrozen() {},
      refreshI18n() {},
      getMood: () => 'idle',
    };
  }

  const ctx = canvas.getContext('2d');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let frozen = false;
  let rafId = 0;
  let lastDraw = 0;
  let oledPreset = (() => {
    try {
      const saved = localStorage.getItem('moss-fui.oled-preset');
      if (saved && OLED_PALETTES[saved]) return saved;
    } catch { /* ignore */ }
    return 'mono-white';
  })();

  let currentMood = 'idle';
  let autoMood = 'idle';
  let manualUntil = 0;
  let previewIdx = 0;
  let audioLevel = 0;
  let runtimeCtx = {
    micActive: false,
    thinking: false,
    speaking: false,
    mediaPlaying: false,
    hwSeverity: 'idle',
    turnError: false,
    pendingApproval: false,
    lastUserText: '',
    lastReplyText: '',
    lastTurnAt: 0,
    lastTools: [],
  };

  function inferSentimentMood() {
    const fresh = Date.now() - (runtimeCtx.lastTurnAt || 0) < 95000;
    if (!fresh) return null;
    const userTxt = String(runtimeCtx.lastUserText || '');
    const replyTxt = String(runtimeCtx.lastReplyText || '');
    if (RE_GRIEVED.test(userTxt)) return 'grieved';
    if (RE_COMFORT.test(userTxt)) return 'comfort';
    if (RE_SURPRISED.test(userTxt)) return 'surprised';
    if (RE_HAPPY.test(userTxt)) return 'happy';
    if ((runtimeCtx.lastTools || []).length > 0 && replyTxt.length > 0) return 'proud';
    return null;
  }

  function computeAutoMood() {
    if (runtimeCtx.hwSeverity === 'crit' || runtimeCtx.hwSeverity === 'warn' || runtimeCtx.turnError || runtimeCtx.pendingApproval) {
      return 'alert';
    }
    if (runtimeCtx.micActive) return 'listening';
    if (runtimeCtx.thinking) return 'thinking';
    if (runtimeCtx.speaking) return 'speaking';
    if (runtimeCtx.mediaPlaying) return 'music';
    const sentiment = inferSentimentMood();
    if (sentiment) return sentiment;
    const hr = new Date().getHours();
    if (hr >= 23 || hr < 6) return 'sleepy';
    return 'idle';
  }

  function syncLabels() {
    const isManual = Date.now() < manualUntil;
    const mood = currentMood;
    const meta = MOOD_META[mood] || MOOD_META.idle;
    if (chipEl) {
      chipEl.textContent = t(`benben.mood_${mood}`);
      chipEl.dataset.state = meta.state;
    }
    if (modeTagEl) modeTagEl.textContent = t(isManual ? 'benben.manual' : 'benben.auto');
    if (quoteEl) quoteEl.textContent = t(`benben.quote_${mood}`);
    if (codeEl) codeEl.textContent = meta.code;
    box.dataset.mood = mood;
  }

  function refreshMood() {
    autoMood = computeAutoMood();
    const next = Date.now() < manualUntil ? currentMood : autoMood;
    if (next !== currentMood) {
      currentMood = next;
    }
    syncLabels();
  }

  function setOledPreset(next) {
    oledPreset = OLED_PALETTES[next] ? next : 'mono-white';
    box.dataset.oled = oledPreset;
    drawFrame(performance.now());
  }

  function drawFrame(nowMs) {
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rectBox = canvas.getBoundingClientRect();
    const w = Math.max(160, Math.round(rectBox.width || 240));
    const h = Math.max(76, Math.round(rectBox.height || 104));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, w, h);

    const pal = OLED_PALETTES[oledPreset] || OLED_PALETTES['mono-white'];
    const padX = 8;
    const padY = 6;
    const cellW = (w - padX * 2) / COLS;
    const cellH = (h - padY * 2) / ROWS;
    const dotSize = Math.max(2, Math.min(cellW, cellH) - 1.25);
    const offsetX = padX + (cellW - dotSize) * 0.5;
    const offsetY = padY + (cellH - dotSize) * 0.5;

    const sec = reduced.matches ? 0.4 : nowMs / 1000;
    const grid = composeFrame(currentMood, sec, audioLevel);

    // Subtle off-pixel OLED dot-matrix substrate so the physical LED matrix is always visible
    ctx.fillStyle = pal.off;
    for (let row = 0; row < ROWS; row += 1) {
      const py = offsetY + row * cellH;
      for (let col = 0; col < COLS; col += 1) {
        if (grid[row * COLS + col] === 0) {
          ctx.fillRect(offsetX + col * cellW, py, dotSize, dotSize);
        }
      }
    }

    // Lit luminous OLED dots with crisp optical bloom
    for (let row = 0; row < ROWS; row += 1) {
      const py = offsetY + row * cellH;
      for (let col = 0; col < COLS; col += 1) {
        const val = grid[row * COLS + col];
        if (!val) continue;
        const px = offsetX + col * cellW;
        const color = val === 1 ? pal.primary
          : val === 2 ? pal.dim
            : val === 3 ? pal.accent
              : val === 4 ? pal.alert : pal.top;
        ctx.shadowColor = color;
        ctx.shadowBlur = val === 2 ? 2 : 7;
        ctx.fillStyle = color;
        ctx.fillRect(px, py, dotSize, dotSize);
      }
    }
    ctx.restore();
  }

  function tick(nowMs) {
    rafId = 0;
    if (frozen || document.hidden) return;
    if (manualUntil && Date.now() >= manualUntil) {
      manualUntil = 0;
      refreshMood();
    }
    if (nowMs - lastDraw >= 45) { // ~22fps crisp hardware dot-matrix cadence
      lastDraw = nowMs;
      drawFrame(nowMs);
    }
    if (!reduced.matches) {
      rafId = requestAnimationFrame(tick); // fui-lint-ignore: R15 user-requested animated BenBen OLED dot-matrix faceplate; pauses when frozen/hidden/reduced-motion
    }
  }

  function syncLoop() {
    if (frozen || document.hidden || reduced.matches) {
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
      }
      drawFrame(performance.now());
      return;
    }
    if (!rafId) {
      rafId = requestAnimationFrame(tick); // fui-lint-ignore: R15 user-requested animated BenBen OLED dot-matrix faceplate; pauses when frozen/hidden/reduced-motion
    }
  }

  // Click to pet BenBen & cycle live animated expressions (auto-resumes smart sensing after 6s)
  box.addEventListener('click', () => {
    const nextMood = PREVIEW_CYCLE[previewIdx % PREVIEW_CYCLE.length];
    previewIdx += 1;
    currentMood = nextMood;
    manualUntil = Date.now() + 6500;
    syncLabels();
    drawFrame(performance.now());
    syncLoop();
  });

  window.addEventListener('moss-oled-preset', (ev) => setOledPreset(ev.detail));
  window.addEventListener('storage', (ev) => {
    if (ev.key === 'moss-fui.oled-preset') setOledPreset(ev.newValue || 'mono-white');
  });
  document.addEventListener('visibilitychange', syncLoop);
  reduced.addEventListener?.('change', syncLoop);
  new ResizeObserver(() => drawFrame(performance.now())).observe(canvas);

  setOledPreset(oledPreset);
  refreshMood();
  syncLoop();

  return {
    updateContext(patch = {}) {
      runtimeCtx = { ...runtimeCtx, ...patch };
      refreshMood();
    },
    setAudioLevel(level) {
      audioLevel = Math.max(0, Math.min(1, Number(level) || 0));
    },
    setOledPreset,
    setFrozen(next) {
      frozen = Boolean(next);
      syncLoop();
    },
    refreshI18n() {
      syncLabels();
    },
    getMood: () => currentMood,
  };
}
