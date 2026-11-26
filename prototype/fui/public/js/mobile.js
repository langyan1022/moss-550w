import { openBestMicStream } from '/js/mic-selector.js';
import { escapeHtml } from '/js/hud/utils.js';
import { safeCopyText } from '/js/safe-web.js';
import { renderSafeMarkdown } from '/js/dialogue.js';
import { renderGenerativeWidget } from '/js/brain-deck.js';

const feed = document.getElementById('m-feed');
const form = document.getElementById('m-form');
const input = document.getElementById('m-input');
const linkEl = document.getElementById('m-link');
const chatSel = document.getElementById('m-chat-sel');
const persSel = document.getElementById('m-personality');
const modelSel = document.getElementById('m-model');
const ttsBtn = document.getElementById('m-tts');
const micBtn = document.getElementById('m-mic');
const newChatBtn = document.getElementById('m-new-chat');
const alertBanner = document.getElementById('m-alert-banner');
const alertBadge = document.getElementById('m-alert-badge');
const alertTitle = document.getElementById('m-alert-title');
const alertBody = document.getElementById('m-alert-body');
const alertDismiss = document.getElementById('m-alert-dismiss');
const mediaBar = document.getElementById('m-media-bar');
const mediaCover = document.getElementById('m-media-cover');
const mediaBadge = document.getElementById('m-media-badge');
const mediaTitle = document.getElementById('m-media-title');
const mediaArtist = document.getElementById('m-media-artist');
const mediaPrev = document.getElementById('m-media-prev');
const mediaToggle = document.getElementById('m-media-toggle');
const mediaNext = document.getElementById('m-media-next');
const mediaSleep = document.getElementById('m-media-sleep');
const mediaSpeed = document.getElementById('m-media-speed');
const mediaSeek = document.getElementById('m-media-seek');
const mediaTime = document.getElementById('m-media-time');
const mediaLrc = document.getElementById('m-media-lrc');
const mediaQueue = document.getElementById('m-media-queue');
const audioEl = document.getElementById('m-audio');
const vizCanvas = document.getElementById('m-viz');
const authOverlay = document.getElementById('m-auth');
const authForm = document.getElementById('m-auth-form');
const authInput = document.getElementById('m-auth-input');
const authErr = document.getElementById('m-auth-err');
const authChip = document.getElementById('m-auth-chip');
const pushBtn = document.getElementById('m-push');
const shellEl = document.querySelector('.m-shell');

let vizCtx = null;
let vizAnalyser = null;
let vizSource = null;
let vizFreq = null;
let vizRaf = 0;
const vizPeaks = new Float32Array(28);

function formatSec(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${String(m).padStart(2, '0')}:${String(rem).padStart(2, '0')}`;
}

function drawMobileViz(playing) {
  if (!vizCanvas) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cw = Math.max(180, vizCanvas.clientWidth || 320);
  const ch = 26;
  if (vizCanvas.width !== Math.round(cw * dpr) || vizCanvas.height !== Math.round(ch * dpr)) {
    vizCanvas.width = Math.round(cw * dpr);
    vizCanvas.height = Math.round(ch * dpr);
  }
  const ctx = vizCanvas.getContext('2d');
  if (!ctx) return;
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cw, ch);
  const styles = getComputedStyle(document.documentElement);
  const cOk = styles.getPropertyValue('--fui-ok').trim() || '#34d399';
  const cInfo = styles.getPropertyValue('--fui-info').trim() || '#38bdf8';
  const cAct = styles.getPropertyValue('--fui-act').trim() || '#f97316';
  const cLine = styles.getPropertyValue('--fui-line-2').trim() || '#334155';

  const bars = 28;
  const gap = 3;
  const bw = Math.max(3, (cw - gap * (bars - 1)) / bars);
  if (playing && vizAnalyser && vizFreq) {
    vizAnalyser.getByteFrequencyData(vizFreq);
  }
  for (let i = 0; i < bars; i++) {
    let v = 0.08;
    if (playing && vizFreq) {
      const bin = Math.min(vizFreq.length - 1, Math.floor((i / bars) * (vizFreq.length * 0.68)));
      v = Math.max(0.08, (vizFreq[bin] || 0) / 255);
    }
    vizPeaks[i] = v > vizPeaks[i] ? v : Math.max(0.08, vizPeaks[i] - 0.025);
    const h = Math.max(2, Math.round(v * (ch - 4)));
    const x = i * (bw + gap);
    ctx.fillStyle = playing ? (v > 0.72 ? cAct : i % 3 === 0 ? cInfo : cOk) : cLine;
    ctx.fillRect(x, ch - h, bw, h);
    if (playing) {
      const py = Math.max(0, ch - Math.round(vizPeaks[i] * (ch - 2)) - 2);
      ctx.fillStyle = cAct;
      ctx.fillRect(x, py, bw, 2);
    }
  }
  ctx.restore();
}

function ensureAudioTap() {
  if (vizAnalyser) {
    if (vizCtx?.state === 'suspended') vizCtx.resume().catch(() => {});
    return;
  }
  try {
    const src = audioEl.currentSrc || audioEl.getAttribute('src') || '';
    if (src && !src.startsWith('blob:') && !src.startsWith('data:')) {
      const parsed = new URL(src, location.href);
      if (parsed.origin !== location.origin) return;
    }
    vizCtx = new (window.AudioContext || window.webkitAudioContext)();
    vizSource = vizCtx.createMediaElementSource(audioEl);
    vizAnalyser = vizCtx.createAnalyser();
    vizAnalyser.fftSize = 128;
    vizAnalyser.smoothingTimeConstant = 0.78;
    vizSource.connect(vizAnalyser);
    vizAnalyser.connect(vizCtx.destination);
    vizFreq = new Uint8Array(vizAnalyser.frequencyBinCount);
  } catch { /* ignore */ }
}

function stopVizLoop() {
  if (vizRaf) { cancelAnimationFrame(vizRaf); vizRaf = 0; }
  drawMobileViz(false);
}

function startVizLoop() {
  if (vizRaf) return;
  const tick = () => {
    if (audioEl.paused || audioEl.ended || document.hidden) {
      vizRaf = 0;
      drawMobileViz(false);
      return;
    }
    drawMobileViz(true);
    vizRaf = requestAnimationFrame(tick); // fui-lint-ignore: R15 bounded media playback spectrum loop
  };
  vizRaf = requestAnimationFrame(tick); // fui-lint-ignore: R15 bounded media playback spectrum loop
}

let chatId = '';
let latestMobileState = null;
let visibleLimit = 20;
let firstRenderDone = false;
let ttsOn = (() => { try { return localStorage.getItem('moss-mobile.tts') === '1'; } catch { return false; } })();
let lastSpokenTurn = '';
if (ttsBtn) {
  ttsBtn.setAttribute('aria-pressed', String(ttsOn));
  ttsBtn.textContent = ttsOn ? '播报 开' : '播报 关';
}
let activeAlertId = null;
let activeMediaId = '';
let activeMediaProvider = '';
let mobileMediaItems = [];
let mobileMediaTurnId = '';
let mobileLrcLines = [];
let mobileLrcAbort = null;
let mobileSleepEnd = 0;
let mobileSleepTick = 0;
let mobileScrobbledStart = false;
let mobileScrobbledDone = false;
let micStream = null;
let micCtx = null;
let micNodeSrc = null;
let micNodeProc = null;
let micNodeGain = null;
let micChunks = [];
let openingMic = false;
let stoppingMic = false;
let micOpenGen = 0;
let preMicAudioVol = null;
let seekingMedia = false;
let activeSpeakAudio = null;
let activeSpeakUrl = '';
let activeSpeakAbort = null;
let isAdminRole = true;
let activeOwnerCode = 'MOSS';
let authRequiredMode = true;
let es = null;

function openAuthPrompt(msg, { switchMode = false } = {}) {
  authRequiredMode = !switchMode;
  if (!switchMode && es) { es.close(); es = null; }
  authOverlay.hidden = false;
  if (shellEl) shellEl.inert = true;
  authErr.textContent = msg || '';
  authErr.hidden = !msg;
  if (authInput) { authInput.value = ''; authInput.focus(); }
}
authOverlay?.addEventListener('click', (ev) => {
  if (!authRequiredMode && ev.target === authOverlay) {
    authOverlay.hidden = true;
    if (shellEl) shellEl.inert = false;
  }
});
document.addEventListener('keydown', (ev) => {
  if (!authRequiredMode && ev.key === 'Escape' && !authOverlay.hidden) {
    authOverlay.hidden = true;
    if (shellEl) shellEl.inert = false;
  }
});

function flashLinkError(msg) {
  linkEl.dataset.state = 'warn';
  linkEl.textContent = String(msg || 'ERR').slice(0, 18);
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => null);
  if (resp.status === 401) {
    openAuthPrompt((data && data.error) || '需要重新验证 4 位授权码');
    return null;
  }
  if (!resp.ok) {
    flashLinkError(`HTTP ${resp.status}`);
    return null;
  }
  return data || {};
}

async function loadModels() {
  try {
    const resp = await fetch('/api/models', { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    const items = data.models || [];
    modelSel.replaceChildren();
    for (const m of items) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.label || m.model || m.id;
      opt.disabled = m.available === false;
      modelSel.append(opt);
    }
    if (data.selected) modelSel.value = data.selected;
  } catch { /* ignore */ }
}

function showMobileAlert(al) {
  if (!alertBanner || !al || al.dismissed) return;
  activeAlertId = al.id ?? null;
  alertBanner.hidden = false;
  alertBanner.dataset.state = al.level || 'info';
  if (alertBadge) alertBadge.textContent = String(al.level || 'pulse').toUpperCase();
  if (alertTitle) alertTitle.textContent = al.title || '主动感知提醒';
  if (alertBody) alertBody.textContent = al.body || '';
}

alertBanner?.addEventListener('click', (ev) => {
  if (ev.target === alertDismiss || !activeAlertId) return;
  const curId = activeAlertId;
  activeAlertId = null;
  alertBanner.hidden = true;
  if (curId) {
    void postJson(`/api/proactive/alerts/${encodeURIComponent(curId)}/dismiss`, { accepted: true });
  }
});

alertDismiss?.addEventListener('click', () => {
  const curId = activeAlertId;
  activeAlertId = null;
  if (alertBanner) alertBanner.hidden = true;
  if (curId && isAdminRole) {
    void postJson(`/api/proactive/alerts/${encodeURIComponent(curId)}/dismiss`, { accepted: false });
  }
});

function _urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replaceAll('-', '+').replaceAll('_', '/');
  const rawData = atob(base64);
  const out = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) out[i] = rawData.charCodeAt(i);
  return out;
}

let swRegistration = null;
async function initMobilePwaAndPush() {
  if (!('serviceWorker' in navigator)) return;
  try {
    swRegistration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
  } catch {
    return;
  }
  if (!pushBtn || !('PushManager' in window) || !window.isSecureContext) return;
  pushBtn.hidden = false;
  try {
    const existing = await swRegistration.pushManager.getSubscription();
    pushBtn.setAttribute('aria-pressed', String(Boolean(existing)));
    pushBtn.textContent = existing ? '推送 开' : '推送';
  } catch { /* ignore */ }
}

pushBtn?.addEventListener('click', async () => {
  if (!swRegistration?.pushManager) return;
  pushBtn.disabled = true;
  try {
    const existing = await swRegistration.pushManager.getSubscription();
    if (existing) {
      await fetch('/api/push/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: existing.endpoint }),
      });
      await existing.unsubscribe();
      pushBtn.setAttribute('aria-pressed', 'false');
      pushBtn.textContent = '推送';
      return;
    }
    const perm = typeof Notification !== 'undefined' ? await Notification.requestPermission() : 'denied';
    if (perm !== 'granted') return;
    const keyRes = await fetch('/api/push/vapid-key', { cache: 'no-store' });
    const keyData = await keyRes.json().catch(() => ({}));
    if (!keyData?.publicKey) return;
    const sub = await swRegistration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _urlBase64ToUint8Array(keyData.publicKey),
    });
    const saveRes = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    if (saveRes.ok) {
      pushBtn.setAttribute('aria-pressed', 'true');
      pushBtn.textContent = '推送 开';
    }
  } catch { /* ignore */ } finally {
    pushBtn.disabled = false;
  }
});

function stopSpeakAudio() {
  if (activeSpeakAbort) {
    activeSpeakAbort.abort();
    activeSpeakAbort = null;
  }
  if (activeSpeakAudio) {
    activeSpeakAudio.pause();
    activeSpeakAudio = null;
  }
  if (activeSpeakUrl) {
    URL.revokeObjectURL(activeSpeakUrl);
    activeSpeakUrl = '';
  }
}

function splitSpeakSegments(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  if (clean.length <= 140) return [clean];
  const parts = clean.match(/[^。！？!?；;\n]+[。！？!?；;]?/gu) || [clean];
  const segs = [];
  let buf = '';
  for (const part of parts) {
    const piece = part.trim();
    if (!piece) continue;
    if (buf && buf.length + piece.length > 135) {
      segs.push(buf.slice(0, 145).trim());
      buf = piece;
      if (segs.length >= 5) break;
    } else {
      buf = buf ? `${buf} ${piece}` : piece;
    }
  }
  if (buf && segs.length < 6) {
    let rem = buf.trim();
    while (rem && segs.length < 6) {
      segs.push(rem.slice(0, 145).trim());
      rem = rem.slice(145).trim();
    }
  }
  return segs.filter(Boolean);
}

async function playSpeakBlob(blob, signal) {
  if (signal.aborted || !blob || !blob.size) return;
  const url = URL.createObjectURL(blob);
  const player = new Audio(url);
  activeSpeakAudio = player;
  activeSpeakUrl = url;
  await new Promise((resolve) => {
    const finish = () => {
      signal.removeEventListener('abort', onAbort);
      URL.revokeObjectURL(url);
      if (activeSpeakUrl === url) activeSpeakUrl = '';
      if (activeSpeakAudio === player) activeSpeakAudio = null;
      resolve();
    };
    const onAbort = () => {
      try { player.pause(); } catch { /* ignore */ }
      finish();
    };
    signal.addEventListener('abort', onAbort, { once: true });
    player.addEventListener('ended', finish, { once: true });
    player.addEventListener('error', finish, { once: true });
    player.play().catch(finish);
  });
}

async function speakReply(text, { force = false } = {}) {
  if ((!ttsOn && !force) || !text || micStream) return;
  stopSpeakAudio();
  const aborter = new AbortController();
  activeSpeakAbort = aborter;
  const segs = splitSpeakSegments(text);
  if (!segs.length) return;
  try {
    for (let i = 0; i < segs.length; i += 2) {
      if (aborter.signal.aborted) break;
      const pair = segs.slice(i, i + 2);
      if (pair.length === 2) {
        const resp = await fetch('/api/speak-batch', {
          method: 'POST',
          signal: aborter.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ texts: pair, category: 'reply', room_amount: 100 }),
        });
        if (resp.ok) {
          const form = await resp.formData();
          const b0 = form.get('0');
          const b1 = form.get('1');
          if (b0 instanceof Blob && b1 instanceof Blob && b0.size > 44 && b1.size > 44) {
            await playSpeakBlob(b0, aborter.signal);
            await playSpeakBlob(b1, aborter.signal);
            continue;
          }
        }
      }
      for (const seg of pair) {
        if (aborter.signal.aborted) break;
        const resp = await fetch('/api/speak', {
          method: 'POST',
          signal: aborter.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: seg, category: 'reply', room_amount: 100 }),
        });
        if (!resp.ok) break;
        await playSpeakBlob(await resp.blob(), aborter.signal);
      }
    }
  } catch { /* ignore */ } finally {
    if (activeSpeakAbort === aborter) activeSpeakAbort = null;
  }
}

function syncMobileSleepBtn() {
  const rem = mobileSleepEnd ? Math.max(0, Math.ceil((mobileSleepEnd - Date.now()) / 60000)) : 0;
  mediaSleep.textContent = rem > 0 ? `⏱ ${rem}m` : '⏱';
  if (rem > 0) mediaSleep.dataset.active = 'true';
  else delete mediaSleep.dataset.active;
}

function armMobileSleep(minutes) {
  clearInterval(mobileSleepTick);
  mobileSleepTick = 0;
  if (!minutes) {
    mobileSleepEnd = 0;
    syncMobileSleepBtn();
    return;
  }
  mobileSleepEnd = Date.now() + minutes * 60000;
  syncMobileSleepBtn();
  mobileSleepTick = setInterval(() => { // fui-lint-ignore: R15 bounded sleep countdown while armed
    if (mobileSleepEnd - Date.now() <= 0) {
      clearInterval(mobileSleepTick);
      mobileSleepTick = 0;
      mobileSleepEnd = 0;
      if (!audioEl.paused) audioEl.pause();
    }
    syncMobileSleepBtn();
  }, 5000);
}

function syncMobileLrc(sec) {
  if (!mediaLrc || !mobileLrcLines.length) return;
  let idx = 0;
  for (let i = 0; i < mobileLrcLines.length; i++) {
    if (mobileLrcLines[i].time <= sec + 0.15) idx = i;
    else break;
  }
  mediaLrc.textContent = mobileLrcLines[idx]?.text || '';
}

function loadMobileLyrics(sel) {
  if (mobileLrcAbort) { mobileLrcAbort.abort(); mobileLrcAbort = null; }
  mobileLrcLines = [];
  mediaLrc.hidden = true;
  mediaLrc.textContent = '';
  if (!sel?.media_id || sel.live) return;
  mobileLrcAbort = new AbortController();
  const qs = new URLSearchParams();
  if (sel.title) qs.set('title', sel.title);
  if (sel.artist) qs.set('artist', sel.artist);
  fetch(`/api/media/${encodeURIComponent(sel.media_id)}/lyrics?${qs}`, { cache: 'no-store', signal: mobileLrcAbort.signal })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (d?.lines?.length && sel.media_id === activeMediaId) {
        mobileLrcLines = d.lines;
        mediaLrc.hidden = false;
        mediaLrc.textContent = d.lines[0]?.text || '';
        if (d.synced !== false) syncMobileLrc(audioEl.currentTime || 0);
      }
    }).catch(() => {});
}

async function selectMobileTrack(item, index) {
  if (!item?.media_id) return;
  if (mobileMediaTurnId && chatId) {
    const res = await postJson('/api/media-select', { chat_id: chatId, turn_id: mobileMediaTurnId, media_id: item.media_id, index });
    if (res && Array.isArray(res.turns)) render(res);
  }
  ensureAudioTap();
  audioEl.play().catch(() => {});
}

function renderMobileQueue(sel) {
  const audioItems = mobileMediaItems.filter((it) => it && it.kind !== 'video');
  if (audioItems.length <= 1) {
    mediaQueue.hidden = true;
    mediaQueue.replaceChildren();
    mediaPrev.disabled = true;
    mediaNext.disabled = true;
    return;
  }
  const curIdx = audioItems.findIndex((it) => it.media_id === sel.media_id);
  mediaPrev.disabled = curIdx <= 0;
  mediaNext.disabled = curIdx < 0 || curIdx >= audioItems.length - 1;
  mediaQueue.hidden = false;
  mediaQueue.replaceChildren();
  audioItems.forEach((item, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'm-media__qbtn';
    btn.setAttribute('aria-current', String(item.media_id === sel.media_id));
    const nameSpan = document.createElement('span');
    nameSpan.className = 'm-media__qname';
    nameSpan.textContent = `${String(i + 1).padStart(2, '0')} · ${item.title || item.media_id}${item.artist ? ` - ${item.artist}` : ''}`;
    const tagSpan = document.createElement('span');
    tagSpan.className = 'fui-num';
    tagSpan.textContent = item.live ? 'LIVE' : String(item.provider || 'LOC').toUpperCase();
    btn.append(nameSpan, tagSpan);
    const rawIdx = mobileMediaItems.indexOf(item);
    btn.addEventListener('click', () => { void selectMobileTrack(item, rawIdx >= 0 ? rawIdx + 1 : i + 1); });
    mediaQueue.append(btn);
  });
}

function syncMedia(turns) {
  const mTurn = [...turns].reverse().find((t) => t.media?.selected?.media_id);
  const sel = mTurn?.media?.selected;
  if (!sel || sel.kind !== 'audio') {
    if (!mediaBar.hidden || activeMediaId) {
      audioEl.pause();
      audioEl.removeAttribute('src');
      try { audioEl.load(); } catch { /* ignore */ }
      activeMediaId = '';
      activeMediaProvider = '';
      mobileMediaItems = [];
      mobileMediaTurnId = '';
      mobileLrcLines = [];
      mediaLrc.hidden = true;
      mediaCover.hidden = true;
      mediaQueue.hidden = true;
      if (mediaSeek) { mediaSeek.value = '0'; mediaSeek.disabled = true; }
      if (mediaTime) mediaTime.textContent = '00:00 / 00:00';
      mediaToggle.textContent = '播放';
      stopVizLoop();
    }
    mediaBar.hidden = true;
    return;
  }
  mediaBar.hidden = false;
  mobileMediaTurnId = mTurn.id || '';
  mobileMediaItems = Array.isArray(mTurn.media?.items) ? mTurn.media.items : [sel];
  mediaBadge.textContent = sel.live ? 'LIVE' : sel.source_kind === 'online' ? String(sel.provider || 'ONL').slice(0, 4).toUpperCase() : 'AUD';
  mediaTitle.textContent = sel.title || sel.media_id;
  mediaArtist.textContent = sel.source_label ? `${sel.artist ? `${sel.artist} · ` : ''}${sel.source_label}` : (sel.artist || '550W MEDIA');
  if (sel.cover_url && String(sel.cover_url).startsWith('/')) {
    if (mediaCover.getAttribute('src') !== sel.cover_url) {
      mediaCover.src = sel.cover_url;
      mediaCover.hidden = false;
    }
  } else {
    mediaCover.hidden = true;
  }
  if (mediaSeek) mediaSeek.disabled = Boolean(sel.live);
  renderMobileQueue(sel);
  const src = sel.url || `/media/${encodeURIComponent(sel.media_id)}`;
  if (activeMediaId !== sel.media_id) {
    activeMediaId = sel.media_id;
    activeMediaProvider = sel.provider || '';
    mobileScrobbledStart = false;
    mobileScrobbledDone = false;
    audioEl.src = src;
    if (mediaSeek) mediaSeek.value = '0';
    if (mediaTime) mediaTime.textContent = sel.live ? 'LIVE STREAM' : '00:00 / --:--';
    mediaToggle.textContent = '播放';
    stopVizLoop();
    loadMobileLyrics(sel);
  } else if (audioEl.paused) {
    drawMobileViz(false);
  }
}

function syncChatSwitcher(state) {
  if (!chatSel) return;
  const chats = Array.isArray(state?.chats) ? state.chats : [];
  if (!chats.length) {
    chatSel.hidden = true;
    return;
  }
  chatSel.hidden = false;
  const curSig = chats.map((c) => `${c.id}:${c.title || ''}`).join('|');
  if (chatSel.dataset.sig !== curSig) {
    chatSel.dataset.sig = curSig;
    chatSel.replaceChildren(...chats.map((c) => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.title || c.id.slice(0, 8);
      return opt;
    }));
  }
  if (state.chat_id && chatSel.value !== state.chat_id) {
    chatSel.value = state.chat_id;
  }
}

function buildExtrasHtml(turn) {
  const tags = [];
  if (turn.weather?.condition) {
    const city = turn.weather.city || turn.weather.location || '';
    const tempVal = turn.weather.temperature ?? turn.weather.temp_c;
    const rangeVal = (turn.weather.temperature_min != null && turn.weather.temperature_max != null)
      ? `${turn.weather.temperature_min}~${turn.weather.temperature_max}°C`
      : '';
    const tmp = tempVal != null ? `${tempVal}°C` : rangeVal;
    tags.push(`WX · ${city} ${turn.weather.condition} ${tmp}`.trim());
  }
  const docLabel = turn.document?.title || turn.document?.filename || turn.document?.name || turn.file?.filename;
  if (docLabel) {
    tags.push(`DOC · ${docLabel}`);
  }
  if (Array.isArray(turn.local_overview) && turn.local_overview.length) {
    tags.push(`HOST · ${turn.local_overview.length} ITEMS`);
  }
  if (turn.media?.selected?.kind === 'video') {
    tags.push(`VID · ${turn.media.selected.title || turn.media.selected.media_id}`);
  }
  if (Array.isArray(turn.images) && turn.images.length) {
    tags.push(`IMG · ${turn.images.length}`);
  }
  if (turn.receipt?.filename) {
    tags.push(`DRAFT · ${turn.receipt.filename}`);
  }
  if (turn.desktop_confirmation?.action) {
    tags.push(`EXEC · ${turn.desktop_confirmation.action}`);
  }
  return tags;
}

function render(state) {
  const prevChat = chatId;
  chatId = state.chat_id || '';
  latestMobileState = state;
  if (prevChat && chatId && prevChat !== chatId) visibleLimit = 20;
  syncChatSwitcher(state);
  if (Array.isArray(state.personalities) && state.personalities.length && persSel.options.length !== state.personalities.length) {
    persSel.replaceChildren();
    for (const item of state.personalities) {
      const opt = document.createElement('option');
      opt.value = item.id;
      opt.textContent = item.label || item.id;
      persSel.append(opt);
    }
  }
  if (state.personality_id && persSel.value !== state.personality_id) {
    persSel.value = state.personality_id;
  }
  if (state.model_id && [...modelSel.options].some((o) => o.value === state.model_id)) {
    modelSel.value = state.model_id;
  }
  const allTurns = state.turns || [];
  const startIdx = Math.max(0, allTurns.length - visibleLimit);
  const turns = startIdx > 0 ? allTurns.slice(startIdx) : allTurns;
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 64;
  syncMedia(allTurns);
  const opTag = activeOwnerCode === 'MOSS' ? 'OP-01' : `OP-${activeOwnerCode}`;
  const keepIds = new Set(turns.map((tr) => String(tr.id || '')));
  for (const node of [...feed.children]) {
    if (node.dataset.loadOlder === '1') { node.remove(); continue; }
    if (!keepIds.has(node.dataset.turnId || '')) node.remove();
  }
  let cursor = null;
  if (startIdx > 0) {
    const loadLi = document.createElement('li');
    loadLi.dataset.loadOlder = '1';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'fui-btn';
    btn.style.width = '100%';
    btn.textContent = `展开更早 ${startIdx} 条对话`;
    btn.addEventListener('click', () => {
      visibleLimit += 20;
      if (latestMobileState) render(latestMobileState);
    });
    loadLi.append(btn);
    feed.prepend(loadLi);
    cursor = loadLi.nextElementSibling;
  } else {
    cursor = feed.firstElementChild;
  }
  const byKey = new Map();
  for (const el of [...feed.children]) {
    if (el.dataset.turnId && el.dataset.role) {
      byKey.set(`${el.dataset.turnId}:${el.dataset.role}`, el);
    }
  }
  for (const turn of turns) {
    const tid = String(turn.id || '');
    const widgetSig = turn.fui_widget ? JSON.stringify(turn.fui_widget) : '';
    const extraSig = [
      turn.weather ? `${turn.weather.city || turn.weather.location || ''}:${turn.weather.condition || ''}:${turn.weather.temperature ?? turn.weather.temp_c ?? ''}:${turn.weather.temperature_min ?? ''}:${turn.weather.temperature_max ?? ''}` : '',
      turn.document?.title || turn.document?.filename || turn.document?.name || '',
      turn.file?.filename || '',
      turn.local_overview?.length || 0,
      turn.images?.length || 0,
      turn.media?.selected?.kind || '',
      turn.preview || '',
      turn.receipt?.filename || '',
      turn.desktop_confirmation?.action || '',
    ].join('^');
    const sig = `${opTag}|${turn.status || ''}|${turn.user || ''}|${turn.reply || turn.partial_reply || turn.error || ''}|${turn.reply_ms || ''}|${widgetSig}|${extraSig}`;
    let u = byKey.get(`${tid}:user`);
    let a = byKey.get(`${tid}:assistant`);
    if (!u) {
      u = document.createElement('li');
      u.className = 'm-msg m-msg--user';
      u.dataset.turnId = tid;
      u.dataset.role = 'user';
      u.innerHTML = `<div class="m-msg__role fui-num"></div><p class="m-msg__text"></p>`;
      feed.insertBefore(u, cursor);
    } else if (cursor !== u) {
      feed.insertBefore(u, cursor);
    } else {
      cursor = u.nextElementSibling;
    }
    if (u.dataset.opTag !== opTag) {
      u.dataset.opTag = opTag;
      u.querySelector('.m-msg__role').textContent = opTag;
    }
    const uTextEl = u.querySelector('.m-msg__text');
    if (uTextEl.textContent !== (turn.user || '')) uTextEl.textContent = turn.user || '';

    if (!a) {
      a = document.createElement('li');
      a.className = 'm-msg';
      a.dataset.turnId = tid;
      a.dataset.role = 'assistant';
      a.innerHTML = `<div class="m-msg__role fui-num"></div><div class="m-msg__body"></div>`;
      feed.insertBefore(a, cursor);
    } else if (cursor !== a) {
      feed.insertBefore(a, cursor);
    } else {
      cursor = a.nextElementSibling;
    }
    if (a.dataset.sig !== sig) {
      a.dataset.sig = sig;
      const msPart = turn.reply_ms != null ? ` · ${Math.round(turn.reply_ms)}ms` : '';
      a.querySelector('.m-msg__role').textContent = `MOSS · ${String(turn.status || '').toUpperCase()}${msPart}`;
      const bodyEl = a.querySelector('.m-msg__body');
      bodyEl.replaceChildren();
      const rawTxt = turn.reply || turn.partial_reply || (turn.status === 'thinking' ? '处理中…' : (turn.error || ''));
      if (rawTxt) {
        if (turn.status === 'replied') {
          bodyEl.append(renderSafeMarkdown(rawTxt));
        } else {
          const p = document.createElement('p');
          p.className = 'm-msg__text';
          p.textContent = rawTxt;
          bodyEl.append(p);
        }
      }
      const extras = buildExtrasHtml(turn);
      if (extras.length) {
        const exRow = document.createElement('div');
        exRow.className = 'm-msg__extras';
        for (const tag of extras) {
          const sp = document.createElement('span');
          sp.className = 'fui-badge';
          sp.dataset.state = 'info';
          sp.textContent = tag;
          exRow.append(sp);
        }
        bodyEl.append(exRow);
      }
      if (turn.fui_widget) {
        const wNode = renderGenerativeWidget(turn.fui_widget);
        if (wNode) bodyEl.append(wNode);
      }
      a.querySelector('.m-msg__actions')?.remove();
      const actions = document.createElement('div');
      actions.className = 'm-msg__actions';
      if (turn.status === 'pending') {
        if (isAdminRole) {
          const okBtn = document.createElement('button');
          okBtn.type = 'button';
          okBtn.className = 'fui-btn fui-btn--primary';
          okBtn.textContent = '确认执行';
          okBtn.addEventListener('click', () => {
            void postJson('/api/approve', { turn_id: turn.id }).then((r) => { if (r && Array.isArray(r.turns)) render(r); });
          });
          actions.append(okBtn);
        }
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'fui-btn';
        cancelBtn.textContent = '取消';
        cancelBtn.addEventListener('click', () => {
          void postJson('/api/cancel', { turn_id: turn.id }).then((r) => { if (r && Array.isArray(r.turns)) render(r); });
        });
        actions.append(cancelBtn);
      } else if (turn.status === 'replied' && turn.reply) {
        const cp = document.createElement('button');
        cp.type = 'button';
        cp.className = 'fui-btn';
        cp.textContent = '复制';
        cp.addEventListener('click', async () => {
          if (await safeCopyText(turn.reply)) {
            cp.textContent = '已复制';
            setTimeout(() => { cp.textContent = '复制'; }, 1200);
          }
        });
        const spk = document.createElement('button');
        spk.type = 'button';
        spk.className = 'fui-btn';
        spk.textContent = '朗读';
        spk.addEventListener('click', () => void speakReply(turn.reply, { force: true }));
        const rty = document.createElement('button');
        rty.type = 'button';
        rty.className = 'fui-btn';
        rty.textContent = '重答';
        rty.addEventListener('click', () => void sendText(turn.user));
        actions.append(cp, spk, rty);
      } else if ((turn.status === 'error' || turn.status === 'cancelled') && turn.user) {
        const rty = document.createElement('button');
        rty.type = 'button';
        rty.className = 'fui-btn';
        rty.textContent = '重答';
        rty.addEventListener('click', () => void sendText(turn.user));
        actions.append(rty);
      }
      if (actions.children.length) a.append(actions);
    }
  }
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
  const latest = allTurns.at(-1);
  if (!firstRenderDone) {
    firstRenderDone = true;
    lastSpokenTurn = latest?.id || '';
    return;
  }
  if (ttsOn && !micStream && latest && latest.status === 'replied' && latest.reply && latest.id !== lastSpokenTurn) {
    lastSpokenTurn = latest.id;
    void speakReply(latest.reply);
  }
}

let sendingText = false;
let pendingNewChat = null;
async function sendText(text) {
  const clean = String(text || '').trim();
  if (!clean || sendingText) return false;
  sendingText = true;
  try {
    if (pendingNewChat) await pendingNewChat.catch(() => {});
    const res = await postJson('/api/message', { text: clean, chat_id: chatId || undefined });
    if (res && Array.isArray(res.turns)) render(res);
    return Boolean(res);
  } finally {
    sendingText = false;
  }
}

chatSel?.addEventListener('change', async () => {
  const prev = chatId;
  const target = chatSel.value;
  if (!target || target === chatId) return;
  const r = await postJson('/api/switch-chat', { id: target });
  if (r && Array.isArray(r.turns)) render(r);
  else if (prev) chatSel.value = prev;
});
persSel.addEventListener('change', async () => {
  const prev = latestMobileState?.personality_id || persSel.value;
  const r = await postJson('/api/select-personality', { id: persSel.value });
  if (r && Array.isArray(r.turns)) render(r);
  else persSel.value = prev;
});
modelSel.addEventListener('change', async () => {
  const prev = latestMobileState?.model_id || '';
  const r = await postJson('/api/select-model', { id: modelSel.value });
  if (!r && prev) modelSel.value = prev;
});
ttsBtn.addEventListener('click', () => {
  ttsOn = !ttsOn;
  try { localStorage.setItem('moss-mobile.tts', ttsOn ? '1' : '0'); } catch { /* ignore */ }
  if (!ttsOn) stopSpeakAudio();
  ttsBtn.setAttribute('aria-pressed', String(ttsOn));
  ttsBtn.textContent = ttsOn ? '播报 开' : '播报 关';
});
mediaToggle.addEventListener('click', () => {
  if (audioEl.paused) {
    ensureAudioTap();
    audioEl.play().then(() => { mediaToggle.textContent = '暂停'; startVizLoop(); }).catch(() => {});
  } else {
    audioEl.pause();
    mediaToggle.textContent = '播放';
    stopVizLoop();
  }
});
mediaCover.addEventListener('error', () => { mediaCover.hidden = true; });
mediaPrev.addEventListener('click', () => {
  const audioItems = mobileMediaItems.filter((it) => it && it.kind !== 'video');
  const cur = audioItems.findIndex((it) => it.media_id === activeMediaId);
  if (cur > 0) {
    const prevItem = audioItems[cur - 1];
    const rawIdx = mobileMediaItems.indexOf(prevItem);
    void selectMobileTrack(prevItem, rawIdx >= 0 ? rawIdx + 1 : cur);
  }
});
mediaNext.addEventListener('click', () => {
  const audioItems = mobileMediaItems.filter((it) => it && it.kind !== 'video');
  const cur = audioItems.findIndex((it) => it.media_id === activeMediaId);
  if (cur >= 0 && cur + 1 < audioItems.length) {
    const nextItem = audioItems[cur + 1];
    const rawIdx = mobileMediaItems.indexOf(nextItem);
    void selectMobileTrack(nextItem, rawIdx >= 0 ? rawIdx + 1 : cur + 2);
  }
});
const MOBILE_SLEEP_STEPS = [0, 15, 30, 60];
mediaSleep.addEventListener('click', () => {
  const curRem = mobileSleepEnd ? Math.ceil((mobileSleepEnd - Date.now()) / 60000) : 0;
  const nextIdx = MOBILE_SLEEP_STEPS.findIndex((m) => m > curRem);
  armMobileSleep(nextIdx >= 0 ? MOBILE_SLEEP_STEPS[nextIdx] : 0);
});
const MOBILE_SPEEDS = [1, 1.25, 1.5, 2];
mediaSpeed?.addEventListener('click', () => {
  const cur = audioEl.playbackRate || 1;
  const idx = MOBILE_SPEEDS.findIndex((s) => Math.abs(s - cur) < 0.05);
  const next = MOBILE_SPEEDS[(idx + 1) % MOBILE_SPEEDS.length];
  audioEl.playbackRate = next;
  mediaSpeed.textContent = `${next}x`;
});
mediaSeek?.addEventListener('pointerdown', () => { seekingMedia = true; });
mediaSeek?.addEventListener('input', () => {
  if (!Number.isFinite(audioEl.duration) || audioEl.duration <= 0) return;
  const target = (Number(mediaSeek.value) / 1000) * audioEl.duration;
  if (mediaTime) mediaTime.textContent = `${formatSec(target)} / ${formatSec(audioEl.duration)}`;
});
mediaSeek?.addEventListener('change', () => {
  seekingMedia = false;
  if (Number.isFinite(audioEl.duration) && audioEl.duration > 0) {
    audioEl.currentTime = (Number(mediaSeek.value) / 1000) * audioEl.duration;
  }
});
audioEl.addEventListener('timeupdate', () => {
  if (mobileLrcLines.length) syncMobileLrc(audioEl.currentTime);
  if (!seekingMedia) {
    const cur = audioEl.currentTime || 0;
    const dur = audioEl.duration;
    if (Number.isFinite(dur) && dur > 0) {
      if (mediaSeek) mediaSeek.value = String(Math.round((cur / dur) * 1000));
      if (mediaTime) mediaTime.textContent = `${formatSec(cur)} / ${formatSec(dur)}`;
    } else if (mediaTime) {
      mediaTime.textContent = activeMediaId ? `${formatSec(cur)} / LIVE` : '00:00 / 00:00';
    }
  }
});
audioEl.addEventListener('play', () => {
  mediaToggle.textContent = '暂停';
  startVizLoop();
  if (isAdminRole && activeMediaProvider === 'navidrome' && activeMediaId && !mobileScrobbledStart) {
    mobileScrobbledStart = true;
    fetch('/api/online-music/scrobble', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_id: activeMediaId, submission: false }),
    }).catch(() => {});
  }
});
audioEl.addEventListener('pause', () => { mediaToggle.textContent = '播放'; stopVizLoop(); });
audioEl.addEventListener('ended', () => {
  mediaToggle.textContent = '播放';
  stopVizLoop();
  if (isAdminRole && activeMediaProvider === 'navidrome' && activeMediaId && !mobileScrobbledDone) {
    mobileScrobbledDone = true;
    fetch('/api/online-music/scrobble', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_id: activeMediaId, submission: true }),
    }).catch(() => {});
  }
});
newChatBtn.addEventListener('click', () => {
  if (pendingNewChat) return;
  newChatBtn.disabled = true;
  pendingNewChat = postJson('/api/new-chat', {})
    .then((r) => { if (r && Array.isArray(r.turns)) render(r); })
    .finally(() => {
      pendingNewChat = null;
      newChatBtn.disabled = false;
    });
});

function resamplePcm16(floatBuf, inRate) {
  const ratio = inRate / 16000;
  const outLen = Math.max(1, Math.floor(floatBuf.length / ratio));
  const pcm = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const s = Math.max(-1, Math.min(1, floatBuf[Math.floor(i * ratio)] || 0));
    pcm[i] = s < 0 ? s * 32768 : s * 32767;
  }
  return pcm;
}

function encodeWav16k(floatBuf, inRate) {
  const pcm = resamplePcm16(floatBuf, inRate);
  const wav = new ArrayBuffer(44 + pcm.byteLength);
  const view = new DataView(wav);
  const writeStr = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); };
  writeStr(0, 'RIFF'); view.setUint32(4, 36 + pcm.byteLength, true); writeStr(8, 'WAVE');
  writeStr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  writeStr(36, 'data'); view.setUint32(40, pcm.byteLength, true);
  new Int16Array(wav, 44).set(pcm);
  return wav;
}

let liveStreamSession = null;
let liveStreamChain = Promise.resolve();
let liveStreamFailed = false;
let micLockRelease = null;

function startMobileStreamSession() {
  liveStreamSession = null;
  liveStreamFailed = false;
  liveStreamChain = fetch('/api/transcribe/stream/start', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (d?.session) liveStreamSession = d.session;
      else liveStreamFailed = true;
    })
    .catch(() => { liveStreamFailed = true; });
}

function enqueueMobileStreamChunk(floatChunk, rate) {
  if (liveStreamFailed) return;
  const pcm = resamplePcm16(floatChunk, rate);
  const raw = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  if (!raw.byteLength) return;
  liveStreamChain = liveStreamChain.then(async () => {
    if (liveStreamFailed || !liveStreamSession) return;
    const resp = await fetch(`/api/transcribe/stream/chunk?session=${encodeURIComponent(liveStreamSession)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: raw,
    });
    if (!resp.ok) liveStreamFailed = true;
  }).catch(() => { liveStreamFailed = true; });
}

async function resetMobileStreamSession(sid) {
  if (!sid) return;
  await fetch('/api/transcribe/stream/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: sid }),
  }).catch(() => {});
}

async function transcribeMobileAudio(merged, rate) {
  try {
    await liveStreamChain;
  } catch { /* ignore */ }
  const sid = liveStreamSession;
  liveStreamSession = null;
  if (sid && !liveStreamFailed) {
    try {
      const fin = await fetch('/api/transcribe/stream/commit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: sid }),
      });
      if (fin.status === 401) {
        await resetMobileStreamSession(sid);
        return { status: 401 };
      }
      if (fin.ok) return { status: 200, data: await fin.json() };
    } catch { /* fallback below */ }
    await resetMobileStreamSession(sid);
  } else if (sid) {
    await resetMobileStreamSession(sid);
  }
  const resp = await fetch('/api/transcribe', {
    method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: encodeWav16k(merged, rate),
  });
  if (resp.status === 401) return { status: 401 };
  if (!resp.ok) return { status: resp.status };
  return { status: 200, data: await resp.json() };
}

async function releaseMicResources() {
  if (preMicAudioVol !== null) {
    audioEl.volume = preMicAudioVol;
    preMicAudioVol = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  try { micNodeSrc?.disconnect(); } catch { /* ignore */ }
  try { micNodeGain?.disconnect(); } catch { /* ignore */ }
  const proc = micNodeProc;
  micNodeSrc = null; micNodeProc = null; micNodeGain = null;
  if (proc) {
    try { proc.port?.postMessage('flush'); } catch { /* ignore */ }
    if (proc.port) await new Promise((resolve) => setTimeout(resolve, 35));
    if (proc.port) proc.port.onmessage = null;
    proc.onaudioprocess = null;
    try { proc.disconnect(); } catch { /* ignore */ }
  }
  const ctx = micCtx;
  micCtx = null;
  if (ctx) await ctx.close?.().catch(() => {});
  if (micLockRelease) {
    try { micLockRelease(); } catch { /* ignore */ }
    micLockRelease = null;
  }
}

micBtn.addEventListener('click', async () => {
  if (!navigator.mediaDevices?.getUserMedia) {
    const httpsPort = Number(location.port) === 18221 ? 18443 : location.port;
    const hint = !window.isSecureContext
      ? `手机/局域网访问麦克风需要 HTTPS 安全上下文：请改用 https://${location.hostname}:${httpsPort}/mobile 并在浏览器中信任自签名证书。`
      : '当前浏览器环境暂不支持麦克风采集。';
    flashLinkError(!window.isSecureContext ? 'HTTPS REQ' : 'NO MIC');
    micBtn.title = hint;
    micBtn.textContent = !window.isSecureContext ? '需HTTPS' : '无麦';
    return;
  }
  if (openingMic || stoppingMic) return;
  if (micStream) {
    stoppingMic = true;
    micBtn.disabled = true;
    micBtn.textContent = '识别中';
    const rate = micCtx?.sampleRate || 48000;
    try {
      await releaseMicResources();
      const total = micChunks.reduce((n, b) => n + b.length, 0);
      const merged = new Float32Array(total);
      let off = 0;
      for (const c of micChunks) { merged.set(c, off); off += c.length; }
      micChunks = [];
      const res = await transcribeMobileAudio(merged, rate);
      if (res.status === 401) {
        openAuthPrompt('需要重新验证 4 位授权码');
      } else if (res.status === 200) {
        if (res.data?.text) await sendText(res.data.text);
      } else {
        flashLinkError(`HTTP ${res.status}`);
      }
    } catch {
      flashLinkError('ASR ERR');
    } finally {
      stoppingMic = false;
      micBtn.disabled = false;
      micBtn.textContent = '语音';
    }
    return;
  }
  openingMic = true;
  const myGen = ++micOpenGen;
  stopSpeakAudio();
  if (!audioEl.paused && audioEl.volume > 0.25) {
    preMicAudioVol = audioEl.volume;
    audioEl.volume = 0.2;
  }
  try {
    if (navigator.locks?.request) {
      let acquired = false;
      await new Promise((resolve) => {
        void navigator.locks.request('moss-microphone', { ifAvailable: true }, (lock) => {
          if (!lock) {
            resolve();
            return;
          }
          acquired = true;
          return new Promise((relLock) => {
            micLockRelease = relLock;
            resolve();
          });
        });
      });
      if (!acquired || myGen !== micOpenGen) {
        if (myGen !== micOpenGen) await releaseMicResources();
        else flashLinkError('MIC BUSY');
        openingMic = false;
        return;
      }
    }
    const { stream } = await openBestMicStream({
      baseAudio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (myGen !== micOpenGen) {
      stream?.getTracks().forEach((t) => t.stop());
      await releaseMicResources();
      return;
    }
    micStream = stream;
    micCtx = new AudioContext();
    micNodeSrc = micCtx.createMediaStreamSource(micStream);
    micChunks = [];
    startMobileStreamSession();
    const rate = micCtx.sampleRate || 48000;
    const silentGain = micCtx.createGain();
    silentGain.gain.value = 0;
    if (micCtx.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
      await micCtx.audioWorklet.addModule('/static/voice-capture.js');
      if (myGen !== micOpenGen || !micCtx) {
        await releaseMicResources();
        return;
      }
      const worklet = new AudioWorkletNode(micCtx, 'moss-voice-capture');
      worklet.port.onmessage = (ev) => {
        if (ev.data instanceof Float32Array) {
          const copy = new Float32Array(ev.data);
          micChunks.push(copy);
          enqueueMobileStreamChunk(copy, rate);
        }
      };
      micNodeSrc.connect(worklet);
      worklet.connect(silentGain);
      silentGain.connect(micCtx.destination);
      micNodeProc = worklet;
      micNodeGain = silentGain;
    } else {
      const proc = micCtx.createScriptProcessor(4096, 1, 1);
      proc.onaudioprocess = (ev) => {
        const copy = new Float32Array(ev.inputBuffer.getChannelData(0));
        micChunks.push(copy);
        enqueueMobileStreamChunk(copy, rate);
      };
      micNodeSrc.connect(proc);
      proc.connect(silentGain);
      silentGain.connect(micCtx.destination);
      micNodeProc = proc;
      micNodeGain = silentGain;
    }
    micBtn.textContent = '结束';
  } catch {
    await resetMobileStreamSession(liveStreamSession);
    liveStreamSession = null;
    micChunks = [];
    await releaseMicResources();
    micBtn.textContent = '无麦';
  } finally {
    openingMic = false;
  }
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !audioEl.paused && !audioEl.ended) startVizLoop();
});
window.addEventListener('pagehide', () => {
  micOpenGen += 1;
  clearInterval(mobileSleepTick);
  mobileSleepTick = 0;
  if (mobileLrcAbort) { mobileLrcAbort.abort(); mobileLrcAbort = null; }
  void releaseMicResources();
  stopSpeakAudio();
  if (es) { es.close(); es = null; }
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const val = input.value;
  if (!val.trim()) return;
  if (await sendText(val)) input.value = '';
});
for (const btn of document.querySelectorAll('[data-quick]')) {
  btn.addEventListener('click', () => void sendText(btn.dataset.quick));
}

let sseAuthChecking = false;
function connectLive() {
  void loadModels();
  if (es) es.close();
  es = new EventSource('/api/events');
  es.addEventListener('state', (ev) => {
    linkEl.dataset.state = 'ok';
    linkEl.textContent = 'ONLINE';
    try { render(JSON.parse(ev.data)); } catch { /* ignore */ }
  });
  es.addEventListener('brain-alert', (ev) => {
    try { showMobileAlert(JSON.parse(ev.data)); } catch { /* ignore */ }
  });
  es.addEventListener('auth-required', () => {
    openAuthPrompt('授权已变更，请重新验证 4 位授权码');
  });
  es.onerror = async () => {
    linkEl.dataset.state = 'warn';
    linkEl.textContent = 'RETRY';
    if (sseAuthChecking) return;
    sseAuthChecking = true;
    try {
      const r = await fetch('/api/auth/status', { cache: 'no-store' });
      if (r.ok) {
        const info = await r.json();
        if (info.enabled && !info.authorized) openAuthPrompt('需要验证 4 位授权码');
      }
    } catch { /* ignore */ } finally {
      sseAuthChecking = false;
    }
  };
}

function applyRole(info) {
  isAdminRole = info?.role === 'CS' || (Boolean(info?.is_admin) && info?.role !== 'C');
  const nextCode = String(info?.code || (isAdminRole ? 'MOSS' : '')).toUpperCase() || 'MOSS';
  if (nextCode !== activeOwnerCode) {
    activeOwnerCode = nextCode;
    chatId = '';
    firstRenderDone = false;
    lastSpokenTurn = '';
    feed.replaceChildren();
    syncMedia([]);
  }
  if (authChip) {
    authChip.textContent = `${isAdminRole ? 'CS' : 'C'} · ${activeOwnerCode}`;
    authChip.dataset.state = isAdminRole ? 'intel' : 'data';
  }
  modelSel.disabled = !isAdminRole;
  modelSel.title = isAdminRole ? '推理模型（管理级权限可切换）' : '仅系统管理员有权切换推理模型';
}

authChip?.addEventListener('click', () => openAuthPrompt('', { switchMode: true }));

authForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  authErr.hidden = true;
  let resp;
  try {
    resp = await fetch('/api/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: authInput.value }),
    });
  } catch {
    authErr.textContent = '网络或服务连接失败，请稍后重试';
    authErr.hidden = false;
    return;
  }
  if (resp.ok) {
    const d = await resp.json().catch(() => ({}));
    applyRole(d);
    authOverlay.hidden = true;
    if (shellEl) shellEl.inert = false;
    authInput.value = '';
    connectLive();
  } else {
    const d = await resp.json().catch(() => ({}));
    authErr.textContent = d.error || '授权码错误';
    authErr.hidden = false;
  }
});

(async () => {
  try {
    const r = await fetch('/api/auth/status', { cache: 'no-store' });
    if (r.ok) {
      const info = await r.json();
      if (info.enabled && !info.authorized) {
        openAuthPrompt('');
        return;
      }
      applyRole(info);
    } else {
      if (linkEl) { linkEl.textContent = 'OFFLINE'; linkEl.dataset.state = 'warn'; }
      return;
    }
  } catch {
    if (linkEl) { linkEl.textContent = 'OFFLINE'; linkEl.dataset.state = 'warn'; }
    return;
  }
  connectLive();
  void initMobilePwaAndPush();
})();
