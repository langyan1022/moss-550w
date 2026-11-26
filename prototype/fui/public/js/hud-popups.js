/**
 * Game-FUI Universal Contextual Viewer & HUD Projection Engine (.moss-hud-layer).
 *
 * Non-blocking, MOSS-avoiding floating tactical HUD windows docked beside the 3D 550W stage
 * (automatically shifting the 550W canvas aside on wide screens so MOSS stays unobstructed;
 * expandable on demand for PDF / Word / widescreen video):
 *   1. intel    — 图文档案查看器 (multi-slide visual plate + spec matrix + archive tabs)
 *   2. image    — 图片专业查看器 (zoom 50-250%, rotate 90°, fit, gallery thumbs)
 *   3. media    — 音频 / 视频专用播放器 (AUD-02 equalizer + seek / VID-02 speed + monitor)
 *   4. doc      — 文件查看器 (PDF iframe / Word .docx OOXML parser / Markdown & Text reader)
 *   5. weather  — 气象情报投影 (hero temp + SVG glyph + precip gauge + 3-day outlook)
 *   6. host     — 本机实时状态投影 (Wandering Earth split-unit clock + 4 bilingual KPI cards)
 *   7. action   — 操作核验闸门 / 草稿回执 (hazard warning gate + direct confirm/cancel)
 *   8. context  — 上下文采集工具 (clipboard / window OCR / screenshot)
 *   9. reminder — 到点提醒战术浮窗
 */
import { createOrbit, drawEnvelope, drawOrbit, drawSpectrum, pushEnvelope, toBands } from '../vendor/fui-audio.js';
import { classifyFileViewer, parseDocxBuffer } from './doc-parser.js';
import { fixed, formatClock, formatCoord, getLocale, t, weatherCondition } from './i18n.js';
import { INTEL_DOSSIERS, matchIntelDossier } from './intel-dossiers.js';
import { safeUuid } from './safe-web.js';
import { escapeHtml, GIB, localizeCity, safePopupUrl } from './hud/utils.js';
import {
  populateActionCard,
  populateContextCard,
  populateDocCard,
  populateImageCard,
  populateIntelCard,
} from './hud/intel-image-doc.js';
import {
  populateHostCard,
  populateWeatherCard,
  renderDueReminders,
  updateHostHardware,
} from './hud/weather-host-view.js';

export { INTEL_DOSSIERS, matchIntelDossier };

export function createHudPopupsController({
  stage,
  telemetry,
  isTtsEnabled,
  onSelectMedia,
  onMediaEvent,
  onApprove,
  onCancel,
  onOpenLocalStatusModal,
  onOpenForecastModal,
  onOpenPopout,
  onSendFollowUp,
  onContextSubmit,
  onDismissTimer,
  onLocateTurn,
  onAutoModeChange,
}) {
  const stageWrapEl = document.getElementById('stage-canvas-wrap');
  const layerEl = document.getElementById('hud-layer');
  const trayEl = document.getElementById('hud-tray');
  const reminderStackEl = document.getElementById('hud-reminders');
  const fileInputEl = document.getElementById('hud-file-input');

  const AUTO_HIDE_AFTER_SPEECH_MS = 5000;
  const AUTO_HIDE_SILENT_MS = 8000;

  let autoPop = true;
  try { autoPop = localStorage.getItem('moss-fui.hud-auto') !== 'off'; } catch { /* ignore */ }

  /** Registry of session cards keyed by `${type}:${id}` */
  const registry = new Map();
  const openKeys = new Set();
  const mountedCards = new Map();
  let deviceStale = false;
  const pinnedKeys = new Set();
  const autoOpenedKeys = new Set();
  const hoveredKeys = new Set();
  const autoHideTimers = new Map();
  let speechState = 'idle'; // 'idle' | 'preparing' | 'speaking'
  let activeKey = null;
  let dockSide = 'left'; // 'left' | 'right' (avoids MOSS 550W in center!)
  let expandedKey = null;
  let mediaMini = false;
  try { mediaMini = localStorage.getItem('moss-fui.media-mini') === '1'; } catch { /* ignore */ }
  const slideIndexMap = new Map();
  const imageViewStates = new Map();
  const lastAutoTurnSignature = new Map();
  let activeMediaEl = null;
  let activeMediaIdentity = null;
  let sleepTimerEnd = 0;
  let sleepTimerTick = 0;
  let sleepFadeTimer = 0;
  let mediaAbort = null;

  function clearSleepFade() {
    if (sleepFadeTimer) {
      clearInterval(sleepFadeTimer);
      sleepFadeTimer = 0;
    }
  }

  function updateSleepButtons() {
    const remMin = sleepTimerEnd ? Math.max(0, Math.ceil((sleepTimerEnd - Date.now()) / 60000)) : 0;
    for (const btn of document.querySelectorAll('[data-media-sleep]')) {
      btn.textContent = remMin > 0 ? t('hud.sleep_min', { m: remMin }) : t('hud.sleep_off');
      if (remMin > 0) btn.dataset.active = 'true';
      else delete btn.dataset.active;
    }
  }

  function armSleepTimer(minutes) {
    clearInterval(sleepTimerTick);
    sleepTimerTick = 0;
    clearSleepFade();
    const mins = Math.max(0, Math.min(240, Number(minutes) || 0));
    if (mins <= 0) {
      sleepTimerEnd = 0;
      updateSleepButtons();
      return;
    }
    sleepTimerEnd = Date.now() + mins * 60000;
    updateSleepButtons();
    sleepTimerTick = setInterval(() => { // fui-lint-ignore: R15 bounded countdown only while user-armed sleep timer is active
      const rem = sleepTimerEnd - Date.now();
      if (rem <= 0) {
        clearInterval(sleepTimerTick);
        sleepTimerTick = 0;
        sleepTimerEnd = 0;
        updateSleepButtons();
        // Smooth 1.2s fade-out then pause
        const media = activeMediaEl;
        if (media && media.isConnected && !media.paused) {
          const origVol = media.volume;
          let step = 0;
          clearSleepFade();
          sleepFadeTimer = setInterval(() => { // fui-lint-ignore: R15 bounded 12-step volume fade-out timer
            step += 1;
            if (!media.isConnected || activeMediaEl !== media) {
              clearSleepFade();
              return;
            }
            media.volume = Math.max(0, origVol * (1 - step / 12));
            if (step >= 12) {
              clearSleepFade();
              media.pause();
              media.volume = origVol;
            }
          }, 100);
        }
        return;
      }
      updateSleepButtons();
    }, 5000);
  }
  let repaintIdlePlayer = null;
  let latestState = null;
  let playbackRequest = null;
  let recoveringTrack = null;
  let lastStartedAtUs = 0;
  const attemptedPlay = new Set();
  let simAudioTimer = 0;
  let simAudioPlaying = false;
  let simAudioPos = 0;
  let simAudioDuration = 228;
  let objectUrls = [];
  window.addEventListener('pagehide', () => {
    for (const u of objectUrls) {
      try { URL.revokeObjectURL(u); } catch { /* ignore */ }
    }
    objectUrls = [];
  });

  function trackObjectUrl(card, url) {
    card._objectUrl = url;
    objectUrls.push(url);
    const localCards = [...registry.values()].filter((c) => c.local && c._objectUrl && !openKeys.has(c.key));
    while (localCards.length > 10) {
      const old = localCards.shift();
      if (!old) break;
      if (old._objectUrl) {
        try { URL.revokeObjectURL(old._objectUrl); } catch { /* ignore */ }
        objectUrls = objectUrls.filter((x) => x !== old._objectUrl);
      }
      registry.delete(old.key);
    }
  }

  /* ── Sync MOSS-Avoiding Stage Offset ── */
  function syncDockGeometry() {
    if (layerEl) {
      layerEl.dataset.hudCount = String(openKeys.size);
      layerEl.toggleAttribute('data-hud-media', [...openKeys].some((key) => registry.get(key)?.type === 'media'));
    }
    if (!stageWrapEl) return;
    if (!openKeys.size) {
      delete stageWrapEl.dataset.hudDock;
    } else {
      stageWrapEl.dataset.hudDock = expandedKey ? 'expand' : dockSide;
    }
  }

  /* ── Simulated Audio/Video Playback Helper (drives equalizer + stage orbit) ── */
  function stopSimAudio() {
    clearInterval(simAudioTimer);
    simAudioTimer = 0;
    simAudioPlaying = false;
    stage?.pushAudioFrame?.('standby', null);
  }

  function startSimAudio(track, onTick) {
    stopSimAudio();
    simAudioPlaying = true;
    simAudioDuration = track.duration_sec || 228;
    simAudioPos = Math.min(simAudioPos, simAudioDuration - 1);
    const freq = new Uint8Array(256);
    const time = new Float32Array(512);
    let step = 0;
    simAudioTimer = setInterval(() => { // fui-lint-ignore: R15 active strictly while simulated demo media track plays
      if (!simAudioPlaying || document.hidden) return;
      step += 1;
      simAudioPos = (simAudioPos + 0.2) % simAudioDuration;
      for (let i = 0; i < freq.length; i++) {
        const wave = 0.5 + 0.5 * Math.sin(step * 0.35 + i * 0.14);
        const env = Math.exp(-((i - 28) ** 2) / 900);
        freq[i] = Math.round(255 * env * (0.28 + 0.62 * wave));
      }
      for (let i = 0; i < time.length; i++) {
        time[i] = 0.16 * Math.sin(step * 0.4 + i * 0.09) * Math.cos(i * 0.02);
      }
      stage?.pushAudioFrame?.('speaking', { freq, time, sampleRate: 48000 });
      onTick?.(simAudioPos, simAudioDuration, freq);
    }, 200);
  }

  function formatMmSs(sec) {
    if (!Number.isFinite(sec)) return '00:00';
    const s = Math.max(0, Math.floor(sec));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }

  function formatBytes(n) {
    if (!Number.isFinite(n) || n <= 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  }

  /* ── Post-Speech Auto-Hide for Dialogue-Triggered HUD Popups ── */
  function isMediaActive() {
    return Boolean(simAudioPlaying || (activeMediaEl && !activeMediaEl.paused && !activeMediaEl.ended));
  }

  function clearAutoHideTimer(key) {
    const timer = autoHideTimers.get(key);
    if (timer) {
      clearTimeout(timer);
      autoHideTimers.delete(key);
    }
  }

  function cancelAutoHide(key, { keepEligible = false } = {}) {
    clearAutoHideTimer(key);
    if (!keepEligible) autoOpenedKeys.delete(key);
  }

  function canAutoHide(key) {
    if (!autoOpenedKeys.has(key) || !openKeys.has(key)) return false;
    if (document.body?.dataset?.frozen === 'true') return false;
    if (pinnedKeys.has(key) || expandedKey === key || hoveredKeys.has(key)) return false;
    const card = registry.get(key);
    if (!card || card.local) return false;
    if (card.type === 'context') return false;
    if (card.turn?.status === 'pending' || card.turn?.status === 'thinking' || card.turn?.status === 'saving') return false;
    if (card.type === 'media' && (card.pendingQuery || isMediaActive())) return false;
    return true;
  }

  function scheduleAutoHide(key, delayMs = AUTO_HIDE_AFTER_SPEECH_MS) {
    clearAutoHideTimer(key);
    if (!canAutoHide(key)) return;
    if (speechState !== 'idle') return;
    const timer = setTimeout(() => {
      autoHideTimers.delete(key);
      if (speechState === 'idle' && canAutoHide(key)) closeActive(key);
    }, delayMs);
    autoHideTimers.set(key, timer);
  }

  function notifySpeechState({ active = false, preparing = false } = {}) {
    const prev = speechState;
    speechState = active ? 'speaking' : preparing ? 'preparing' : 'idle';
    if (speechState !== 'idle') {
      for (const key of autoOpenedKeys) clearAutoHideTimer(key);
      return;
    }
    if (prev !== 'idle') {
      for (const key of [...autoOpenedKeys]) scheduleAutoHide(key, AUTO_HIDE_AFTER_SPEECH_MS);
    }
  }

  /* ── Register / Update Cards ── */
  function hideCard(key) {
    cancelAutoHide(key);
    hoveredKeys.delete(key);
    openKeys.delete(key);
    pinnedKeys.delete(key);
    if (expandedKey === key) expandedKey = null;
    if (activeKey === key) activeKey = [...openKeys].at(-1) || null;
  }

  function showCard(key, { replaceMedia = true, auto = false } = {}) {
    const card = registry.get(key);
    if (!card) return;
    if (auto) autoOpenedKeys.add(key);
    else cancelAutoHide(key);
    if (!openKeys.has(key)) {
      if (card.type === 'media') {
        const previous = [...openKeys].find((openKey) => registry.get(openKey)?.type === 'media');
        if (previous) {
          if (!replaceMedia && activeMediaEl) return;
          hideCard(previous);
        }
      }
      if (openKeys.size >= 3) {
        const candidates = [...openKeys].filter((openKey) => {
          const openCard = registry.get(openKey);
          return openCard?.type !== 'media' && openCard?.turn?.status !== 'pending';
        });
        const evict = candidates.find((openKey) => !pinnedKeys.has(openKey)) ||
          (card.type === 'media' ? candidates[0] : null);
        if (!evict) return;
        hideCard(evict);
      }
      openKeys.add(key);
    }
    activeKey = key;
  }

  function registerCard(card, { open = false, refresh = false } = {}) {
    registry.set(card.key, card);
    if (open) showCard(card.key);
    renderTray();
    if (openKeys.has(card.key)) renderOpenCards({ refreshKey: refresh ? card.key : null });
    onAutoModeChange?.({ autoPop, count: registry.size, activeKey });
  }

  /* ── Open Local File Directly into Dedicated FUI Viewer (PDF / Word / Image / Audio / Video / Text) ── */
  async function openLocalFile(file) {
    if (!file) return;
    const kind = classifyFileViewer(file.name, file.type);
    const id = `local-${Date.now()}`;
    const sizeText = formatBytes(file.size);

    if (kind === 'image') {
      const url = URL.createObjectURL(file);
      const card = {
        key: `image:${id}`,
        type: 'image',
        code: 'IMG-02',
        state: 'data',
        chipLabel: () => `${t('hud.chip_image')} · ${file.name.slice(0, 8)}`,
        images: [{ src: url, title: file.name, caption: `${file.type || 'IMAGE'} · ${sizeText}` }],
        local: true,
      };
      trackObjectUrl(card, url);
      registerCard(card, { open: true });
      return;
    }

    if (kind === 'audio' || kind === 'video') {
      const url = URL.createObjectURL(file);
      const item = { media_id: id, url, title: file.name, artist: sizeText || 'LOCAL FILE', kind };
      const card = {
        key: `media:${id}`,
        type: 'media',
        code: kind === 'video' ? 'VID-02' : 'AUD-02',
        state: kind === 'video' ? 'data' : 'ok',
        chipLabel: () => `${t(kind === 'video' ? 'hud.chip_video' : 'hud.chip_audio')} · ${file.name.slice(0, 8)}`,
        turn: { id, chat_id: 'local', media: { title: file.name, items: [item], selected: item } },
        local: true,
      };
      trackObjectUrl(card, url);
      registerCard(card, { open: true });
      return;
    }

    if (kind === 'pdf') {
      const url = URL.createObjectURL(file);
      const card = {
        key: `doc:${id}`,
        type: 'doc',
        code: 'PDF-06',
        state: 'intel',
        chipLabel: () => `PDF · ${file.name.slice(0, 8)}`,
        doc: { format: 'pdf', title: file.name, size: sizeText, url },
        local: true,
      };
      trackObjectUrl(card, url);
      registerCard(card, { open: true });
      return;
    }

    if (kind === 'word') {
      try {
        const buf = await file.arrayBuffer();
        const blocks = await parseDocxBuffer(buf);
        registerCard({
          key: `doc:${id}`,
          type: 'doc',
          code: 'DOCX-06',
          state: 'intel',
          chipLabel: () => `WORD · ${file.name.slice(0, 8)}`,
          doc: { format: 'word', title: file.name, size: sizeText, blocks },
          local: true,
        }, { open: true });
      } catch (err) {
        registerCard({
          key: `doc:${id}`,
          type: 'doc',
          code: 'DOCX-06',
          state: 'warn',
          chipLabel: () => `WORD · ${file.name.slice(0, 8)}`,
          doc: { format: 'text', title: file.name, size: sizeText, text: `${t('hud.doc_parse_err')}: ${err.message}` },
          local: true,
        }, { open: true });
      }
      return;
    }

    // Plain text / Markdown / JSON / Code
    const text = await file.text();
    registerCard({
      key: `doc:${id}`,
      type: 'doc',
      code: 'FILE-06',
      state: 'intel',
      chipLabel: () => `${t('hud.chip_doc')} · ${file.name.slice(0, 8)}`,
      doc: { format: 'text', title: file.name, size: sizeText, text: text.slice(0, 60000) },
      local: true,
    }, { open: true });
  }

  /* ── Build Contextual Cards from Conversation Turns ── */
  function buildCardsForTurn(turn) {
    const cards = [];
    if (!turn) return cards;

    if (turn.context_action) {
      cards.push({
        key: `context:${turn.id}`,
        type: 'context',
        code: 'CTX-06',
        state: 'data',
        chipLabel: () => t('hud.chip_context'),
        turn,
      });
    }
    if (turn.status === 'pending' || turn.receipt) {
      cards.push({
        key: `action:${turn.id}`,
        type: 'action',
        code: turn.status === 'pending' ? 'AUTH-05' : 'RCPT-05',
        state: turn.status === 'pending' ? 'warn' : 'ok',
        chipLabel: () => t(turn.status === 'pending' ? 'hud.chip_pending' : 'hud.chip_receipt'),
        turn,
      });
    }
    if (turn.document || turn.file) {
      const doc = turn.document || turn.file;
      const fmt = doc.format || classifyFileViewer(doc.filename || doc.title || '', doc.mime || '');
      const code = fmt === 'pdf' ? 'PDF-06' : fmt === 'word' ? 'DOCX-06' : 'FILE-06';
      cards.push({
        key: `doc:${turn.id}`,
        type: 'doc',
        code,
        state: 'intel',
        chipLabel: () => `${fmt.toUpperCase()} · ${String(doc.title || doc.filename || 'DOC').slice(0, 8)}`,
        turn,
        doc: { ...doc, format: fmt },
      });
    }
    if (turn.images?.length || (turn.media?.selected?.kind === 'image')) {
      const imgs = turn.images?.length
        ? turn.images.map((im, i) => typeof im === 'string'
          ? { src: im, title: `${t('hud.chip_image')} #${i + 1}`, caption: '' }
          : { src: im.url || im.src, title: im.title || `${t('hud.chip_image')} #${i + 1}`, caption: im.caption || '' })
        : (turn.media?.items || [turn.media.selected]).filter((x) => x && x.kind === 'image').map((x) => ({
          src: x.url || `/media/${encodeURIComponent(x.media_id)}`,
          title: x.title || x.filename || 'Image',
          caption: x.caption || x.artist || '',
        }));
      cards.push({
        key: `image:${turn.id}`,
        type: 'image',
        code: 'IMG-02',
        state: 'data',
        chipLabel: () => `${t('hud.chip_image')} · ${String(imgs[0]?.title || '').slice(0, 8)}`,
        turn,
        images: imgs,
      });
    } else if (turn.media?.items?.length || turn.media?.selected || turn.media_in_flight) {
      const sel = turn.media?.selected || turn.media?.items?.[0];
      const isVid = sel?.kind === 'video';
      const mTitle = sel?.title || turn.media?.title || '';
      cards.push({
        key: `media:${turn.id}`,
        type: 'media',
        code: isVid ? 'VID-02' : 'AUD-02',
        state: isVid ? 'data' : 'ok',
        chipLabel: () => mTitle ? `${t(isVid ? 'hud.chip_video' : 'hud.chip_media')} · ${mTitle.slice(0, 8)}` : t('hud.chip_media'),
        turn,
      });
    }
    if (turn.weather) {
      const cityLabel = localizeCity(turn.weather.city);
      cards.push({
        key: `weather:${turn.id}`,
        type: 'weather',
        code: 'METEO-03',
        state: 'data',
        chipLabel: () => `${t('hud.chip_weather')}${cityLabel ? ` · ${cityLabel}` : ''}`,
        turn,
      });
    }
    if (turn.local_overview?.length || turn.local_overview_partial?.length) {
      cards.push({
        key: `host:${turn.id}`,
        type: 'host',
        code: 'HOST-04',
        state: 'warn',
        chipLabel: () => t('hud.chip_host'),
        turn,
      });
    }
    const intelMatch = matchIntelDossier(turn);
    if (intelMatch) {
      const d = intelMatch.dossier || intelMatch.custom;
      cards.push({
        key: `intel:${d.id || turn.id}`,
        type: 'intel',
        code: d.code || 'INTEL-01',
        state: 'intel',
        chipLabel: () => {
          const isZh = getLocale().startsWith('zh');
          const title = intelMatch.dossier ? intelMatch.dossier[isZh ? 'zh' : 'en'].title : d.title;
          return `${t('hud.chip_intel')} · ${String(title || '').split(/[·(]/)[0].trim().slice(0, 8)}`;
        },
        turn,
        intelMatch,
      });
    }
    return cards;
  }

  /* ── Observe Full State Updates from API ── */
  function armAutoplay(chatId, text = null, target = null) {
    const request = {
      chatId,
      text,
      previous: new Set((latestState?.turns || []).map(turn => turn.id)),
      turnId: target?.turnId || null,
      prevMediaId: target?.prevMediaId || null,
    };
    playbackRequest = request;
    return () => { if (playbackRequest === request) playbackRequest = null; };
  }

  function selectQueueItem(card, item, index) {
    if (card.customMedia) {
      card.customMedia = { ...card.customMedia, selected: item, autoplay: true, playback: null };
      if (card.key) {
        for (const k of [...attemptedPlay]) {
          if (k.includes(card.key)) attemptedPlay.delete(k);
        }
      }
      renderOpenCards({ autoplayKey: card.key });
      return;
    }
    const prevMediaId = card.turn?.media?.selected?.media_id || null;
    for (const k of [...attemptedPlay]) {
      if (k.includes(card.turn.id)) attemptedPlay.delete(k);
    }
    const cancel = armAutoplay(card.turn.chat_id, null, { turnId: card.turn.id, prevMediaId });
    Promise.resolve(onSelectMedia?.({ chatId: card.turn.chat_id, turnId: card.turn.id, item, index })).catch(cancel);
  }

  function observeState(state, { initial = false, beatId = null } = {}) {
    if (latestState?.chat_id && latestState.chat_id !== state?.chat_id) {
      playbackRequest = null;
      recoveringTrack = null;
    }
    latestState = state;
    const turns = state?.turns || [];
    const seenKeys = new Set();
    let autoCandidates = [];

    // Keep core 550W dossier readily accessible in the HUD tray
    const defaultIntel = {
      key: 'intel:550w',
      type: 'intel',
      code: 'INTEL-550W',
      state: 'intel',
      chipLabel: () => `${t('hud.chip_intel')} · 550W`,
      turn: null,
      intelMatch: { dossier: INTEL_DOSSIERS[0] },
    };
    if (!registry.has(defaultIntel.key)) registry.set(defaultIntel.key, defaultIntel);
    seenKeys.add(defaultIntel.key);

    for (const turn of turns) {
      const cards = buildCardsForTurn(turn);
      for (const c of cards) {
        const prev = registry.get(c.key);
        if (prev?.customMedia && prev.turn?.media?.selected?.media_id === turn.media?.selected?.media_id) {
          c.customMedia = prev.customMedia;
        }
        seenKeys.add(c.key);
        registry.set(c.key, c);
      }

      const sig = JSON.stringify({
        st: turn.status,
        hasW: Boolean(turn.weather),
        hasL: (turn.local_overview?.length || 0) + (turn.local_overview_partial?.length || 0),
        hasM: Boolean(turn.media?.selected?.media_id || turn.media?.items?.length || turn.media_in_flight),
        hasD: Boolean(turn.document || turn.file),
        hasI: Boolean(turn.images?.length),
        hasR: Boolean(turn.receipt),
        hasC: Boolean(turn.context_action),
      });
      const prevSig = lastAutoTurnSignature.get(turn.id);
      lastAutoTurnSignature.set(turn.id, sig);

      const isLatest = turn === turns.at(-1);
      if (isLatest && cards.length) {
        const shouldAuto =
          (!initial && prevSig !== sig && autoPop && !pinnedKeys.has(activeKey)) ||
          (beatId === 'degraded' && turn.status === 'pending') ||
          (initial && new URLSearchParams(location.search).has('hud'));
        if (shouldAuto) {
          autoCandidates = cards;
        }
      }
    }

    // Prune stale turn cards while preserving user-loaded local files & active pending searches
    const hasRealMedia = [...seenKeys].some((k) => k.startsWith('media:') || k.startsWith('image:'));
    const hasThinkingTurn = turns.some((tr) => tr?.status === 'thinking' || tr?.status === 'saving');
    for (const [key, val] of [...registry.entries()]) {
      if (val.local) continue;
      if (!seenKeys.has(key) && (key !== 'media:pending' || hasRealMedia || !hasThinkingTurn)) {
        hideCard(key);
        registry.delete(key);
        slideIndexMap.delete(key);
        imageViewStates.delete(key);
      }
    }

    const hudParam = new URLSearchParams(location.search).get('hud');
    if (initial && hudParam) {
      const hit = [...registry.values()].find((c) => c.type === hudParam || c.key === hudParam);
      if (hit) showCard(hit.key);
    } else {
      for (const card of autoCandidates) showCard(card.key, { replaceMedia: false, auto: !hudParam });
    }

    const latest = turns.at(-1);
    const media = latest?.media;
    const request = playbackRequest;
    const inPlaceSelection = Boolean(
      request && request.turnId && latest?.id === request.turnId
      && media?.selected?.media_id && media.selected.media_id !== request.prevMediaId,
    );
    const eligible = request && latest?.chat_id === request.chatId && (
      (!request.previous.has(latest.id) && (request.text === null || latest.user === request.text))
      || inPlaceSelection
    );
    const approvedRecovery = recoveringTrack && latest?.id === recoveringTrack.turn_id &&
      latest.chat_id === recoveringTrack.chat_id && media?.recovered_from === recoveringTrack.media_id;
    const playNow = !initial && latest?.status === 'replied' && media?.selected && media.autoplay && (eligible || approvedRecovery);
    const autoplayKey = playNow ? `media:${latest.id}` : null;
    if (playNow) {
      showCard(autoplayKey);
      if (eligible) playbackRequest = null;
      if (approvedRecovery) recoveringTrack = null;
    } else if (eligible && ['replied', 'cancelled', 'error'].includes(latest.status)) {
      playbackRequest = null;
    }
    renderTray();
    renderOpenCards({ autoplayKey });
    for (const key of [...autoOpenedKeys]) {
      if (speechState === 'idle' && canAutoHide(key) && !autoHideTimers.has(key)) {
        // When TTS is enabled, give a brief window for speech preparation to claim the card;
        // once speech starts, notifySpeechState cancels this timer and restarts a 5s timer after speech finishes.
        scheduleAutoHide(key, isTtsEnabled?.() ? AUTO_HIDE_SILENT_MS + 4000 : AUTO_HIDE_SILENT_MS);
      }
    }
    renderDueReminders(reminderStackEl, state?.timers || [], { onLocateTurn, onDismissTimer });
    onAutoModeChange?.({ autoPop, count: registry.size, activeKey });
  }

  function showPendingMedia(queryText) {
    if (!autoPop || pinnedKeys.has(activeKey) || activeMediaEl) return;
    registerCard({
      key: 'media:pending',
      type: 'media',
      code: 'MEDIA-02',
      state: 'ok',
      chipLabel: () => t('hud.chip_media'),
      pendingQuery: queryText,
      turn: null,
    }, { open: true });
  }

  function openForTurn(turn, preferredType = null) {
    const cards = buildCardsForTurn(turn);
    if (!cards.length) return;
    const chosen = preferredType ? cards.find((c) => c.type === preferredType) || cards[0] : cards[0];
    registerCard(chosen, { open: true });
  }

  function openHostStatus() {
    registerCard({
      key: 'host:current',
      type: 'host',
      code: 'HOST-04',
      state: 'warn',
      chipLabel: () => t('hud.chip_host'),
      local: true,
    }, { open: true, refresh: true });
  }

  function openCurrentForecast(data, label) {
    if (!data) return;
    registerCard({
      key: 'weather:current',
      type: 'weather',
      code: 'METEO-03',
      state: 'data',
      chipLabel: () => label ? `${t('hud.chip_weather')} · ${label}` : t('hud.chip_weather'),
      turn: { weather: { ...data, ...data.current, city: label || data.city } },
      local: true,
    }, { open: true });
  }

  function openByType(type) {
    const match = [...registry.values()].reverse().find((c) => c.type === type);
    if (match) {
      showCard(match.key);
      renderTray();
      renderOpenCards();
      onAutoModeChange?.({ autoPop, count: registry.size, activeKey });
    }
  }

  function openDossier(dossierId) {
    const d = INTEL_DOSSIERS.find((x) => x.id === dossierId) || INTEL_DOSSIERS[0];
    registerCard({
      key: `intel:${d.id}`,
      type: 'intel',
      code: d.code,
      state: 'intel',
      chipLabel: () => {
        const isZh = getLocale().startsWith('zh');
        return `${t('hud.chip_intel')} · ${d[isZh ? 'zh' : 'en'].title.split(/[·(]/)[0].trim().slice(0, 8)}`;
      },
      turn: null,
      intelMatch: { dossier: d },
    }, { open: true });
  }

  function closeActive(key = activeKey) {
    if (!openKeys.has(key)) return;
    if (registry.get(key)?.type === 'media') {
      playbackRequest = null;
      recoveringTrack = null;
    }
    hideCard(key);
    renderTray();
    renderOpenCards();
    onAutoModeChange?.({ autoPop, count: registry.size, activeKey });
  }

  /* ── Render Viewport Header HUD Switcher Tray + Local File Loader ── */
  function renderTray() {
    if (!trayEl) return;
    trayEl.replaceChildren();
    // Deduplicate identical topic/file labels, keeping the most recent card for each
    const dedup = new Map();
    for (const card of registry.values()) {
      dedup.set(`${card.type}:${card.chipLabel()}`, card);
    }
    const cards = [...dedup.values()].slice(-5);
    for (const card of cards) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-hud-chip';
      btn.dataset.type = card.type;
      btn.dataset.state = card.state || 'data';
      btn.setAttribute('aria-pressed', String(openKeys.has(card.key)));
      btn.innerHTML = `<span class="moss-hud-chip__dot" aria-hidden="true"></span><span>${escapeHtml(card.chipLabel())}</span>`;
      btn.addEventListener('click', () => {
        if (openKeys.has(card.key)) closeActive(card.key);
        else {
          showCard(card.key);
          renderTray();
          renderOpenCards();
          onAutoModeChange?.({ autoPop, count: registry.size, activeKey });
        }
      });
      trayEl.append(btn);
    }

    const openFileBtn = document.createElement('button');
    openFileBtn.type = 'button';
    openFileBtn.className = 'moss-hud-chip moss-hud-chip--file';
    openFileBtn.textContent = t('hud.open_file');
    openFileBtn.addEventListener('click', () => fileInputEl?.click());
    trayEl.append(openFileBtn);

    if (activeKey) {
      const hideBtn = document.createElement('button');
      hideBtn.type = 'button';
      hideBtn.className = 'moss-hud-chip moss-hud-chip--close';
      hideBtn.textContent = t('hud.collapse');
      hideBtn.addEventListener('click', () => closeActive());
      trayEl.append(hideBtn);
    }
  }

  /* ── Render Active MOSS-Avoiding Floating HUD Card ── */
  function mediaSignature(card) {
    const media = card.customMedia || card.turn?.media;
    return JSON.stringify([card.key, Boolean(card.customMedia), card.pendingQuery, media?.selected?.media_id,
      media?.selected?.url, media?.playback, media?.autoplay, media?.resume_position_s, media?.items,
      card.turn?.status === 'cancelled' || card.turn?.status === 'error']);
  }

  function cardSignature(card) {
    if (card.type === 'media') return mediaSignature(card);
    const turn = card.turn;
    if (!turn) return card.key;
    const values = {
      action: [turn.status, turn.desktop_confirmation, turn.preview, turn.verify_only, turn.receipt],
      weather: turn.weather, host: [turn.local_overview, turn.local_overview_partial],
      image: card.images, doc: card.doc, intel: card.intelMatch?.custom,
    };
    return JSON.stringify(values[card.type] ?? null);
  }

  function releasePlayer() {
    clearSleepFade();
    if (mediaAbort) {
      mediaAbort.abort();
      mediaAbort = null;
    }
    stopSimAudio();
    if (activeMediaEl) {
      if (!activeMediaEl.paused) activeMediaEl.pause();
      activeMediaEl.removeAttribute('src');
      try { activeMediaEl.load(); } catch { /* ignore */ }
    }
    activeMediaEl = null;
    activeMediaIdentity = null;
    repaintIdlePlayer = null;
    stage?.stopMediaTap?.();
  }

  function renderOpenCards({ autoplayKey = null, refreshKey = null, refreshInfo = false } = {}) {
    if (!layerEl) return;
    for (const [key, mounted] of mountedCards) {
      const card = registry.get(key);
      if (openKeys.has(key) && key !== autoplayKey && key !== refreshKey &&
          (!refreshInfo || mounted.type === 'media') && cardSignature(card) === mounted.signature) continue;
      if (mounted.type === 'media') releasePlayer();
      mounted.el.remove();
      mountedCards.delete(key);
    }
    let infoSlot = 0;
    for (const key of openKeys) {
      const card = registry.get(key);
      if (!mountedCards.has(key)) {
        const el = renderCard(card, key === autoplayKey);
        mountedCards.set(key, { el, signature: cardSignature(card), type: card.type });
        layerEl.append(el);
      }
      const { el } = mountedCards.get(key);
      if (card.type !== 'media') el.dataset.hudSlot = String(++infoSlot);
      if (refreshInfo) syncCardChrome(el, card);
    }
    syncDockGeometry();
  }

  function syncCardChrome(el, card) {
    const expanded = expandedKey === card.key;
    el.toggleAttribute('data-expanded', expanded);
    if (expanded) el.dataset.expanded = 'true';
    el.setAttribute('aria-label', card.chipLabel());
    const miniButton = el.querySelector('[data-hud-mini]');
    if (miniButton && card.type === 'media') {
      const mini = mediaMini && !expanded;
      el.toggleAttribute('data-mini', mini);
      if (mini) el.dataset.mini = 'true';
      else delete el.dataset.mini;
      miniButton.setAttribute('aria-pressed', String(mini));
      miniButton.title = t(mini ? 'hud.media_restore' : 'hud.media_mini');
      miniButton.setAttribute('aria-label', miniButton.title);
      miniButton.textContent = mini ? '+' : '−';
    }
    const swapButton = el.querySelector('[data-hud-swap]');
    swapButton.title = t('hud.swap_side');
    swapButton.setAttribute('aria-label', swapButton.title);
    const expandButton = el.querySelector('[data-hud-expand]');
    expandButton.setAttribute('aria-pressed', String(expanded));
    expandButton.title = t(expanded ? 'hud.compact' : 'hud.expand');
    expandButton.setAttribute('aria-label', expandButton.title);
    expandButton.textContent = expanded ? '⤡' : '⤢';
    const pinButton = el.querySelector('[data-hud-pin]');
    const pinned = pinnedKeys.has(card.key);
    pinButton.setAttribute('aria-pressed', String(pinned));
    pinButton.textContent = t(pinned ? 'hud.pinned' : 'hud.pin');
    el.querySelector('[data-hud-close]').setAttribute('aria-label', t('hud.close'));
  }

  function renderCard(card, autoplay) {
    const el = document.createElement('article');
    el.className = 'moss-hud-card fui-bracket';
    el.dataset.hudType = card.type;
    el.dataset.state = card.state || 'data';

    const head = document.createElement('header');
    head.className = 'moss-hud-card__head';
    const badgeState = card.state === 'intel' ? 'warn' : (card.state || 'data');
    head.innerHTML = `
      <span class="fui-badge${card.state === 'warn' || card.state === 'intel' ? ' fui-badge--solid' : ''}" data-state="${escapeHtml(badgeState)}" data-hud-badge="${escapeHtml(card.type)}">${escapeHtml(card.code)}</span>
      <strong class="moss-hud-card__title"></strong>
      <div class="moss-hud-card__ctrls">
        ${card.type === 'media' ? '<button class="moss-hud-ctrl" type="button" data-hud-mini>−</button>' : ''}
        <button class="moss-hud-ctrl" type="button" data-hud-swap>⇄</button>
        <button class="moss-hud-ctrl" type="button" data-hud-expand></button>
        <button class="moss-hud-ctrl" type="button" data-hud-pin></button>
        <button class="moss-hud-ctrl" type="button" data-hud-close>×</button>
      </div>`;

    const titleEl = head.querySelector('.moss-hud-card__title');
    head.querySelector('[data-hud-mini]')?.addEventListener('click', () => {
      cancelAutoHide(card.key);
      mediaMini = !mediaMini;
      try { localStorage.setItem('moss-fui.media-mini', mediaMini ? '1' : '0'); } catch { /* ignore */ }
      if (mediaMini && expandedKey === card.key) expandedKey = null;
      syncCardChrome(el, card);
      syncDockGeometry();
      requestAnimationFrame(() => repaintIdlePlayer?.()); // fui-lint-ignore: R15 one-shot repaint after resize
    });
    head.querySelector('[data-hud-swap]').addEventListener('click', () => {
      cancelAutoHide(card.key);
      dockSide = dockSide === 'left' ? 'right' : 'left';
      const previous = mountedCards.get(expandedKey)?.el;
      const previousCard = registry.get(expandedKey);
      expandedKey = null;
      if (previous) syncCardChrome(previous, previousCard);
      syncDockGeometry();
    });
    head.querySelector('[data-hud-expand]').addEventListener('click', () => {
      cancelAutoHide(card.key);
      const previous = expandedKey;
      expandedKey = previous === card.key ? null : card.key;
      if (expandedKey === card.key && card.type === 'media' && mediaMini) {
        mediaMini = false;
        try { localStorage.setItem('moss-fui.media-mini', '0'); } catch { /* ignore */ }
      }
      activeKey = card.key;
      if (previous && previous !== card.key) syncCardChrome(mountedCards.get(previous).el, registry.get(previous));
      syncCardChrome(el, card);
      syncDockGeometry();
    });
    head.querySelector('[data-hud-pin]').addEventListener('click', () => {
      if (pinnedKeys.has(card.key)) pinnedKeys.delete(card.key);
      else {
        pinnedKeys.add(card.key);
        cancelAutoHide(card.key);
      }
      activeKey = card.key;
      syncCardChrome(el, card);
    });
    head.querySelector('[data-hud-close]').addEventListener('click', () => closeActive(card.key));

    el.addEventListener('pointerenter', () => {
      hoveredKeys.add(card.key);
      clearAutoHideTimer(card.key);
    });
    el.addEventListener('pointerleave', () => {
      hoveredKeys.delete(card.key);
      if (autoOpenedKeys.has(card.key) && speechState === 'idle') {
        scheduleAutoHide(card.key, AUTO_HIDE_AFTER_SPEECH_MS);
      }
    });
    el.addEventListener('focusin', () => {
      hoveredKeys.add(card.key);
      clearAutoHideTimer(card.key);
    });
    el.addEventListener('focusout', () => {
      if (!el.contains(document.activeElement)) {
        hoveredKeys.delete(card.key);
        if (autoOpenedKeys.has(card.key) && speechState === 'idle') {
          scheduleAutoHide(card.key, AUTO_HIDE_AFTER_SPEECH_MS);
        }
      }
    });

    const body = document.createElement('div');
    body.className = 'moss-hud-card__body';

    if (card.type === 'intel') {
      populateIntelCard(card, titleEl, body, { slideIndexMap, renderOpenCards, openDossier, onSendFollowUp });
    } else if (card.type === 'image') {
      populateImageCard(card, titleEl, body, { imageViewStates, renderOpenCards });
    } else if (card.type === 'media') {
      populateMediaCard(card, titleEl, body, autoplay);
    } else if (card.type === 'doc') {
      populateDocCard(card, titleEl, body, { fileInputEl });
    } else if (card.type === 'weather') {
      populateWeatherCard(card, titleEl, body, { onOpenForecastModal });
    } else if (card.type === 'host') {
      populateHostCard(card, titleEl, body, { telemetry, isDeviceStale: () => deviceStale, onOpenLocalStatusModal, onOpenPopout });
    } else if (card.type === 'action') {
      populateActionCard(card, titleEl, body, el, { onApprove, onCancel });
    } else if (card.type === 'context') {
      populateContextCard(card, titleEl, body, { getLatestState: () => latestState, onContextSubmit });
    }

    el.append(head, body);
    syncCardChrome(el, card);
    return el;
  }

  /* ── Viewer 3: 专用音频 / 视频多媒体播放器 (AUD-02 / VID-02) ── */
  const simAudioOrbit = createOrbit(24);
  const simAudioHist = new Float32Array(192);
  let channelAudioStyle = (() => {
    try {
      const saved = localStorage.getItem('moss-fui.channel-wave');
      if (['fui', 'bars', 'orbit'].includes(saved)) return saved;
    } catch { /* ignore */ }
    return 'bars';
  })();

  function drawPlayerViz(canvas, bands, hist, orbitState, { playing = false, dt = 0.032 } = {}) {
    if (!canvas) return;
    if (channelAudioStyle === 'fui') {
      if (!playing) hist.fill(0);
      drawEnvelope(canvas, hist, { solid: true });
    } else if (channelAudioStyle === 'bars') {
      drawSpectrum(canvas, playing ? bands : new Float32Array(bands.length).fill(0.08), {
        style: 'bars',
        segments: 8,
        gap: 2,
        ink: 'data',
      });
    } else {
      drawOrbit(canvas, orbitState, {
        bands: playing ? bands : new Float32Array(bands.length),
        dt: playing ? dt : 0,
        layout: 'line',
        waves: 3,
        glow: true,
        particles: true,
        ambient: true,
        ink: 'data',
      });
    }
  }

  function populateMediaCard(card, titleEl, body, autoplay) {
    if (card.pendingQuery) {
      titleEl.textContent = t('hud.media_searching');
      body.innerHTML = `
        <div class="moss-hud-pending">
          <span class="fui-label">${escapeHtml(t('hud.media_query_label'))}</span>
          <p class="moss-hud-summary">${escapeHtml(card.pendingQuery)}</p>
          <div class="fui-progress" role="progressbar" aria-busy="true"></div>
        </div>`;
      return;
    }

    const m = card.customMedia || card.turn?.media || {};
    const items = m.items || [];
    const selected = m.selected || items[0] || null;
    const selIdx = selected ? items.findIndex((x) => x.media_id === selected.media_id) : -1;
    titleEl.textContent = selected?.title || m.title || t('hud.media_title');

    const isSimTrack = Boolean(selected?.simulated || String(selected?.media_id || '').startsWith('sim-media'));
    const kind = selected?.kind || 'audio';
    const clip = m.playback?.status === 'played' ? m.playback : null;
    const clipStart = Number(clip?.clip_start_s || 0);
    const clipEnd = clipStart + Number(clip?.clip_s || 0);
    const hasClip = Number.isFinite(clipStart) && Number.isFinite(clipEnd) && clipStart >= 0 && clipEnd > clipStart;
    const source = selected?.url || (selected?.media_id ? `/media/${encodeURIComponent(selected.media_id)}` : '');
    const mediaSrc = hasClip && source ? `${source.split('#', 1)[0]}#t=${clipStart},${clipEnd}` : source;
    const coverSrc = !isSimTrack && kind !== 'video' ? safePopupUrl(selected?.cover_url) : null;

    const mediaStage = document.createElement('div');
    mediaStage.className = 'moss-hud-media-stage';

    if (selected && !isSimTrack && mediaSrc) {
      // 100% Custom Wandering Earth FUI Media Transport Deck (zero native browser pill controls)
      const isVideo = kind === 'video';
      const isLive = Boolean(selected.live);
      const isOnline = selected.source_kind === 'online';
      const badgeLabel = isVideo ? 'VIDEO' : isLive ? 'LIVE RADIO' : isOnline ? String(selected.source_label || 'ONLINE').toUpperCase() : 'AUDIO';
      const el = document.createElement(isVideo ? 'video' : 'audio');
      el.controls = false;
      el.preload = isLive ? 'none' : 'metadata';
      el.playsInline = true;
      el.src = mediaSrc;
      const isCustom = Boolean(card.customMedia);
      const identity = (card.local || (!isCustom && m.selected?.media_id !== selected.media_id)) ? null : {
        chat_id: card.turn?.chat_id || 'browse',
        turn_id: isCustom ? card.key : card.turn.id,
        media_id: selected.media_id,
        custom: isCustom,
      };
      let playId = null;
      let playing = false;
      let pausedByUser = false;
      let failureReported = false;
      let scrobbledStart = false;
      let scrobbledDone = false;
      function reportPlayback(event, extra = {}) {
        if (!identity || identity.custom || !el.isConnected) return;
        return onMediaEvent?.({ ...identity, event, play_id: playId, ...extra });
      }
      function pingScrobble(submission) {
        if (document.body?.dataset?.authRole === 'C') return;
        if (selected.provider !== 'navidrome' || !selected.media_id || card.local) return;
        fetch('/api/online-music/scrobble', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ media_id: selected.media_id, submission: Boolean(submission) }),
        }).catch(() => {});
      }
      const startPosition = Number.isFinite(m.resume_position_s) ? m.resume_position_s : hasClip ? clipStart : 0;
      if (startPosition > 0) {
        el.addEventListener('loadedmetadata', () => { el.currentTime = startPosition; }, { once: true });
      }
      el.setAttribute('aria-label', selected.title || 'Media player');
      el.dataset.mossMedia = '1';
      if (isVideo) {
        el.className = 'moss-fui-video';
      } else {
        el.className = 'media-audio';
        el.hidden = true;
      }

      const hasPrev = selIdx > 0;
      const hasNext = selIdx >= 0 && selIdx < items.length - 1;
      const miniSkip = items.length > 1;
      const deck = document.createElement('div');
      deck.className = 'moss-hud-media-deck';
      deck.innerHTML = `
        <div class="moss-hud-media-meta">
          ${coverSrc ? `<img class="moss-hud-cover" data-media-cover src="${escapeHtml(coverSrc)}" alt="" loading="lazy">` : ''}
          <span class="fui-badge" data-media-badge data-state="${isLive ? 'warn' : isVideo ? 'data' : 'ok'}">${escapeHtml(badgeLabel)}</span>
          <span class="moss-hud-media-artist">${escapeHtml(selected.artist || selected.source_label || selected.title || '550W MEDIA')}</span>
          <span class="fui-num" data-media-time>${isLive ? 'LIVE · 00:00' : '00:00 / --:--'}</span>
          <div class="moss-hud-media-mini-nav">
            ${miniSkip ? `<button class="moss-hud-ctrl moss-hud-media-mini-btn" type="button" data-media-mini-prev aria-label="${escapeHtml(t('hud.prev'))}" ${hasPrev ? '' : 'disabled'}>‹</button>` : ''}
            <button class="moss-hud-ctrl moss-hud-media-mini-play" type="button" data-media-mini-play>${escapeHtml(t('hud.play'))}</button>
            ${miniSkip ? `<button class="moss-hud-ctrl moss-hud-media-mini-btn" type="button" data-media-mini-next aria-label="${escapeHtml(t('hud.next'))}" ${hasNext ? '' : 'disabled'}>›</button>` : ''}
          </div>
        </div>
        ${isVideo ? '' : '<canvas class="moss-hud-audio-viz" data-media-viz role="img" aria-label="音频波形监测"></canvas>'}
        ${isVideo ? '' : '<div class="moss-hud-lyrics" data-media-lyrics hidden><span class="moss-hud-lyrics__cur" data-lrc-cur></span><span class="moss-hud-lyrics__nxt" data-lrc-nxt></span></div>'}
        <div class="moss-fui-scrub" data-media-scrub data-accent="${isLive ? 'warn' : isVideo ? 'data' : 'ok'}" role="slider" tabindex="${isLive ? '-1' : '0'}" aria-label="Playback progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" ${isLive ? 'aria-disabled="true"' : ''}>
          <div class="moss-fui-scrub__ticks" aria-hidden="true"></div>
          <div class="moss-fui-scrub__fill" data-media-fill style="width: 0%"></div>
          <div class="moss-fui-scrub__head" data-media-head style="left: 0%"></div>
        </div>
        <div class="moss-fui-media-controls">
          <div class="moss-row moss-row--tight">
            <button class="fui-btn fui-btn--primary" type="button" data-media-play>${escapeHtml(t('hud.play'))}</button>
            <button class="moss-hud-ctrl" type="button" data-media-seek="-10" ${isLive ? 'disabled' : ''}>-10s</button>
            <button class="moss-hud-ctrl" type="button" data-media-seek="10" ${isLive ? 'disabled' : ''}>+10s</button>
            <button class="moss-hud-ctrl" type="button" data-media-rate ${isLive ? 'disabled' : ''}>1.0x</button>
            ${isVideo ? `<button class="moss-hud-ctrl" type="button" data-media-fs>${escapeHtml(t('strip.fullscreen'))}</button>` : `<button class="moss-hud-ctrl" type="button" data-media-fav aria-pressed="false" title="${escapeHtml(t('hud.fav_title'))}">${escapeHtml(t('hud.fav'))}</button><button class="moss-hud-ctrl" type="button" data-media-sleep title="${escapeHtml(t('hud.sleep_title'))}">${escapeHtml(t('hud.sleep_off'))}</button>${!card.local ? `<button class="moss-hud-ctrl" type="button" data-media-nav aria-expanded="false" title="${escapeHtml(t('hud.nav_browse_title'))}">${escapeHtml(t('hud.nav_browse'))}</button>` : ''}`}
          </div>
          <div class="moss-fui-vol-wrap">
            <button class="moss-hud-ctrl" type="button" data-media-mute aria-pressed="false">${escapeHtml(t('hud.vol'))}</button>
            <div class="moss-fui-vol" data-media-vol role="slider" tabindex="0" aria-label="Volume" aria-valuemin="0" aria-valuemax="100" aria-valuenow="100">
              <div class="moss-fui-vol__fill" data-media-vol-fill style="width: 100%"></div>
            </div>
          </div>
        </div>
        ${!isVideo && !card.local ? '<div class="moss-hud-nav-shelf" data-media-nav-shelf hidden></div>' : ''}`;

      mediaStage.append(el, deck);
      const coverEl = deck.querySelector('[data-media-cover]');
      coverEl?.addEventListener('error', () => { coverEl.hidden = true; });

      const playBtn = deck.querySelector('[data-media-play]');
      const miniPlayBtn = deck.querySelector('[data-media-mini-play]');
      const timeEl = deck.querySelector('[data-media-time]');
      const badgeEl = deck.querySelector('[data-media-badge]');
      const scrubEl = deck.querySelector('[data-media-scrub]');
      const fillEl = deck.querySelector('[data-media-fill]');
      const headEl = deck.querySelector('[data-media-head]');
      const rateBtn = deck.querySelector('[data-media-rate]');
      const favBtn = deck.querySelector('[data-media-fav]');
      const sleepBtn = deck.querySelector('[data-media-sleep]');
      const navBtn = deck.querySelector('[data-media-nav]');
      const navShelf = deck.querySelector('[data-media-nav-shelf]');
      const lyricsBox = deck.querySelector('[data-media-lyrics]');
      const lrcCurEl = deck.querySelector('[data-lrc-cur]');
      const lrcNxtEl = deck.querySelector('[data-lrc-nxt]');
      const muteBtn = deck.querySelector('[data-media-mute]');
      const volEl = deck.querySelector('[data-media-vol]');
      const volFillEl = deck.querySelector('[data-media-vol-fill]');
      const vizCanvas = deck.querySelector('[data-media-viz]');

      let lrcLines = [];
      let isFavorited = false;
      const syncLyrics = (sec) => {
        if (!lyricsBox || !lrcLines.length) return;
        let idx = -1;
        for (let i = 0; i < lrcLines.length; i++) {
          if (lrcLines[i].time <= sec + 0.15) idx = i;
          else break;
        }
        if (idx < 0) idx = 0;
        if (lrcCurEl) lrcCurEl.textContent = lrcLines[idx]?.text || '';
        if (lrcNxtEl) lrcNxtEl.textContent = lrcLines[idx + 1]?.text || '';
      };

      if (!isVideo && selected.media_id && !card.local) {
        if (mediaAbort) mediaAbort.abort();
        mediaAbort = new AbortController();
        const { signal } = mediaAbort;
        if (!isLive) {
          const lrcQs = new URLSearchParams();
          if (selected.title) lrcQs.set('title', selected.title);
          if (selected.artist) lrcQs.set('artist', selected.artist);
          const lrcSuffix = lrcQs.toString() ? `?${lrcQs}` : '';
          fetch(`/api/media/${encodeURIComponent(selected.media_id)}/lyrics${lrcSuffix}`, { cache: 'no-store', signal })
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
              if (d?.lines?.length && lyricsBox?.isConnected) {
                lrcLines = d.lines;
                lyricsBox.hidden = false;
                if (d.synced === false) {
                  if (lrcCurEl) lrcCurEl.textContent = lrcLines[0]?.text || '';
                  if (lrcNxtEl) lrcNxtEl.textContent = lrcLines[1]?.text || '';
                } else {
                  syncLyrics(el.currentTime || 0);
                }
              }
            }).catch(() => {});
        }
        const isAdminRole = !document.body.dataset.authRole || document.body.dataset.authRole === 'CS';
        if (isAdminRole) {
          fetch('/api/music/prefs', { cache: 'no-store', signal })
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
              if (!favBtn?.isConnected || !d) return;
              isFavorited = Array.isArray(d.favorite_ids) && d.favorite_ids.includes(selected.media_id);
              favBtn.setAttribute('aria-pressed', String(isFavorited));
              favBtn.textContent = t(isFavorited ? 'hud.faved' : 'hud.fav');
              if (isFavorited) favBtn.dataset.active = 'true';
            }).catch(() => {});
        }
      }

      favBtn?.addEventListener('click', async () => {
        if (!selected.media_id) return;
        if (document.body.dataset.authRole && document.body.dataset.authRole !== 'CS') return;
        const nextStatus = isFavorited ? 'clear' : 'favorite';
        try {
          const resp = await fetch('/api/music/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              media_id: selected.media_id, status: nextStatus,
              title: selected.title || '', artist: selected.artist || '',
            }),
          });
          if (resp.ok) {
            isFavorited = nextStatus === 'favorite';
            favBtn.setAttribute('aria-pressed', String(isFavorited));
            favBtn.textContent = t(isFavorited ? 'hud.faved' : 'hud.fav');
            if (isFavorited) favBtn.dataset.active = 'true';
            else delete favBtn.dataset.active;
          }
        } catch { /* ignore */ }
      });

      const SLEEP_STEPS = [0, 15, 30, 60];
      sleepBtn?.addEventListener('click', () => {
        const curRem = sleepTimerEnd ? Math.ceil((sleepTimerEnd - Date.now()) / 60000) : 0;
        const nextIdx = SLEEP_STEPS.findIndex((m) => m > curRem);
        armSleepTimer(nextIdx >= 0 ? SLEEP_STEPS[nextIdx] : 0);
      });

      let navMode = 'playlists';
      async function loadNavBrowse(kind, itemId = '') {
        if (!navShelf) return;
        if (document.body.dataset.authRole && document.body.dataset.authRole !== 'CS') {
          navShelf.innerHTML = `<p class="moss-hud-nav-empty">${escapeHtml(t('hud.nav_empty'))}</p>`;
          return;
        }
        navShelf.innerHTML = `<p class="moss-hud-nav-empty">${escapeHtml(t('hud.nav_loading'))}</p>`;
        try {
          const qs = new URLSearchParams({ kind, ...(itemId ? { id: itemId } : {}) });
          const resp = await fetch(`/api/online-music/browse?${qs}`, { cache: 'no-store' });
          const data = await resp.json().catch(() => null);
          if (!resp.ok || !data) {
            navShelf.innerHTML = `<p class="moss-hud-nav-empty">${escapeHtml(data?.error || t('hud.nav_empty'))}</p>`;
            return;
          }
          if (Array.isArray(data.tracks)) {
            if (!data.tracks.length) {
              navShelf.innerHTML = `<p class="moss-hud-nav-empty">${escapeHtml(t('hud.nav_empty'))}</p>`;
              return;
            }
            card.customMedia = {
              ...m,
              title: data.name || m.title,
              items: data.tracks,
              selected: data.tracks[0],
              autoplay: true,
              playback: null,
              online: true,
            };
            renderOpenCards({ autoplayKey: card.key });
            return;
          }
          const rows = data.items || [];
          navShelf.innerHTML = `
            <div class="moss-hud-nav-tabs">
              <button class="moss-hud-ctrl" type="button" data-nav-tab="playlists" data-active="${String(kind === 'playlists')}">${escapeHtml(t('hud.nav_playlists'))}</button>
              <button class="moss-hud-ctrl" type="button" data-nav-tab="albums" data-active="${String(kind === 'albums')}">${escapeHtml(t('hud.nav_albums'))}</button>
            </div>
            ${rows.length ? '' : `<p class="moss-hud-nav-empty">${escapeHtml(t('hud.nav_empty'))}</p>`}`;
          for (const tabBtn of navShelf.querySelectorAll('[data-nav-tab]')) {
            tabBtn.addEventListener('click', () => {
              navMode = tabBtn.dataset.navTab || 'playlists';
              void loadNavBrowse(navMode);
            });
          }
          for (const entry of rows) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'moss-hud-track';
            const sub = entry.artist ? ` · ${entry.artist}` : '';
            btn.innerHTML = `<span class="fui-num">${kind === 'playlists' ? 'PL' : 'AL'}</span><span class="moss-hud-track__name">${escapeHtml(entry.name)}${escapeHtml(sub)}</span><span class="fui-num moss-hud-track__kind">${escapeHtml(t('hud.nav_tracks', { n: entry.song_count || 0 }))}</span>`;
            btn.addEventListener('click', () => {
              void loadNavBrowse(kind === 'playlists' ? 'playlist' : 'album', entry.id);
            });
            navShelf.append(btn);
          }
        } catch {
          navShelf.innerHTML = `<p class="moss-hud-nav-empty">${escapeHtml(t('hud.nav_empty'))}</p>`;
        }
      }
      navBtn?.addEventListener('click', () => {
        if (!navShelf) return;
        const willOpen = navShelf.hidden;
        navShelf.hidden = !willOpen;
        navBtn.setAttribute('aria-expanded', String(willOpen));
        if (willOpen) navBtn.dataset.active = 'true';
        else delete navBtn.dataset.active;
        if (willOpen) void loadNavBrowse(navMode);
      });

      const RATES = [1, 1.25, 1.5, 2, 0.75];
      let rateIdx = 0;
      const trackHist = new Float32Array(192);
      const trackOrbit = createOrbit(24);
      const idleBands = new Float32Array(20);
      let lastVizTs = performance.now();

      const paintIdleViz = () => {
        if (!vizCanvas?.isConnected) return;
        drawPlayerViz(vizCanvas, idleBands, trackHist, trackOrbit, { playing: false, dt: 0 });
      };
      repaintIdlePlayer = () => {
        if (el.paused || el.ended) paintIdleViz();
      };
      requestAnimationFrame(paintIdleViz); // fui-lint-ignore: R15 one-shot initial canvas paint after DOM attach

      const fallbackDur = Number(selected.duration_s || selected.duration_sec || 0);
      const syncProgress = () => {
        const hasElDur = Number.isFinite(el.duration) && el.duration > 0;
        const dur = hasElDur ? el.duration : (!isLive && fallbackDur > 0 ? fallbackDur : 0);
        const isLiveStream = isLive || (!dur && !Number.isFinite(el.duration) && el.readyState >= 1);
        const cur = Number.isFinite(el.currentTime) ? el.currentTime : 0;
        const pct = isLiveStream ? 100 : (dur > 0 ? Math.min(100, Math.max(0, (cur / dur) * 100)) : 0);
        if (fillEl) fillEl.style.width = `${pct.toFixed(2)}%`;
        if (headEl) headEl.style.left = isLiveStream ? '100%' : `${pct.toFixed(2)}%`;
        if (scrubEl) scrubEl.setAttribute('aria-valuenow', String(Math.round(pct)));
        if (timeEl) timeEl.textContent = isLiveStream ? `LIVE · ${formatMmSs(cur)}` : `${formatMmSs(cur)} / ${dur > 0 ? formatMmSs(dur) : '--:--'}`;
      };

      const syncVolume = () => {
        const v = el.muted ? 0 : Math.round(el.volume * 100);
        if (volFillEl) volFillEl.style.width = `${v}%`;
        if (volEl) volEl.setAttribute('aria-valuenow', String(v));
        if (muteBtn) {
          muteBtn.setAttribute('aria-pressed', String(el.muted || v === 0));
          muteBtn.textContent = t(el.muted || v === 0 ? 'hud.muted' : 'hud.vol');
        }
      };

      const paintLiveViz = ({freq, time, sampleRate}) => {
        if (!vizCanvas || !deck.isConnected) return;
        const now = performance.now();
        const dt = Math.min(0.05, Math.max(0.008, (now - lastVizTs) / 1000));
        lastVizTs = now;
        const bands = toBands(freq, sampleRate, 20);
        const energy = Math.sqrt(time.reduce((total, sample) => total + sample * sample, 0) / time.length);
        pushEnvelope(trackHist, Math.min(1, energy * 3));
        drawPlayerViz(vizCanvas, bands, trackHist, trackOrbit, { playing: true, dt });
      };

      el.addEventListener('loadedmetadata', syncProgress);
      el.addEventListener('durationchange', syncProgress);
      el.addEventListener('timeupdate', () => {
        if (hasClip && !el.paused && el.currentTime >= clipEnd) el.pause();
        syncProgress();
        if (lrcLines.length) syncLyrics(el.currentTime);
      });
      el.addEventListener('volumechange', syncVolume);
      el.addEventListener('play', () => {
        clearSleepFade();
        delete el.dataset.mossStopped;
        pausedByUser = false;
        activeMediaEl = el;
        if (hasClip && (el.currentTime < clipStart || el.currentTime >= clipEnd)) el.currentTime = clipStart;
        if (new URL(mediaSrc, location.href).origin === location.origin) stage?.attachMediaElement?.(el, paintLiveViz);
      });
      el.addEventListener('playing', (event) => {
        if (playing || el.paused || !el.isConnected) return;
        playing = true;
        failureReported = false;
        if (playBtn) playBtn.textContent = t('hud.pause');
        if (miniPlayBtn) miniPlayBtn.textContent = t('hud.pause');
        if (badgeEl) { badgeEl.dataset.state = 'ok'; badgeEl.classList.add('fui-badge--solid'); }
        playId = safeUuid();
        const micros = Math.round((performance.timeOrigin + event.timeStamp) * 1000);
        lastStartedAtUs = Math.max(micros, lastStartedAtUs + 1);
        Promise.resolve(reportPlayback('started', { started_at_us: lastStartedAtUs })).catch(() => {});
        if (!scrobbledStart) {
          scrobbledStart = true;
          pingScrobble(false);
        }
        window.dispatchEvent(new CustomEvent('moss-media-start', { detail: { turnId: card.turn?.id, title: selected.title, element: el } }));
      });
      const onStop = () => {
        playing = false;
        if (!el.error) pausedByUser = true;
        if (playBtn) playBtn.textContent = t('hud.play');
        if (miniPlayBtn) miniPlayBtn.textContent = t('hud.play');
        if (badgeEl) { badgeEl.dataset.state = isVideo ? 'data' : 'ok'; badgeEl.classList.remove('fui-badge--solid'); }
        if (activeMediaEl === el) stage?.stopMediaTap?.();
        paintIdleViz();
      };
      el.addEventListener('pause', onStop);
      el.addEventListener('ended', () => {
        if (!scrobbledDone) {
          scrobbledDone = true;
          pingScrobble(true);
        }
        playId = null;
        onStop();
      });

      function reportFailure() {
        if (!identity || !el.isConnected || failureReported) return;
        failureReported = true;
        const mayRecover = latestState?.turns?.at(-1)?.id === identity.turn_id && m.autoplay && !m.recovered_from;
        const resume = mayRecover && latestState.media_resume && !pausedByUser && el.error?.code === 2 &&
          playId && el.played.length && el.currentTime >= 0.05;
        const recover = Boolean(resume || (mayRecover && !playId && m.retry_track_source));
        const ticket = recover ? identity : null;
        if (ticket) recoveringTrack = ticket;
        Promise.resolve(reportPlayback('failed', { ...(recover ? { recover: true } : {}),
          ...(resume ? { position_s: el.currentTime } : {}) })).catch(() => {}).finally(() => {
          if (ticket && recoveringTrack === ticket) recoveringTrack = null;
        });
      }

      const play = () => el.play().catch((error) => {
        if (!el.isConnected || error.name === 'AbortError') return;
        if (error.name === 'NotAllowedError') {
          Promise.resolve(reportPlayback('blocked')).catch(() => {});
        } else reportFailure();
      });
      el.addEventListener('error', () => {
        if (el.error?.code !== 1) {
          if (badgeEl) {
            badgeEl.dataset.state = 'crit';
            badgeEl.textContent = 'ERR';
          }
          reportFailure();
        }
      });

      const togglePlay = () => {
        if (!identity && !card.local) { selectQueueItem(card, selected, selIdx + 1); return; }
        if (el.paused || el.ended) play();
        else el.pause();
      };
      playBtn?.addEventListener('click', togglePlay);
      miniPlayBtn?.addEventListener('click', togglePlay);
      deck.querySelector('[data-media-mini-prev]')?.addEventListener('click', () => {
        const prev = items[selIdx - 1];
        if (prev) selectQueueItem(card, prev, selIdx);
      });
      deck.querySelector('[data-media-mini-next]')?.addEventListener('click', () => {
        const next = items[selIdx + 1];
        if (next) selectQueueItem(card, next, selIdx + 2);
      });
      if (isVideo) el.addEventListener('click', togglePlay);

      for (const sb of deck.querySelectorAll('[data-media-seek]')) {
        sb.addEventListener('click', () => {
          const dur = Number.isFinite(el.duration) ? el.duration : 0;
          el.currentTime = Math.min(dur || el.currentTime + 10, Math.max(0, el.currentTime + Number(sb.dataset.mediaSeek)));
          syncProgress();
        });
      }

      rateBtn?.addEventListener('click', () => {
        rateIdx = (rateIdx + 1) % RATES.length;
        el.playbackRate = RATES[rateIdx];
        rateBtn.textContent = `${RATES[rateIdx]}x`;
      });

      deck.querySelector('[data-media-fs]')?.addEventListener('click', () => {
        el.requestFullscreen?.().catch(() => {});
      });

      muteBtn?.addEventListener('click', () => {
        el.muted = !el.muted;
        syncVolume();
      });

      // Click & drag FUI scrub bar
      const bindRail = (railEl, onFraction) => {
        if (!railEl) return;
        let dragging = false;
        const apply = (ev) => {
          const rect = railEl.getBoundingClientRect();
          if (rect.width <= 0) return;
          const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
          onFraction(frac);
        };
        railEl.addEventListener('pointerdown', (ev) => {
          dragging = true;
          railEl.setPointerCapture?.(ev.pointerId);
          apply(ev);
        });
        railEl.addEventListener('pointermove', (ev) => { if (dragging) apply(ev); });
        railEl.addEventListener('pointerup', () => { dragging = false; });
        railEl.addEventListener('pointercancel', () => { dragging = false; });
        railEl.addEventListener('keydown', (event) => {
          const position = Number(railEl.getAttribute('aria-valuenow')) / 100;
          const values = { Home: 0, End: 1, ArrowLeft: position - .05, ArrowDown: position - .05,
            ArrowRight: position + .05, ArrowUp: position + .05 };
          if (!(event.key in values)) return;
          event.preventDefault();
          onFraction(Math.max(0, Math.min(1, values[event.key])));
        });
      };

      bindRail(scrubEl, (frac) => {
        if (!isLive && Number.isFinite(el.duration) && el.duration > 0) {
          el.currentTime = frac * el.duration;
          syncProgress();
        }
      });
      bindRail(volEl, (frac) => {
        el.muted = false;
        el.volume = frac;
        syncVolume();
      });

      syncProgress();
      syncVolume();
      updateSleepButtons();
      activeMediaEl = el;
      activeMediaIdentity = identity;
      const key = identity ? JSON.stringify([identity.chat_id, identity.turn_id, identity.media_id, clip?.command_id]) : '';
      if (autoplay && key && m.autoplay && !attemptedPlay.has(key)) {
        attemptedPlay.add(key);
        queueMicrotask(() => { if (el.isConnected) play(); });
      }
    } else if (selected) {
      // Simulated interactive audio/video HUD deck using the exact 3-mode FUI waveform canvas
      const dur = selected.duration_sec || selected.duration_s || 228;
      const pct = Math.min(1, simAudioPos / dur);
      const miniSkip = items.length > 1;
      mediaStage.innerHTML = `
        <div class="moss-hud-media-deck">
          <div class="moss-hud-media-meta">
            <span class="fui-badge" data-state="${kind === 'video' ? 'data' : 'ok'}">${escapeHtml(kind.toUpperCase())}</span>
            <span class="moss-hud-media-artist">${escapeHtml(selected.artist || '550W MEDIA ARCHIVE')}</span>
            <span class="fui-num" data-sim-time>${formatMmSs(simAudioPos)} / ${formatMmSs(dur)}</span>
            <div class="moss-hud-media-mini-nav">
              ${miniSkip ? `<button class="moss-hud-ctrl moss-hud-media-mini-btn" type="button" data-media-mini-prev aria-label="${escapeHtml(t('hud.prev'))}" ${selIdx <= 0 ? 'disabled' : ''}>‹</button>` : ''}
              <button class="moss-hud-ctrl moss-hud-media-mini-play" type="button" data-sim-mini-play>${escapeHtml(t('hud.play'))}</button>
              ${miniSkip ? `<button class="moss-hud-ctrl moss-hud-media-mini-btn" type="button" data-media-mini-next aria-label="${escapeHtml(t('hud.next'))}" ${selIdx < 0 || selIdx >= items.length - 1 ? 'disabled' : ''}>›</button>` : ''}
            </div>
          </div>
          <canvas class="moss-hud-audio-viz" data-sim-viz role="img" aria-label="音频波形监测"></canvas>
          <div class="fui-gauge moss-seek-bar" data-sim-bar data-state="${kind === 'video' ? 'data' : 'ok'}" style="--segments: 24; --fui-value: ${pct.toFixed(3)}" role="meter" aria-label="Playback progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct * 100)}"></div>
          <div class="moss-hud-media-transport">
            <button class="fui-btn" type="button" data-media-prev ${selIdx <= 0 ? 'disabled' : ''}>${escapeHtml(t('hud.prev'))}</button>
            <button class="fui-btn fui-btn--primary" type="button" data-sim-play>${escapeHtml(t('hud.play'))}</button>
            <button class="fui-btn" type="button" data-media-next ${selIdx < 0 || selIdx >= items.length - 1 ? 'disabled' : ''}>${escapeHtml(t('hud.next'))}</button>
          </div>
        </div>`;

      const playBtn = mediaStage.querySelector('[data-sim-play]');
      const simMiniPlayBtn = mediaStage.querySelector('[data-sim-mini-play]');
      const timeEl = mediaStage.querySelector('[data-sim-time]');
      const barEl = mediaStage.querySelector('[data-sim-bar]');
      const simViz = mediaStage.querySelector('[data-sim-viz]');
      const simBands = new Float32Array(20);
      repaintIdlePlayer = () => {
        if (simViz?.isConnected && !simAudioPlaying) {
          drawPlayerViz(simViz, simBands, simAudioHist, simAudioOrbit, { playing: false, dt: 0 });
        }
      };
      requestAnimationFrame(repaintIdlePlayer); // fui-lint-ignore: R15 one-shot initial canvas paint

      barEl?.addEventListener('click', (ev) => {
        const rect = barEl.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        simAudioPos = Math.round(frac * dur);
        barEl.style.setProperty('--fui-value', frac.toFixed(3));
        if (timeEl) timeEl.textContent = `${formatMmSs(simAudioPos)} / ${formatMmSs(dur)}`;
      });

      const toggleSimPlay = () => {
        if (simAudioPlaying) {
          stopSimAudio();
          if (playBtn) playBtn.textContent = t('hud.play');
          if (simMiniPlayBtn) simMiniPlayBtn.textContent = t('hud.play');
          drawPlayerViz(simViz, simBands, simAudioHist, simAudioOrbit, { playing: false, dt: 0 });
        } else {
          if (playBtn) playBtn.textContent = t('hud.pause');
          if (simMiniPlayBtn) simMiniPlayBtn.textContent = t('hud.pause');
          startSimAudio(selected, (pos, total, freq) => {
            if (timeEl) timeEl.textContent = `${formatMmSs(pos)} / ${formatMmSs(total)}`;
            if (barEl) barEl.style.setProperty('--fui-value', Math.min(1, pos / total).toFixed(3));
            let acc = 0;
            for (let i = 0; i < simBands.length; i++) {
              simBands[i] = Math.min(1, (freq[i * 3 + 2] || 40) / 255);
              acc += simBands[i];
            }
            pushEnvelope(simAudioHist, Math.min(1, (acc / simBands.length) * 1.4));
            drawPlayerViz(simViz, simBands, simAudioHist, simAudioOrbit, { playing: true, dt: 0.032 });
          });
        }
      };
      playBtn?.addEventListener('click', toggleSimPlay);
      simMiniPlayBtn?.addEventListener('click', toggleSimPlay);
      for (const btn of mediaStage.querySelectorAll('[data-media-prev], [data-media-mini-prev]')) {
        btn.addEventListener('click', () => {
          const prev = items[selIdx - 1];
          if (prev) selectQueueItem(card, prev, selIdx);
        });
      }
      for (const btn of mediaStage.querySelectorAll('[data-media-next], [data-media-mini-next]')) {
        btn.addEventListener('click', () => {
          const next = items[selIdx + 1];
          if (next) selectQueueItem(card, next, selIdx + 2);
        });
      }
    }

    body.append(mediaStage);

    if (items.length) {
      const list = document.createElement('div');
      list.className = 'moss-hud-tracklist';
      items.forEach((item, i) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'moss-hud-track';
        btn.setAttribute('aria-current', String(i === selIdx));
        const artist = item.artist ? ` · ${item.artist}` : '';
        const tag = item.live ? 'LIVE' : item.source_kind === 'online' ? String(item.provider || 'ONLINE').toUpperCase() : String(item.kind || 'AUDIO').toUpperCase();
        btn.innerHTML = `<span class="fui-num">${String(i + 1).padStart(2, '0')}</span><span class="moss-hud-track__name">${escapeHtml(item.title || item.filename || item.media_id)}${escapeHtml(artist)}</span><span class="fui-num moss-hud-track__kind">${escapeHtml(tag)}</span>`;
        btn.addEventListener('click', () => {
          selectQueueItem(card, item, i + 1);
        });
        list.append(btn);
      });
      body.append(list);
    }
  }

  /* ── Local File Picker & Stage Drag-and-Drop Support ── */
  fileInputEl?.addEventListener('change', () => {
    const file = fileInputEl.files?.[0];
    if (file) openLocalFile(file);
    fileInputEl.value = '';
  });
  stageWrapEl?.addEventListener('dragover', (e) => {
    if (e.dataTransfer?.types?.includes('Files')) {
      e.preventDefault();
      stageWrapEl.dataset.dropActive = 'true';
    }
  });
  stageWrapEl?.addEventListener('dragleave', () => {
    delete stageWrapEl.dataset.dropActive;
  });
  stageWrapEl?.addEventListener('drop', (e) => {
    delete stageWrapEl.dataset.dropActive;
    const file = e.dataTransfer?.files?.[0];
    if (file) {
      e.preventDefault();
      openLocalFile(file);
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.isComposing && !e.defaultPrevented &&
        !e.target.closest?.('input, textarea, [contenteditable="true"]') && !document.querySelector('dialog[open]')) {
      const key = pinnedKeys.has(activeKey) ? [...openKeys].findLast((item) => !pinnedKeys.has(item)) : activeKey;
      closeActive(key || null);
    }
  });

  return {
    observeState,
    notifySpeechState,
    showPendingMedia,
    openForTurn,
    openHostStatus,
    openCurrentForecast,
    openByType,
    openDossier,
    openLocalFile,
    closeActive,
    isAutoPopEnabled: () => autoPop,
    setAutoPopEnabled(next) {
      autoPop = Boolean(next);
      try { localStorage.setItem('moss-fui.hud-auto', autoPop ? 'on' : 'off'); } catch { /* ignore */ }
      onAutoModeChange?.({ autoPop, count: registry.size, activeKey });
    },
    getCardCount: () => registry.size,
    updateDevice(device, {stale = false} = {}) {
      deviceStale = stale;
      for (const mounted of mountedCards.values()) {
        if (mounted.type === 'host') updateHostHardware(mounted.el, device, deviceStale);
      }
    },
    armAutoplay,
    player(chatId) {
      const target = activeMediaIdentity;
      const node = activeMediaEl;
      if (!node?.isConnected || !target || target.chat_id !== chatId) return { snapshot: { state: 'idle' } };
      const state = node.error ? 'failed' : node.ended ? 'ended' : node.paused
        ? (node.dataset.mossStopped ? 'stopped' : 'paused')
        : node.readyState < HTMLMediaElement.HAVE_FUTURE_DATA ? 'buffering' : 'playing';
      return { node, snapshot: { state, turn_id: target.turn_id, media_id: target.media_id } };
    },
    hasPlayingMedia: () => Boolean(simAudioPlaying || (activeMediaEl && !activeMediaEl.paused && !activeMediaEl.ended)),
    getMediaElements: () => (activeMediaEl?.isConnected ? [activeMediaEl] : []),
    pauseMedia() {
      playbackRequest = null;
      recoveringTrack = null;
      stopSimAudio();
      if (activeMediaEl && !activeMediaEl.paused) activeMediaEl.pause();
    },
    toggleActiveMedia() {
      for (const [key, mounted] of mountedCards.entries()) {
        if (mounted.type === 'media' && openKeys.has(key)) {
          const btn = mounted.el?.querySelector('[data-media-play], [data-sim-play]');
          if (btn) { btn.click(); return true; }
        }
      }
      return false;
    },
    skipActiveMedia(dir = 1) {
      for (const [key, mounted] of mountedCards.entries()) {
        if (mounted.type === 'media' && openKeys.has(key)) {
          const sel = dir > 0 ? '[data-media-mini-next], [data-media-next]' : '[data-media-mini-prev], [data-media-prev]';
          const btn = mounted.el?.querySelector(sel);
          if (btn && !btn.disabled) { btn.click(); return true; }
        }
      }
      return false;
    },
    seekActiveMedia(deltaSec = 10) {
      if (activeMediaEl && activeMediaEl.isConnected && Number.isFinite(activeMediaEl.duration)) {
        activeMediaEl.currentTime = Math.min(activeMediaEl.duration, Math.max(0, activeMediaEl.currentTime + deltaSec));
        return true;
      }
      return false;
    },
    pauseActiveMedia() {
      if (activeMediaEl && activeMediaEl.isConnected && !activeMediaEl.paused) {
        activeMediaEl.pause();
        return true;
      }
      for (const [key, mounted] of mountedCards.entries()) {
        if (mounted.type === 'media' && openKeys.has(key)) {
          const media = mounted.el?.querySelector('audio, video');
          if (media && !media.paused) { media.pause(); return true; }
        }
      }
      return false;
    },
    setSleepTimer(minutes) {
      armSleepTimer(minutes);
    },
    setChannelAudioStyle(style) {
      channelAudioStyle = ['fui', 'bars', 'orbit'].includes(style) ? style : 'bars';
      repaintIdlePlayer?.();
    },
    refreshI18n() {
      renderTray();
      renderOpenCards({ refreshInfo: true });
    },
  };
}
