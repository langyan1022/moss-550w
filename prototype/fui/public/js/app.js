/**
 * MOSS 550W Console Main Orchestrator.
 * Connects Opening Splash, Backend/Sim API, Stage Viewport, Game-FUI Contextual HUD Popups,
 * Telemetry/Geo/Weather, Right Authority & Comms Rail, Voice I/O, Alert Banner (Level 2), and Modals.
 */
import { createMossApi } from './api.js';
import { buildGreetingSpeech, createBadgeController } from './badge.js';
import { createBenBenController } from './benben.js';
import { createBrainDeckController } from './brain-deck.js';
import { createCityPickerController } from './city-picker.js';
import { createDialogueController, isExplicitPlaybackControl } from './dialogue.js';
import { createDuplexVoice } from './duplex-voice.js';
import { createHandGestureController } from './gestures.js';
import { createHudPopupsController } from './hud-popups.js';
import { fixed, formatClock, getLocale, initialLocale, onLocaleChange, setLocale, t } from './i18n.js';
import { createMiniMossController } from './mini-moss.js';
import { createModalsController } from './modals.js';
import { initOpening } from './opening.js';
import { initLeftRailOrder } from './rail-order.js';
import { createCommsDrawerController } from './comms-drawer.js';
import { createAuthPrefsController } from './auth-prefs.js';
import { safeUuid } from './safe-web.js';
import { createStageController } from './stage.js';
import { createTelemetryController } from './telemetry.js';
import { createVoiceEngine } from './voice.js';
import '/static/agent-transport.js';

const GIB = 1024 ** 3;
let lastSleepTurnId = '';
let lastNotifiedTurnId = '';
let meetingMuted = false;
let preMuteTts = true;
let authPrefs = null;
let commsDrawer = null;
let railLayout = null;

// Status strip & banner elements
const liveEl = document.getElementById('strip-live');
const liveWordEl = document.getElementById('strip-live-word');
const clockEl = document.getElementById('strip-clock');
const stageChronoTimeEl = document.getElementById('stage-chrono-time');
const stageUptimeEl = document.getElementById('stage-uptime');
const sessionStartMs = Date.now();
const simBadgeEl = document.getElementById('strip-sim-badge');
const modelBadgeEl = document.getElementById('strip-model');
const pauseBtn = document.getElementById('strip-pause');
const badgeStripBtn = document.getElementById('strip-badge-btn');
const settingsBtn = document.getElementById('strip-settings');
const fullscreenBtn = document.getElementById('strip-fullscreen');
const brandReplayBtn = document.getElementById('strip-brand');

// Right-rail Authority Mode Deck, Stage Tag Strips & Utility Counters
const modeTtsBtn = document.getElementById('mode-tts');
const modeTtsOff = document.getElementById('mode-tts-off');
const modeTtsOn = document.getElementById('mode-tts-on');
const modeTtsVal = document.getElementById('mode-tts-val');
const modeHudBtn = document.getElementById('mode-hud');
const modeHudManual = document.getElementById('mode-hud-manual');
const modeHudAuto = document.getElementById('mode-hud-auto');
const modeHudVal = document.getElementById('mode-hud-val');
const modeCoreBtn = document.getElementById('mode-core');
const modeCoreLink = document.getElementById('mode-core-link');
const modeCoreVal = document.getElementById('mode-core-val');
const authorityLinkBadge = document.getElementById('authority-link-badge');
const deckHistoryCount = document.getElementById('deck-history-count');
const deckMemoryCount = document.getElementById('deck-memory-count');

// Upper & Lower Stage Tag Strips
const tagLinkEl = document.getElementById('tag-link');
const tagLinkChipEl = document.getElementById('tag-link-chip');
const tagMicEl = document.getElementById('tag-mic');
const tagMicChipEl = document.getElementById('tag-mic-chip');
const tagTtsEl = document.getElementById('tag-tts');
const tagTtsChipEl = document.getElementById('tag-tts-chip');
const tagHudEl = document.getElementById('tag-hud');
const tagHudChipEl = document.getElementById('tag-hud-chip');
const tagCoreCodeEl = document.getElementById('tag-core-code');
const tagPersCodeEl = document.getElementById('tag-pers-code');
const tagTurnCodeEl = document.getElementById('tag-turn-code');

// Left-Rail Subsystem Chip Rows (.fui-chiprows)
const subReadyCountEl = document.getElementById('sub-ready-count');
const subEls = {
  link:  { row: document.getElementById('sub-link'),  chip: document.getElementById('sub-link-chip'),  val: document.getElementById('sub-link-val')  },
  scene: { row: document.getElementById('sub-scene'), chip: document.getElementById('sub-scene-chip'), val: document.getElementById('sub-scene-val') },
  auth:  { row: document.getElementById('sub-auth'),  chip: document.getElementById('sub-auth-chip'),  val: document.getElementById('sub-auth-val')  },
  core:  { row: document.getElementById('sub-core'),  chip: document.getElementById('sub-core-chip'),  val: document.getElementById('sub-core-val')  },
  voice: { row: document.getElementById('sub-voice'), chip: document.getElementById('sub-voice-chip'), val: document.getElementById('sub-voice-val') },
  hud:   { row: document.getElementById('sub-hud'),   chip: document.getElementById('sub-hud-chip'),   val: document.getElementById('sub-hud-val')   },
  rem:   { row: document.getElementById('sub-rem'),   chip: document.getElementById('sub-rem-chip'),   val: document.getElementById('sub-rem-val')   },
};
let lastTurnCount = 0;
let lastReminderCount = 0;
let miniMoss = null;
let gestures = null;
let benben = null;
let controllersReady = false;

const bannerEl = document.getElementById('alert-banner');
const bannerBadgeEl = document.getElementById('banner-badge');
const bannerTitleEl = document.getElementById('banner-title');
const bannerDetailEl = document.getElementById('banner-detail');
const bannerTimeEl = document.getElementById('banner-time');
const bannerAckBtn = document.getElementById('banner-ack');
const bannerRetryBtn = document.getElementById('banner-retry');

const footerLinkEl = document.getElementById('footer-link');
const footerBeatEl = document.getElementById('footer-beat');

let bannerAcknowledged = false;
let currentLink = { mode: 'connecting', online: false, beatId: 'nominal' };
let currentModelName = 'DEEPSEEK';
let latestModels = null;
let voiceMicActive = false;
let voiceSpeaking = false;
let lastStateWorking = false;

function computeStagePhase() {
  if (api.getMode() === 'simulated') {
    const beat = api.getBeat();
    if (beat?.phase && beat.phase !== 'standby') return beat.phase;
  }
  if (voiceMicActive) return 'listening';
  if (voiceSpeaking) return 'speaking';
  if (lastStateWorking) return 'thinking';
  return 'standby';
}

function syncAuthorityDeck() {
  const ttsOn = voice.getTtsEnabled();
  if (modeTtsBtn) {
    modeTtsBtn.setAttribute('aria-pressed', String(ttsOn));
    if (ttsOn) modeTtsBtn.removeAttribute('data-state');
    else modeTtsBtn.dataset.state = 'warn';
  }
  if (modeTtsOn) modeTtsOn.dataset.on = String(ttsOn);
  if (modeTtsOff) modeTtsOff.dataset.on = String(!ttsOn);
  if (modeTtsVal) modeTtsVal.textContent = ttsOn ? 'TTS-01' : t('mode.off');

  const hudAuto = hudPopups.isAutoPopEnabled();
  if (modeHudBtn) {
    modeHudBtn.setAttribute('aria-pressed', String(hudAuto));
    if (hudAuto) modeHudBtn.removeAttribute('data-state');
    else modeHudBtn.dataset.state = 'warn';
  }
  if (modeHudAuto) modeHudAuto.dataset.on = String(hudAuto);
  if (modeHudManual) modeHudManual.dataset.on = String(!hudAuto);
  if (modeHudVal) {
    const cnt = String(hudPopups.getCardCount()).padStart(2, '0');
    modeHudVal.textContent = `HUD · ${cnt}`;
  }

  const shortCore = currentModelName.toUpperCase().replace(/-V\d+$/, '').slice(0, 10);
  if (modeCoreVal) modeCoreVal.textContent = shortCore;
  if (tagCoreCodeEl) tagCoreCodeEl.textContent = shortCore;
  if (modeCoreLink) {
    modeCoreLink.textContent = currentLink.mode === 'live'
      ? (currentLink.online ? t('mode.online_short') : t('mode.lost_short'))
      : (currentLink.online ? t('mode.sim_short') : t('mode.lost_short'));
    modeCoreLink.dataset.on = String(currentLink.online);
  }
  if (modeCoreBtn) {
    if (!currentLink.online) modeCoreBtn.dataset.state = 'crit';
    else if (currentLink.mode !== 'live') modeCoreBtn.dataset.state = 'warn';
    else modeCoreBtn.removeAttribute('data-state');
  }

  if (authorityLinkBadge) {
    authorityLinkBadge.dataset.state = currentLink.mode === 'live'
      ? (currentLink.online ? 'ok' : 'crit')
      : (currentLink.online ? 'warn' : 'crit');
    authorityLinkBadge.textContent = currentLink.mode === 'live'
      ? (currentLink.online ? t('auth.live') : t('auth.lost'))
      : (currentLink.online ? t('auth.sim') : t('auth.lost'));
  }

  // Sync Upper Stage Tag Strip Chips with Real Live Subsystems
  if (tagLinkEl && tagLinkChipEl) {
    tagLinkEl.dataset.state = currentLink.mode === 'live'
      ? (currentLink.online ? 'ok' : 'crit')
      : (currentLink.online ? 'warn' : 'crit');
    tagLinkChipEl.textContent = currentLink.mode === 'live'
      ? (currentLink.online ? t('tag.live') : t('tag.lost'))
      : (currentLink.online ? t('tag.sim') : t('tag.lost'));
  }
  if (tagMicEl && tagMicChipEl) {
    if (voiceMicActive) tagMicEl.dataset.state = 'crit';
    else tagMicEl.removeAttribute('data-state');
    tagMicChipEl.textContent = voiceMicActive ? t('tag.mic_live') : t('tag.null');
  }
  if (tagTtsEl && tagTtsChipEl) {
    if (ttsOn) tagTtsEl.dataset.state = 'ok';
    else tagTtsEl.removeAttribute('data-state');
    tagTtsChipEl.textContent = ttsOn ? t('tag.on') : t('tag.off');
  }
  if (tagHudEl && tagHudChipEl) {
    tagHudEl.dataset.state = hudAuto ? 'data' : 'warn';
    tagHudChipEl.textContent = hudAuto ? t('tag.auto') : t('tag.manual');
  }

  // Keep Settings TTS checkbox & MOSS audio channel muted state synced
  const setTts = document.getElementById('set-tts');
  if (setTts) setTts.checked = ttsOn;
  telemetry.setMossMuted(!ttsOn);
  syncSubsystemChipRows();
}

function setSubRow(entry, state, chipText, readingText) {
  if (!entry?.row) return;
  if (state) entry.row.dataset.state = state;
  else entry.row.removeAttribute('data-state');
  if (entry.chip) entry.chip.textContent = chipText;
  if (entry.val) entry.val.textContent = readingText;
}

function syncSubsystemChipRows() {
  if (!controllersReady) return;
  let nominal = 0;

  // 1. Link
  const linkLive = currentLink.mode === 'live' && currentLink.online;
  const linkSim = currentLink.mode !== 'live' && currentLink.online;
  if (linkLive || linkSim) nominal++;
  setSubRow(
    subEls.link,
    linkLive ? 'ok' : linkSim ? 'warn' : 'crit',
    linkLive ? t('tag.live') : linkSim ? t('tag.sim') : t('tag.lost'),
    linkLive ? ':18221' : linkSim ? t('sub.sim_mode') : t('auth.lost'),
  );

  // 2. 550W 3D Stage
  const st = stage.getStatus();
  if (st.sceneActive) nominal++;
  setSubRow(
    subEls.scene,
    st.sceneActive ? 'data' : 'warn',
    st.sceneActive ? t('tag.active') : t('tag.paused'),
    `${t(st.cameraKey)} ${String(st.cameraIndex + 1).padStart(2, '0')}/06`,
  );

  // 3. Operator Badge ID
  const bCfg = badge.getConfig();
  nominal++;
  setSubRow(
    subEls.auth,
    'ok',
    t('sub.pass'),
    bCfg.name || bCfg.idCode,
  );

  // 4. Core LLM
  const shortCore = currentModelName.toUpperCase().replace(/-V\d+$/, '').slice(0, 8);
  if (currentLink.online) nominal++;
  setSubRow(
    subEls.core,
    currentLink.online ? 'data' : 'crit',
    currentLink.online ? t('tag.active') : t('tag.lost'),
    shortCore,
  );

  // 5. Voice Engine
  const ttsOn = voice.getTtsEnabled();
  if (ttsOn || voiceMicActive) nominal++;
  setSubRow(
    subEls.voice,
    voiceMicActive ? 'crit' : ttsOn ? 'ok' : '',
    voiceMicActive ? t('tag.mic_live') : ttsOn ? t('tag.on') : t('tag.off'),
    voiceMicActive ? t('state.listening') : ttsOn ? t('mode.on') : t('mode.off'),
  );

  // 6. Contextual HUD Deck
  const hudAuto = hudPopups.isAutoPopEnabled();
  const hudCnt = hudPopups.getCardCount();
  nominal++;
  setSubRow(
    subEls.hud,
    hudAuto ? 'data' : 'warn',
    hudAuto ? t('tag.auto') : t('tag.manual'),
    t('sub.ready_items', { n: String(hudCnt).padStart(2, '0') }),
  );

  // 7. Active Reminders
  nominal++;
  setSubRow(
    subEls.rem,
    lastReminderCount > 0 ? 'warn' : '',
    lastReminderCount > 0 ? t('sub.due') : t('tag.null'),
    lastReminderCount > 0 ? t('sub.rem_active', { n: lastReminderCount }) : t('sub.none'),
  );

  if (subReadyCountEl) subReadyCountEl.textContent = `${nominal} / 7`;
}

function updateBannerAndStage() {
  const dev = telemetry.getDevice();
  const hwSev = telemetry.getHardwareSeverity();
  const isLinkCrit = !currentLink.online && (currentLink.mode === 'live' || (currentLink.mode === 'simulated' && api.getBeat()?.linkLost));
  const isHwCrit = hwSev === 'crit';
  const isDegraded = hwSev === 'warn';

  let showBanner = false;
  let state = 'warn';
  let title = '';
  let detail = '';
  let showRetry = false;

  if (isLinkCrit && !bannerAcknowledged) {
    showBanner = true;
    state = 'crit';
    title = t('banner.crit_title');
    detail = t('banner.crit_detail');
    showRetry = true;
  } else if (isHwCrit && !bannerAcknowledged) {
    showBanner = true;
    state = 'crit';
    title = t('banner.hw_crit_title');
    detail = t('banner.hw_crit_detail', {
      cpu: dev?.cpu_percent !== undefined ? fixed(dev.cpu_percent, 0) : '—',
      disk: dev?.disk ? fixed(dev.disk.free / GIB, 1) : '—',
      bat: dev?.battery?.percent !== undefined ? fixed(dev.battery.percent, 0) : '—',
    });
  } else if (isDegraded && !bannerAcknowledged) {
    showBanner = true;
    state = 'warn';
    title = t('banner.degraded_title');
    detail = t('banner.degraded_detail', {
      cpu: dev?.cpu_percent !== undefined ? fixed(dev.cpu_percent, 0) : '—',
      ram: dev?.memory ? fixed(dev.memory.available / GIB, 1) : '—',
    });
  }

  if (bannerEl) {
    bannerEl.hidden = !showBanner;
    if (showBanner) {
      bannerEl.dataset.state = state;
      bannerBadgeEl.dataset.state = state;
      bannerBadgeEl.textContent = t(state === 'crit' ? 'level.crit' : 'level.warn');
      bannerTitleEl.textContent = title;
      bannerDetailEl.textContent = detail;
      bannerTimeEl.textContent = formatClock();
      bannerRetryBtn.hidden = !showRetry;
    }
  }

  const phase = computeStagePhase();
  stage.updatePhase(phase, { model: currentModelName, bannerActive: showBanner });
  miniMoss?.setPhase(phase, phase === 'speaking' ? 0.72 : phase === 'listening' ? 0.56 : phase === 'thinking' ? 0.35 : 0);

  // Update simulated audio envelope strips when previewing listening / speaking beats
  // without clobbering active live/fallback voice greetings or mic capture.
  if (api.getMode() === 'simulated') {
    const beatId = api.getBeat()?.id;
    const makeEnvelope = (seed) => Float32Array.from({ length: 120 }, (_, i) => {
      const syll = Math.max(0, Math.sin(i * 0.23 + seed) * Math.cos(i * 0.09 + seed * 0.5));
      const detail = 0.2 + 0.8 * Math.abs(Math.sin(i * 0.71 + seed * 1.7));
      return Math.min(0.92, syll * detail * 0.85 + 0.05);
    });
    if (beatId === 'listening' && !voiceMicActive) {
      telemetry.updateAudioChannel('mic', { envelopeHistory: makeEnvelope(1.2), db: -18.4 });
      if (!voiceSpeaking) telemetry.updateAudioChannel('moss', null);
    } else if (beatId === 'speaking' && !voiceSpeaking) {
      if (!voiceMicActive) telemetry.updateAudioChannel('mic', null);
      telemetry.updateAudioChannel('moss', { envelopeHistory: makeEnvelope(2.7), db: -12.6 });
      stage.setCaption('白日依山尽，黄河入海流。欲穷千里目，更上一层楼。');
    } else {
      if (!voiceMicActive) telemetry.updateAudioChannel('mic', null);
      if (!voiceSpeaking) {
        telemetry.updateAudioChannel('moss', null);
        stage.setCaption('');
      }
    }
  }
}

function updateStripLink() {
  const paused = api.isPaused();
  document.body.dataset.frozen = String(paused);
  if (simBadgeEl) {
    simBadgeEl.hidden = currentLink.mode !== 'simulated';
    simBadgeEl.dataset.state = 'warn';
  }
  if (liveEl) {
    liveEl.dataset.live = String(!paused && currentLink.online);
    liveEl.dataset.linkState = paused ? 'warn' : currentLink.online ? 'ok' : 'crit';
  }
  if (liveWordEl) {
    liveWordEl.textContent = paused
      ? t('strip.paused')
      : currentLink.online
        ? t('strip.live')
        : currentLink.mode === 'connecting'
          ? t('strip.connecting')
          : t('strip.offline');
  }
  if (pauseBtn) {
    pauseBtn.setAttribute('aria-pressed', String(paused));
    pauseBtn.textContent = t(paused ? 'strip.resume' : 'strip.pause');
  }
  if (footerLinkEl) {
    const liveHost = location.host || '127.0.0.1:18221';
    footerLinkEl.textContent = currentLink.mode === 'live'
      ? (currentLink.online ? `LIVE ${liveHost}` : `RECONNECTING :${location.port || '18221'}`)
      : currentLink.mode === 'simulated' ? 'SIMULATION' : t('strip.offline');
  }
  if (footerBeatEl) {
    footerBeatEl.textContent = currentLink.mode === 'live' ? 'live' : (currentLink.beatId || 'nominal');
  }
  syncAuthorityDeck();
}

/* ── Instantiate Subsystems ── */
const opening = initOpening({
  onFinish(meta) {
    if (authPrefs?.maybePromptAfterOpening()) return;
    if (meta?.wasVisible && badge.isShowAfterOpening()) {
      badge.open({ mode: 'dual' });
    }
  },
});
const stage = createStageController({
  onPose(pose) {
    miniMoss?.updatePose(pose);
  },
  onStatusChange() {
    gestures?.refresh();
    if (controllersReady) {
      miniMoss?.setPreset(stage.getStatus().cameraId);
      syncSubsystemChipRows();
    }
  },
});

miniMoss = createMiniMossController({
  onCycleCamera() {
    stage.cycleCamera();
  },
});

gestures = createHandGestureController({
  toggle: document.getElementById('set-gesture'),
  stageButton: document.getElementById('stage-gesture-btn'),
  camButton: document.getElementById('rail-cam-gesture'),
  status: document.getElementById('set-gesture-status'),
  video: document.getElementById('gesture-video'),
  hudEl: document.getElementById('stage-gesture-hud'),
  overlayCanvas: document.getElementById('gesture-overlay'),
  badgeEl: document.getElementById('stage-gesture-badge'),
  noticeEl: document.getElementById('stage-gesture-notice'),
  stopButton: document.getElementById('stage-gesture-stop'),
  stage,
  canvas: document.getElementById('moss-model'),
  acquireStream: () => miniMoss?.getCameraStream?.(),
});

const badgeVoiceFeedback = [
  document.getElementById('badge-greeting-line'),
  document.getElementById('set-badge-speak-btn')?.closest('.moss-row'),
].filter(Boolean).map(anchor => {
  const status = document.createElement('p');
  status.className = 'moss-card__meta';
  status.setAttribute('role', 'status');
  status.dataset.badgeVoiceFeedback = '';
  status.hidden = true;
  anchor.after(status);
  return status;
});
let badgeVoiceAttempt = 0;
let greetingPlan = null;
let greetingTimer = 0;

function setBadgeVoiceFeedback(key) {
  for (const status of badgeVoiceFeedback) {
    status.textContent = key ? t(key) : '';
    status.hidden = !key;
  }
}

function pauseGreetingPreparation({retry = false} = {}) {
  clearTimeout(greetingTimer);
  if (!greetingPlan) return;
  greetingPlan.controller?.abort();
  greetingPlan.controller = null;
  if (retry) greetingTimer = setTimeout(prepareOpeningGreeting, 500);
  else greetingPlan = null;
}

async function prepareOpeningGreeting() {
  const plan = greetingPlan;
  if (!plan || plan.controller) return;
  if (performance.now() > plan.deadline || !voice.getTtsEnabled()) {
    greetingPlan = null;
    setBadgeVoiceFeedback('badge.greeting_deferred');
    return;
  }
  if (api.getMode() !== 'live' || document.hidden || !document.hasFocus() || opening.isActive() ||
      document.querySelector('dialog[open]') || lastStateWorking || voice.isSpeaking() || voice.isMicActive() ||
      hudPopups.hasPlayingMedia()) {
    greetingTimer = setTimeout(prepareOpeningGreeting, 500);
    return;
  }
  const controller = plan.controller = new AbortController();
  setBadgeVoiceFeedback('badge.greeting_preparing');
  const result = await voice.prepareGreeting(plan.text, {space:plan.space, signal:controller.signal});
  if (greetingPlan !== plan || plan.controller !== controller) return;
  greetingPlan = null;
  setBadgeVoiceFeedback('badge.greeting_' + result);
}

function prepareGreetingAfterSettings() {
  pauseGreetingPreparation();
  const config = badge.getConfig();
  if (!config.showAfterOpening || !config.speakGreeting || !voice.getTtsEnabled()) return;
  greetingPlan = {
    text:buildGreetingSpeech(config), space:voice.getSpeechRoomAmount(),
    deadline:performance.now() + 30000, controller:null,
  };
  greetingTimer = setTimeout(prepareOpeningGreeting, 250);
}

const badge = createBadgeController({
  deferLocalCache: true,
  async onSpeakGreeting(speech) {
    const attempt = ++badgeVoiceAttempt;
    pauseGreetingPreparation({retry:true});
    setBadgeVoiceFeedback('voice.preparing');
    const result = await voice.speakText(speech, { category: 'reply', optional: true, useGreetingCache:true });
    if (attempt === badgeVoiceAttempt) setBadgeVoiceFeedback(result === true || result === 'stopped' ? null : 'voice.' + result);
  },
  onVerified(cfg, code) {
    telemetry.addLog({
      time: Date.now(),
      source: 'AUTH',
      text: `ID VERIFIED · IDL.${cfg.idCode} ${cfg.name} (${cfg.clearance}) [${code}]`,
      level: 'ok',
    });
  },
  onOpenSettings() {
    modals.open('settings', { tab: 'badge' });
  },
  onChange() {
    syncSubsystemChipRows();
    if (!controllersReady || !authPrefs) return;
    const cfg = badge.getConfig();
    if (!authPrefs.isAdmin()) cfg.clearance = 'C';
    authPrefs.savePrefs({ badge: cfg });
  },
});

const api = createMossApi({
  onState(state, meta = {}) {
    currentModelName = state.model || state.model_id || 'MOSS';
    if (modelBadgeEl) modelBadgeEl.textContent = currentModelName.toUpperCase();
    lastStateWorking = (state.turns || []).some((tr) => tr.status === 'thinking' || tr.status === 'saving');
    if (lastStateWorking && greetingPlan?.controller) pauseGreetingPreparation({retry:true});

    if (deckHistoryCount) deckHistoryCount.textContent = String((state.chats || []).length).padStart(2, '0');
    if (deckMemoryCount) deckMemoryCount.textContent = String((state.memories || []).length).padStart(2, '0');
    lastTurnCount = (state.turns || []).length;
    lastReminderCount = (state.timers || []).filter((tm) => !tm.dismissed).length;
    if (tagTurnCodeEl) tagTurnCodeEl.textContent = String(lastTurnCount).padStart(2, '0');

    dialogue.renderState(state);
    renderMcpElicitations(state.mcp_elicitations || []);
    telemetry.renderReminders(state.timers || []);
    modals.renderHistory(state);
    modals.renderMemories(state);
    modals.renderPersonalities(state);
    modals.syncSelectedModel(state.model_id);
    if (tagPersCodeEl) {
      const pList = state.personalities || [];
      const pActive = pList.find((item) => item.id === state.personality_id) || pList[0];
      if (pActive) {
        tagPersCodeEl.textContent = getLocale().startsWith('zh') ? pActive.label : (pActive.label_en || pActive.label);
      }
    }

    // v5.5: Arm music sleep timer & unread indicator when new turn arrives
    const newestTurn = (state.turns || []).at(-1);
    if (newestTurn?.sleep_timer && newestTurn.id !== lastSleepTurnId) {
      lastSleepTurnId = newestTurn.id;
      hudPopups.setSleepTimer(newestTurn.sleep_timer.minutes);
    }
    if (newestTurn?.status === 'replied' && newestTurn.id !== lastNotifiedTurnId) {
      lastNotifiedTurnId = newestTurn.id;
      if (commsDrawer && !commsDrawer.isOpen()) commsDrawer.markUnread();
    }

    // Sync latest media queue & weather coordinates from conversation turns
    const lastMediaTurn = [...(state.turns || [])].reverse().find((tr) => tr.media?.items?.length || tr.media?.selected);
    telemetry.renderMediaQueue({
      turnId: lastMediaTurn?.id,
      chatId: lastMediaTurn?.chat_id,
      items: lastMediaTurn?.media.items || [],
      selectedId: lastMediaTurn?.media.selected?.media_id,
    });
    const lastTurn = (state.turns || []).at(-1);
    if (!telemetry.isManualCityLocked() && lastTurn?.weather && Number.isFinite(lastTurn.weather.latitude) && Number.isFinite(lastTurn.weather.longitude)) {
      telemetry.focusSite({
        lat: lastTurn.weather.latitude,
        lon: lastTurn.weather.longitude,
        id: 'WTH-01',
        customLabel: lastTurn.weather.city || null,
        labelKey: 'geo.query_site',
        weather: lastTurn.weather,
        fetchWeather: false,
      });
    }

    hudPopups.observeState(state, {
      initial: Boolean(meta.initial),
      beatId: api.getMode() === 'simulated' ? api.getBeat()?.id : null,
    });
    voice.observeState(state, { initial: meta.initial });
    const latestTurn = (state.turns || []).at(-1) || null;
    const latestTs = latestTurn?.replied_at ? Date.parse(latestTurn.replied_at) : latestTurn?.created_at ? Date.parse(latestTurn.created_at) : 0;
    benben?.updateContext({
      thinking: lastStateWorking,
      mediaPlaying: hudPopups.hasPlayingMedia(),
      turnError: latestTurn?.status === 'error',
      pendingApproval: latestTurn?.status === 'pending',
      lastUserText: latestTurn?.user || '',
      lastReplyText: latestTurn?.reply || '',
      lastTurnAt: Number.isFinite(latestTs) ? latestTs : 0,
      lastTools: latestTurn?.holo_tools || [],
    });
    syncVoiceUi();
    syncAuthorityDeck();
    updateBannerAndStage();
  },
  onDevice(device, meta = {}) {
    telemetry.renderDevice(device, { ...meta, live: api.getMode() === 'live' });
    hudPopups.updateDevice?.(device, meta);
    benben?.updateContext({ hwSeverity: telemetry.getHardwareSeverity(), mediaPlaying: hudPopups.hasPlayingMedia() });
    updateBannerAndStage();
  },
  onModels(body) {
    latestModels = body;
    modals.renderModels(body);
  },
  onLink(info) {
    const wasLive = currentLink.mode === 'live' && currentLink.online;
    currentLink = info;
    document.body.dataset.backend = info.mode;
    if (wasLive && (info.mode !== 'live' || !info.online)) voice.connectionInterrupted?.();
    updateStripLink();
    updateBannerAndStage();
    if (info.mode === 'live' && info.online && !wasLive) {
      void loadLiveLocation();
      voice.connectionRestored?.();
      if (document.body.dataset.authRole === 'CS') void telemetry.refreshLogSource?.();
    }
  },
  onLog(entry) {
    telemetry.addLog(entry);
  },
  onBeat() {
    bannerAcknowledged = false;
    updateBannerAndStage();
  },
  onWakeOffer(ticket) {
    void voice.acceptWake(ticket);
  },
  onError(msg) {
    dialogue.showError(msg);
  },
});

function renderMcpElicitations(items) {
  const rack = document.getElementById('comms-elicit-rack');
  if (!rack) return;
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    rack.hidden = true;
    rack.replaceChildren();
    return;
  }
  rack.hidden = false;
  if (commsDrawer && !commsDrawer.isOpen()) commsDrawer.markUnread();
  const isVisitor = document.body?.dataset?.authRole === 'C';
  const activeIds = new Set(list.map((it) => it.id));
  for (const child of [...rack.children]) {
    if (!activeIds.has(child.dataset.elicitId)) child.remove();
  }
  const rendered = new Set([...rack.children].map((c) => c.dataset.elicitId));
  for (const item of list) {
    if (!item?.id || rendered.has(item.id)) continue;
    const card = document.createElement('form');
    card.className = 'moss-card moss-elicit-card';
    card.dataset.state = 'warn';
    card.dataset.elicitId = item.id;

    const head = document.createElement('div');
    head.className = 'moss-card__head';
    const title = document.createElement('strong');
    title.className = 'moss-card__title';
    title.textContent = `${item.server || 'MCP'} (${String(item.mode || 'form').toUpperCase()})`;
    const badge = document.createElement('span');
    badge.className = 'fui-badge';
    badge.dataset.state = 'warn';
    badge.textContent = t('elicit.badge');
    head.append(title, badge);
    card.append(head);

    const msg = document.createElement('p');
    msg.className = 'moss-card__body';
    msg.textContent = item.message || '';
    card.append(msg);

    const fieldRefs = [];
    const props = item.schema?.properties;
    if (item.mode !== 'url' && props && typeof props === 'object') {
      const reqSet = new Set(Array.isArray(item.schema.required) ? item.schema.required : []);
      const formGrid = document.createElement('div');
      formGrid.className = 'moss-elicit-form';
      for (const [key, spec] of Object.entries(props)) {
        const s = spec && typeof spec === 'object' ? spec : {};
        const wrap = document.createElement('label');
        wrap.className = s.type === 'boolean' ? 'moss-check' : 'moss-field';
        const lbl = document.createElement('span');
        lbl.className = 'fui-label';
        lbl.textContent = s.title || key;
        let ctrl;
        if (s.type === 'boolean') {
          ctrl = document.createElement('input');
          ctrl.type = 'checkbox';
          ctrl.checked = Boolean(s.default);
          wrap.append(ctrl, lbl);
        } else if (Array.isArray(s.enum) && s.enum.length) {
          ctrl = document.createElement('select');
          ctrl.className = 'fui-dialog__input';
          for (const optVal of s.enum) {
            const opt = document.createElement('option');
            opt.value = String(optVal);
            opt.textContent = String(optVal);
            if (s.default === optVal) opt.selected = true;
            ctrl.append(opt);
          }
          wrap.append(lbl, ctrl);
        } else {
          ctrl = document.createElement('input');
          ctrl.className = 'fui-dialog__input';
          ctrl.type = s.type === 'number' || s.type === 'integer'
            ? 'number'
            : /secret|token|password|key/i.test(key) ? 'password' : 'text';
          if (s.default !== undefined) ctrl.value = String(s.default);
          if (s.description) ctrl.placeholder = String(s.description);
          ctrl.required = reqSet.has(key);
          wrap.append(lbl, ctrl);
        }
        ctrl.disabled = isVisitor;
        formGrid.append(wrap);
        fieldRefs.push({ key, spec: s, ctrl });
      }
      card.append(formGrid);
    }

    if (item.mode === 'url' && typeof item.url === 'string' && /^https?:\/\//i.test(item.url)) {
      const link = document.createElement('a');
      link.className = 'fui-btn fui-btn--primary';
      link.href = item.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = t('elicit.open_url');
      card.append(link);
    }

    const actions = document.createElement('div');
    actions.className = 'moss-row moss-row--tight moss-elicit-actions';
    if (isVisitor) {
      const note = document.createElement('span');
      note.className = 'moss-card__meta';
      note.textContent = t('elicit.cs_only');
      actions.append(note);
    } else {
      const sendDecision = async (action) => {
        for (const b of actions.querySelectorAll('button')) b.disabled = true;
        const content = {};
        if (action === 'accept') {
          for (const { key, spec, ctrl } of fieldRefs) {
            if (spec.type === 'boolean') content[key] = Boolean(ctrl.checked);
            else if (spec.type === 'integer') content[key] = Number.parseInt(ctrl.value || '0', 10);
            else if (spec.type === 'number') content[key] = Number(ctrl.value || '0');
            else content[key] = ctrl.value;
          }
        }
        try {
          const resp = await fetch(`/api/mcp/elicitations/${encodeURIComponent(item.id)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(action === 'accept' ? { action, content } : { action }),
          });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          card.remove();
          if (!rack.children.length) rack.hidden = true;
        } catch (err) {
          telemetry.addLog({ time: Date.now(), source: 'MCP', text: `Elicitation failed: ${err.message}`, level: 'warn' });
          for (const b of actions.querySelectorAll('button')) b.disabled = false;
        }
      };
      const okBtn = document.createElement('button');
      okBtn.type = 'submit';
      okBtn.className = 'fui-btn fui-btn--primary';
      okBtn.textContent = t('elicit.accept');
      const decBtn = document.createElement('button');
      decBtn.type = 'button';
      decBtn.className = 'fui-btn';
      decBtn.textContent = t('elicit.decline');
      decBtn.addEventListener('click', () => void sendDecision('decline'));
      const canBtn = document.createElement('button');
      canBtn.type = 'button';
      canBtn.className = 'fui-btn';
      canBtn.textContent = t('elicit.cancel');
      canBtn.addEventListener('click', () => void sendDecision('cancel'));
      card.addEventListener('submit', (ev) => {
        ev.preventDefault();
        void sendDecision('accept');
      });
      actions.append(okBtn, decBtn, canBtn);
    }
    card.append(actions);
    rack.append(card);
  }
}

const telemetry = createTelemetryController({
  fetchLocalWeather: (lat, lon) => api.fetchLocalWeather(lat, lon),
  onOpenOverview: () => hudPopups.openHostStatus(),
  onOpenForecast: (data, label) => hudPopups.openCurrentForecast(data, label),
  onOpenCityPicker() {
    modals.open('settings', { tab: 'location' });
    cityPicker.scrollIntoView();
  },
  onDismissTimer: (id) => api.dismissTimer(id).catch(reportFailure),
  onOpenTimerSource(item) {
    void locateTimer(item);
  },
  async onSelectMedia({ chatId, turnId, item, index }) {
    const state = await selectMedia({ chatId, turnId, item, index });
    if (state) hudPopups.openByType('media');
  },
  onGetLogSource: () => api.getRuntimeLogSource(),
});

const cityPicker = createCityPickerController({
  onSelectManualCity(city) {
    telemetry.setManualCity(city, { fetchWeather: true });
    telemetry.addLog({
      time: Date.now(),
      source: 'GEO',
      text: `CITY LOCKED · ${city.name} (${city.region}) [${city.lat.toFixed(2)}, ${city.lon.toFixed(2)}]`,
      level: 'ok',
    });
  },
  onRestoreAuto() {
    telemetry.clearManualCity();
    telemetry.addLog({
      time: Date.now(),
      source: 'GEO',
      text: 'CITY LOCK RELEASED · AUTO GEOLOCATION ACTIVE',
      level: 'info',
    });
  },
});

const hudPopups = createHudPopupsController({
  stage,
  telemetry,
  isTtsEnabled: () => voice.getTtsEnabled(),
  onSelectMedia: selectMedia,
  onMediaEvent(payload) {
    if (api.getMode() === 'live') return api.postAction('media-event', payload).catch(reportFailure);
  },
  onTransportResult: (payload) => api.postAction('transport-result', payload).catch(reportFailure),
  onApprove: (turnId) => api.postAction('approve', { turn_id: turnId }),
  onCancel: (turnId) => api.postAction('cancel', { turn_id: turnId }),
  onOpenLocalStatusModal: (turn) => modals.openLocalStatus(turn),
  onOpenForecastModal: (w, label) => modals.openForecast(w, label),
  onOpenPopout: () => telemetry.openStatusPopout(),
  onSendFollowUp: (text) => dialogue.fillComposer(text, { submit: true }),
  async onContextSubmit({ turn, kind, excerpt, question, incoming, goal, requestId, image, modelId }) {
    if (!turn?.id || api.getState().chat_id !== turn.chat_id) return false;
    voice.prepareOutput();
    const chat_id = turn.chat_id;
    try {
      if (kind === 'reply') return await api.postAction('message', {
        text:`帮我回复：“${incoming}”`, chat_id, reply_context_turn:turn.id,
        reply_context_goal:goal || '', reply_context_request_id:requestId,
      });
      if (kind === 'question') return await api.postAction('message', {
        text:question, chat_id, window_context:{ turn_id:turn.id, excerpt, model_id:modelId },
      });
      if (kind === 'image') return await api.postAction('context/image-question', {
        text:question, chat_id, turn_id:turn.id, image, model_id:modelId,
      });
    } catch (error) { reportFailure(error); }
    return false;
  },
  onDismissTimer: (id) => api.dismissTimer(id).catch(reportFailure),
  onLocateTurn: (turnId) => dialogue.scrollToTurn(turnId),
  onAutoModeChange: () => syncAuthorityDeck(),
});

let _micDuckedEls = [];
function onMicState({ active }) {
  if (active && greetingPlan?.controller) pauseGreetingPreparation({retry:true});
  voiceMicActive = active;
  for (const el of _micDuckedEls) {
    if (el && typeof el.dataset?.mossPreDuckVol === 'string') {
      el.volume = Number(el.dataset.mossPreDuckVol) || 1;
      delete el.dataset.mossPreDuckVol;
    }
  }
  _micDuckedEls = [];
  if (active) {
    for (const el of (hudPopups.getMediaElements?.() || [])) {
      if (el && !el.paused && el.volume > 0.28) {
        el.dataset.mossPreDuckVol = String(el.volume);
        el.volume = 0.25;
        _micDuckedEls.push(el);
      }
    }
  }
  benben?.updateContext({ micActive: active });
  syncVoiceUi();
  updateBannerAndStage();
}

function onMicFrame(frame) {
  telemetry.updateAudioChannel('mic', frame);
  stage.pushAudioFrame('listening', frame);
  if (frame?.rms != null) benben?.setAudioLevel(frame.rms * 3.2);
}

const voiceOutput = createVoiceEngine({
  getMode: () => api.getMode(),
  canStartOptionalSpeech: () => !lastStateWorking && !hudPopups.hasPlayingMedia(),
  getMediaElements: () => hudPopups.getMediaElements?.() || [],
  onMicState,
  onMicFrame,
  onSpeechState({ active }) {
    voiceSpeaking = active;
    hudPopups.notifySpeechState?.({ active });
    benben?.updateContext({ speaking: active });
    syncVoiceUi();
    updateBannerAndStage();
  },
  onSpeechFrame(frame) {
    telemetry.updateAudioChannel('moss', frame);
    stage.pushAudioFrame('speaking', frame);
    if (frame?.rms != null) benben?.setAudioLevel(frame.rms * 3.0);
  },
  onCaption(text) {
    stage.setCaption(text);
  },
  onTranscribed(text) {
    void dialogue.submitText(text, { spoken: true });
  },
  onStatus(key, extra = '') {
    if (key === 'voice.preparing') hudPopups.notifySpeechState?.({ active: false, preparing: true });
    else if (key === 'voice.ready' || key === 'voice.unavailable' || key === 'voice.disabled' || key === 'voice.interrupted') {
      hudPopups.notifySpeechState?.({ active: voiceSpeaking, preparing: false });
    }
    syncVoiceUi(key, extra);
  },
});
const voice = createDuplexVoice(voiceOutput, {
  getMode: () => api.getMode(), getState: () => api.getState(),
  onTranscript: (text, context) => dialogue.submitText(text, context),
  onMicState, onMicFrame, onStatus: syncVoiceUi,
  focusComposer: () => dialogue.focusInput(),
  targetsPlayback: (instruction) => isExplicitPlaybackControl(instruction) && hudPopups.hasPlayingMedia(),
});

function reportFailure(error) {
  dialogue.showError(error.message);
  return null;
}

async function locateTimer(item) {
  try {
    if (item.chat_id && item.chat_id !== api.getState().chat_id) {
      await api.postAction('switch-chat', { id:item.chat_id });
    }
    if (item.turn_id) dialogue.scrollToTurn(item.turn_id);
  } catch (error) { reportFailure(error); }
}

async function selectMedia({ chatId, turnId, item, index }) {
  voice.prepareOutput();
  try {
    return await api.postAction('media-select', { chat_id:chatId, turn_id:turnId,
      media_id:item.media_id, index });
  } catch (error) {
    return reportFailure(error);
  }
}

async function loadLiveLocation() {
  try {
    await cityPicker.loadCatalog();
    const city = cityPicker.getManualCity();
    if (city) telemetry.setManualCity(city, { fetchWeather:true });
    else if (!telemetry.getBrowserLocation()) await locateIfAllowed();
  } catch (error) { reportFailure(error); }
}

async function sendDialogueMessage(text, { playback = false } = {}) {
  const state = api.getState();
  const chatId = state.chat_id;
  voice.userAction('message', { text }, state, { deferOutput: true, keepSpeech: playback });
  const player = () => hudPopups.player(chatId);
  const requestId = safeUuid();
  const location = await locationForMessage(text, state);
  const cancel = playback ? null : hudPopups.armAutoplay?.(chatId, text);
  const pending = hudPopups.holdPending?.(chatId);
  try {
    const next = await api.postAction('message', { text, chat_id: chatId || undefined, ...location,
      player: player().snapshot, queued_player: pending?.target, transport_request_id: requestId });
    const settled = await window.mossTransport.apply(next, requestId, player, pending);
    if (settled !== next) api.acceptState(settled);
  } catch (error) {
    cancel?.();
    throw error;
  }
}

const dialogue = createDialogueController({
  onSendMessage: sendDialogueMessage,
  onMediaIntent(text) {
    const { snapshot } = hudPopups.player(api.getState().chat_id);
    if (snapshot.state === 'idle' && !/暂停|停止|继续|恢复|停一下|别放了/u.test(text)) {
      hudPopups.showPendingMedia(text);
    }
  },
  onApprove: (turnId, scope) => {
    voice.userAction('approve', { turn_id: turnId }, api.getState(), { deferOutput: true });
    return api.postAction('approve', { turn_id: turnId, ...scope });
  },
  onCancel: (turnId, scope) => {
    voice.userAction('cancel', { turn_id: turnId }, api.getState());
    return api.postAction('cancel', { turn_id: turnId, ...scope });
  },
  async onReplaceReply({ turnId, chatId, text }) {
    voice.userAction('cancel', { turn_id: turnId }, api.getState());
    await api.postAction('cancel', { turn_id: turnId, chat_id: chatId, conversation_only: true });
    return sendDialogueMessage(text);
  },
  onStopSpeech() {
    voice.interrupt();
    syncVoiceUi('voice.interrupted');
  },
  onToggleMic: () => voice.toggleMic(),
  onInterrupt() {
    const wasSpeaking = voice.isSpeaking() || voice.isBusy?.();
    voice.interrupt();
    if (wasSpeaking) { syncVoiceUi('voice.interrupted'); return; }
    hudPopups.pauseMedia();
    const active = (api.getState().turns || []).find((tr) => tr.status === 'thinking' || tr.status === 'saving');
    if (active) api.postAction('cancel', { turn_id: active.id }).catch(reportFailure);
    syncVoiceUi('voice.interrupted');
  },
  onOpenHud: (turn, type) => hudPopups.openForTurn(turn, type),
  onOpenJevHandoff: (payload) => modals.openJevHandoff(payload),
  onNewChat: () => {
    voice.userAction('new-chat', {}, api.getState());
    return api.postAction('new-chat', {}).catch(reportFailure);
  },
  onOpenModal: (name) => modals.open(name),
  onSpeakTurn: (turn) => {
    if (!turn?.reply) return;
    if (meetingMuted) setMeetingMute(false);
    voice.prepareOutput();
    void voice.speakText(turn.reply, { category: 'reply', optional: false });
  },
  onRetryTurn: (turn, opts = {}) => {
    if (!turn?.user) return;
    commsDrawer?.open();
    if (opts?.editFirst) {
      dialogue.fillComposer(turn.user, { submit: false });
      return;
    }
    void dialogue.submitText(turn.user);
  },
  async onSaveNoteTurn(turn) {
    if (!turn?.reply || document.body.dataset.authRole === 'C') return false;
    try {
      const resp = await fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: turn.reply.slice(0, 600), title: (turn.user || 'MOSS 摘录').slice(0, 40) }),
      });
      if (resp.ok) {
        telemetry.addLog({ time: Date.now(), source: 'NOTE', text: 'Saved reply to notes', level: 'ok' });
        return true;
      }
      return false;
    } catch { return false; }
  },
  async onAttachSubmit({ name, mime, data_b64, prompt }) {
    if (api.getMode() !== 'live') {
      const zh = (document.documentElement.lang || '').toLowerCase().startsWith('zh');
      throw new Error(zh ? '仿真演示模式下暂不支持上传附件。' : 'File upload is unavailable in simulated mode.');
    }
    commsDrawer?.open();
    const chatId = api.getState()?.chat_id;
    const resp = await fetch('/api/chat/attach', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, mime, data_b64, prompt, chat_id: chatId }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    if (data && typeof data === 'object' && Array.isArray(data.turns)) {
      api.acceptState(data);
    }
    return true;
  },
  onSlashCommand(action) {
    if (action === 'toggle-mute') setMeetingMute(!meetingMuted);
    else if (action === 'toggle-float') commsDrawer?.toggleDetach();
    else if (action === 'open-cmdk') openCmdkPalette();
    else if (action === 'open-history') modals.open('history');
    else if (action === 'toggle-gesture') {
      const btn = document.getElementById('stage-gesture-btn');
      if (btn && !btn.disabled) btn.click();
    }
  },
});

async function locationForMessage(text, state) {
  if (api.getMode() !== 'live') return {};
  const weather = /天气|天儿|气温|温度|下雨|下雪|降水|带伞|穿什么|怎么穿|冷不冷|热不热|几度|多少度/u;
  const here = /当前(?:的)?(?:定位点|定位|位置|经纬度|坐标)|我(?:现在|当前)?在(?:哪里|哪儿)|本机(?:位置|定位)|我这[边里儿]|这里|这儿|附近|本地|当地/u;
  const previous = state.turns?.at(-1);
  const followUp = /^[这那]?[早晚今明后][天天儿个早晚的]?(?:的)?(?:呢|怎么样)[？?。]?$/u;
  if (!weather.test(text) && !here.test(text) && !(previous?.weather && followUp.test(text))) return {};
  const city = cityPicker.getManualCity();
  if (city) return city.backendId ? { preferred_city_id: city.backendId }
    : { browser_location: { latitude: city.lat, longitude: city.lon } };
  const current = telemetry.getBrowserLocation();
  let timer;
  const position = current || await Promise.race([
    locateIfAllowed(),
    new Promise(resolve => { timer = setTimeout(resolve, here.test(text) && !weather.test(text) ? 8000 : 800, null); }),
  ]).finally(() => clearTimeout(timer));
  if (!position) return {};
  const precision = weather.test(text) ? 100 : 10;
  return { browser_location: Object.fromEntries(Object.entries(position).map(([axis, value]) =>
    [axis, Math.round(value * precision) / precision])) };
}

async function locateIfAllowed() {
  if (api.getMode() === 'simulated') return null;
  const permission = await navigator.permissions?.query({ name: 'geolocation' }).catch(() => null);
  return permission?.state === 'granted' && !cityPicker.isManualLocked() ? telemetry.locateBrowser() : null;
}

const modals = createModalsController({
  api,
  voice,
  stage,
  dialogue,
  telemetry,
  hudPopups,
  opening,
  onSelectModel: (id) => api.postAction('select-model', { id }),
  onSelectPersonality: async (id) => {
    const res = await api.postAction('select-personality', { id });
    authPrefs?.savePrefs({ personality_id: id });
    return res;
  },
});

const roomSlider = document.getElementById('set-voice-room');
const roomValue = document.getElementById('set-voice-room-value');
const roomPreview = document.getElementById('set-voice-room-preview');
const roomStatus = document.getElementById('set-voice-room-status');
let roomAudition;

function syncSpeechRoom() {
  const percent = voice.getSpeechRoomAmount();
  roomSlider.value = String(percent);
  roomSlider.setAttribute('aria-valuetext', percent + '%');
  roomValue.value = percent + '%';
}

function stopRoomAudition() {
  if (!roomAudition) return;
  roomAudition.abort();
  roomAudition = null;
  roomPreview.dataset.i18n = 'set.voice_room_preview';
  roomPreview.textContent = t(roomPreview.dataset.i18n);
  roomStatus.value = '';
}

function changeSpeechRoom(value) {
  stopRoomAudition();
  voice.setSpeechRoomAmount(value);
  syncSpeechRoom();
}

roomSlider.addEventListener('input', () => changeSpeechRoom(roomSlider.valueAsNumber));
document.getElementById('set-voice-room-reset').addEventListener('click', () => changeSpeechRoom(100));
const settingsDialog = document.getElementById('dlg-settings');
settingsDialog.addEventListener('close', () => {
  stopRoomAudition();
  prepareGreetingAfterSettings();
});
new MutationObserver(() => {
  if (settingsDialog.open) pauseGreetingPreparation();
}).observe(settingsDialog, {attributes:true, attributeFilter:['open']});
window.addEventListener('moss-speech-pending', () => {
  if (greetingPlan?.controller) pauseGreetingPreparation({retry:true});
});
window.addEventListener('pagehide', () => pauseGreetingPreparation());
roomPreview.addEventListener('click', async () => {
  if (roomAudition) { stopRoomAudition(); return; }
  voice.prepareOutput();
  const audition = new AbortController();
  roomAudition = audition;
  roomPreview.dataset.i18n = 'set.voice_room_stop';
  roomPreview.textContent = t(roomPreview.dataset.i18n);
  roomStatus.value = t('voice.preparing');
  let result;
  try {
    result = await voice.speakText('这里是 MOSS。语音通道已就绪。', {
      optional:true, space:voice.getSpeechRoomAmount(), signal:audition.signal,
    });
  } catch { result = 'unavailable'; }
  if (roomAudition !== audition) return;
  stopRoomAudition();
  const key = result === true ? 'set.voice_room_done' : 'voice.' + (result || 'unavailable');
  roomStatus.value = t(key);
});
syncSpeechRoom();

function syncVoiceUi(statusKey = voiceSpeaking ? 'voice.speaking' : voiceMicActive ? 'voice.listening' : 'voice.ready', extra = '') {
  const canInterrupt = voiceMicActive || voice.isBusy?.() || voiceSpeaking || lastStateWorking || hudPopups.hasPlayingMedia();
  dialogue.setVoiceUi({ micActive: voiceMicActive, canInterrupt, statusKey, extra });
}

/* ── Right Rail Authority Mode Deck Controls ── */
modeTtsBtn?.addEventListener('click', () => {
  voice.setTtsEnabled(!voice.getTtsEnabled());
  syncAuthorityDeck();
});
modeHudBtn?.addEventListener('click', () => {
  hudPopups.setAutoPopEnabled(!hudPopups.isAutoPopEnabled());
  syncAuthorityDeck();
});
modeCoreBtn?.addEventListener('click', () => {
  modals.open('settings', { tab: 'model' });
});
tagPersCodeEl?.closest('li')?.addEventListener('click', () => {
  modals.open('settings', { tab: 'model' });
});
document.getElementById('set-tts')?.addEventListener('change', () => syncAuthorityDeck());

/* ── Left Rail Subsystem Chip Row Direct Actions ── */
subEls.link.row?.addEventListener('click', () => {
  if (!currentLink.online) api.reconnect();
  else modals.open('settings', { tab: 'model' });
});
subEls.scene.row?.addEventListener('click', () => {
  if (stage.getStatus().sceneActive) stage.cycleCamera();
  else stage.setSceneEnabled(true);
});
subEls.auth.row?.addEventListener('click', () => badge.open({ mode: 'dual' }));
subEls.core.row?.addEventListener('click', () => modals.open('settings', { tab: 'model' }));
subEls.voice.row?.addEventListener('click', () => {
  voice.setTtsEnabled(!voice.getTtsEnabled());
  syncAuthorityDeck();
});
subEls.hud.row?.addEventListener('click', () => {
  hudPopups.setAutoPopEnabled(!hudPopups.isAutoPopEnabled());
  syncAuthorityDeck();
});
subEls.rem.row?.addEventListener('click', () => telemetry.setDockTab('reminders'));

/* ── Strip & Banner Controls ── */
pauseBtn?.addEventListener('click', () => {
  const next = !api.isPaused();
  api.setPaused(next);
  stage.setFrozen(next);
  miniMoss.setFrozen(next);
  telemetry.setFrozen(next);
  benben?.setFrozen(next);
  updateStripLink();
});

bannerAckBtn?.addEventListener('click', () => {
  bannerAcknowledged = true;
  telemetry.addLog({ time: Date.now(), source: 'OP-01', text: 'Alert acknowledged', level: 'info' });
  updateBannerAndStage();
});
bannerRetryBtn?.addEventListener('click', () => {
  bannerAcknowledged = false;
  api.reconnect();
});

brandReplayBtn?.addEventListener('click', () => opening.start(true));
badgeStripBtn?.addEventListener('click', () => badge.open({ mode: 'dual' }));
settingsBtn?.addEventListener('click', () => modals.open('settings'));
fullscreenBtn?.addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { /* ignored */ }
});
document.addEventListener('fullscreenchange', () => {
  if (!fullscreenBtn) return;
  const full = Boolean(document.fullscreenElement);
  fullscreenBtn.setAttribute('aria-pressed', String(full));
  fullscreenBtn.textContent = t(full ? 'strip.exit_fullscreen' : 'strip.fullscreen');
});

// Language switch
for (const opt of document.querySelectorAll('.fui-lang__opt')) {
  opt.addEventListener('click', () => {
    setLocale(opt.lang);
    authPrefs?.savePrefs({ locale: opt.lang });
  });
}
onLocaleChange(() => {
  updateStripLink();
  updateBannerAndStage();
  stage.refreshI18n();
  hudPopups.refreshI18n();
  telemetry.refreshI18n();
  dialogue.refreshI18n();
  modals.refreshI18n();
  badge.refreshI18n();
  cityPicker.refreshI18n();
  miniMoss.refreshI18n();
  gestures?.refreshI18n();
  benben?.refreshI18n();
  commsDrawer?.refreshLocale();
  if (tagPersCodeEl) {
    const st = api.getState();
    const pList = st?.personalities || [];
    const pActive = pList.find((item) => item.id === st?.personality_id) || pList[0];
    if (pActive) {
      tagPersCodeEl.textContent = getLocale().startsWith('zh') ? pActive.label : (pActive.label_en || pActive.label);
    }
  }
  syncAuthorityDeck();
  syncVoiceUi();
});

// Keyboard shortcuts (non-conflicting with inputs, textareas, and open dialogs)
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    openCmdkPalette();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'm') {
    e.preventDefault();
    setMeetingMute(!meetingMuted);
    return;
  }
  if (e.altKey && e.key.toLowerCase() === 'c') {
    e.preventDefault();
    commsDrawer?.toggle();
    return;
  }
  if (e.altKey && e.key.toLowerCase() === 'm') {
    e.preventDefault();
    commsDrawer?.open();
    voice.toggleMic();
    return;
  }
  if ((e.altKey && e.key.toLowerCase() === 'g') || ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'g')) {
    e.preventDefault();
    const btn = document.getElementById('stage-gesture-btn');
    if (btn && !btn.disabled) btn.click();
    return;
  }
  const tag = e.target?.tagName;
  const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target?.isContentEditable || document.querySelector('dialog[open]');
  if (typing) return;
  if (e.code === 'Space' && !e.ctrlKey && !e.altKey && !e.metaKey && tag !== 'BUTTON' && tag !== 'A') {
    if (hudPopups.toggleActiveMedia()) e.preventDefault();
  } else if (e.altKey && e.key === 'ArrowRight') {
    if (hudPopups.skipActiveMedia(1)) e.preventDefault();
  } else if (e.altKey && e.key === 'ArrowLeft') {
    if (hudPopups.skipActiveMedia(-1)) e.preventDefault();
  } else if (e.shiftKey && e.key === 'ArrowRight') {
    if (hudPopups.seekActiveMedia(10)) e.preventDefault();
  } else if (e.shiftKey && e.key === 'ArrowLeft') {
    if (hudPopups.seekActiveMedia(-10)) e.preventDefault();
  }
});

// Status Strip & Stage Wandering-Earth Split-Unit Chronometer (1 Hz, stops when paused or hidden)
function renderSplitChronoHtml(date) {
  const pad2 = (n) => String(n).padStart(2, '0');
  return `<span class="moss-chrono__major">${date.getFullYear()}</span><span class="moss-chrono__unit">Y</span>`
    + `<span class="moss-chrono__major">${pad2(date.getMonth() + 1)}</span><span class="moss-chrono__unit">M</span>`
    + `<span class="moss-chrono__major">${pad2(date.getDate())}</span><span class="moss-chrono__unit">D</span>`
    + `<span class="moss-chrono__gap" aria-hidden="true"></span>`
    + `<span class="moss-chrono__minor">${pad2(date.getHours())}</span><span class="moss-chrono__unit">H</span>`
    + `<span class="moss-chrono__minor">${pad2(date.getMinutes())}</span><span class="moss-chrono__unit">MIN</span>`
    + `<span class="moss-chrono__minor moss-chrono__sec">${pad2(date.getSeconds())}</span><span class="moss-chrono__unit">S</span>`;
}

function tickClock() {
  const now = new Date();
  if (clockEl) {
    clockEl.innerHTML = renderSplitChronoHtml(now);
    clockEl.dateTime = now.toISOString();
  }
  if (stageChronoTimeEl) {
    stageChronoTimeEl.innerHTML = renderSplitChronoHtml(now);
  }
  if (stageUptimeEl) {
    const sec = Math.max(0, Math.floor((Date.now() - sessionStartMs) / 1000));
    const hh = String(Math.floor(sec / 3600)).padStart(2, '0');
    const mm = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
    const ss = String(sec % 60).padStart(2, '0');
    stageUptimeEl.innerHTML = `${hh}<span class="moss-chrono__unit">H</span> ${mm}<span class="moss-chrono__unit">MIN</span> ${ss}<span class="moss-chrono__unit">S</span>`;
  }
}
tickClock();
setInterval(() => { // fui-lint-ignore: R15 real 1 Hz wall clock; skips while paused or tab hidden
  if (!api.isPaused() && !document.hidden) tickClock();
}, 1000);

const brainDeck = createBrainDeckController({
  getMode: () => api.getMode(),
  isPaused: () => api.isPaused(),
  onSendPrompt: (prompt) => {
    commsDrawer?.open();
    if (prompt) void dialogue.fillComposer(prompt, { submit: true });
  },
  onSpeakBriefing: (text) => {
    if (!meetingMuted && voice.getTtsEnabled() && text) {
      voice.prepareOutput();
      void voice.speakText(text, { category: 'reply', optional: true });
    }
  },
  onAddLog: (entry) => telemetry.addLog(entry),
});

/* ── v5.5: Slide-Out Comms Drawer, Dual-Rail Layout, Meeting Mute, Ctrl+K Palette & Backend Auth/Prefs ── */
const muteStripBtn = document.getElementById('strip-mute');
const cmdkStripBtn = document.getElementById('strip-cmdk');
const cmdkDlg = document.getElementById('dlg-cmdk');
const cmdkInput = document.getElementById('cmdk-input');
const cmdkList = document.getElementById('cmdk-list');

function setMeetingMute(on, { persist = true } = {}) {
  const next = Boolean(on);
  if (next === meetingMuted) return;
  meetingMuted = next;
  document.body.dataset.meetingMuted = String(meetingMuted);
  if (meetingMuted) {
    preMuteTts = voice.getTtsEnabled();
    voice.interrupt();
    voice.setTtsEnabled(false);
    hudPopups.pauseActiveMedia();
  } else {
    voice.setTtsEnabled(preMuteTts);
  }
  if (muteStripBtn) {
    muteStripBtn.setAttribute('aria-pressed', String(meetingMuted));
    muteStripBtn.textContent = t(meetingMuted ? 'strip.muted' : 'strip.mute');
    if (meetingMuted) muteStripBtn.dataset.state = 'warn';
    else muteStripBtn.removeAttribute('data-state');
  }
  syncAuthorityDeck();
  if (persist) authPrefs?.savePrefs({ meeting_mute: meetingMuted });
}
muteStripBtn?.addEventListener('click', () => setMeetingMute(!meetingMuted));

commsDrawer = createCommsDrawerController({
  onModeChange(st) {
    authPrefs?.savePrefs({ comms_drawer: st });
  },
});

let suppressLayoutSave = false;
railLayout = initLeftRailOrder(
  document.getElementById('left-rail'),
  document.getElementById('right-rail-cards'),
);
window.addEventListener('moss-rail-layout-change', (ev) => {
  if (!suppressLayoutSave && ev.detail) authPrefs?.savePrefs({ rail_layout: ev.detail });
});

function buildCmdkItems() {
  const isZh = getLocale().startsWith('zh');
  const L = (zh, en) => (isZh ? zh : en);
  const items = [
    { group: L('通讯与布局', 'LAYOUT'), kw: 'drawer chat 抽屉 聊天 通讯 侧滑', label: L('展开 / 收起侧滑聊天抽屉 (Alt+C)', 'Toggle Slide-Out Comms Drawer (Alt+C)'), run: () => commsDrawer.toggle() },
    { group: L('通讯与布局', 'LAYOUT'), kw: 'float popup detach 浮窗 弹窗 独立 停靠', label: L('切换聊天框独立悬浮窗 / 停靠抽屉', 'Toggle Floating Comms Window / Drawer'), run: () => commsDrawer.toggleDetach() },
    { group: L('通讯与布局', 'LAYOUT'), kw: 'reset layout 布局 还原 默认 双栏 拖拽', label: L('恢复默认左右双栏与聊天框布局', 'Reset Dual-Rail & Comms Drawer Layout'), run: () => { railLayout.reset(); commsDrawer.resetLayout(); } },
    { group: L('声学控制', 'AUDIO'), kw: 'mute focus 静音 静默 会议 关闭声音', label: L('一键切换会议静音 / 静默总闸 (Ctrl+Shift+M)', 'Toggle Meeting Focus Mute (Ctrl+Shift+M)'), run: () => setMeetingMute(!meetingMuted) },
    { group: L('陪伴语气', 'TONE'), label: L('陪伴语气 · 熟人搭档', 'Tone · Trusted Partner'), run: () => void api.postAction('select-personality', { id: 'partner' }).then(() => authPrefs?.savePrefs({ personality_id: 'partner' })).catch(reportFailure) },
    { group: L('陪伴语气', 'TONE'), label: L('陪伴语气 · 550W 绝对理性', 'Tone · 550W Rational'), run: () => void api.postAction('select-personality', { id: 'rational' }).then(() => authPrefs?.savePrefs({ personality_id: 'rational' })).catch(reportFailure) },
    { group: L('陪伴语气', 'TONE'), label: L('陪伴语气 · 鲜活热络', 'Tone · Lively & Warm'), run: () => void api.postAction('select-personality', { id: 'lively' }).then(() => authPrefs?.savePrefs({ personality_id: 'lively' })).catch(reportFailure) },
    { group: L('陪伴语气', 'TONE'), label: L('陪伴语气 · 傲娇嘴硬', 'Tone · Cheeky & Snarky'), run: () => void api.postAction('select-personality', { id: 'sassy' }).then(() => authPrefs?.savePrefs({ personality_id: 'sassy' })).catch(reportFailure) },
    { group: L('控制台入口', 'PANEL'), label: L('打开灵魂与记忆档案 (USER / BELIEFS / .md)', 'Open Soul & Memory Archive'), run: () => modals.open('settings', { tab: 'soul' }) },
    { group: L('机位视角', 'CAM'), label: L('切换下一个 550W 机位视角', 'Cycle Next 550W Camera Angle'), run: () => stage.cycleCamera() },
    { group: L('机位视角', 'CAM'), label: L('复位 550W 主视角', 'Reset 550W Main View'), run: () => stage.resetCamera() },
    { group: L('机位视角', 'CAM'), label: L('开启 / 关闭手势视觉交互（Alt+G）', 'Toggle Hand Gesture Control (Alt+G)'), run: () => document.getElementById('stage-gesture-btn')?.click() },
    { group: L('控制台入口', 'PANEL'), label: L('打开历史对话全文检索与导出 MD', 'Open History Search & Export MD'), run: () => modals.open('history') },
    { group: L('控制台入口', 'PANEL'), label: L('打开 550W 大脑中枢（RSI / SAG / 脉搏 / 技能）', 'Open 550W Brain Hub'), run: () => document.getElementById('strip-brain-btn')?.click() },
    { group: L('控制台入口', 'PANEL'), label: L('打开控制台设置', 'Open Settings'), run: () => modals.open('settings') },
    { group: L('控制台入口', 'PANEL'), label: L('切换 4 位身份授权码（CS / C）', 'Switch 4-Char Identity Code (CS / C)'), run: () => authPrefs?.openGate({ switchMode: true }) },
    { group: L('快捷指令', 'ACTION'), label: L('生成当前时刻态势简报', 'Generate Situational Briefing'), run: () => { commsDrawer.open(); void dialogue.fillComposer('请给我一份当前时刻的态势简报', { submit: true }); } },
    { group: L('快捷指令', 'ACTION'), label: L('执行夜间深度整理并汇报世界模型', 'Run Nightly World-Model Consolidation'), run: () => { commsDrawer.open(); void dialogue.fillComposer('帮我做一次深度整理并汇报世界模型', { submit: true }); } },
    { group: L('快捷指令', 'ACTION'), label: L('先来首歌吧', 'Play Local Music'), run: () => { commsDrawer.open(); void dialogue.fillComposer('先来首歌吧', { submit: true }); } },
    { group: L('快捷指令', 'ACTION'), label: L('音乐 30 分钟后睡眠停播', 'Set 30m Music Sleep Timer'), run: () => hudPopups.setSleepTimer(30) },
  ];
  if (authPrefs?.isAdmin()) {
    for (const m of (latestModels?.models || [])) {
      if (m.available !== false) {
        items.push({
          group: L('切换模型 (CS)', 'MODEL (CS)'),
          label: `${m.label || m.id} (${m.model || m.id})`,
          run: () => void api.postAction('select-model', { id: m.id }).catch(reportFailure),
        });
      }
    }
  }
  return items;
}

let cmdkFiltered = [];
let cmdkActiveIdx = 0;

function renderCmdkList(query = '') {
  if (!cmdkList) return;
  const q = String(query || '').trim().toLowerCase();
  const all = buildCmdkItems();
  cmdkFiltered = q ? all.filter((it) => `${it.group} ${it.label} ${it.kw || ''}`.toLowerCase().includes(q)) : all;
  cmdkActiveIdx = 0;
  cmdkList.replaceChildren();
  if (!cmdkFiltered.length) {
    cmdkList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${t('cmdk.empty')}</span></li>`;
    return;
  }
  cmdkFiltered.forEach((item, idx) => {
    const li = document.createElement('li');
    li.className = 'moss-cmdk__item';
    li.setAttribute('role', 'option');
    li.dataset.active = String(idx === cmdkActiveIdx);
    li.innerHTML = `<span class="fui-badge" data-state="data">${item.group}</span><span class="moss-cmdk__label"></span>`;
    li.querySelector('.moss-cmdk__label').textContent = item.label;
    li.addEventListener('click', () => {
      if (cmdkDlg?.open) cmdkDlg.close();
      item.run();
    });
    cmdkList.append(li);
  });
}

function openCmdkPalette() {
  if (!cmdkDlg) return;
  if (cmdkInput) cmdkInput.value = '';
  renderCmdkList('');
  if (!cmdkDlg.open) cmdkDlg.showModal();
  setTimeout(() => cmdkInput?.focus(), 30);
}

cmdkStripBtn?.addEventListener('click', openCmdkPalette);
cmdkInput?.addEventListener('input', () => renderCmdkList(cmdkInput.value));
cmdkInput?.addEventListener('keydown', (ev) => {
  if (!cmdkFiltered.length) return;
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    cmdkActiveIdx = (cmdkActiveIdx + (ev.key === 'ArrowDown' ? 1 : -1) + cmdkFiltered.length) % cmdkFiltered.length;
    [...cmdkList.children].forEach((el, i) => { el.dataset.active = String(i === cmdkActiveIdx); });
    cmdkList.children[cmdkActiveIdx]?.scrollIntoView({ block: 'nearest' });
  } else if (ev.key === 'Enter') {
    ev.preventDefault();
    const chosen = cmdkFiltered[cmdkActiveIdx];
    if (cmdkDlg?.open) cmdkDlg.close();
    chosen?.run();
  }
});

authPrefs = createAuthPrefsController({
  onApplyPrefs(prefs, ident) {
    if (!prefs || typeof prefs !== 'object') return;
    const scope = ident.code || (ident.role === 'CS' ? 'MOSS' : 'C');
    if (prefs.badge && typeof prefs.badge === 'object') {
      badge.applyExternalConfig(prefs.badge, {
        forcedClearance: ident.role === 'CS' ? '' : 'C',
        storageScope: scope,
        silent: true,
      });
    } else {
      badge.applyExternalConfig(null, {
        forcedClearance: ident.role === 'CS' ? '' : 'C',
        storageScope: scope,
        silent: true,
      });
    }
    syncSubsystemChipRows();
    if (prefs.rail_layout && Array.isArray(prefs.rail_layout.left)) {
      suppressLayoutSave = true;
      try {
        for (const id of prefs.rail_layout.right || []) railLayout.moveCard(id, 'right');
        for (const id of prefs.rail_layout.left || []) railLayout.moveCard(id, 'left');
      } finally {
        suppressLayoutSave = false;
      }
    }
    if (typeof prefs.meeting_mute === 'boolean') {
      setMeetingMute(prefs.meeting_mute, { persist: false });
    }
    if (prefs.locale === 'en' || prefs.locale === 'zh-CN') {
      setLocale(prefs.locale, { persist: false });
    }
  },
  onIdentityChange(ident) {
    if (ident.role === 'CS') {
      void authPrefs?.refreshUsersList();
      void telemetry?.refreshLogSource?.();
    } else {
      telemetry?.clearLogs?.();
      modals?.clearAdminCaches?.();
    }
  },
});

document.getElementById('strip-settings')?.addEventListener('click', () => {
  if (authPrefs?.isAdmin()) void authPrefs.refreshUsersList();
});

/* ── Boot Sequence ── */
benben = createBenBenController();
controllersReady = true;
miniMoss.setPreset(stage.getStatus().cameraId);
setLocale(initialLocale(), { persist: false });
const bootPaused = api.isPaused();
document.body.dataset.frozen = String(bootPaused);
stage.setFrozen(bootPaused);
miniMoss.setFrozen(bootPaused);
telemetry.setFrozen(bootPaused);
benben.setFrozen(bootPaused);
window.addEventListener('moss-providers-changed', () => void api.refreshModels());
void authPrefs.init().then((ident) => {
  if (ident?.statusUnavailable || !ident?.enabled) {
    badge.applyExternalConfig(null, { storageScope: 'MOSS', silent: true });
  }
  if (ident?.statusUnavailable || ident?.authorized !== false) api.start();
  if (!opening.isActive()) authPrefs.maybePromptAfterOpening();
  if (ident?.role === 'CS') void authPrefs.refreshUsersList();
});
window.addEventListener('moss-auth-switched', () => {
  api.reconnect({ reset: true });
  void brainDeck?.refresh();
});
window.addEventListener('moss-auth-required', () => {
  authPrefs?.openGate({ forceRequired: true });
});
setTimeout(() => void brainDeck.refresh(), 1500);
const savedCity = cityPicker.getManualCity();
if (savedCity) {
  telemetry.setManualCity(savedCity, { fetchWeather:false });
}
syncAuthorityDeck();
opening.start(false);
if (new URLSearchParams(location.search).get('badge') === '1') {
  badge.open({ mode: 'dual' });
}
if ('serviceWorker' in navigator && !new URLSearchParams(location.search).has('sim')) {
  void navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
}
