import {
  conversationSpeech, explicitRecitation, expressiveText, isVerseCategory, speechSegments,
} from '/static/speech-delivery.js?v=v5-erhua';
import { createGreetingAudioCache } from './prepared-greeting.js';
import { openBestMicStream, rankMicDevices, watchBestMicChange } from './mic-selector.js';
import { safeUuid } from './safe-web.js';

/**
 * Voice I/O Engine for MOSS 550W Console.
 *
 * - Microphone input (simulated fallback): 16 kHz mono PCM WAV capture with Web Audio AnalyserNode.
 *   (Live duplex capture is orchestrated by duplex-voice.js / agent-voice.moss.js.)
 * - Speech output: OmniVoice TTS playback via POST /api/speak & /api/speak-batch with
 *   Chinese/verse segmentation, explicit narration & inline [pause] cues, leading zero-silence
 *   skip, inter-segment quiet-edge trimming, early lead/first-paragraph pre-synthesis,
 *   draft/thinking pre-import, cached short music cues with background full-title warming,
 *   automatic media ducking, audio-synced timed captions, live AnalyserNode FFT feed,
 *   and instant interrupt.
 */

function encodePcmWav(chunks, sampleRate) {
  const total = chunks.reduce((sum, c) => sum + c.length, 0);
  const samples = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) {
    samples.set(c, offset);
    offset += c.length;
  }
  const targetRate = 16000;
  const count = Math.floor((total * targetRate) / sampleRate);
  const buffer = new ArrayBuffer(44 + count * 2);
  const view = new DataView(buffer);
  const writeStr = (pos, s) => { for (let i = 0; i < s.length; i++) view.setUint8(pos + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + count * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, targetRate, true);
  view.setUint32(28, targetRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, count * 2, true);

  for (let i = 0; i < count; i++) {
    const a = Math.floor((i * sampleRate) / targetRate);
    const b = Math.min(total, Math.max(a + 1, Math.floor(((i + 1) * sampleRate) / targetRate)));
    let acc = 0;
    for (let j = a; j < b; j++) acc += samples[j];
    const v = Math.max(-1, Math.min(1, acc / (b - a)));
    view.setInt16(44 + i * 2, Math.round(v * (v < 0 ? 32768 : 32767)), true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

function splitInlineCues(text) {
  const cues = [];
  const regex = /\[pause(?:\s+(\d+(?:\.\d+)?)(ms|s))?\]/gi;
  let last = 0;
  for (const m of text.matchAll(regex)) {
    const before = text.slice(last, m.index).trim();
    if (before) cues.push({ text: before });
    const num = m[1] ? Number(m[1]) : 350;
    const ms = m[2]?.toLowerCase() === 's' ? num * 1000 : num;
    cues.push({ pause_ms: Math.min(4000, Math.max(50, ms)) });
    last = m.index + m[0].length;
  }
  const tail = text.slice(last).trim();
  if (tail) cues.push({ text: tail });
  return cues;
}

function playbackPlan(text, category, narration) {
  const cues = narration || (/\[pause(?:\s+\d+(?:\.\d+)?(?:ms|s))?\]/i.test(text) ? splitInlineCues(text) : null);
  if (!cues) return { sentences: speechSegments(text, category), finalPause: 0 };
  const sentences = [];
  let pause = 0;
  for (const cue of cues) {
    if (cue.pause_ms) {
      pause += cue.pause_ms;
      continue;
    }
    const spoken = speechSegments(cue.text || '', category);
    if (!spoken.length) continue;
    spoken[0].pauseBefore = pause;
    pause = 0;
    sentences.push(...spoken);
  }
  return { sentences, finalPause: pause };
}

function waitForPause(ms, signal) {
  return new Promise((resolve) => {
    let timer;
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    }
    if (signal.aborted) finish();
    else {
      signal.addEventListener('abort', finish, { once: true });
      timer = setTimeout(finish, ms);
    }
  });
}

function leadingReplySilence(audio) {
  const rate = audio.sampleRate;
  let silentFrames = Math.min(audio.length, Math.floor(rate * 0.12));
  for (let channel = 0; channel < audio.numberOfChannels; channel++) {
    const pcm = audio.getChannelData(channel);
    for (let frame = 0; frame < silentFrames; frame++) {
      if (pcm[frame] === 0) continue;
      silentFrames = frame;
      break;
    }
  }
  // Retain 12 ms exact silence for the 4 ms gain attack ramp.
  const skipped = silentFrames - Math.ceil(rate * 0.012);
  return skipped >= rate * 0.02 ? skipped / rate : 0;
}

function quietEdges(audio) {
  const rate = audio.sampleRate;
  const win = Math.max(1, Math.round(rate * 0.01));
  const channels = Array.from({ length: audio.numberOfChannels }, (_, i) => audio.getChannelData(i));
  const blocks = Math.ceil(audio.length / win);
  const blockRms = new Float32Array(blocks);
  let peakRms = 0;
  for (let b = 0; b < blocks; b++) {
    const start = b * win;
    const end = Math.min(start + win, audio.length);
    const len = end - start;
    let maxRms = 0;
    for (const pcm of channels) {
      let power = 0;
      for (let i = start; i < end; i++) power += pcm[i] * pcm[i];
      const rms = Math.sqrt(power / len);
      if (rms > maxRms) maxRms = rms;
    }
    blockRms[b] = maxRms;
    if (maxRms > peakRms) peakRms = maxRms;
  }
  const rmsGate = Math.max(0.0012, peakRms * 0.03);
  function quiet(block) {
    return blockRms[block] < rmsGate;
  }
  let first = 0;
  let last = blocks - 1;
  while (first < blocks && quiet(first)) first++;
  while (last > first && quiet(last)) last--;
  if (first >= last) return { head: 0, tail: 0 };
  // Keep 35ms natural attack head and 95ms room decay tail so consecutive segments flow seamlessly.
  const head = Math.min(0.24, (first * win) / rate - 0.035);
  const tail = Math.min(0.45, (audio.length - (last + 1) * win) / rate - 0.095);
  return { head: head >= 0.02 ? head : 0, tail: tail >= 0.02 ? tail : 0 };
}

function timedCaptions(text, category, audio, offset) {
  const boundaries = isVerseCategory(category)
    ? /(?:[，,。！？!?；;]+|\.(?=\s|$)|\r?\n+)[”’」』）)]*\s*/gu
    : /(?:[。！？!?；;]+|\.(?=\s|$)|\r?\n+)[”’」』）)]*\s*/gu;
  const parts = [];
  let cursor = 0;
  for (const end of text.matchAll(boundaries)) {
    const next = end.index + end[0].length;
    if (/[\p{L}\p{N}]/u.test(text.slice(cursor, next))) {
      parts.push(text.slice(cursor, next));
      cursor = next;
    }
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  if (parts.length < 2) return [{ at: 0, text }];

  const rate = audio.sampleRate;
  const pcm = audio.getChannelData(0).subarray(Math.round(offset * rate));
  const frame = Math.max(1, Math.round(rate * 0.02));
  const stride = Math.max(1, Math.floor(rate / 6000));
  const levels = [];
  for (let from = 0; from < pcm.length; from += frame) {
    const to = Math.min(from + frame, pcm.length);
    let power = 0;
    let count = 0;
    for (let i = from; i < to; i += stride) {
      power += pcm[i] * pcm[i];
      count++;
    }
    levels.push(Math.sqrt(power / count));
  }
  const ordered = [...levels].sort((a, b) => a - b);
  const gate = Math.max(0.0002, Math.min(0.014, ordered[Math.floor(ordered.length * 0.9)] * 0.10));
  const first = levels.findIndex((value) => value > gate);
  const last = levels.findLastIndex((value) => value > gate);
  if (first < 0 || first === last) return [{ at: 0, text }];
  const frameSeconds = frame / rate;
  const pauses = [];
  let voiced = first;
  for (let i = first + 1; i <= last; i++) {
    if (levels[i] <= gate) continue;
    const duration = (i - voiced - 1) * frameSeconds;
    if (duration >= 0.08) pauses.push({ mid: ((voiced + 1 + i) * frameSeconds) / 2, at: i * frameSeconds - 0.04 });
    voiced = i;
  }
  const weights = parts.map((part) => Math.max(1, [...part.replace(/[^\p{L}\p{N}]/gu, '')].length));
  const secondsPerUnit = ((last + 1 - first) * frameSeconds) / weights.reduce((sum, n) => sum + n, 0);
  let target = first * frameSeconds;
  let startAt = 0;
  let group = 0;
  const cues = [];
  for (let i = 0; i < parts.length - 1; i++) {
    target += weights[i] * secondsPerUnit;
    const radius = Math.min(1.5, 0.5 * Math.min(weights[i], weights[i + 1]) * secondsPerUnit);
    let distance = radius;
    let nearest;
    for (const pause of pauses) {
      const delta = Math.abs(pause.mid - target);
      if (pause.at > startAt && delta <= distance) {
        nearest = pause;
        distance = delta;
      }
    }
    if (!nearest) continue;
    cues.push({ at: startAt, text: parts.slice(group, i + 1).join('').trim() });
    startAt = nearest.at;
    group = i + 1;
  }
  cues.push({ at: startAt, text: parts.slice(group).join('').trim() });
  return cues;
}

function prepareForReading(text) {
  const input = (text || '').trimStart();
  return explicitRecitation(input) ||
    /^(?:(?:请(?:你|您)?|帮我|麻烦(?:你|您)?)[\s，,]*)*(?:(?:朗读|朗诵|念|读出|读)(?:一下)?(?:(?:以下|下面)的?(?:这段)?|这段)(?:文字|内容|正文|话)?|把(?:(?:以下|下面)的?(?:这段)?|这段)(?:文字|内容|正文|话)?(?:朗读|朗诵|念|读)给我听)\s*[：:]\s*\S/u.test(input);
}

function prepareForDeviceQuery(text) {
  const parts = (text || '').normalize('NFKC').split(/[,、;。!?\n]+/u).map((part) => part.trim()).filter(Boolean);
  const clock = /^(?:(?:现在|这会儿|此刻)(?:几点(?:钟)?(?:了|啦)?|的?时间)|(?:今天|今日)(?:是)?(?:几号|几月几[日号]|星期几|周几))[啊呀呢吗么]?$/u;
  const battery = /^(?:(?:我的?|这台)?(?:笔记本(?:电脑)?|电脑)的?)?(?:当前|现在|目前|这会儿)?(?:(?:(?:电池)?电量|电池状态)(?:还有多少|是多少|多少|多大)?|(?:还有|还剩|剩余|剩|有)?多少电(?:量)?|(?:有没有|是否)在充电)[啊呀呢吗么]?$/u;
  const volume = /^(?:(?:当前|现在|目前)的?)?(?:(?:本机|电脑|系统)(?:上|的)?|总)?(?:音量(?:是|有|为)?(?:多大|多少)|声音(?:有)?多大)[啊呀呢吗么]?$/u;
  return parts.length > 0 && parts.every((part) => {
    const query = part.replace(/^(?:(?:请你?|帮我|告诉我|顺便|查一下|看看|查看)\s*)+/u, '');
    return clock.test(query) || battery.test(query) || volume.test(query);
  });
}

const speechRoomKey = 'moss.speech.space.percent';
function validRoomAmount(amount) {
  return Number.isInteger(amount) && amount >= 0 && amount <= 150 && amount % 5 === 0;
}

export function createVoiceEngine({
  onMicState,
  onMicFrame,
  onSpeechState,
  onSpeechFrame,
  onCaption,
  onTranscribed,
  onStatus,
  getMode,
  canStartOptionalSpeech,
  getMediaElements,
  composerEl = document.getElementById('comms-input'),
}) {
  const greetingCache = createGreetingAudioCache();
  let ttsEnabled = true;
  let voiceResident = true;
  let roomAmount = 100;
  try {
    ttsEnabled = (localStorage.getItem('moss-fui.tts') ?? localStorage.getItem('moss.voice.enabled')) !== 'off';
    voiceResident = localStorage.getItem('moss-fui.voice-resident') !== 'off';
    const saved = localStorage.getItem(speechRoomKey);
    if (saved !== null && /^\d{1,3}$/.test(saved) && validRoomAmount(Number(saved))) roomAmount = Number(saved);
  } catch { /* ignore */ }
  let micDeviceId = '';
  try { micDeviceId = localStorage.getItem('moss-fui.mic') || ''; } catch { /* ignore */ }
  let activeMicInfo = null;
  let wakeEnabled = false;
  let bargeInEnabled = false;

  // Simulated mic capture state
  let micStream = null;
  let micCtx = null;
  let micAnalyser = null;
  let micProcessor = null;
  let micChunks = [];
  let micActive = false;
  let micOpening = false;
  let micOpenAborted = false;
  let micRaf = 0;
  let micSpokeAt = 0;
  let micStartedAt = 0;
  let micHasVoice = false;
  let simFailedMicIds = new Set();

  // TTS output & pre-preparation state
  let ttsCtx = null;
  let ttsAnalyser = null;
  let ttsResuming = null;
  let openingOutput = null;
  let ttsSource = null;
  let ttsAbort = null;
  let ttsId = null;
  let speechCounter = 0;
  let ttsActive = false;
  let ttsRaf = 0;
  let lastSpeechState = null;
  let lastImportRequest = -Infinity;
  let speechPrepare = false;
  let speechSessionWarm = false;
  let currentModelId = null;
  let sessionLease = null;
  let sessionWarmStatus = voiceResident ? 'idle' : 'off';
  let sessionWarmRetryAt = 0;
  let sessionWarmRenewAt = 0;
  let draftChat = null;
  let draftBusy = false;
  let draftPrepared = false;
  let draftTimer = 0;
  let composingDraft;
  let owner = null;
  let prepared = null;
  let held = null;
  const observed = new Set();

  function setActiveMicInfo(track = null, meta = null) {
    activeMicInfo = meta
      ? {
          deviceId: meta.deviceId || '',
          label: track?.label || meta.label || '',
          tier: meta.tier || '',
          autoSelected: Boolean(meta.autoSelected),
          fallbackFromPreferred: Boolean(meta.fallbackFromPreferred),
        }
      : track
        ? { deviceId: '', label: track.label || '', tier: '', autoSelected: !micDeviceId, fallbackFromPreferred: false }
        : null;
    window.dispatchEvent(new CustomEvent('moss-mic-changed', { detail: activeMicInfo }));
  }

  async function listMicDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return rankMicDevices(devices);
    } catch {
      return [];
    }
  }

  function stopMicCapture({ transcribe = false } = {}) {
    cancelAnimationFrame(micRaf);
    micRaf = 0;
    const chunks = micChunks;
    const rate = micCtx?.sampleRate || 48000;
    micChunks = [];
    if (micOpening) micOpenAborted = true;
    micActive = false;
    micHasVoice = false;
    setActiveMicInfo(null, null);

    if (micProcessor) {
      try { micProcessor.port?.postMessage('flush'); } catch { /* ignore */ }
      if (micProcessor.port) micProcessor.port.onmessage = null;
      try { micProcessor.disconnect(); } catch { /* ignore */ }
      micProcessor = null;
    }
    if (micStream) {
      micStream.getTracks().forEach((tr) => tr.stop());
      micStream = null;
    }
    if (micCtx) {
      micCtx.close().catch(() => {});
      micCtx = null;
      micAnalyser = null;
    }
    onMicFrame?.(null);
    onMicState?.({ active: false });

    if (transcribe && chunks.length) {
      submitWavForTranscription(encodePcmWav(chunks, rate));
    }
  }

  async function submitWavForTranscription(blob) {
    if (getMode?.() === 'simulated') {
      onStatus?.('voice.ready');
      onTranscribed?.('现在几点、电脑还有多少电、音量多大？');
      return;
    }
    onStatus?.('voice.transcribing');
    try {
      const resp = await fetch('/api/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav' },
        body: blob,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      let text = (data.text || '').trim();
      onStatus?.('voice.ready');
      if (!text) return;
      if (wakeEnabled) {
        const wakeMatch = /^(?:你好[,，\s]*)?(?:莫斯|moss)[,，\s]*(.*)$/i.exec(text);
        if (!wakeMatch) return;
        text = wakeMatch[1].trim();
        if (!text) return;
      }
      onTranscribed?.(text);
    } catch (err) {
      onStatus?.('voice.ready', err.message);
    }
  }

  async function startMic() {
    if (getMode?.() !== 'simulated') {
      onStatus?.('voice.unavailable');
      return;
    }
    if (micActive) {
      stopMicCapture({ transcribe: micHasVoice });
      return;
    }
    if (micOpening) return;
    if (ttsActive && !bargeInEnabled) interruptSpeech();
    if (!navigator.mediaDevices?.getUserMedia) {
      onStatus?.('voice.no_mic');
      return;
    }
    micOpening = true;
    micOpenAborted = false;
    try {
      const { stream, track, meta, failedIds = [] } = await openBestMicStream({
        preferredId: micDeviceId,
        skipIds: [...simFailedMicIds],
        baseAudio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        onSelected: (info) => setActiveMicInfo(null, info),
      });
      for (const badId of failedIds) simFailedMicIds.add(badId);
      if (meta?.deviceId) simFailedMicIds.delete(meta.deviceId);
      if (micOpenAborted) {
        stream.getTracks().forEach((tr) => tr.stop());
        setActiveMicInfo(null, null);
        return;
      }
      micStream = stream;
      setActiveMicInfo(track, meta);
      if (track) {
        track.addEventListener('ended', async () => {
          if (!micActive) return;
          const prevId = activeMicInfo?.deviceId || '';
          const avail = await listMicDevices();
          if (!micActive) return;
          const hasNext = avail.some((d) => !d.virtual && d.id !== prevId)
            || avail.some((d) => d.id !== prevId);
          if (hasNext) {
            stopMicCapture({ transcribe: false });
            void startMic();
          } else {
            stopMicCapture({ transcribe: false });
            onStatus?.('voice.no_mic');
          }
        }, { once: true });
      }
      micCtx = new AudioContext();
      if (micOpenAborted) {
        stopMicCapture({ transcribe: false });
        return;
      }
      const source = micCtx.createMediaStreamSource(micStream);
      micAnalyser = micCtx.createAnalyser();
      micAnalyser.fftSize = 512;
      micAnalyser.smoothingTimeConstant = 0.75;
      source.connect(micAnalyser);

      micChunks = [];
      micStartedAt = performance.now();
      micSpokeAt = micStartedAt;
      micHasVoice = false;
      let micVoiceStreak = 0;

      const handlePcmFrame = (input) => {
        if (!micActive || !input?.length) return;
        micChunks.push(new Float32Array(input));
        let sum = 0;
        for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
        const rms = Math.sqrt(sum / input.length);
        const now = performance.now();
        if (rms > 0.022) {
          micHasVoice = true;
          micVoiceStreak += 1;
          micSpokeAt = now;
          if (ttsActive && micVoiceStreak >= 3) interruptSpeech();
        } else {
          micVoiceStreak = 0;
          if (micHasVoice && now - micSpokeAt > 1250 && now - micStartedAt > 900) {
            // Keep full-duplex stream continuously open across turns; flush only the finished utterance.
            const batch = micChunks;
            micChunks = [];
            micHasVoice = false;
            micStartedAt = now;
            if (batch.length) submitWavForTranscription(encodePcmWav(batch, micCtx?.sampleRate || 48000));
          } else if (!micHasVoice && micChunks.length > 24) {
            micChunks.splice(0, micChunks.length - 12);
          }
        }
      };

      if (micCtx.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
        await micCtx.audioWorklet.addModule('/static/voice-capture.js');
        if (micOpenAborted || !micCtx) {
          stopMicCapture({ transcribe: false });
          return;
        }
        const worklet = new AudioWorkletNode(micCtx, 'moss-voice-capture');
        const silentGain = micCtx.createGain();
        silentGain.gain.value = 0;
        worklet.port.onmessage = (ev) => {
          if (ev.data instanceof Float32Array) handlePcmFrame(ev.data);
        };
        source.connect(worklet);
        worklet.connect(silentGain);
        silentGain.connect(micCtx.destination);
        micProcessor = worklet;
      } else {
        const proc = micCtx.createScriptProcessor(4096, 1, 1);
        proc.onaudioprocess = (ev) => handlePcmFrame(ev.inputBuffer.getChannelData(0));
        source.connect(proc);
        proc.connect(micCtx.destination);
        micProcessor = proc;
      }
      micActive = true;
      onMicState?.({ active: true });
      onStatus?.('voice.listening');

      const freq = new Uint8Array(micAnalyser.frequencyBinCount);
      const time = new Float32Array(micAnalyser.fftSize);
      const pump = () => {
        if (!micActive || !micAnalyser) return;
        micAnalyser.getByteFrequencyData(freq);
        micAnalyser.getFloatTimeDomainData(time);
        onMicFrame?.({ freq, time, sampleRate: micCtx.sampleRate });
        micRaf = requestAnimationFrame(pump); // fui-lint-ignore: R15 active only while microphone stream is live
      };
      pump();
    } catch (err) {
      stopMicCapture({ transcribe: false });
      onStatus?.('voice.no_mic', err.message);
    } finally {
      micOpening = false;
      micOpenAborted = false;
    }
  }

  function emitSpeech(event, detail) {
    window.dispatchEvent(new CustomEvent('moss-speech-' + event, { detail }));
  }

  function setSessionWarmStatus(next) {
    if (sessionWarmStatus === next) return;
    sessionWarmStatus = next;
    window.dispatchEvent(new CustomEvent('moss-speech-warm-state', {
      detail: { status: next, enabled: voiceResident },
    }));
  }

  function releaseSessionWarm(nextStatus = voiceResident && ttsEnabled ? 'idle' : 'off') {
    const lease = sessionLease;
    sessionLease = null;
    sessionWarmRenewAt = 0;
    if (lease && document.body?.dataset?.authRole !== 'C') {
      void fetch('/api/prepare-speech', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: lease.key, release: true }),
        keepalive: true,
      }).catch(() => {});
    }
    setSessionWarmStatus(nextStatus);
  }

  function syncSessionWarm({ force = false } = {}) {
    if (document.body?.dataset?.authRole === 'C') {
      releaseSessionWarm('off');
      return false;
    }
    if (!voiceResident || !ttsEnabled) {
      releaseSessionWarm('off');
      return false;
    }
    if (getMode?.() !== 'live' || !speechSessionWarm) {
      releaseSessionWarm('idle');
      return false;
    }
    if (currentModelId === 'local') {
      releaseSessionWarm('local');
      return false;
    }
    if (document.hidden) {
      releaseSessionWarm('idle');
      return false;
    }
    const now = performance.now();
    if (sessionLease?.status === 'warming') return true;
    if (sessionLease?.status === 'ready') {
      if (force || now >= sessionWarmRenewAt) {
        sessionWarmRenewAt = now + 240000;
        const active = sessionLease;
        void fetch('/api/prepare-speech', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session: active.key }),
        }).then((resp) => {
          if (sessionLease !== active) return;
          if (!resp.ok || resp.headers.get('X-MOSS-Session-Prepared') !== '1') {
            sessionLease = null;
            sessionWarmRetryAt = performance.now() + 30000;
            setSessionWarmStatus('refused');
          }
        }).catch(() => {});
      }
      return true;
    }
    if (!force && now < sessionWarmRetryAt) return false;
    const lease = { key: safeUuid().replaceAll('-', ''), status: 'warming' };
    sessionLease = lease;
    setSessionWarmStatus('warming');
    void fetch('/api/prepare-speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: lease.key }),
    }).then((resp) => {
      const preparedOk = resp.ok && resp.headers.get('X-MOSS-Session-Prepared') === '1';
      if (sessionLease !== lease) {
        if (preparedOk) {
          void fetch('/api/prepare-speech', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session: lease.key, release: true }),
            keepalive: true,
          }).catch(() => {});
        }
        return;
      }
      if (preparedOk) {
        lease.status = 'ready';
        sessionWarmRenewAt = performance.now() + 240000;
        setSessionWarmStatus('ready');
      } else {
        sessionLease = null;
        sessionWarmRetryAt = performance.now() + 30000;
        setSessionWarmStatus('refused');
      }
    }).catch(() => {
      if (sessionLease === lease) {
        sessionLease = null;
        sessionWarmRetryAt = performance.now() + 30000;
        setSessionWarmStatus('refused');
      }
    });
    return true;
  }

  function discardPrepared() {
    prepared?.aborter.abort();
    prepared = null;
  }

  function interruptSpeech() {
    discardPrepared();
    const id = ttsId;
    ttsId = null;
    if (ttsAbort) {
      ttsAbort.abort();
      ttsAbort = null;
    }
    held = null;
    if (ttsSource) {
      try { ttsSource.stop(); } catch { /* ignore */ }
      try { ttsSource.disconnect(); } catch { /* ignore */ }
      ttsSource = null;
    }
    cancelAnimationFrame(ttsRaf);
    ttsRaf = 0;
    const wasActive = ttsActive;
    ttsActive = false;
    onSpeechFrame?.(null);
    onSpeechState?.({ active: false });
    onCaption?.('');
    if (id) emitSpeech('stop', { id });
    if (wasActive) onStatus?.('voice.interrupted');
  }

  function prepareImports() {
    if (document.body?.dataset?.authRole === 'C' || !ttsEnabled || getMode?.() !== 'live' || document.hidden || !document.hasFocus()) return false;
    const now = performance.now();
    if (now - lastImportRequest < 8000) return true;
    lastImportRequest = now;
    void fetch('/api/prepare-speech', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .catch(() => { if (lastImportRequest === now) lastImportRequest = -Infinity; });
    return true;
  }

  function canPrepareDraft() {
    return speechPrepare && ttsEnabled && !ttsActive && !draftBusy && draftChat &&
      getMode?.() === 'live' && !document.hidden && document.hasFocus() &&
      document.activeElement === composerEl;
  }

  function clearDraftPreparation() {
    clearTimeout(draftTimer);
    composingDraft = undefined;
  }

  function scheduleDraftPreparation(event) {
    clearTimeout(draftTimer);
    const text = composerEl?.value || '';
    if (!text.trim()) draftPrepared = false;
    if (event.isComposing) {
      if (event.isTrusted) composingDraft = text;
      return;
    }
    const committed = event.type === 'compositionend' && composingDraft === text;
    composingDraft = undefined;
    const chat = draftChat;
    if ((!event.isTrusted && !committed) || draftPrepared || !canPrepareDraft() ||
        !(prepareForReading(text) || prepareForDeviceQuery(text))) return;
    draftTimer = setTimeout(() => {
      if (chat !== draftChat || text !== composerEl?.value || !canPrepareDraft()) return;
      draftPrepared = true;
      prepareImports();
    }, 550);
  }

  composerEl?.addEventListener('input', scheduleDraftPreparation);
  composerEl?.addEventListener('compositionend', scheduleDraftPreparation);
  composerEl?.addEventListener('blur', clearDraftPreparation);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearDraftPreparation();
      if (prepared?.localReading) discardPrepared();
      releaseSessionWarm('idle');
      return;
    }
    syncSessionWarm();
  });
  window.addEventListener('focus', () => syncSessionWarm());
  window.addEventListener('pagehide', () => releaseSessionWarm('idle'));

  function speechContext() {
    if (!ttsCtx || ttsCtx.state === 'closed') {
      ttsCtx = new AudioContext();
      ttsAnalyser = ttsCtx.createAnalyser();
      ttsAnalyser.fftSize = 512;
      ttsAnalyser.smoothingTimeConstant = 0.75;
      ttsAnalyser.connect(ttsCtx.destination);
    }
    return ttsCtx;
  }

  function unlock() {
    const ctx = speechContext();
    if (ctx.state === 'running') return;
    const resume = ctx.resume().catch(() => {});
    ttsResuming = resume;
    void resume.then(() => { if (ttsResuming === resume) ttsResuming = null; });
  }

  function prepareOutput({ deferred = false } = {}) {
    if (!ttsEnabled || getMode?.() !== 'live') return;
    if (deferred && !ttsCtx) {
      openingOutput ??= new Promise((resolve) => setTimeout(() => {
        prepareOutput();
        openingOutput = null;
        resolve();
      }, 0));
      return;
    }
    try { unlock(); } catch { /* No output device available. */ }
  }

  async function openSpeechOutput(signal) {
    if (openingOutput) await openingOutput;
    if (signal.aborted) return;
    const ctx = speechContext();
    if (ctx.state === 'running') return;
    await new Promise((resolve, reject) => {
      function finish(error) {
        clearTimeout(timer);
        signal.removeEventListener('abort', abort);
        if (error) reject(error);
        else resolve();
      }
      function abort() { finish(signal.reason); }
      const timer = setTimeout(() => finish(new Error('Audio output is unavailable')), 1800);
      signal.addEventListener('abort', abort, { once: true });
      (ttsResuming || ctx.resume()).then(() => finish(), finish);
    });
    if (ttsCtx.state !== 'running') throw new Error('Audio output is unavailable');
  }

  function voiceBody(fields, space) {
    return JSON.stringify(space === 100 ? fields : { ...fields, room_amount: space });
  }

  function requestVoice(text, category, preferCached, signal, space) {
    const body = preferCached === undefined ? { text, category } : { text, category, prefer_cached: preferCached };
    return fetch('/api/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: voiceBody(body, space),
      signal,
    });
  }

  async function fetchWavResponse(text, category, space, signal, preferCached = undefined) {
    const resp = await requestVoice(text, category, preferCached, signal, space);
    if (!resp.ok || resp.status === 204 ||
        resp.headers.get('X-MOSS-Voice') !== 'OmniVoice' ||
        !resp.headers.get('Content-Type')?.startsWith('audio/wav')) {
      await resp.body?.cancel();
      throw new Error('OmniVoice did not return WAV audio');
    }
    return resp;
  }

  async function prepareGreeting(text, { space = roomAmount, signal } = {}) {
    const { sentences } = playbackPlan(text, 'reply');
    if (!sentences.length || signal?.aborted || getMode?.() !== 'live') return 'deferred';
    try {
      const saved = await greetingCache.read(text, space);
      if (saved?.length === sentences.length) return 'ready';
      const clips = [];
      for (const { spoken } of sentences) {
        if (signal?.aborted) return 'deferred';
        const resp = await fetchWavResponse(spoken, 'reply', space, signal);
        const clip = await resp.blob();
        const header = await clip.slice(0, 12).text();
        if (clip.size <= 44 || !header.startsWith('RIFF') || header.slice(8) !== 'WAVE') {
          throw new Error('The greeting clip is not WAV audio');
        }
        clips.push(clip);
      }
      if (signal?.aborted) return 'deferred';
      await greetingCache.write(text, space, clips);
      return 'ready';
    } catch {
      return 'deferred';
    }
  }

  function activeMediaNodes() {
    const fromCallback = getMediaElements?.() || [];
    const fromDom = [...document.querySelectorAll('#hud-layer audio, #hud-layer video.moss-fui-video, .media-audio')];
    return [...new Set([...fromCallback, ...fromDom])].filter((el) => el?.isConnected);
  }

  function userAction(action, payload = {}, state = null, { deferOutput = false, keepSpeech = false } = {}) {
    clearDraftPreparation();
    draftPrepared = false;
    if (['cancel', 'new-chat', 'switch-chat'].includes(action)) {
      if (!keepSpeech) {
        owner = null;
        interruptSpeech();
      }
      return;
    }
    if (action !== 'message' && action !== 'approve') return;
    if (keepSpeech) return;
    interruptSpeech();
    prepareOutput({ deferred: deferOutput });
    if (state) {
      owner = {
        chat: state.chat_id,
        id: payload.turn_id || null,
        text: payload.text,
        previous: new Set((state.turns || []).map((t) => t.id)),
        importsRequested: false,
        prepared: false,
      };
      if (action === 'message' && ttsEnabled && state.speech_prepare === true && prepareForReading(payload.text)) {
        owner.importsRequested = true;
        prepareImports();
      }
    }
  }

  function prepareLeadingSpeech(turn) {
    if (!turn.partial_reply) {
      discardPrepared();
      return;
    }
    if (prepared?.id === turn.id || !ttsEnabled || getMode?.() !== 'live' ||
        turn.narration || explicitRecitation(turn.user)) return;
    if (!ttsCtx && !openingOutput) return;
    const reading = turn.source === 'context' ? turn.local_overview_speech_lead : null;
    if (reading && (document.hidden || !document.hasFocus())) return;
    if (!reading && turn.source !== 'holo') return;
    const { category, text, expression } = conversationSpeech(reading ? {
      ...turn, status: 'replied', local_overview: [reading],
    } : { ...turn, reply: turn.partial_reply }, []);
    if (category !== 'reply' && category !== 'warm') return;
    if (reading && text === reading.reply) return;
    const segments = speechSegments(reading ? text : turn.partial_reply, category);
    const closedParagraph = Boolean(reading) || /\r?\n[ \t]*\r?\n[ \t]*$/u.test(turn.partial_reply);
    let leading = segments[0];
    let sentenceLead = false;
    if (!closedParagraph && segments.length < 2 && !reading) {
      // Single-paragraph early sentence pre-synthesis: start OmniVoice as soon as sentence 1 closes (>= 14 chars)
      // while the LLM is still streaming sentence 2, cutting spoken first-audio wait by ~0.8-1.5s.
      const raw = (turn.partial_reply || '').trim();
      if (raw.length >= 14 && !raw.includes('\n') && !/^\s*\d+[.、]/u.test(raw)) {
        const m = raw.match(/^([^。！？!?\n]{12,58}[。！？!?]["”’」』）】》]*)/u);
        if (m) {
          const firstSeg = speechSegments(m[1], category)[0];
          if (firstSeg && firstSeg.spoken.length >= 12) {
            leading = firstSeg;
            sentenceLead = true;
          }
        }
      }
    }
    if (!leading || (!closedParagraph && segments.length < 2 && !sentenceLead) ||
        !/[。！？!?；;.…]["”’」』）】》]*\s*$/u.test(leading.spoken)) return;
    const first = expressiveText(leading.spoken, expression);
    const aborter = new AbortController();
    const space = roomAmount;
    const response = requestVoice(first, category, false, aborter.signal, space).then(async (value) => {
      if (value.status === 200 && value.headers.get('Content-Type')?.startsWith('audio/wav')) return value;
      await value.body?.cancel();
      return null;
    }).catch(() => null);
    if (owner && owner.id === turn.id) owner.prepared = true;
    prepared = { id: turn.id, category, first, aborter, response, roomAmount: space, localReading: Boolean(reading), sentenceLead };
  }

  async function speakText(text, {
    category = 'reply',
    turnId = null,
    optional = false,
    signal,
    space = roomAmount,
    useGreetingCache = false,
    expression = undefined,
    narration = undefined,
    allowEdgeTrim = true,
    replaying = false,
  } = {}) {
    if (category === 'greeting') { category = 'reply'; optional = true; }
    if (!ttsEnabled) {
      if (optional) onStatus?.('voice.disabled');
      return 'disabled';
    }
    if (!text?.trim() || signal?.aborted) return 'stopped';
    if (getMode?.() !== 'live') {
      onStatus?.('voice.unavailable');
      return 'unavailable';
    }
    if (optional && (ttsActive || micActive || canStartOptionalSpeech?.() === false)) {
      onStatus?.('voice.busy');
      return 'busy';
    }

    const { sentences: plannedSentences, finalPause } = playbackPlan(text, category, narration);
    if (!plannedSentences.length) return 'unavailable';
    const sentences = [...plannedSentences];
    let firstSpoken = expressiveText(sentences[0].spoken, expression);
    const canReusePrepared = !narration && !replaying && !useGreetingCache && prepared?.id && prepared.id === turnId &&
      prepared.category === category && prepared.roomAmount === space;
    let lead = canReusePrepared && prepared.first === firstSpoken ? prepared : null;
    if (!lead && canReusePrepared && prepared.sentenceLead && sentences.length === 1 && !expression &&
        firstSpoken.startsWith(prepared.first) && firstSpoken.length > prepared.first.length) {
      const tail = firstSpoken.slice(prepared.first.length).trim();
      if (tail.length >= 2) {
        sentences.splice(0, 1,
          { ...sentences[0], spoken: prepared.first, caption: prepared.first },
          { spoken: tail, caption: tail, pauseBefore: 0 },
        );
        firstSpoken = prepared.first;
        lead = prepared;
      }
    }
    const trim = allowEdgeTrim && ['reply', 'warm'].includes(category) && !expression &&
      sentences.length > 1 && firstSpoken === sentences[0].spoken;
    const trimLead = allowEdgeTrim && ['reply', 'warm'].includes(category) && !expression && !narration && !replaying && !useGreetingCache;
    if (lead) prepared = null;

    interruptSpeech();
    const id = turnId || `fui-voice-${++speechCounter}`;
    const controller = lead?.aborter ?? new AbortController();
    const cancel = () => {
      if (ttsAbort === controller) interruptSpeech();
    };
    ttsId = id;
    ttsAbort = controller;
    ttsActive = true;
    signal?.addEventListener('abort', cancel, { once: true });
    emitSpeech('pending', { id });
    onStatus?.('voice.preparing');

    const ducked = new Map();
    function duckMusic(mediaEl) {
      if (!mediaEl || ducked.has(mediaEl)) return;
      const original = mediaEl.volume;
      const level = Math.min(original, 0.15);
      let adjusted = false;
      const observeVolume = () => { if (mediaEl.volume !== level) adjusted = true; };
      const restore = () => {
        if (ducked.get(mediaEl) !== restore) return;
        ducked.delete(mediaEl);
        mediaEl.removeEventListener('volumechange', observeVolume);
        if (!adjusted && mediaEl.volume === level) mediaEl.volume = original;
      };
      mediaEl.addEventListener('volumechange', observeVolume);
      ducked.set(mediaEl, restore);
      mediaEl.volume = level;
    }
    function restoreMusic() {
      for (const restore of ducked.values()) restore();
    }
    const onLateMediaStart = (ev) => {
      if (controller.signal.aborted || !ttsActive) return;
      const el = ev.detail?.element;
      if (el && !el.paused) duckMusic(el);
      else {
        for (const node of activeMediaNodes()) if (!node.paused) duckMusic(node);
      }
    };
    window.addEventListener('moss-media-start', onLateMediaStart, { signal: controller.signal });
    controller.signal.addEventListener('abort', restoreMusic, { once: true });

    let outcome = 'stopped';
    let savedGreeting = null;
    let resolveFinish;
    const finishedPromise = new Promise((resolve) => { resolveFinish = resolve; });
    held = { id, promise: finishedPromise };

    try {
      await openSpeechOutput(controller.signal);
      if (useGreetingCache) {
        const clips = await greetingCache.read(text, space);
        if (clips?.length === sentences.length) savedGreeting = clips;
      }
      if (controller.signal.aborted) return 'stopped';

      const allowsPair = !useGreetingCache && (isVerseCategory(category) || ['reply', 'warm'].includes(category));
      let batchAvailable = false;
      let batchFailed = false;

      async function prepareOne(index) {
        const { spoken, caption } = sentences[index];
        const voiceText = index === 0 && !isVerseCategory(category) ? expressiveText(spoken, expression) : spoken;
        if (savedGreeting?.[index]) {
          const audio = await ttsCtx.decodeAudioData(await savedGreeting[index].arrayBuffer());
          return { audio, caption, shortCue: false };
        }
        const automatic = category === 'media' && !replaying && sentences.length === 1;
        const earlyResp = index === 0 ? await lead?.response : null;
        if (controller.signal.aborted) throw controller.signal.reason;
        const resp = earlyResp ?? await fetchWavResponse(voiceText, category, space, controller.signal, automatic);
        batchAvailable = allowsPair && resp.headers.get('X-MOSS-Batch-Capable') === '1';
        const shortCue = resp.headers.get('X-MOSS-Announcement') === 'short';
        if (shortCue && turnId) {
          let warmed = false;
          let warmTimer = 0;
          const warm = (ev) => {
            if (warmed || ev.detail?.turnId !== turnId) return;
            warmed = true;
            clearTimeout(warmTimer);
            window.removeEventListener('moss-media-start', warm);
            void finishedPromise.then(() => {
              if (outcome === true) {
                return requestVoice(voiceText, category, false, undefined, space)
                  .then((r) => r.body?.cancel()).catch(() => {});
              }
            }).catch(() => {});
          };
          const alreadyPlaying = activeMediaNodes().some((n) => !n.paused && !n.ended);
          if (alreadyPlaying) {
            warm({ detail: { turnId } });
          } else {
            window.addEventListener('moss-media-start', warm);
            warmTimer = setTimeout(() => window.removeEventListener('moss-media-start', warm), 12000);
          }
        }
        const audio = await ttsCtx.decodeAudioData(await resp.arrayBuffer());
        return { audio, caption, shortCue };
      }

      async function preparePair(index) {
        const parts = sentences.slice(index, index + 2);
        function singleSegments() {
          if (controller.signal.aborted) throw controller.signal.reason;
          batchFailed = true;
          return null;
        }
        const resp = await fetch('/api/speak-batch', {
          method: 'POST',
          signal: controller.signal,
          headers: { 'Content-Type': 'application/json' },
          body: voiceBody({ category, texts: parts.map((p) => p.spoken) }, space),
        });
        if ([404, 405, 503].includes(resp.status)) return singleSegments();
        if (!resp.ok) throw new Error('MOSS 后续段落合成失败');
        try {
          const files = await resp.formData();
          const waves = parts.map((_, i) => files.get(String(i)));
          if (!waves.every((wav) => wav instanceof Blob && wav.size)) return singleSegments();
          const decoded = await Promise.all(waves.map(async (wav, i) => ({
            audio: await ttsCtx.decodeAudioData(await wav.arrayBuffer()),
            caption: parts[i].caption,
            shortCue: false,
          })));
          batchAvailable = resp.headers.get('X-MOSS-Batch-Capable') === '1';
          return decoded;
        } catch {
          return singleSegments();
        }
      }

      function prefetch(promise) {
        const slot = { ready: false };
        slot.done = promise.then(
          (value) => { slot.value = value; },
          (error) => { slot.error = error; },
        ).finally(() => { slot.ready = true; });
        return slot;
      }

      const queue = [prefetch(prepareOne(0))];
      let audioEnd = null;

      async function pauseIfNeeded(ms) {
        const elapsed = audioEnd === null ? 0 : Math.max(0, (ttsCtx.currentTime - audioEnd) * 1000);
        if (ms <= elapsed) return;
        await waitForPause(ms - elapsed, controller.signal);
      }

      function fillQueue(index, playingSeconds) {
        if (queue.length || index >= sentences.length) return;
        const parts = sentences.slice(index, index + 2);
        if (batchAvailable && !batchFailed && parts.length === 2 && playingSeconds >= 2.5 &&
            parts.every((p) => p.spoken.length <= 160) &&
            parts[0].spoken.length + parts[1].spoken.length <= 240) {
          const pair = preparePair(index);
          const first = pair.then((value) => (value ? value[0] : prepareOne(index)));
          const second = pair.then((value) => (value ? value[1] : first.then(() => prepareOne(index + 1))));
          queue.push(prefetch(first), prefetch(second));
        } else {
          queue.push(prefetch(prepareOne(index)));
        }
      }

      for (let index = 0; index < sentences.length; index++) {
        const next = queue.shift();
        if (index && !next.ready) {
          emitSpeech('pending', { id, sentence: index + 1, count: sentences.length });
        }
        await next.done;
        if (controller.signal.aborted) return 'stopped';
        if (next.error) throw next.error;

        const { audio, caption, shortCue } = next.value;
        fillQueue(index + 1, audio.duration);
        if (sentences[index].pauseBefore) {
          await pauseIfNeeded(sentences[index].pauseBefore);
          if (controller.signal.aborted) return 'stopped';
        }

        const src = ttsCtx.createBufferSource();
        src.buffer = audio;
        const edges = trim && !shortCue ? quietEdges(audio) : { head: 0, tail: 0 };
        const nextSentence = sentences[index + 1];
        const offset = sentences[index].pauseBefore ? 0
          : index ? edges.head : (trimLead && !shortCue ? leadingReplySilence(audio) : 0);
        const tail = nextSentence && !nextSentence.pauseBefore ? edges.tail : 0;
        const gain = offset || tail ? ttsCtx.createGain() : null;
        if (gain) src.connect(gain).connect(ttsAnalyser);
        else src.connect(ttsAnalyser);
        ttsSource = src;

        for (const mediaNode of activeMediaNodes()) {
          if (!mediaNode.paused) duckMusic(mediaNode);
        }

        const clock = ttsCtx.currentTime;
        const fade = 0.004;
        if (offset) {
          gain.gain.setValueAtTime(0, clock);
          gain.gain.linearRampToValueAtTime(1, clock + fade);
        }
        const duration = audio.duration - offset;
        audioEnd = clock + duration;
        const fullCaption = shortCue ? '为你播放。' : caption;
        const cues = timedCaptions(fullCaption, category, audio, offset);
        let cueIdx = 0;
        let lastProgress = -1;
        const timing = {
          id,
          text: fullCaption,
          category,
          sentence: index + 1,
          count: sentences.length,
          seconds: duration,
          shortCue,
        };

        const successor = queue[0];
        if (tail && successor) {
          void successor.done.then(() => {
            if (successor.error || controller.signal.aborted || ttsSource !== src) return;
            const tailFade = 0.018;
            const stopAt = Math.max(clock + duration - tail, ttsCtx.currentTime + tailFade + 0.006);
            if (stopAt >= audioEnd - 0.015) return;
            gain.gain.setValueAtTime(1, stopAt - tailFade);
            gain.gain.linearRampToValueAtTime(0, stopAt);
            src.stop(stopAt);
            audioEnd = stopAt;
            timing.seconds = stopAt - clock;
          });
        }

        await new Promise((resolve, reject) => {
          const freq = new Uint8Array(ttsAnalyser.frequencyBinCount);
          const time = new Float32Array(ttsAnalyser.fftSize);
          const pump = () => {
            if (!ttsActive || ttsSource !== src || !ttsAnalyser) return;
            ttsAnalyser.getByteFrequencyData(freq);
            ttsAnalyser.getFloatTimeDomainData(time);
            onSpeechFrame?.({ freq, time, sampleRate: ttsCtx.sampleRate });
            const position = Math.min(timing.seconds, Math.max(0, ttsCtx.currentTime - clock));
            const prevCue = cueIdx;
            while (cueIdx + 1 < cues.length && position >= cues[cueIdx + 1].at) cueIdx++;
            if (cueIdx !== prevCue) {
              onCaption?.(cues[cueIdx].text);
              emitSpeech('progress', { ...timing, position, caption: cues[cueIdx].text });
              lastProgress = position;
            } else if (position - lastProgress >= 0.24) {
              emitSpeech('progress', { ...timing, position, caption: cues[cueIdx].text });
              lastProgress = position;
            }
            ttsRaf = requestAnimationFrame(pump); // fui-lint-ignore: R15 active only while TTS buffer is playing
          };

          let finished = false;
          const finish = (error) => {
            if (finished) return;
            finished = true;
            controller.signal.removeEventListener('abort', abort);
            try { src.disconnect(); } catch { /* ignore */ }
            try { gain?.disconnect(); } catch { /* ignore */ }
            if (ttsSource === src) {
              ttsSource = null;
              cancelAnimationFrame(ttsRaf);
              ttsRaf = 0;
              onSpeechFrame?.(null);
            }
            if (error) reject(error);
            else resolve();
          };
          const abort = () => {
            try { src.stop(); } catch { /* ignore */ }
            finish();
          };
          src.onended = () => finish();
          controller.signal.addEventListener('abort', abort, { once: true });

          try {
            if (offset) src.start(0, offset);
            else src.start();
          } catch (error) {
            finish(error);
            return;
          }
          onSpeechState?.({ active: true, turnId: id });
          onStatus?.('voice.speaking');
          onCaption?.(cues[0].text);
          emitSpeech('start', { ...timing, caption: cues[0].text });
          pump();
        });
        if (controller.signal.aborted) return 'stopped';
      }

      if (finalPause) {
        await pauseIfNeeded(finalPause);
        if (controller.signal.aborted) return 'stopped';
      }
      emitSpeech('end', { id });
      outcome = true;
      return outcome;
    } catch {
      if (controller.signal.aborted) return 'stopped';
      emitSpeech('error', { id });
      outcome = 'unavailable';
      return outcome;
    } finally {
      window.removeEventListener('moss-media-start', onLateMediaStart);
      controller.signal.removeEventListener('abort', restoreMusic);
      restoreMusic();
      resolveFinish?.();
      controller.abort();
      signal?.removeEventListener('abort', cancel);
      if (ttsAbort === controller) {
        ttsId = null;
        ttsAbort = null;
        ttsActive = false;
        held = null;
        onSpeechFrame?.(null);
        onSpeechState?.({ active: false });
        onCaption?.('');
        onStatus?.(outcome === 'unavailable' ? 'voice.unavailable' : 'voice.ready');
      }
    }
  }

  function observeState(state, { initial = false } = {}) {
    if (draftChat !== state?.chat_id) {
      clearDraftPreparation();
      draftPrepared = false;
      draftChat = state?.chat_id || null;
    }
    draftBusy = (state?.turns || []).some((turn) => ['thinking', 'pending', 'saving'].includes(turn.status));
    speechPrepare = state?.speech_prepare === true;
    speechSessionWarm = state?.speech_session_warm === true;
    currentModelId = state?.model_id || null;
    syncSessionWarm();

    const latest = state?.turns?.at(-1);
    const key = latest ? `${latest.id}:${latest.status}` : null;
    if (initial || !latest) {
      lastSpeechState = key;
      if (prepared && prepared.id !== latest?.id) discardPrepared();
      return;
    }
    if (owner && owner.chat === state.chat_id && !owner.id &&
        latest.user === owner.text && !owner.previous.has(latest.id)) {
      owner.id = latest.id;
    }
    if (prepared && (prepared.id !== latest.id || (owner && owner.chat !== state.chat_id))) {
      discardPrepared();
    }
    if (latest.status === 'cancelled') {
      if (ttsId === latest.id || prepared?.id === latest.id) interruptSpeech();
      lastSpeechState = key;
      return;
    }
    if (latest.status === 'thinking') {
      if (speechPrepare && ttsEnabled && !document.hidden && latest.source === 'holo' &&
          (!owner || (owner.id === latest.id && !owner.importsRequested))) {
        if (owner && owner.id === latest.id) owner.importsRequested = true;
        prepareImports();
      }
      prepareLeadingSpeech(latest);
      return;
    }
    if (key === lastSpeechState) return;
    if (!['pending', 'replied', 'saved', 'error'].includes(latest.status)) return;

    const { text, category, expression, narration } = conversationSpeech(latest, state.turns);
    const words = text || (latest.status === 'saved' && latest.receipt ? '已保存。' : '');
    const obsKey = [latest.id, category, words, expression, narration && JSON.stringify(narration)].join('\n');
    lastSpeechState = key;
    if (!words) {
      discardPrepared();
      return;
    }
    if (observed.has(obsKey)) return;
    observed.add(obsKey);
    if (observed.size > 64) {
      const oldest = observed.values().next().value;
      observed.delete(oldest);
    }
    const allowEdgeTrim = !latest.media && !latest.transport && !['media', 'transport'].includes(latest.source);
    if (ttsEnabled && getMode?.() === 'live') {
      void speakText(words, { category, turnId: latest.id, expression, narration, allowEdgeTrim });
    }
  }

  const stopSimMicWatch = watchBestMicChange({
    getPreferredId: () => micDeviceId,
    getActiveDeviceId: () => (micActive ? (activeMicInfo?.deviceId || '') : ''),
    getSkipIds: () => [...simFailedMicIds],
    isTrackLive: () => micStream?.getAudioTracks?.()[0]?.readyState === 'live',
    onDevicesChanged: () => { simFailedMicIds.clear(); },
    onUpgradeOrFailover: ({ reason }) => {
      if (micOpening || !micActive || micHasVoice) return;
      if (reason === 'better_device' || reason === 'unplugged') {
        stopMicCapture({ transcribe: false });
        void startMic();
      }
    },
  });
  window.addEventListener('pagehide', stopSimMicWatch);

  return {
    toggleMic: startMic,
    stopMic: () => stopMicCapture({ transcribe: false }),
    interrupt: () => {
      if (micActive) stopMicCapture({ transcribe: false });
      interruptSpeech();
    },
    stopFor(id) {
      if (ttsId === id) interruptSpeech();
      else if (prepared?.id === id) discardPrepared();
    },
    userAction,
    speakText,
    prepareGreeting,
    prepareOutput,
    prepareImports,
    isOutputReady: () => ttsCtx?.state === 'running',
    isBusy: () => ttsActive,
    observeState,
    listMicDevices,
    setActiveMicInfo,
    getActiveMicInfo: () => activeMicInfo,
    isMicActive: () => micActive,
    isSpeaking: () => ttsActive,
    setTtsEnabled(on) {
      ttsEnabled = Boolean(on);
      try {
        localStorage.setItem('moss-fui.tts', ttsEnabled ? 'on' : 'off');
        localStorage.setItem('moss.voice.enabled', ttsEnabled ? 'on' : 'off');
      } catch { /* ignore */ }
      clearDraftPreparation();
      draftPrepared = false;
      if (!ttsEnabled) interruptSpeech();
      syncSessionWarm({ force: true });
    },
    getTtsEnabled: () => ttsEnabled,
    setVoiceResident(on) {
      voiceResident = Boolean(on);
      try { localStorage.setItem('moss-fui.voice-resident', voiceResident ? 'on' : 'off'); } catch { /* ignore */ }
      sessionWarmRetryAt = 0;
      syncSessionWarm({ force: true });
    },
    getVoiceResident: () => voiceResident,
    getSessionWarmStatus: () => sessionWarmStatus,
    releaseSessionWarm,
    setSpeechRoomAmount(amount) {
      if (!validRoomAmount(amount)) return;
      roomAmount = amount;
      if (prepared && prepared.roomAmount !== amount) discardPrepared();
      try { localStorage.setItem(speechRoomKey, String(amount)); } catch { /* ignore */ }
    },
    getSpeechRoomAmount: () => roomAmount,
    setMicDevice(id) {
      micDeviceId = id || '';
      try { localStorage.setItem('moss-fui.mic', micDeviceId); } catch { /* ignore */ }
      window.dispatchEvent(new CustomEvent('moss-mic-changed', { detail: activeMicInfo }));
    },
    getMicDevice: () => micDeviceId,
    setWakeEnabled(on) { wakeEnabled = Boolean(on); },
    setBargeInEnabled(on) { bargeInEnabled = Boolean(on); },
  };
}
