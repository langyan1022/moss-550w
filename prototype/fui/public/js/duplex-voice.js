import { activateVoice } from './agent-voice.moss.js?v=fui-live';
import { normalizeSpoken } from '/static/voice-wake.js?v=spoken-stop';
import { safeUuid } from './safe-web.js';

const ACTIVE = new Set(['thinking', 'pending', 'saving']);
const CALENDAR = new Set(['calendar_create', 'calendar_reschedule', 'calendar_cancel']);
const STOP_SPEECH = /^(?:停止播报|(?:先)?别(?:继续)?(?:说|播报)了|不用(?:继续)?说了|不要继续说了)$/u;
const candidateTicket = /^#native-wake=([A-Za-z0-9_-]{16,128})$/u;
const MIC_PRELOAD_MS = 25000;

function nativeListenEnabled() {
  try { return localStorage.getItem('moss.voice.nativeNext') === 'on'; } catch { return false; }
}

export function createDuplexVoice(output, {
  getMode, getState, onTranscript, onMicState, onMicFrame, onStatus, focusComposer, targetsPlayback,
}) {
  const button = document.createElement('button');
  button.append(document.createElement('span'));
  const checkbox = () => Object.assign(document.createElement('input'), {type: 'checkbox'});
  const wake = checkbox(), wakeInterrupt = checkbox();
  const message = document.getElementById('comms-input');
  const controls = {mic: button, 'mic-state': document.createElement('small'), 'wake-listen': wake,
    'wake-interrupt': wakeInterrupt, 'wake-status': document.createElement('small'),
    interrupt: document.getElementById('comms-interrupt'), message};
  let recordState = false, changeDevice, lastChat, lastMode, microphonePreparation;

  try {
    const previous = localStorage.getItem('moss.conversation.micDevice');
    if (previous && localStorage.getItem('moss-fui.mic') === null) output.setMicDevice(previous);
  } catch { /* Storage is optional for capture. */ }

  function speakStatus(detail) {
    onStatus(recordState ? 'voice.listening' : 'voice.ready', detail);
  }

  function interruptSpeech() {
    if (!(output.isSpeaking() || output.isBusy?.())) return false;
    output.interrupt();
    return true;
  }

  function syncWakeState({enabled, interrupt, detail}) {
    const settingsWake = document.getElementById('set-wake');
    const settingsInterrupt = document.getElementById('set-barge');
    if (settingsWake) settingsWake.checked = enabled;
    if (settingsInterrupt) { settingsInterrupt.checked = interrupt; settingsInterrupt.disabled = !enabled; }
    speakStatus(detail);
  }

  function preparationAllowed(state) {
    return state?.speech_prepare === true && output.getTtsEnabled() &&
      !output.isSpeaking() && visible() && !state.turns?.some(turn => ACTIVE.has(turn.status));
  }

  function activityPreloadEnabled(state) {
    return state?.speech_mic_warm === true && state.speech_mic_warm_on_activity === true &&
      state.model_id !== 'local';
  }

  function prepareImports() {
    if (!preparationAllowed(getState())) return false;
    return output.prepareImports();
  }

  function releaseMicrophone(key) {
    void fetch('/api/prepare-speech', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mic:key, release:true}), keepalive:true,
    }).catch(() => {});
  }

  function releasePreparation() {
    const lease = microphonePreparation;
    if (!lease) return;
    microphonePreparation = null;
    lease.signal.removeEventListener('abort', releasePreparation);
    releaseMicrophone(lease.key);
  }

  function prepareForSpokenInput(signal) {
    const state = getState();
    if (!preparationAllowed(state) || !output.isOutputReady?.()) return false;
    if (!signal || !activityPreloadEnabled(state) || targetsPlayback?.()) return prepareImports();
    if (signal.aborted) return false;
    const now = performance.now();
    if (microphonePreparation &&
        (microphonePreparation.signal !== signal || now - microphonePreparation.started >= MIC_PRELOAD_MS)) {
      releasePreparation();
    }
    if (microphonePreparation) return true;
    const lease = {key:safeUuid().replaceAll('-', ''), signal, started:now};
    microphonePreparation = lease;
    signal.addEventListener('abort', releasePreparation, {once:true});
    void fetch('/api/prepare-speech', {
      method:'POST', headers:{'Content-Type':'application/json', 'X-MOSS-Voice-Activity':'1'},
      body:JSON.stringify({mic:lease.key}),
    }).then(response => {
      if (microphonePreparation !== lease) {
        if (response.ok) releaseMicrophone(lease.key);
      } else if (!response.ok || response.headers.get('X-MOSS-Mic-Prepared') !== '1') {
        releasePreparation();
        prepareImports();
      }
    }).catch(() => {
      if (microphonePreparation === lease) {
        releasePreparation();
        prepareImports();
      }
    });
    return true;
  }

  const capture = activateVoice({controls, contextKey: () => getState()?.chat_id,
    bindInputs(onChange) {
      changeDevice = onChange;
      return {
        get value() { return output.getMicDevice(); },
        commit: id => output.setMicDevice(id),
        setTrack: (track, meta) => output.setActiveMicInfo?.(track, meta),
      };
    },
    onCaptureState({recording}) { recordState = recording; onMicState({active: recording}); },
    onWakeState: syncWakeState,
    status: speakStatus,
    draw: (time, freq, sampleRate) => onMicFrame(time ? {time, freq, sampleRate} : null),
    pendingCalendar: () => getState()?.turns?.find(row => row.status === 'pending' && CALENDAR.has(row.desktop_confirmation?.action))?.id,
    replaceableReply: () => getState()?.turns?.find(row => row.status === 'thinking' && row.conversation_replaceable === true)?.id,
    targetsPlayback,
    onNextListening: prepareImports,
    onActualSpeech: prepareForSpokenInput,
    onAcceptedWake: prepareImports,
    onInterrupt: interruptSpeech,
    send: (text, calendarTurn, replyTurn) => STOP_SPEECH.test(normalizeSpoken(text)) && interruptSpeech() ? true :
      onTranscript(text, {spoken: true, calendarTurn, replyTurn}),
    submitOverlappingDecision({original, filtered, turnId, calendarTurn, heardInPlayback}) {
      if (!turnId || heardInPlayback || !/^(?:确认|取消|算了|保留|不)/u.test(normalizeSpoken(original))) return null;
      const shortDecline = !filtered && /^(?:取消|算了)$/u.test(normalizeSpoken(original));
      if (!shortDecline && normalizeSpoken(original) !== normalizeSpoken(filtered)) return Promise.resolve(false);
      return onTranscript(original, {spoken: true, calendarTurn, overlapTurn: turnId});
    },
  });

  function visible() {
    return !document.hidden && document.hasFocus() && !document.querySelector('dialog[open]') && getMode() === 'live';
  }

  async function wakeAction(action, payload) {
    const response = await fetch(`/api/wake/${action}`, {method: 'POST',
      headers: {'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if (!response.ok) throw new Error('Wake offer expired');
    return response;
  }

  async function acceptWake(ticket) {
    if (!visible() || typeof ticket !== 'string') return;
    try {
      const {grant, listen_next} = await (await wakeAction('claim', {ticket})).json();
      if (!visible() || !message?.getClientRects().length) return;
      if (!message.value.trim() && !message.closest('form')?.contains(document.activeElement)) focusComposer();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      if (!visible() || !message.getClientRects().length) return;
      await wakeAction('shown', {ticket, grant});
      const chat = getState()?.chat_id;
      if (listen_next && chat && nativeListenEnabled()) {
        void capture.listenAfterWake(() => visible() && nativeListenEnabled() && !message.value.trim() &&
          getState()?.chat_id === chat && !output.isSpeaking() && !output.isBusy?.() &&
          !getState()?.turns.some(turn => ACTIVE.has(turn.status)));
      }
    } catch { /* The native wake launcher handles an unacknowledged offer. */ }
  }

  const wakeUrl = new URL(location.href);
  let coldTicket = wakeUrl.hash.match(candidateTicket)?.[1];
  if (wakeUrl.hash.startsWith('#native-wake=')) {
    wakeUrl.hash = '';
    history.replaceState(history.state, '', wakeUrl);
  }
  function acceptColdWake() {
    if (!coldTicket || !getState()?.chat_id || !visible()) return;
    const ticket = coldTicket;
    coldTicket = null;
    void acceptWake(ticket);
  }
  window.addEventListener('focus', acceptColdWake);
  document.addEventListener('visibilitychange', acceptColdWake);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) releasePreparation();
  });
  window.addEventListener('pagehide', releasePreparation);

  function setChecked(control, enabled) {
    control.checked = Boolean(enabled);
    control.dispatchEvent(new Event('change'));
  }

  return {...output,
    toggleMic() {
      if (getMode() !== 'live') return output.toggleMic();
      output.prepareOutput();
      return capture.toggle();
    },
    stopMic: () => { capture.stop(); output.stopMic(); },
    connectionInterrupted() {
      releasePreparation();
      output.releaseSessionWarm?.('idle');
      capture.stop();
    },
    isMicActive: () => getMode() === 'live' ? recordState : output.isMicActive(),
    interrupt: () => output.interrupt(),
    setTtsEnabled(on) {
      output.setTtsEnabled(on);
      if (on) output.prepareOutput();
      else releasePreparation();
    },
    setMicDevice(id) {
      if (getMode() === 'live') {
        changeDevice?.(id);
      } else {
        output.setMicDevice(id);
        if (output.isMicActive?.()) {
          output.stopMic();
          void output.toggleMic();
        }
      }
    },
    setWakeEnabled: enabled => getMode() === 'live' ? setChecked(wake, enabled) : output.setWakeEnabled(enabled),
    setBargeInEnabled: enabled => getMode() === 'live' ? setChecked(wakeInterrupt, enabled) : output.setBargeInEnabled(enabled),
    acceptWake,
    observeState(state, options) {
      const switched = lastChat && state.chat_id !== lastChat;
      if (switched) {
        capture.stop('已停止上一段语音输入。');
        output.stopMic();
        interruptSpeech();
      }
      const mode = getMode();
      if (mode !== 'live' || state.speech_prepare !== true || !activityPreloadEnabled(state)) releasePreparation();
      if (mode !== 'live') output.releaseSessionWarm?.('idle');
      if (lastMode === 'live' && mode !== 'live') capture.stop();
      if (lastMode && lastMode !== 'live' && mode === 'live') output.stopMic();
      lastChat = state.chat_id;
      lastMode = mode;
      output.observeState(state, {initial: options?.initial === true || Boolean(switched)});
      acceptColdWake();
    },
  };
}
