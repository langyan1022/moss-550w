/**
 * Center Primary Viewport Controller (.fui-console__viewport on .fui-field).
 *
 * Manages:
 *  - Real 550W 3D WebGL stage (proxied /conversation-assets/moss-view.js)
 *  - 6-position Camera Matrix presets + live AZ/EL telemetry + reset
 *  - Honest WebGL standby notice ONLY when 3D is disabled or unavailable
 *  - Dynamic Headline Plate (.fui-plate) strictly for active stage beats
 *  - Bottom Voice Orbit / Spectrum visualiser via vendor/fui-audio.js (with freeze!)
 *  - Audio element FFT tap bridge for contextual HUD media playback
 */
import { createOrbit, drawOrbit, drawSpectrum, toBands } from '../vendor/fui-audio.js';
import { t } from './i18n.js';

export const CAMERAS = [
  { id: 'interview',   code: '01 · MAIN',   short: 'MAIN',   key: 'cam.interview' },
  { id: 'front',       code: '02 · FRONT',  short: 'FRONT',  key: 'cam.front' },
  { id: 'optics',      code: '03 · LENS',   short: 'LENS',   key: 'cam.optics' },
  { id: 'instruments', code: '04 · PANEL',  short: 'PANEL',  key: 'cam.instruments' },
  { id: 'right',       code: '05 · ARRAY',  short: 'ARRAY',  key: 'cam.right' },
  { id: 'whole',       code: '06 · WHOLE',  short: 'WHOLE',  key: 'cam.whole' },
];

export function createStageController({ onPose, onStatusChange } = {}) {
  const qs = new URLSearchParams(location.search);
  let canvas = document.getElementById('moss-model');
  const loadBox = document.getElementById('model-loading');
  const standbyNotice = document.getElementById('stage-standby-notice');
  const reload3dBtn = document.getElementById('stage-reload-3d');
  const matrixEl = document.getElementById('cam-matrix');
  const stageColsEl = document.getElementById('stage-cols');
  const camIndexEl = document.getElementById('cam-index');
  const tagCamCodeEl = document.getElementById('tag-cam-code');
  const tagSceneEl = document.getElementById('tag-scene');
  const tagSceneChipEl = document.getElementById('tag-scene-chip');
  const yawEl = document.getElementById('cam-yaw');
  const pitchEl = document.getElementById('cam-pitch');
  const figAzEl = document.getElementById('stage-fig-az');
  const figElEl = document.getElementById('stage-fig-el');
  const resetBtn = document.getElementById('reset-view');

  const stageBadge = document.getElementById('stage-badge');
  const stageChrono = document.getElementById('stage-chrono');
  const stageSweep = document.getElementById('stage-sweep');
  const stageLock = document.getElementById('stage-lock');
  const plateEl = document.getElementById('stage-plate');
  const plateKicker = document.getElementById('plate-kicker');
  const plateTitle = document.getElementById('plate-title');
  const plateMeta = document.getElementById('plate-meta');
  const plateDataTag = document.getElementById('plate-datatag');
  let plateFlipTimer = 0;

  const orbitCanvas = document.getElementById('stage-orbit');
  const orbitStateEl = document.getElementById('stage-orbit-state');
  const captionEl = document.getElementById('stage-caption');

  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const orbit = createOrbit(44);
  const silentBands = new Float32Array(44);
  let activeBands = silentBands;
  let audioVisualStyle = (() => {
    try {
      const saved = localStorage.getItem('moss-fui.stage-wave');
      if (['orbit', 'spectrum', 'bars'].includes(saved)) return saved;
    } catch { /* ignore */ }
    return 'spectrum';
  })();
  let frozen = qs.get('paused') === '1';
  let sceneEnabled = qs.get('scene') !== 'off';
  let activeCamera = 'interview';
  let pendingPreset = null;
  let sceneLoaded = false;
  let currentPhase = 'standby'; // standby | listening | thinking | speaking
  let currentBannerActive = false;
  let currentModelLabel = 'DEEPSEEK';
  const CLOSEUP_CAMERAS = new Set(['instruments', 'optics', 'right']);
  let orbitRaf = 0;
  let lastOrbitTick = performance.now();

  function syncChronoVisibility() {
    if (!stageChrono) return;
    const showPlate = !currentBannerActive && currentPhase !== 'standby';
    stageChrono.hidden = showPlate || CLOSEUP_CAMERAS.has(activeCamera);
  }

  let activeMediaEl = null;
  let mediaAudioCtx = null;
  let mediaAnalyser = null;
  let mediaFrameRaf = 0;
  let loadWatchTimer = 0;
  let loadEpoch = 0;
  let canvasTransferred = false;
  let mountMossViewFn = null;
  let unmountScene = null;
  let canvasObserver = null;
  let orbitObserver = null;

  /* ── Build 6-position Rounded Key Matrix + Top-of-Stage Dashed Column Headings ── */
  function buildCameraMatrix() {
    if (matrixEl) {
      matrixEl.replaceChildren();
      CAMERAS.forEach((cam, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'fui-key fui-matrix__key';
        btn.dataset.camera = cam.id;
        btn.setAttribute('aria-pressed', String(cam.id === activeCamera));
        btn.innerHTML = `<span class="fui-key__name fui-matrix__name" data-i18n="${cam.key}">${t(cam.key)}</span><span class="fui-key__code fui-matrix__code fui-num">${cam.short}</span>`;
        btn.addEventListener('click', () => selectCamera(cam.id, idx));
        matrixEl.append(btn);
      });
    }
    if (stageColsEl) {
      stageColsEl.replaceChildren();
      CAMERAS.forEach((cam, idx) => {
        const li = document.createElement('li');
        li.dataset.camera = cam.id;
        if (cam.id === activeCamera) li.setAttribute('aria-current', 'true');
        li.textContent = `${String(idx + 1).padStart(2, '0')} ${cam.short}`;
        stageColsEl.append(li);
      });
    }
  }

  function triggerOpticalLock() {
    if (frozen || reduced.matches) return;
    if (stageSweep) {
      stageSweep.classList.remove('is-sweeping');
      void stageSweep.offsetWidth;
      stageSweep.classList.add('is-sweeping');
    }
    if (stageLock) {
      stageLock.classList.remove('is-locking');
      void stageLock.offsetWidth;
      stageLock.classList.add('is-locking');
    }
  }

  function selectCamera(preset, idx = CAMERAS.findIndex((c) => c.id === preset)) {
    activeCamera = preset;
    for (const btn of matrixEl?.querySelectorAll('.fui-matrix__key') || []) {
      btn.setAttribute('aria-pressed', String(btn.dataset.camera === preset));
    }
    for (const li of stageColsEl?.querySelectorAll('li') || []) {
      if (li.dataset.camera === preset) li.setAttribute('aria-current', 'true');
      else li.removeAttribute('aria-current');
    }
    const label = `${String(idx + 1).padStart(2, '0')} / 06`;
    if (camIndexEl) camIndexEl.textContent = label;
    if (tagCamCodeEl) tagCamCodeEl.textContent = label;
    pendingPreset = preset;
    syncChronoVisibility();
    applyCameraPreset();
    triggerOpticalLock();
    onStatusChange?.();
  }

  function applyCameraPreset() {
    if (!canvas || canvas.dataset.ready !== 'true' || !pendingPreset) return;
    const p = pendingPreset;
    pendingPreset = null;
    canvas.dispatchEvent(new CustomEvent('moss-view-preset', { detail: p }));
  }

  /* ── 3D 550W Scene Loader ── */
  function showStandbyNotice(show) {
    if (standbyNotice) standbyNotice.hidden = !show;
    if (canvas) canvas.hidden = Boolean(show);
    if (tagSceneEl) {
      if (show) tagSceneEl.dataset.state = 'warn';
      else tagSceneEl.dataset.state = 'data';
    }
    if (tagSceneChipEl) tagSceneChipEl.textContent = t(show ? 'tag.paused' : 'tag.active');
    onStatusChange?.();
  }

  function clearLoadWatch() {
    if (loadWatchTimer) {
      clearTimeout(loadWatchTimer);
      loadWatchTimer = 0;
    }
  }

  function onCanvasOptics(ev) {
    const o = ev.detail;
    if (!o) return;
    const yaw = Math.round((((o.yaw + 180) % 360) + 360) % 360 - 180);
    const pitch = Math.round(o.pitch);
    const yawStr = `${yaw > 0 ? '+' : ''}${yaw}°`;
    const pitchStr = `${pitch > 0 ? '+' : ''}${pitch}°`;
    if (yawEl) yawEl.textContent = yawStr;
    if (pitchEl) pitchEl.textContent = pitchStr;
    if (figAzEl) figAzEl.textContent = yawStr;
    if (figElEl) figElEl.textContent = pitchStr;
    onPose?.({ yaw, pitch, x: o.x });
  }

  function bindStageCanvas(target) {
    if (canvas) canvas.removeEventListener('moss-optics', onCanvasOptics);
    canvasObserver?.disconnect();
    canvas = target || null;
    if (!canvas) return;
    canvasObserver = new MutationObserver(() => {
      if (canvas.dataset.error) {
        clearLoadWatch();
        sceneLoaded = false;
        if (loadBox) loadBox.hidden = true;
        showStandbyNotice(true);
      } else if (canvas.dataset.ready === 'true') {
        clearLoadWatch();
        sceneLoaded = true;
        if (loadBox) loadBox.hidden = true;
        showStandbyNotice(false);
        applyCameraPreset();
      }
    });
    canvasObserver.observe(canvas, { attributes: true, attributeFilter: ['data-ready', 'data-error'] });
    canvas.addEventListener('moss-optics', onCanvasOptics);
  }

  function resetCanvasCloneIfTransferred() {
    if (!canvas) return;
    if (canvasTransferred) {
      // OffscreenCanvas can only be transferred once per <canvas> element; swap in a clean clone.
      const fresh = canvas.cloneNode(false);
      delete fresh.dataset.error;
      delete fresh.dataset.ready;
      canvas.replaceWith(fresh);
      bindStageCanvas(fresh);
      canvasTransferred = false;
      document.dispatchEvent(new CustomEvent('moss-stage-canvas-replaced', { detail: { canvas: fresh } }));
    } else {
      delete canvas.dataset.error;
      delete canvas.dataset.ready;
    }
  }

  function load3DScene() {
    showStandbyNotice(false);
    if (sceneLoaded) return;
    sceneLoaded = true;
    const myEpoch = ++loadEpoch;
    if (loadBox) loadBox.hidden = false;
    clearLoadWatch();
    loadWatchTimer = setTimeout(() => {
      loadWatchTimer = 0;
      if (canvas?.dataset?.ready !== 'true') {
        sceneLoaded = false;
        if (loadBox) loadBox.hidden = true;
        showStandbyNotice(true);
      }
    }, 45000);
    const mountNow = () => {
      if (myEpoch !== loadEpoch || !sceneEnabled || !canvas) return;
      unmountScene?.();
      unmountScene = null;
      resetCanvasCloneIfTransferred();
      canvasTransferred = true;
      unmountScene = mountMossViewFn(canvas);
    };
    if (mountMossViewFn) {
      mountNow();
      return;
    }
    import('/conversation-assets/moss-view.js?entry=fui-console&lifecycle=1')
      .then((mod) => {
        if (typeof mod?.mountMossView === 'function') {
          mountMossViewFn = mod.mountMossView;
          mountNow();
        } else if (myEpoch === loadEpoch) {
          clearLoadWatch();
          canvasTransferred = true;
        }
      })
      .catch(() => {
        if (myEpoch !== loadEpoch) return;
        clearLoadWatch();
        sceneLoaded = false;
        if (loadBox) loadBox.hidden = true;
        showStandbyNotice(true);
      });
  }

  bindStageCanvas(canvas);

  resetBtn?.addEventListener('click', () => selectCamera('interview', 0));
  reload3dBtn?.addEventListener('click', () => {
    unmountScene?.();
    unmountScene = null;
    resetCanvasCloneIfTransferred();
    sceneLoaded = false;
    sceneEnabled = true;
    load3DScene();
  });

  /* ── Dynamic Headline Plate & Stage Phase ── */
  function updatePhase(phase, { model = currentModelLabel, bannerActive = false } = {}) {
    const prevPhase = currentPhase;
    currentPhase = phase || 'standby';
    currentModelLabel = model || currentModelLabel;

    const badgeMap = {
      standby:   { state: 'idle', key: 'state.standby' },
      listening: { state: 'crit', key: 'state.listening' },
      thinking:  { state: 'warn', key: 'state.thinking' },
      speaking:  { state: 'ok',   key: 'state.speaking' },
    };
    const b = badgeMap[currentPhase] || badgeMap.standby;
    if (stageBadge) {
      stageBadge.dataset.state = b.state;
      stageBadge.textContent = t(b.key);
    }

    currentBannerActive = Boolean(bannerActive);
    const showPlate = !currentBannerActive && currentPhase !== 'standby';
    syncChronoVisibility();
    if (plateEl) {
      plateEl.hidden = !showPlate;
      if (showPlate) {
        plateEl.dataset.state = currentPhase === 'speaking' ? 'ok' : currentPhase === 'listening' ? 'crit' : 'warn';
        plateKicker.textContent = t('plate.kicker');
        if (currentPhase === 'listening') {
          plateTitle.textContent = t('plate.listening');
          plateMeta.textContent = t('plate.listening_meta');
        } else if (currentPhase === 'thinking') {
          plateTitle.textContent = t('plate.thinking');
          plateMeta.textContent = t('plate.thinking_meta', { model: currentModelLabel.toUpperCase() });
        } else if (currentPhase === 'speaking') {
          plateTitle.textContent = t('plate.speaking');
          plateMeta.textContent = t('plate.speaking_meta');
        }
        if (plateDataTag) {
          plateDataTag.hidden = currentPhase !== 'thinking';
        }
        if (prevPhase !== currentPhase && !frozen && !reduced.matches) {
          plateEl.removeAttribute('data-flip');
          void plateEl.offsetWidth;
          plateEl.setAttribute('data-flip', '');
          clearTimeout(plateFlipTimer);
          plateFlipTimer = setTimeout(() => plateEl.removeAttribute('data-flip'), 1550);
        }
      }
    }

    updateOrbitLabel();
    syncOrbitLoop();
    drawOrbitFrame(0);
    dispatchLensSignal(currentPhase, null);
  }

  function dispatchLensSignal(phase, frame) {
    if (!canvas || canvas.dataset.ready !== 'true') return;
    const bins = frame?.freq || new Uint8Array(0);
    let rms = 0;
    if (frame?.time?.length) {
      let acc = 0;
      for (let i = 0; i < frame.time.length; i++) acc += frame.time[i] * frame.time[i];
      rms = Math.sqrt(acc / frame.time.length);
    }
    const mapPhase = { standby: 'standby', listening: 'listen', thinking: 'think', speaking: 'speak' }[phase] || 'standby';
    canvas.dispatchEvent(new CustomEvent('moss-signal', {
      detail: {
        phase: mapPhase,
        source: frame ? (phase === 'listening' ? 'mic' : 'voice') : 'idle',
        bins,
        sampleRate: frame?.sampleRate || 48000,
        dbfs: 20 * Math.log10(Math.max(0.00001, rms)),
        level: Math.min(1, rms * 4.5),
      },
    }));
  }

  /* ── Stage Glowing Sine-Wave Orbit Audio Visualiser (fui-audio.js) ── */
  function updateOrbitLabel() {
    if (!orbitStateEl) return;
    if (frozen) {
      orbitStateEl.textContent = t('stage.orbit_frozen');
      return;
    }
    const key = {
      standby: 'stage.orbit_standby',
      listening: 'stage.orbit_listening',
      thinking: 'stage.orbit_thinking',
      speaking: 'stage.orbit_speaking',
    }[currentPhase] || 'stage.orbit_standby';
    orbitStateEl.textContent = activeMediaEl && !activeMediaEl.paused ? t('stage.orbit_media') : t(key);
  }

  function syntheticActiveBands(now) {
    const out = new Float32Array(44);
    const amp = currentPhase === 'speaking' ? 0.58 : currentPhase === 'listening' ? 0.45 : currentPhase === 'thinking' ? 0.24 : 0.12;
    for (let i = 0; i < 44; i++) {
      const wave = 0.5 + 0.5 * Math.sin(now * (currentPhase === 'standby' ? 0.0024 : 0.0062) + i * 0.36);
      const bell = Math.exp(-((i - 20) ** 2) / 220);
      out[i] = Math.min(1, amp * bell * (0.35 + 0.65 * wave));
    }
    return out;
  }

  function drawOrbitFrame(dt) {
    if (!orbitCanvas) return;
    const isMediaLive = Boolean(activeMediaEl && !activeMediaEl.paused && !activeMediaEl.ended);
    const hasRealSignal = activeBands !== silentBands;
    const isAnimated = !frozen && !reduced.matches;
    const isIdle = !hasRealSignal && currentPhase === 'standby' && !isMediaLive;

    // Official Wandering Earth FUI audio idle rule (fui-audio.js drawOrbit):
    // When idle, feed silentBands with ambient: true so waves settle into the softer, wider
    // stepped halo with a slow living shallow swell and drifting particles.
    const bands = hasRealSignal
      ? activeBands
      : (isIdle ? silentBands : syntheticActiveBands(performance.now()));

    const orbitState = currentPhase === 'listening' ? 'crit' : currentPhase === 'thinking' ? 'warn' : undefined;

    if (audioVisualStyle === 'spectrum' || audioVisualStyle === 'bars') {
      const specBands = isIdle && isAnimated ? syntheticActiveBands(performance.now()) : bands;
      drawSpectrum(orbitCanvas, specBands, {
        style: audioVisualStyle === 'bars' ? 'bars' : 'lines',
        ink: 'data',
        state: orbitState,
        segments: audioVisualStyle === 'bars' ? 10 : 12,
      });
    } else {
      drawOrbit(orbitCanvas, orbit, {
        bands,
        dt: isAnimated ? (isIdle ? dt * 2.1 : dt) : 0,
        layout: 'line',
        waves: 3,
        glow: true,
        particles: true,
        ambient: true,
        ink: 'data',
        state: orbitState,
      });
    }
  }

  function syncOrbitLoop() {
    const needsAnimation = !frozen && !reduced.matches && !document.hidden;

    if (!needsAnimation) {
      cancelAnimationFrame(orbitRaf);
      orbitRaf = 0;
      drawOrbitFrame(0);
      return;
    }
    if (orbitRaf) return;
    lastOrbitTick = performance.now();
    const tick = (now) => {
      if (frozen || reduced.matches || document.hidden) {
        orbitRaf = 0;
        drawOrbitFrame(0);
        return;
      }
      const dt = Math.min(0.05, Math.max(0.001, (now - lastOrbitTick) / 1000));
      lastOrbitTick = now;
      drawOrbitFrame(dt);
      orbitRaf = requestAnimationFrame(tick); // fui-lint-ignore: R15 user-requested live ambient acoustic carrier wave; stops cleanly when frozen, hidden, or reduced-motion
    };
    orbitRaf = requestAnimationFrame(tick); // fui-lint-ignore: R15 user-requested live ambient acoustic carrier wave; stops cleanly when frozen, hidden, or reduced-motion
  }

  function pushAudioFrame(sourcePhase, frame) {
    if (!frame) {
      activeBands = silentBands;
      dispatchLensSignal(currentPhase, null);
      updateOrbitLabel();
      syncOrbitLoop();
      return;
    }
    const raw = toBands(frame.freq, frame.sampleRate || 48000, 44, { fMin: 70, fMax: 9000 });
    for (let i = 0; i < raw.length; i++) raw[i] = Math.min(1, raw[i] > 0.01 ? Math.pow(raw[i], 0.72) : raw[i]);
    activeBands = raw;
    dispatchLensSignal(sourcePhase, frame);
    if (!frozen) drawOrbitFrame(0.016);
    updateOrbitLabel();
    syncOrbitLoop();
  }

  /* ── Media Element FFT Tap Bridge (used by hud-popups.js) ── */
  function stopMediaTap() {
    cancelAnimationFrame(mediaFrameRaf);
    mediaFrameRaf = 0;
    if (activeMediaEl?._fuiSource && activeMediaEl._fuiSourceConnected) {
      try { activeMediaEl._fuiSource.disconnect(); } catch { /* ignore */ }
      activeMediaEl._fuiSourceConnected = false;
    }
    activeMediaEl = null;
    activeBands = silentBands;
    updateOrbitLabel();
    syncOrbitLoop();
  }

  function attachMediaElement(el, onFrame) {
    cancelAnimationFrame(mediaFrameRaf);
    activeMediaEl = el;
    updateOrbitLabel();
    syncOrbitLoop();
    try {
      if (!mediaAudioCtx) {
        mediaAudioCtx = new AudioContext();
        mediaAnalyser = mediaAudioCtx.createAnalyser();
        mediaAnalyser.fftSize = 512;
        mediaAnalyser.connect(mediaAudioCtx.destination);
      }
      if (!el._fuiSource) {
        const src = el.currentSrc || el.getAttribute('src') || '';
        if (src && !src.startsWith('blob:') && !src.startsWith('data:')) {
          const parsed = new URL(src, location.href);
          if (parsed.origin !== location.origin) return;
        }
        el._fuiSource = mediaAudioCtx.createMediaElementSource(el);
      }
      if (el._fuiSource && !el._fuiSourceConnected) {
        el._fuiSource.connect(mediaAnalyser);
        el._fuiSourceConnected = true;
      }
      if (mediaAudioCtx.state === 'suspended') {
        void mediaAudioCtx.resume().catch(() => {});
        const unlockOnce = () => {
          if (mediaAudioCtx?.state === 'suspended') void mediaAudioCtx.resume().catch(() => {});
        };
        window.addEventListener('pointerdown', unlockOnce, { once: true, capture: true });
        window.addEventListener('keydown', unlockOnce, { once: true, capture: true });
      }
      const freq = new Uint8Array(mediaAnalyser.frequencyBinCount);
      const time = new Float32Array(mediaAnalyser.fftSize);
      const pump = () => {
        if (activeMediaEl !== el || el.paused || el.ended) {
          stopMediaTap();
          return;
        }
        mediaAnalyser.getByteFrequencyData(freq);
        mediaAnalyser.getFloatTimeDomainData(time);
        const frame = { freq, time, sampleRate: mediaAudioCtx.sampleRate };
        onFrame?.(frame);
        if (!el.muted && el.volume > 0) pushAudioFrame('speaking', frame);
        mediaFrameRaf = requestAnimationFrame(pump); // fui-lint-ignore: R15 runs strictly while media element plays
      };
      pump();
    } catch { /* element already connected or unsupported */ }
  }

  function setCaption(text) {
    if (!captionEl) return;
    const clean = (text || '').trim();
    captionEl.hidden = !clean;
    if (captionEl.textContent !== clean) captionEl.textContent = clean;
  }

  function setFrozen(next) {
    frozen = Boolean(next);
    updateOrbitLabel();
    syncOrbitLoop();
  }

  function setAudioStyle(style) {
    audioVisualStyle = ['orbit', 'spectrum', 'bars'].includes(style) ? style : 'orbit';
    try { localStorage.setItem('moss-fui.stage-wave', audioVisualStyle); } catch { /* ignore */ }
    drawOrbitFrame(0);
  }
  function getAudioStyle() {
    return audioVisualStyle;
  }

  function setSceneEnabled(enabled) {
    sceneEnabled = Boolean(enabled);
    if (sceneEnabled) {
      if (canvas?.dataset?.error) resetCanvasCloneIfTransferred();
      load3DScene();
    } else {
      loadEpoch += 1;
      unmountScene?.();
      unmountScene = null;
      sceneLoaded = false;
      clearLoadWatch();
      if (loadBox) loadBox.hidden = true;
      showStandbyNotice(true);
    }
  }

  if (orbitCanvas) {
    orbitObserver = new ResizeObserver(() => drawOrbitFrame(0));
    orbitObserver.observe(orbitCanvas);
  }
  document.addEventListener('visibilitychange', syncOrbitLoop);
  reduced.addEventListener('change', syncOrbitLoop);

  buildCameraMatrix();
  if (sceneEnabled) load3DScene();
  else showStandbyNotice(true);
  drawOrbitFrame(0);
  syncOrbitLoop();

  return {
    selectCamera,
    updatePhase,
    pushAudioFrame,
    attachMediaElement,
    stopMediaTap,
    setCaption,
    setFrozen,
    setAudioStyle,
    getAudioStyle,
    setSceneEnabled,
    destroy() {
      loadEpoch += 1;
      unmountScene?.();
      unmountScene = null;
      clearLoadWatch();
      cancelAnimationFrame(orbitRaf);
      cancelAnimationFrame(mediaFrameRaf);
      canvasObserver?.disconnect();
      orbitObserver?.disconnect();
      document.removeEventListener('visibilitychange', syncOrbitLoop);
      reduced.removeEventListener('change', syncOrbitLoop);
      mediaAudioCtx?.close?.().catch(() => {});
      mediaAudioCtx = null;
      mediaAnalyser = null;
    },
    getStatus() {
      const idx = Math.max(0, CAMERAS.findIndex((c) => c.id === activeCamera));
      return {
        sceneActive: standbyNotice?.hidden !== false,
        cameraId: activeCamera,
        cameraIndex: idx,
        cameraKey: CAMERAS[idx]?.key || 'cam.interview',
      };
    },
    cycleCamera() {
      const idx = Math.max(0, CAMERAS.findIndex((c) => c.id === activeCamera));
      const nextIdx = (idx + 1) % CAMERAS.length;
      selectCamera(CAMERAS[nextIdx].id, nextIdx);
    },
    prevCamera() {
      const idx = Math.max(0, CAMERAS.findIndex((c) => c.id === activeCamera));
      const prevIdx = (idx - 1 + CAMERAS.length) % CAMERAS.length;
      selectCamera(CAMERAS[prevIdx].id, prevIdx);
    },
    resetCamera() {
      selectCamera('interview', 0);
    },
    refreshI18n() {
      updatePhase(currentPhase);
      updateOrbitLabel();
      if (tagSceneChipEl) tagSceneChipEl.textContent = t(standbyNotice?.hidden === false ? 'tag.paused' : 'tag.active');
    },
  };
}
