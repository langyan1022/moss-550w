/**
 * Opt-in local webcam hand-gesture camera deck for the MOSS 550W Console.
 * Uses MediaPipe HandLandmarker served from /conversation-assets/hand-gesture/.
 *
 * 5 real gestures:
 *   - ☝ point : hold ~320ms -> next camera preset (stage.cycleCamera)
 *   - ✌ peace : hold ~320ms -> previous camera preset (stage.prevCamera)
 *   - ✊ fist  : hold ~420ms -> reset to main interview camera (stage.resetCamera)
 *   - 🖐 palm  : continuous mirrored palm tracking -> smooth 550W 3D joint yaw/pitch rotation
 *   - 🤏 pinch : thumb + index pinch vertical slide -> smooth 550W 3D zoom in/out
 *
 * Strict privacy lifecycle: never starts automatically; stops tracks on toggle-off,
 * HUD close, 3D stage disable, opening replay, tab hide, freeze, pagehide, track end,
 * or permission revoke.
 */
import { t } from './i18n.js';

const ASSETS = '/conversation-assets/hand-gesture';
const HAND_CONNECTIONS = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20],
  [0, 17],
];

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

export function pointsUp(points) {
  if (points?.length !== 21) return false;
  const wrist = points[0];
  const palm = distance(wrist, points[9]);
  if (palm < 0.025) return false;
  const reach = (joint, tip) => distance(wrist, points[tip]) / Math.max(0.001, distance(wrist, points[joint]));
  return reach(6, 8) > 1.15 && (points[6].y - points[8].y) / palm > 0.22
    && reach(10, 12) < 1.05 && reach(14, 16) < 1.05 && reach(18, 20) < 1.05;
}

export function classifyGesture(points) {
  if (points?.length !== 21) return 'none';
  const wrist = points[0];
  const palm = distance(wrist, points[9]);
  if (palm < 0.025) return 'none';
  const reach = (joint, tip) => distance(wrist, points[tip]) / Math.max(0.001, distance(wrist, points[joint]));

  const indexReach = reach(6, 8);
  const middleReach = reach(10, 12);
  const ringReach = reach(14, 16);
  const pinkyReach = reach(18, 20);

  const indexExt = indexReach > 1.15;
  const middleExt = middleReach > 1.15;
  const ringExt = ringReach > 1.12;
  // Little finger naturally stays partly flexed under flexor tendon coupling when holding 3 fingers up.
  const pinkyExt = pinkyReach > 1.24 && pinkyReach >= ringReach * 0.85;

  const indexCurled = indexReach < 1.05;
  const middleCurled = middleReach < 1.05;
  const ringCurled = ringReach < 1.05;
  const pinkyCurled = pinkyReach < 1.08;
  const pinchDist = distance(points[4], points[8]) / palm;
  const openFingers4 = (indexExt ? 1 : 0) + (middleExt ? 1 : 0) + (ringExt ? 1 : 0) + (pinkyExt ? 1 : 0);

  if (indexExt && (points[6].y - points[8].y) / palm > 0.20 && middleCurled && ringCurled && pinkyCurled) {
    return 'point';
  }
  if (indexExt && middleExt && ringCurled && pinkyCurled && pinchDist > 0.36) {
    return 'peace';
  }
  // 3 fingers extended (Index + Middle + Ring, Pinky curled / not extended) -> Overhead Rail Pan
  if (indexExt && middleExt && ringExt && !pinkyExt && pinchDist > 0.36) {
    return 'rail';
  }
  if (pinchDist < 0.36 && openFingers4 >= 2) {
    return 'pinch';
  }
  if (indexCurled && middleCurled && ringCurled && pinkyCurled) {
    return 'fist';
  }
  // 4-5 fingers (Open Palm) -> Body Yaw & Pitch Attitude Control
  if (indexExt && middleExt && ringExt && pinkyExt) {
    return 'palm';
  }
  return 'none';
}

function readToken(name, fallback) {
  try {
    const val = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (val) return val;
  } catch { /* ignore */ }
  return fallback;
}

export function createHandGestureController({
  toggle,
  stageButton,
  camButton,
  status,
  video,
  hudEl,
  overlayCanvas,
  badgeEl,
  noticeEl,
  stopButton,
  stage,
  canvas,
  acquireStream,
  onCycle,
} = {}) {
  const stageMain = document.querySelector('main');
  const triggerButtons = [stageButton, camButton].filter(Boolean);
  const getCameraApi = () => navigator.mediaDevices?.getUserMedia?.bind(navigator.mediaDevices);

  let current = null;
  let startingSession = null;
  let wasAvailable = false;
  let lastStatusKey = 'gesture.idle';
  let lastStatusVars = null;
  let lastBadgeKey = 'gesture.badge_ready';
  let noticeTimer = 0;
  let activeNoticeKey = null;
  let activeNoticeVars = null;

  let lastOptics = { yaw: 0, pitch: 0, x: 0.5, grip: null };
  if (canvas) {
    canvas.addEventListener('moss-optics', (ev) => {
      if (ev.detail) {
        lastOptics = {
          yaw: typeof ev.detail.yaw === 'number' ? ev.detail.yaw : lastOptics.yaw,
          pitch: typeof ev.detail.pitch === 'number' ? ev.detail.pitch : lastOptics.pitch,
          x: typeof ev.detail.x === 'number' ? ev.detail.x : lastOptics.x,
          grip: ev.detail.grip !== undefined ? ev.detail.grip : lastOptics.grip,
        };
      }
    });
  }

  if (toggle) toggle.checked = false;
  for (const btn of triggerButtons) btn.setAttribute('aria-pressed', 'false');
  if (hudEl) hudEl.hidden = true;
  if (noticeEl) noticeEl.hidden = true;
  if (video) {
    video.muted = true;
    video.playsInline = true;
  }

  function clearNotice() {
    if (noticeTimer) {
      clearTimeout(noticeTimer);
      noticeTimer = 0;
    }
    activeNoticeKey = null;
    activeNoticeVars = null;
    if (noticeEl) {
      noticeEl.hidden = true;
      noticeEl.textContent = '';
    }
    if (hudEl) {
      delete hudEl.dataset.mode;
      if (!current) hudEl.hidden = true;
    }
  }

  function showNotice(key, vars = null) {
    if (!hudEl || !noticeEl) return;
    if (noticeTimer) clearTimeout(noticeTimer);
    activeNoticeKey = key;
    activeNoticeVars = vars;
    hudEl.dataset.mode = 'notice';
    hudEl.hidden = false;
    noticeEl.textContent = vars ? t(key, vars) : t(key);
    noticeEl.hidden = false;
    setBadge('gesture.badge_error', 'crit');
    noticeTimer = setTimeout(() => { // fui-lint-ignore: R15 one-shot auto-dismiss for camera error notice
      clearNotice();
    }, 6500);
  }

  function setStatus(key, vars = null) {
    lastStatusKey = key;
    lastStatusVars = vars;
    if (status) status.textContent = vars ? t(key, vars) : t(key);
  }

  function setBadge(key, state = 'data') {
    lastBadgeKey = key;
    if (badgeEl) {
      badgeEl.textContent = t(key);
      badgeEl.dataset.state = state;
    }
  }

  function syncButtons(active) {
    const pressed = Boolean(active);
    if (toggle) toggle.checked = pressed;
    for (const btn of triggerButtons) {
      btn.setAttribute('aria-pressed', String(pressed));
      if (pressed) btn.dataset.state = 'ok';
      else btn.removeAttribute('data-state');
    }
    if (hudEl) {
      if (active) {
        clearNotice();
        hudEl.hidden = false;
      } else if (hudEl.dataset.mode !== 'notice') {
        hudEl.hidden = true;
      }
    }
  }

  function clearOverlay() {
    if (!overlayCanvas) return;
    const ctx = overlayCanvas.getContext('2d');
    ctx?.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
  }

  function drawHandSkeleton(points, gesture) {
    if (!overlayCanvas) return;
    const ctx = overlayCanvas.getContext('2d');
    if (!ctx) return;
    const w = overlayCanvas.width || 240;
    const h = overlayCanvas.height || 180;
    ctx.clearRect(0, 0, w, h);
    if (!points?.length) return;

    const strokeColor = gesture === 'rail'
      ? readToken('--fui-ok', '#36e090')
      : gesture === 'palm'
        ? readToken('--fui-data', '#38d3ff')
        : gesture === 'fist'
          ? readToken('--fui-crit', '#ff4d4d')
          : (gesture === 'point' || gesture === 'peace')
            ? readToken('--fui-act', '#ff9b26')
            : readToken('--fui-data', '#38d3ff');
    const jointColor = readToken('--fui-text-hi', '#e6f4ff');

    ctx.lineWidth = 1.8;
    ctx.strokeStyle = strokeColor;
    ctx.beginPath();
    for (const [a, b] of HAND_CONNECTIONS) {
      const pa = points[a];
      const pb = points[b];
      if (!pa || !pb) continue;
      // Mirror X to match mirrored preview video
      ctx.moveTo((1 - pa.x) * w, pa.y * h);
      ctx.lineTo((1 - pb.x) * w, pb.y * h);
    }
    ctx.stroke();

    ctx.fillStyle = jointColor;
    for (let i = 0; i < points.length; i += 1) {
      const p = points[i];
      const isTip = i === 4 || i === 8 || i === 12 || i === 16 || i === 20;
      ctx.beginPath();
      ctx.arc((1 - p.x) * w, p.y * h, isTip ? 3.2 : 2.0, 0, Math.PI * 2);
      ctx.fill();
    }
    if (gesture === 'rail') {
      const mx = (1 - points[9].x) * w;
      const my = points[9].y * h;
      ctx.strokeStyle = strokeColor;
      ctx.lineWidth = 2.2;
      ctx.beginPath();
      ctx.moveTo(mx - 24, my);
      ctx.lineTo(mx + 24, my);
      ctx.moveTo(mx - 18, my - 5);
      ctx.lineTo(mx - 24, my);
      ctx.lineTo(mx - 18, my + 5);
      ctx.moveTo(mx + 18, my - 5);
      ctx.lineTo(mx + 24, my);
      ctx.lineTo(mx + 18, my + 5);
      ctx.stroke();
    } else if (gesture === 'pinch') {
      const mx = (1 - (points[4].x + points[8].x) * 0.5) * w;
      const my = ((points[4].y + points[8].y) * 0.5) * h;
      ctx.strokeStyle = strokeColor;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(mx, my, 10, 0, Math.PI * 2);
      ctx.stroke();
    } else if (gesture === 'palm') {
      const mx = (1 - points[9].x) * w;
      const my = points[9].y * h;
      ctx.strokeStyle = strokeColor;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(mx, my - 14);
      ctx.lineTo(mx, my + 14);
      ctx.moveTo(mx - 4, my - 10);
      ctx.lineTo(mx, my - 14);
      ctx.lineTo(mx + 4, my - 10);
      ctx.moveTo(mx - 4, my + 10);
      ctx.lineTo(mx, my + 14);
      ctx.lineTo(mx + 4, my + 10);
      ctx.stroke();
    }
  }

  function releasePinchZoom(session) {
    if (session) session.pinchZoom = null;
  }

  function updatePinchZoom(session, hand) {
    if (!canvas || canvas.dataset.ready !== 'true') return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;
    const pinchY = (hand[4].y + hand[8].y) * 0.5;
    if (!session.pinchZoom) {
      session.pinchZoom = { lastY: pinchY };
      setStatus('gesture.zooming');
      return;
    }
    const dy = pinchY - session.pinchZoom.lastY;
    session.pinchZoom.lastY = pinchY;
    if (Math.abs(dy) < 0.005) return;
    // Slide pinched hand up -> zoom in (negative wheel deltaY); down -> zoom out
    setBadge(dy < 0 ? 'gesture.badge_pinch_in' : 'gesture.badge_pinch_out', 'data');
    const deltaY = Math.max(-160, Math.min(160, dy * 1400));
    try {
      canvas.dispatchEvent(new CustomEvent('moss-pinch-zoom', {
        detail: { deltaY },
      }));
    } catch { /* ignore */ }
  }

  function releaseRailDrag(session) {
    if (!session?.railDrag) return;
    session.railDrag = null;
  }

  function updateRailDrag(session, hand) {
    if (!canvas || canvas.dataset.ready !== 'true') return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;
    const handX = 1 - hand[9].x; // Mirrored X

    if (!session.railDrag) {
      const baseAnchor = typeof lastOptics.grip?.anchor === 'number'
        ? lastOptics.grip.anchor
        : (typeof lastOptics.x === 'number' ? lastOptics.x : 0.45);
      session.railDrag = {
        lastX: handX,
        travelX: Math.max(0.08, Math.min(0.92, baseAnchor)),
      };
      setStatus('gesture.rail_pan');
      setBadge('gesture.badge_rail', 'ok');
      return;
    }

    const dx = handX - session.railDrag.lastX;
    session.railDrag.lastX = handX;
    if (Math.abs(dx) < 0.002) return;

    // Slide 550W mount directly along overhead world-X rail across all presets (never falls back to body yaw).
    session.railDrag.travelX = Math.max(0.08, Math.min(0.92, session.railDrag.travelX + dx * 1.6));

    try {
      canvas.dispatchEvent(new CustomEvent('moss-rail-slide', {
        detail: { x: session.railDrag.travelX },
      }));
    } catch { /* ignore */ }

    const dir = dx > 0 ? '→' : '←';
    const pct = Math.round(session.railDrag.travelX * 100);
    setBadge(dx > 0 ? 'gesture.badge_rail_right' : 'gesture.badge_rail_left', 'ok');
    setStatus('gesture.rail_panning', { dir, pos: pct });
  }

  function releasePalmDrag(session) {
    if (!session?.palmDrag) return;
    session.palmDrag = null;
  }

  function updatePalmDrag(session, hand) {
    if (!canvas || canvas.dataset.ready !== 'true') return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;
    // Mirrored palm anchor at middle-finger MCP (landmark 9)
    const palmX = 1 - hand[9].x;
    const palmY = hand[9].y;

    if (!session.palmDrag) {
      session.palmDrag = { lastX: palmX, lastY: palmY };
      const curPitch = Math.round(lastOptics.pitch ?? 0);
      const curYaw = Math.round(lastOptics.yaw ?? 0);
      setStatus('gesture.rotating', {
        yaw: curYaw > 0 ? `+${curYaw}` : String(curYaw),
        pitch: curPitch > 0 ? `+${curPitch}` : String(curPitch),
      });
      setBadge('gesture.badge_palm', 'ok');
      return;
    }

    const dx = palmX - session.palmDrag.lastX;
    const dy = palmY - session.palmDrag.lastY;
    session.palmDrag.lastX = palmX;
    session.palmDrag.lastY = palmY;

    if (Math.abs(dx) < 0.003 && Math.abs(dy) < 0.003) return;

    // GainX controls yaw rotation sensitivity
    const gainX = Math.min(rect.width * 0.85, 520);
    // GainY controls pitch elevation sensitivity (boosted 5x for responsive tilt)
    const gainY = Math.min(rect.height * 2.8, 1600);

    const isPitchPriority = Math.abs(dy) > Math.abs(dx) * 1.5 && Math.abs(dy) > 0.006;
    const stepX = isPitchPriority ? 0 : Math.max(-85, Math.min(85, dx * gainX));
    const stepY = Math.max(-75, Math.min(75, dy * gainY));

    // Dispatch joint delta directly so OrbitControls never calls setPointerCapture on synthetic pointers.
    try {
      canvas.dispatchEvent(new CustomEvent('moss-palm-drag', { detail: { dx: stepX, dy: stepY } }));
    } catch { /* ignore */ }

    const curPitch = Math.round(lastOptics.pitch ?? 0);
    const curYaw = Math.round(lastOptics.yaw ?? 0);

    if (isPitchPriority) {
      setBadge('gesture.badge_pitch', 'ok');
      setStatus('gesture.pitching', { pitch: curPitch > 0 ? `+${curPitch}` : String(curPitch) });
    } else {
      setBadge('gesture.badge_palm', 'ok');
      setStatus('gesture.rotating', {
        yaw: curYaw > 0 ? `+${curYaw}` : String(curYaw),
        pitch: curPitch > 0 ? `+${curPitch}` : String(curPitch),
      });
    }
  }

  function sceneReady() {
    const st = stage?.getStatus?.();
    return Boolean(
      st?.sceneActive
      && canvas?.dataset?.ready === 'true'
      && !stageMain?.hasAttribute('data-opening'),
    );
  }

  function stop(statusKey = 'gesture.closed', vars = null, { notifyStage = false } = {}) {
    const session = current;
    current = null;
    startingSession = null;
    if (!notifyStage) clearNotice();
    syncButtons(false);
    clearOverlay();
    setBadge('gesture.badge_ready', 'data');
    if (session) {
      releaseRailDrag(session);
      releasePalmDrag(session);
      releasePinchZoom(session);
      clearTimeout(session.frame);
      session.unwatchPermission?.();
      session.stream?.getTracks?.().forEach((track) => track.stop());
      if (video) {
        video.pause();
        video.srcObject = null;
      }
      // Always release webcam before closing the recognizer so recognizer errors never block cleanup.
      try { session.tracker?.close?.(); } catch { /* ignore */ }
    }
    setStatus(statusKey, vars);
    if (notifyStage) showNotice(statusKey, vars);
  }

  function refresh() {
    const requestCamera = getCameraApi();
    const unavailableKey = !requestCamera
      ? 'gesture.no_camera'
      : !sceneReady()
        ? 'gesture.need_3d'
        : '';
    if (toggle) toggle.disabled = Boolean(unavailableKey);
    // Keep stage button clickable when only camera is missing so user gets an honest stage notice
    if (stageButton) stageButton.disabled = !sceneReady();
    if (unavailableKey) {
      if (current) stop(unavailableKey);
      else setStatus(unavailableKey);
    } else if (!wasAvailable && !current) {
      setStatus('gesture.idle');
    }
    wasAvailable = !unavailableKey;
  }

  async function watchPermission(session) {
    try {
      const permission = await navigator.permissions?.query({ name: 'camera' });
      if (!permission || current !== session) return;
      const changed = () => {
        if (current === session && permission.state !== 'granted') stop('gesture.revoked');
      };
      permission.addEventListener('change', changed);
      session.unwatchPermission = () => permission.removeEventListener('change', changed);
      if (permission.state === 'denied') changed();
    } catch { /* Permissions API camera query is optional */ }
  }

  async function start() {
    if (!video || current) return;
    const requestCamera = getCameraApi();
    if (!requestCamera || !sceneReady() || document.hidden) {
      const reason = !requestCamera ? 'gesture.no_camera' : !sceneReady() ? 'gesture.need_3d' : 'gesture.closed';
      syncButtons(false);
      setStatus(reason);
      if (!document.hidden) showNotice(reason);
      return;
    }
    if (startingSession) {
      stop('gesture.busy', null, { notifyStage: true });
      return;
    }
    const session = {
      latched: false,
      holdGesture: 'none',
      holdSince: null,
      releaseSince: null,
      palmDrag: null,
      pinchZoom: null,
      mediaTime: -1,
      lastSample: 0,
    };
    current = session;
    startingSession = session;
    syncButtons(true);
    setStatus('gesture.loading');
    setBadge('gesture.badge_ready', 'data');
    try {
      const { FilesetResolver, HandLandmarker } = await import(`${ASSETS}/vision_bundle.mjs`);
      if (current !== session) return;
      const files = await FilesetResolver.forVisionTasks(`${ASSETS}/wasm`);
      if (current !== session) return;
      const tracker = await HandLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: `${ASSETS}/hand_landmarker.task`, delegate: 'CPU' },
        runningMode: 'VIDEO',
        numHands: 1,
      });
      if (current !== session) {
        try { tracker.close?.(); } catch { /* ignore */ }
        return;
      }
      session.tracker = tracker;
      setStatus('gesture.awaiting_auth');
      const shared = acquireStream?.();
      let stream = null;
      if (shared && shared.getVideoTracks?.().some((tr) => tr.readyState === 'live')) {
        try {
          stream = shared.clone();
        } catch {
          stream = null;
        }
      }
      if (!stream) {
        stream = await requestCamera({
          audio: false,
          video: { width: { ideal: 480 }, height: { ideal: 360 }, frameRate: { ideal: 12, max: 15 } },
        });
      }
      if (current !== session) {
        stream?.getTracks?.().forEach((track) => track.stop());
        return;
      }
      session.stream = stream;
      const track = stream.getVideoTracks()[0];
      if (!track || track.readyState !== 'live') {
        stop('gesture.disconnected');
        return;
      }
      track.addEventListener('ended', () => {
        if (current === session) stop('gesture.disconnected');
      }, { once: true });
      void watchPermission(session);
      video.srcObject = stream;
      await video.play();
      if (current !== session) return;
      setStatus('gesture.ready');

      function frame() {
        if (current !== session) return;
        if (track.readyState !== 'live') {
          stop('gesture.disconnected', null, { notifyStage: true });
          return;
        }
        if (document.hidden || !sceneReady()) {
          stop('gesture.closed');
          return;
        }
        if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.currentTime !== session.mediaTime) {
          const now = performance.now();
          if (now - session.lastSample > 800) {
            session.holdGesture = 'none';
            session.holdSince = null;
            session.releaseSince = null;
            releaseRailDrag(session);
            releasePalmDrag(session);
            releasePinchZoom(session);
          }
          session.lastSample = now;
          session.mediaTime = video.currentTime;
          try {
            const detected = tracker.detectForVideo(video, now);
            const hand = detected?.landmarks?.[0];
            const gesture = classifyGesture(hand);
            drawHandSkeleton(hand, gesture);

            if (gesture === 'rail') {
              session.holdGesture = 'none';
              session.holdSince = null;
              session.releaseSince = null;
              releasePalmDrag(session);
              releasePinchZoom(session);
              updateRailDrag(session, hand);
            } else if (gesture === 'palm') {
              session.holdGesture = 'none';
              session.holdSince = null;
              session.releaseSince = null;
              releaseRailDrag(session);
              releasePinchZoom(session);
              updatePalmDrag(session, hand);
            } else if (gesture === 'pinch') {
              session.holdGesture = 'none';
              session.holdSince = null;
              session.releaseSince = null;
              releaseRailDrag(session);
              releasePalmDrag(session);
              setBadge('gesture.badge_pinch', 'data');
              updatePinchZoom(session, hand);
            } else {
              releaseRailDrag(session);
              releasePalmDrag(session);
              releasePinchZoom(session);
              if (gesture === 'point' || gesture === 'peace' || gesture === 'fist') {
                session.releaseSince = null;
                if (session.holdGesture !== gesture) {
                  session.holdGesture = gesture;
                  session.holdSince = now;
                }
                const badgeMap = {
                  point: ['gesture.badge_point', 'warn'],
                  peace: ['gesture.badge_peace', 'warn'],
                  fist: ['gesture.badge_fist', 'crit'],
                };
                setBadge(badgeMap[gesture][0], badgeMap[gesture][1]);
                const holdMs = gesture === 'fist' ? 420 : 320;
                if (!session.latched && session.holdSince !== null && now - session.holdSince >= holdMs) {
                  session.latched = true;
                  if (gesture === 'point') {
                    stage.cycleCamera();
                    const next = stage.getStatus();
                    onCycle?.(next);
                    setStatus('gesture.switched', { cam: t(next.cameraKey) });
                  } else if (gesture === 'peace') {
                    stage.prevCamera?.();
                    const next = stage.getStatus();
                    onCycle?.(next);
                    setStatus('gesture.switched', { cam: t(next.cameraKey) });
                  } else if (gesture === 'fist') {
                    stage.resetCamera?.();
                    const next = stage.getStatus();
                    onCycle?.(next);
                    setStatus('gesture.reset');
                  }
                }
              } else {
                session.holdGesture = 'none';
                session.holdSince = null;
                setBadge('gesture.badge_ready', 'data');
                if (session.releaseSince === null) session.releaseSince = now;
                if (now - session.releaseSince >= 420) session.latched = false;
              }
            }
          } catch {
            stop('gesture.interrupted');
            return;
          }
        }
        session.frame = setTimeout(frame, 85); // fui-lint-ignore: R15 bounded camera polling only while user-enabled and tab visible
      }
      frame();
    } catch (error) {
      if (current !== session) return;
      const errKey = error?.name === 'NotAllowedError'
        ? 'gesture.denied'
        : error?.name === 'NotFoundError'
          ? 'gesture.no_camera'
          : 'gesture.failed';
      stop(errKey, null, { notifyStage: true });
    } finally {
      if (startingSession === session) startingSession = null;
    }
  }

  toggle?.addEventListener('change', () => (toggle.checked ? start() : stop('gesture.closed')));
  const onTriggerClick = () => {
    if (current) stop('gesture.closed');
    else {
      clearNotice();
      void start();
    }
  };
  for (const btn of triggerButtons) btn.addEventListener('click', onTriggerClick);
  stopButton?.addEventListener('click', () => {
    clearNotice();
    stop('gesture.closed');
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden && current) stop('gesture.closed'); });
  document.addEventListener('freeze', () => { if (current) stop('gesture.closed'); });
  window.addEventListener('pagehide', () => { if (current) stop('gesture.closed'); });

  const observer = new MutationObserver(refresh);
  if (canvas) observer.observe(canvas, { attributes: true, attributeFilter: ['data-ready', 'data-error'] });
  if (stageMain) observer.observe(stageMain, { attributes: true, attributeFilter: ['data-opening'] });
  refresh();

  return {
    refresh,
    start,
    stop: () => stop('gesture.closed'),
    isActive: () => Boolean(current),
    refreshI18n() {
      if (lastStatusKey) setStatus(lastStatusKey, lastStatusVars);
      if (lastBadgeKey) setBadge(lastBadgeKey, badgeEl?.dataset?.state || 'data');
      if (activeNoticeKey && noticeEl) noticeEl.textContent = activeNoticeVars ? t(activeNoticeKey, activeNoticeVars) : t(activeNoticeKey);
    },
  };
}
