/**
 * Smart real-time microphone ranking, best-device selection, and live failover.
 * Runs in browser FUI / mobile and exposes pure ranking helpers for unit tests.
 */

const VIRTUAL_MIC_RE = /stereo\s*mix|立体声混音|what u hear|vb-?audio|cable\s*(?:output|input|a|b)|voicemeeter|virtual\s*audio|steam\s*streaming|blackhole|soundflower|loopback|obs\s*(?:audio|virtual)|scream|line[\s-]*in|线路输入/i;
const DEDICATED_MIC_RE = /usb.*(?:mic|audio|headset|condenser|dac)|blue\s*(?:yeti|snowball)|shure|rode|elgato|hyperx|razer|sennheiser|audio-?technica|fifine|maono|dji\s*mic|logitech.*(?:pro|g\d|headset|zone)|jabra|poly|plantronics|steelseries|sony.*wh|airpods|耳麦|头戴式|外接麦克风|独立麦克风|桌面麦克风|领夹/i;
const BT_HANDSFREE_RE = /hands-?free|hf\s*audio|\bhfp\b|\bhsp\b|ag\s*audio|蓝牙免提/i;
const ARRAY_MIC_RE = /microphone\s*array|realtek|intel.*smart\s*sound|amd\s*audio|conexant|synaptics|cirrus|macbook|internal|built-?in|麦克风阵列|内置麦克风/i;
const WEBCAM_MIC_RE = /webcam|camera|brio|c920|c922|c930|streamcam|摄像头|display\s*audio/i;
const DEFAULT_PREFIX_RE = /^(?:default|默认(?:设备)?|communications|通信)\s*[-–:]\s*(.+)$/i;

export function extractDefaultSuffix(devices = []) {
  for (const d of devices) {
    if (d?.kind === 'audioinput' && d.deviceId === 'default' && d.label) {
      const m = DEFAULT_PREFIX_RE.exec(String(d.label).trim());
      if (m && m[1]) return m[1].trim().toLowerCase();
    }
  }
  return '';
}

export function scoreMicDevice(device, index = 0, { defaultSuffix = '' } = {}) {
  const label = String(device?.label || '').trim();
  const id = String(device?.deviceId || '');
  if (!id || id === 'default' || id === 'communications') {
    return { score: -10, tier: 'system', virtual: false };
  }

  let score = 0;
  let tier = 'standard';
  let virtual = false;

  if (!label) {
    score = 5;
    tier = 'unknown';
  } else if (VIRTUAL_MIC_RE.test(label)) {
    score = -80;
    tier = 'virtual';
    virtual = true;
  } else if (BT_HANDSFREE_RE.test(label)) {
    score = -18;
    tier = 'bluetooth_hf';
  } else if (DEDICATED_MIC_RE.test(label)) {
    score = 40;
    tier = 'dedicated';
  } else if (ARRAY_MIC_RE.test(label)) {
    score = 24;
    tier = 'array';
  } else if (WEBCAM_MIC_RE.test(label)) {
    score = 10;
    tier = 'webcam';
  } else {
    score = 16;
    tier = 'standard';
  }

  const isSystemDefault = Boolean(defaultSuffix && label && label.toLowerCase() === defaultSuffix);
  if (isSystemDefault && !virtual) {
    score += 8;
  }
  score -= index * 0.05;
  return { score: Math.round(score * 100) / 100, tier, virtual, isSystemDefault };
}

export function rankMicDevices(devices = []) {
  const defaultSuffix = extractDefaultSuffix(devices);
  const seen = new Set();
  const ranked = [];
  let idx = 0;
  for (const d of devices) {
    if (!d || d.kind !== 'audioinput') continue;
    const id = String(d.deviceId || '');
    if (!id || id === 'default' || id === 'communications' || seen.has(id)) continue;
    seen.add(id);
    const meta = scoreMicDevice(d, idx++, { defaultSuffix });
    ranked.push({
      id,
      deviceId: id,
      groupId: d.groupId || '',
      label: String(d.label || '').trim(),
      ...meta,
    });
  }
  ranked.sort((a, b) => b.score - a.score);
  return ranked.map((item, pos) => ({
    ...item,
    recommended: pos === 0 && !item.virtual,
  }));
}

export function buildCandidateConstraints({ preferredId = '', devices = [], baseAudio = {}, skipIds = [] } = {}) {
  const skip = new Set(skipIds || []);
  const ranked = rankMicDevices(devices);
  const candidates = [];
  const addedIds = new Set();

  if (preferredId && !skip.has(preferredId)) {
    const known = ranked.find((d) => d.id === preferredId);
    candidates.push({
      deviceId: preferredId,
      label: known?.label || '',
      tier: known?.tier || 'pinned',
      score: known?.score ?? 100,
      preferred: true,
      constraints: { ...baseAudio, deviceId: { exact: preferredId } },
    });
    addedIds.add(preferredId);
  }

  for (const d of ranked) {
    if (addedIds.has(d.id) || skip.has(d.id)) continue;
    // Skip virtual loopback traps as long as non-virtual hardware exists.
    if (d.virtual && ranked.some((other) => !other.virtual && !skip.has(other.id))) continue;
    candidates.push({
      deviceId: d.id,
      label: d.label,
      tier: d.tier,
      score: d.score,
      preferred: false,
      fallbackFromPreferred: Boolean(preferredId),
      constraints: { ...baseAudio, deviceId: { exact: d.id } },
    });
    addedIds.add(d.id);
  }

  // Always end with browser/system default unconstrained audio as last resort.
  candidates.push({
    deviceId: '',
    label: '',
    tier: 'system',
    score: -50,
    preferred: false,
    fallbackFromPreferred: Boolean(preferredId),
    constraints: { ...baseAudio },
  });
  return candidates;
}

function isPermissionError(err) {
  return Boolean(err && (
    err.name === 'NotAllowedError'
    || err.name === 'PermissionDeniedError'
    || err.name === 'SecurityError'
  ));
}

export async function openBestMicStream({ preferredId = '', baseAudio = {}, skipIds = [], onSelected } = {}) {
  if (!navigator.mediaDevices?.getUserMedia) {
    const err = new Error('Microphone API unavailable');
    err.name = 'NotSupportedError';
    throw err;
  }

  const audioBase = {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    ...baseAudio,
  };
  let devices = [];
  try {
    devices = (await navigator.mediaDevices.enumerateDevices()) || [];
  } catch {
    devices = [];
  }

  const hadBlankLabels = devices.some((d) => d.kind === 'audioinput' && !d.label);
  const candidates = buildCandidateConstraints({ preferredId, devices, baseAudio: audioBase, skipIds });
  let lastErr = null;
  let opened = null;
  let pickedMeta = null;
  const failedIds = [];

  for (const cand of candidates) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: cand.constraints });
      const track = stream.getAudioTracks()[0];
      if (!track || track.readyState !== 'live') {
        stream.getTracks().forEach((t) => t.stop());
        if (cand.deviceId) failedIds.push(cand.deviceId);
        continue;
      }
      const settings = typeof track.getSettings === 'function' ? track.getSettings() : {};
      opened = stream;
      pickedMeta = {
        deviceId: settings.deviceId || cand.deviceId || '',
        label: track.label || cand.label || '',
        tier: cand.tier,
        score: cand.score,
        autoSelected: !preferredId,
        fallbackFromPreferred: Boolean(cand.fallbackFromPreferred),
      };
      break;
    } catch (err) {
      if (isPermissionError(err)) throw err;
      if (cand.deviceId) failedIds.push(cand.deviceId);
      lastErr = err;
    }
  }

  if (!opened) {
    throw lastErr || new Error('没有可用的麦克风输入设备。');
  }

  // After first-time permission grant reveals real hardware labels, upgrade automatically
  // if AUTO mode is active and a strictly better microphone exists than blind default.
  try {
    devices = (await navigator.mediaDevices.enumerateDevices()) || devices;
  } catch { /* ignore */ }

  const skipSet = new Set([...(skipIds || []), ...failedIds]);
  if (!preferredId && hadBlankLabels && devices.length) {
    const ranked = rankMicDevices(devices);
    const cur = ranked.find((d) => d.id === pickedMeta.deviceId) || null;
    const best = ranked.find((d) => !d.virtual && !skipSet.has(d.id)) || null;
    if (best && best.id !== pickedMeta.deviceId && (!cur || cur.virtual || best.score >= cur.score + 8)) {
      try {
        const better = await navigator.mediaDevices.getUserMedia({ audio: { ...audioBase, deviceId: { exact: best.id } } });
        const bTrack = better.getAudioTracks()[0];
        if (bTrack && bTrack.readyState === 'live') {
          opened.getTracks().forEach((t) => t.stop());
          opened = better;
          const bSet = typeof bTrack.getSettings === 'function' ? bTrack.getSettings() : {};
          pickedMeta = {
            deviceId: bSet.deviceId || best.id,
            label: bTrack.label || best.label,
            tier: best.tier,
            score: best.score,
            autoSelected: true,
            fallbackFromPreferred: false,
          };
        } else {
          better.getTracks().forEach((t) => t.stop());
          failedIds.push(best.id);
        }
      } catch {
        failedIds.push(best.id);
      }
    }
  }

  const track = opened.getAudioTracks()[0];
  onSelected?.({ ...pickedMeta, devices, failedIds });
  return { stream: opened, track, meta: pickedMeta, devices, failedIds };
}

export function watchBestMicChange({
  getPreferredId,
  getActiveDeviceId,
  getSkipIds,
  isTrackLive,
  onUpgradeOrFailover,
  onDevicesChanged,
} = {}) {
  const md = navigator.mediaDevices;
  if (!md?.addEventListener) return () => {};
  let timer = 0;

  const evaluate = async () => {
    let devices = [];
    try {
      devices = (await md.enumerateDevices()) || [];
    } catch {
      return;
    }
    const ranked = rankMicDevices(devices);
    onDevicesChanged?.(ranked);

    const activeId = String(getActiveDeviceId?.() || '');
    if (!activeId) return;
    const preferredId = String(getPreferredId?.() || '');
    const skipSet = new Set(getSkipIds?.() || []);
    const isAlias = activeId === 'default' || activeId === 'communications';
    const activeEntry = isAlias ? null : ranked.find((d) => d.id === activeId);
    const best = ranked.find((d) => !d.virtual && !skipSet.has(d.id)) || ranked.find((d) => !skipSet.has(d.id)) || ranked[0] || null;

    if (!isAlias && !activeEntry) {
      onUpgradeOrFailover?.({ reason: 'unplugged', target: best, ranked });
      return;
    }
    if (isAlias && isTrackLive && !isTrackLive()) {
      onUpgradeOrFailover?.({ reason: 'unplugged', target: best, ranked });
      return;
    }
    const pinnedMissing = Boolean(preferredId && ranked.some((d) => d.label) && !ranked.some((d) => d.id === preferredId));
    if ((!preferredId || pinnedMissing) && best && !best.virtual && !skipSet.has(best.id) && best.id !== activeId) {
      if (!activeEntry || activeEntry.virtual || best.score >= activeEntry.score + 12) {
        onUpgradeOrFailover?.({ reason: 'better_device', target: best, current: activeEntry, ranked });
      }
    }
  };

  const onDeviceChange = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { void evaluate(); }, 260);
  };
  md.addEventListener('devicechange', onDeviceChange);
  return () => {
    clearTimeout(timer);
    md.removeEventListener('devicechange', onDeviceChange);
  };
}
