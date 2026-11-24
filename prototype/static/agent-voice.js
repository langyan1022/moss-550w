import {microphoneInputs} from "/conversation-assets/microphone-input.js";
import {playbackEndpointOptions, voiceEndpoint} from "./voice-endpoint.js?v=asr-preview";
import {admitsConversationSpeech, newSpeechInPlayback, normalizeSpoken, speechStopInPlayback, wakeCommand} from "./voice-wake.js?v=spoken-stop";

const ECHO_TAIL_MS = 400;
const PLAYBACK_CLIP_TAIL = .16;
const RECOGNITION_PREVIEW_PAUSE = .94;
const RECOGNITION_PREVIEW_CONTINUATION = .18;

function playbackVoiceBlocks(blocks, rate, {elapsed, firstVoice, lastVoice}) {
  // 断句计时包括已经滚出缓冲区的采样，按声音结束处倒推保留范围。
  const length = blocks.reduce((count, block) => count + block.length, 0);
  const start = Math.max(0, length - Math.ceil((elapsed - firstVoice + .18) * rate));
  const end = length - Math.max(0, Math.floor((elapsed - lastVoice - PLAYBACK_CLIP_TAIL) * rate));
  if (end - start < rate / 8 || (start === 0 && end === length)) return blocks;
  const kept = [];
  let offset = 0;
  for (const block of blocks) {
    const first = Math.max(0, start - offset);
    const after = Math.min(block.length, end - offset);
    if (first < after) kept.push(block.subarray(first, after));
    offset += block.length;
    if (offset >= end) break;
  }
  return kept;
}

export function pcmWav(chunks, sampleRate) {
  const length = chunks.reduce((sum, block) => sum + block.length, 0);
  const samples = new Float32Array(length);
  let offset = 0;
  for (const block of chunks) { samples.set(block, offset); offset += block.length; }
  const count = Math.floor(length * 16000 / sampleRate);
  const buffer = new ArrayBuffer(44 + 2 * count);
  const view = new DataView(buffer);
  const ascii = (pos, text) => [...text].forEach((char, i) => view.setUint8(pos + i, char.charCodeAt(0)));
  ascii(0, "RIFF"); view.setUint32(4, 36 + 2 * count, true); ascii(8, "WAVE"); ascii(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); ascii(36, "data"); view.setUint32(40, 2 * count, true);
  for (let i = 0; i < count; i++) {
    const a = Math.floor(i * sampleRate / 16000), b = Math.min(length, Math.max(a + 1, Math.floor((i + 1) * sampleRate / 16000)));
    let total = 0;
    for (let j = a; j < b; j++) total += samples[j];
    const value = Math.max(-1, Math.min(1, total / (b - a)));
    view.setInt16(44 + 2 * i, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  }
  return new Blob([buffer], {type: "audio/wav"});
}

export function activateVoice({send, status, draw, onInterrupt, onNextListening, onActualSpeech, onAcceptedWake, onConversationGranted,
                               contextKey, pendingCalendar, replaceableReply, targetsPlayback, submitOverlappingDecision}) {
  const button = document.getElementById("mic");
  const meter = document.getElementById("mic-state");
  const wake = document.getElementById("wake-listen");
  const interrupt = document.getElementById("wake-interrupt");
  const wakeStatus = document.getElementById("wake-status");
  let current, resumeTimer, speaking = false, captureGeneration = 0, finishedSpeech;
  const playbackSegments = [];
  let preparing, inputTask;
  wake.checked = false;
  interrupt.checked = false;
  interrupt.disabled = true;
  const inputs = microphoneInputs(id => {
    const reopen = current?.kind === "conversation" && ["opening", "recording"].includes(current.phase);
    inputs.commit(id);
    if (current) cancel("已切换输入设备，上一段录音已取消。");
    if (reopen) begin();
    else resumeWake();
  });
  function showWake(text) {
    wakeStatus.textContent = text;
    if (wake.checked && (!current || current.kind === "wake")) {
      button.querySelector("span").textContent = "直接说话";
      meter.textContent = text;
    } else if (!current) {
      display("开启麦克风", false);
    }
  }
  function prepareInput() {
    preparing ??= fetch("/api/prepare-voice", {method:"POST", headers:{"Content-Type":"application/json"}})
      .then(response => response.ok).catch(() => false).finally(() => { preparing = null; });
    return preparing;
  }
  function readyForInterruption() {
    const generation = captureGeneration;
    showWake("播报中，正在准备本机语音识别");
    void prepareInput().then(ready => {
      if (generation !== captureGeneration || !wake.checked || !interrupt.checked) return;
      showWake(ready ? "播报中，可以说“等一下”" : "本机语音识别未就绪，可点击打断");
    });
  }
  function resumeWake(delay = 0) {
    clearTimeout(resumeTimer);
    if (!wake.checked || (speaking && !interrupt.checked) || current) return;
    resumeTimer = setTimeout(() => {
      if (wake.checked && (!speaking || interrupt.checked) && !current) begin("wake");
    }, delay);
  }
  function resetCapture(session) {
    session.blocks = [];
    session.awakeCapture = false;
    session.outputPrepared = false;
    session.stopProbe = null;
    session.recognitionPreview = null;
    session.calendarTurn = null;
    session.replyTurn = null;
    session.endpoint = voiceEndpoint(speaking ? playbackEndpointOptions : undefined);
  }
  function capturedClip(session) {
    const timing = session.endpoint.speechTiming;
    const {elapsed, firstVoice, lastVoice} = timing;
    const now = performance.now();
    const from = now - (elapsed-firstVoice)*1000;
    const to = now - (elapsed-lastVoice)*1000;
    const overlap = speaking || (session.kind === "conversation" && from < (session.echoUntil ?? 0));
    const segments = overlap ? playbackSegments
      .filter(segment => segment.from <= to + 100 && segment.to + ECHO_TAIL_MS >= from - 80) : [];
    const references = segments.map(segment => segment.text);
    const confirmationPrompt = segments.findLast(segment => segment.category === "confirm")?.id;
    const blocks = overlap ? playbackVoiceBlocks(session.blocks, session.context.sampleRate, timing) : session.blocks;
    const probe = overlap && session.stopProbe?.lastVoice === lastVoice ? session.stopProbe.result : null;
    return {blocks, awake:session.awakeCapture, generation:captureGeneration, references, overlap, confirmationPrompt,
      calendarTurn:session.calendarTurn, replyTurn:overlap ? null : session.replyTurn,
      voicedSeconds:session.endpoint.voicedSeconds, probe,
      recognitionPreview:overlap ? null : session.recognitionPreview};
  }
  function recentlyFinished(piece) {
    return interrupt.checked && finishedSpeech?.generation === piece.generation && performance.now() < finishedSpeech.until;
  }
  function display(label, recording) {
    button.querySelector("span").textContent = label;
    button.setAttribute("aria-pressed", String(recording));
    button.title = recording ? "说完停顿片刻，MOSS 自动识别" : "";
    meter.textContent = recording ? "录音中" : "未开启";
  }
  function release(session) {
    session.flush?.();
    if (session.node) session.node.port.onmessage = null;
    session.source?.disconnect();
    session.node?.disconnect();
    session.stream?.getTracks().forEach(track => track.stop());
    if (session.context && session.context.state !== "closed") session.context.close().catch(() => {});
    session.context = null;
    session.endInput?.();
    session.endInput = null;
  }
  function reset(session) {
    if (current !== session) return;
    current = null;
    clearTimeout(session.openingTimer);
    session.aborter.abort();
    release(session);
    inputs.setTrack(null);
    display("开启麦克风", false);
    draw(null);
  }
  function cancel(note) {
    if (!current) return;
    reset(current);
    if (note) status(note);
  }
  function owns(session) {
    return current === session && session.chat === contextKey();
  }
  function canContinue(session) {
    if (!owns(session)) return false;
    if (session.kind !== "native-next" || (session.eligible() && session.permission.state === "granted" && !speaking)) return true;
    cancel("本次接续录音已结束。");
    return false;
  }
  function abortOpen(session) {
    if (current === session) reset(session);
    else release(session);
  }
  function legacyVirtualMic(label = "") {
    return /stereo mix|立体声混音|cable output|vb-audio|virtual|loopback|blackhole|soundflower|hands-free|ag audio|telephony/i.test(label);
  }
  async function openLegacyStream(preferredId) {
    const baseAudio = {channelCount: 1, echoCancellation: true, noiseSuppression: true};
    let devs = [];
    try { devs = await navigator.mediaDevices?.enumerateDevices?.() || []; } catch {}
    const hardware = devs.filter(d => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications");
    const candidates = [];
    if (preferredId) candidates.push({deviceId: preferredId, audio: {...baseAudio, deviceId: {exact: preferredId}}});
    else {
      const real = hardware.filter(d => !legacyVirtualMic(d.label || ""));
      for (const d of (real.length ? real : hardware)) {
        candidates.push({deviceId: d.deviceId, audio: {...baseAudio, deviceId: {exact: d.deviceId}}});
      }
    }
    candidates.push({deviceId: "", audio: baseAudio});
    let lastErr = null;
    for (const cand of candidates) {
      try {
        const s = await navigator.mediaDevices.getUserMedia({audio: cand.audio});
        const t = s.getAudioTracks()[0];
        if (t && t.readyState === "live") {
          const id = t.getSettings?.().deviceId || cand.deviceId || "";
          return {stream: s, track: t, deviceId: id};
        }
        s.getTracks().forEach(x => x.stop());
      } catch (err) {
        if (err && (err.name === "NotAllowedError" || err.name === "PermissionDeniedError" || err.name === "SecurityError")) throw err;
        lastErr = err;
      }
    }
    throw lastErr || new Error("没有可用的麦克风输入设备。");
  }
  async function transcribeWav(wav, signal, preview = false) {
    const headers = {"Content-Type":"audio/wav"};
    if (preview) headers["X-MOSS-ASR-Preview"] = "1";
    const result = await fetch("/api/transcribe", {method:"POST", body:wav,
      headers, signal});
    const body = await result.json();
    if (!result.ok) throw new Error(body.error || "本机识别服务暂不可用。");
    if (preview && body.text === null) return null;
    return body.text?.trim() || "";
  }
  async function transcription(blocks, rate, signal, preview) {
    const wav = pcmWav(blocks, rate);
    if (preview && preview.wav.size === wav.size) {
      const [actual, expected] = (await Promise.all([wav.arrayBuffer(), preview.wav.arrayBuffer()]))
        .map(buffer => new Uint8Array(buffer));
      if (actual.every((sample, index) => sample === expected[index])) {
        const text = await preview.result;
        if (text !== null) return text;
      }
    }
    return transcribeWav(wav, signal);
  }
  function previewConversation(session, samples, rate) {
    if (session.kind !== "conversation") return;
    const voicedSeconds = session.endpoint.voicedSeconds;
    const previous = session.recognitionPreview;
    if (previous && (previous.rearmed || voicedSeconds - previous.voicedSeconds < RECOGNITION_PREVIEW_CONTINUATION)) return;
    if (session.busy || session.probing || speaking || !owns(session) || document.hidden || !document.hasFocus()) return;
    const {elapsed, firstVoice, lastVoice} = session.endpoint.speechTiming;
    // The completed clip still uses playback rules if its speech started during the echo tail.
    if (performance.now() - (elapsed - firstVoice) * 1000 < (session.echoUntil ?? 0)) return;
    if (!lastVoice || elapsed - lastVoice < RECOGNITION_PREVIEW_PAUSE || voicedSeconds < .35 ||
        samples.some(sample => Math.abs(sample) >= 1 / 65536)) return;
    const count = session.endpoint.quietBlocksUntilDone(rate, samples.length);
    if (!count) return;
    const silence = new Float32Array(samples.length);
    const wav = pcmWav([...session.blocks, ...Array(count).fill(silence)], rate);
    // A pause followed by more speech invalidates the first draft. Allow one replacement.
    session.recognitionPreview = {wav, voicedSeconds, rearmed:Boolean(previous),
      result:transcribeWav(wav, session.aborter.signal, true).catch(() => null)};
  }
  function showSpokenStop(session, finished = false) {
    if (session.kind === "conversation") {
      status("已停止播报，我在听。");
    } else {
      session.awakeUntil = performance.now() + 10000;
      showWake(finished ? "请接着说" : "已停止播报，请接着说");
    }
  }
  function probeSpokenStop(session, rate) {
    const {elapsed, firstVoice, lastVoice} = session.endpoint.speechTiming;
    const previous = session.stopProbe;
    if (!speaking || !playbackSegments.length || session.busy || session.probing ||
        (previous && (!previous.retry || previous.lastVoice === lastVoice)) ||
        session.endpoint.voicedSeconds < .22 || lastVoice - firstVoice > 2.1 || elapsed - lastVoice < PLAYBACK_CLIP_TAIL) return;
    const clip = capturedClip(session);
    const result = transcription(clip.blocks, rate, session.aborter.signal).catch(() => null);
    const probe = session.stopProbe = {lastVoice, result};
    session.probing = true;
    void result.then(words => {
      session.probing = false;
      if (!words || !owns(session) || !speaking || clip.generation !== captureGeneration ||
          session.stopProbe !== probe || !speechStopInPlayback(words, clip.references)) return;
      if (session.endpoint.speechTiming.lastVoice !== lastVoice) {
        // 有后续声音时重新确认整句，避免把前半句当作独立的停播指令。
        probe.retry = !previous;
        return;
      }
      // 普通接话仍等原来的断句。只有停播指令能在识别后直接执行。
      resetCapture(session);
      if (onInterrupt()) showSpokenStop(session);
    });
  }
  async function submitConversationSpeech(session, piece, words) {
    const command = wakeCommand(words) ?? words;
    if (!admitsConversationSpeech(command,piece.voicedSeconds)) return;
    if (piece.overlap && speechStopInPlayback(command, piece.references)) {
      if (piece.generation === captureGeneration && onInterrupt()) showSpokenStop(session);
      return;
    }
    const instruction = piece.overlap ? newSpeechInPlayback(command, piece.references) : command;
    if (!owns(session)) return;
    if (piece.references.length) {
      const spoken = normalizeSpoken(command);
      const decision = submitOverlappingDecision?.({
        original:command, filtered:instruction, chatId:session.chat, turnId:piece.confirmationPrompt, calendarTurn:piece.calendarTurn,
        heardInPlayback:piece.references.some(line => line.includes(spoken))
      });
      if (decision) { await decision; return; }
    }
    if (!instruction) return;
    if (speaking && !targetsPlayback?.(instruction)) onInterrupt();
    await send(instruction, piece.calendarTurn, piece.replyTurn);
  }
  async function recognizeWake(session, clip, rate) {
    // 上一次本机识别时，麦克风仍保留下一条短句；最多等待一条，避免长时间排队。
    if (session.busy) {
      if (!session.next) session.next = clip;
      return;
    }
    session.busy = true;
    const conversation = session.kind === "conversation";
    try {
      let piece = clip;
      while (piece && owns(session)) {
        const words = conversation || piece.generation === captureGeneration || recentlyFinished(piece)
          ? (await piece.probe) ?? await transcription(piece.blocks, rate, session.aborter.signal, piece.recognitionPreview) : "";
        if (!owns(session)) return;
        if (conversation) {
          await submitConversationSpeech(session,piece,words);
          piece = session.next;
          session.next = null;
          continue;
        }
        const finished = recentlyFinished(piece);
        const command = wakeCommand(words);
        const awake = piece.awake || performance.now() < session.awakeUntil;
        if (words && (finished || (piece.generation === captureGeneration && speaking))) {
          if (speechStopInPlayback(words, piece.references) && (finished || onInterrupt())) {
            showSpokenStop(session, finished);
          }
        } else if (words && piece.generation === captureGeneration && (awake || command !== null)) {
          if (/^(?:嗯|唔)+[\s\p{P}]*$/u.test(words)) {
            piece = session.next;
            session.next = null;
            continue;
          }
          if (command === "") {
            session.awakeUntil = performance.now() + 10000;
            showWake("已唤醒，请说指令");
            onAcceptedWake?.();
          } else {
            session.awakeUntil = 0;
            if (session.next) session.next.awake = false;
            session.awakeCapture = false;
            const instruction = command ?? words;
            showWake("已收到语音指令");
            await send(instruction, piece.calendarTurn, piece.replyTurn);
            if (owns(session)) showWake("等待唤醒");
          }
        }
        piece = session.next;
        session.next = null;
      }
    } catch (error) {
      session.next = null;
      if (owns(session) && error.name !== "AbortError") {
        if (conversation) status(error.message);
        else showWake("等待唤醒 · " + error.message);
      }
    } finally {
      session.busy = false;
    }
  }
  async function recognize(session) {
    if (!canContinue(session)) return;
    session.phase = "recognizing";
    display("取消识别", false);
    try {
      await new Promise(resolve => {
        const timer = setTimeout(resolve, 150);
        session.flush = () => { clearTimeout(timer); resolve(); };
        session.node.port.postMessage("flush");
      });
      if (!canContinue(session)) return;
      const rate = session.context.sampleRate;
      release(session);
      inputs.setTrack(null);
      draw(null);
      if (!session.blocks.length) { status("这一段没有收到录音。"); return; }
      status("正在本机识别语音…");
      const text = await transcription(session.blocks, rate, session.aborter.signal);
      if (!canContinue(session)) return;
      if (!text) throw new Error("没有识别出文字。");
      status("已在本机识别：" + text);
      await send(text, session.calendarTurn, session.replyTurn);
    } catch (error) {
      if (owns(session) && error.name !== "AbortError") status(error.message || "本机识别服务暂不可用。");
    } finally { reset(session); resumeWake(); }
  }
  async function begin(kind = "conversation", next) {
    if (kind === "conversation") {
      if (current?.kind === "wake") disableWake();
      else if (current) { disableWake(); cancel("麦克风已关闭。"); return; }
      clearTimeout(resumeTimer);
    }
    const background = kind === "wake";
    const session = {kind, phase:"opening", chat:contextKey(), aborter:new AbortController(), awakeUntil:0};
    if (next) Object.assign(session, next);
    if (background || kind === "conversation") resetCapture(session);
    else Object.assign(session, {blocks:[], endpoint:voiceEndpoint()});
    current = session;
    if (next) {
      session.openingTimer = setTimeout(() => {
        if (owns(session)) cancel("接续录音未能启动，可手动开启麦克风。");
      }, 5000);
      session.permission.addEventListener("change", () => canContinue(session), {signal:session.aborter.signal});
    }
    if (background) showWake("正在打开本机麦克风");
    else {
      display("取消开启", false);
      document.getElementById("interrupt").disabled = false;
      status(next ? "已唤醒，正在打开麦克风…" : "正在打开麦克风…");
    }
    const previous = inputTask;
    const task = inputTask = (async () => {
      await previous;
      if (!canContinue(session)) return;
      if (!navigator.locks) return openInput(session, background);
      try {
        await navigator.locks.request("moss-microphone", {ifAvailable:true}, async lock => {
          if (!canContinue(session)) return;
          if (!lock) {
            const note = "另一个 MOSS 标签正在使用麦克风。关闭那页的录音或唤醒后可重试。";
            reset(session);
            if (background) { disableWake(); showWake(note); }
            status(note);
            return;
          }
          const ended = new Promise(resolve => { session.endInput = resolve; });
          // 就算先点击了取消，也要等浏览器结束这次授权请求并收回可能返回的轨道。
          await openInput(session, background);
          await ended;
        });
      } catch (error) {
        if (owns(session)) {
          reset(session);
          if (background) disableWake();
          status("无法取得本页的麦克风使用权：" + error.message);
        }
      }
    })();
    await task;
    if (inputTask === task) inputTask = null;
  }
  async function openInput(session, background) {
    try {
      const {stream, track, deviceId} = await openLegacyStream(inputs.value);
      if (!canContinue(session)) {
        stream.getTracks().forEach(t => t.stop());
        abortOpen(session);
        return;
      }
      session.stream = stream;
      session.activeDeviceId = deviceId;
      // 授权后让本机识别器与录音同时启动。准备失败时，提交录音仍会尝试启动。
      void prepareInput();
      const context = session.context = new AudioContext();
      await context.audioWorklet.addModule("/static/voice-capture.js");
      if (!canContinue(session)) { abortOpen(session); return; }
      const source = session.source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser(); analyser.fftSize = 512;
      source.connect(analyser);
      const frequencies = new Uint8Array(analyser.frequencyBinCount);
      const node = session.node = new AudioWorkletNode(context, "moss-voice-capture");
      const silent = context.createGain(); silent.gain.value = 0;
      source.connect(node).connect(silent).connect(context.destination);
      await context.resume();
      if (!canContinue(session)) { abortOpen(session); return; }
      clearTimeout(session.openingTimer);
      session.phase = "recording";
      track.addEventListener("ended", async () => {
        if (!owns(session)) return;
        const captureKind = session.kind;
        let nextBest = null;
        try {
          const devs = await navigator.mediaDevices?.enumerateDevices?.() || [];
          const real = devs.filter(d => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default"
            && d.deviceId !== "communications" && d.deviceId !== session.activeDeviceId);
          nextBest = real.find(d => !legacyVirtualMic(d.label || "")) || real[0] || null;
        } catch {}
        if (nextBest && (captureKind === "wake" || captureKind === "conversation")) {
          cancel(`原麦克风已断开，已自动切换为「${nextBest.label || "可用设备"}」。`);
          if (captureKind === "wake") resumeWake(150);
          else void begin("conversation");
          return;
        }
        disableWake();
        cancel();
        status("麦克风已断开，本次录音已取消。");
      }, {once:true, signal:session.aborter.signal});
      inputs.setTrack(track);
      const conversation = session.kind === "conversation";
      if (conversation && session.endInput) onConversationGranted?.(session.aborter.signal);
      node.port.onmessage = ({data}) => {
        if (data === "flushed") { session.flush?.(); return; }
        if (background && performance.now() < (session.quietUntil ?? 0)) return;
        session.blocks.push(data);
        if (session.phase !== "recording") return;
        const wasHeard = session.endpoint.heard;
        const result = session.endpoint.push(data, context.sampleRate);
        if (!wasHeard && session.endpoint.heard) {
          session.calendarTurn = pendingCalendar?.() ?? null;
          session.replyTurn = speaking ? null : replaceableReply?.() ?? null;
        }
        if (!background) {
          analyser.getByteFrequencyData(frequencies);
          draw(data, frequencies, context.sampleRate, analyser);
          if (!result && !session.outputPrepared && !speaking && session.endpoint.voicedSeconds >= .7 && owns(session)) {
            session.outputPrepared = Boolean(onActualSpeech?.(
              conversation && session.endInput ? session.aborter.signal : undefined));
          }
        }
        if (background || conversation) {
          if (!wasHeard && session.endpoint.heard) session.awakeCapture = performance.now() < session.awakeUntil;
          if (!session.endpoint.heard && session.blocks.length > Math.ceil(.55*context.sampleRate/data.length)) session.blocks.shift();
          if (session.awakeUntil && performance.now() >= session.awakeUntil && !session.endpoint.heard) {
            session.awakeUntil = 0;
            showWake("等待唤醒");
          }
          if (result === "done") {
            const clip = capturedClip(session);
            resetCapture(session);
            recognizeWake(session, clip, context.sampleRate);
          } else if (result === "idle") {
            resetCapture(session);
          } else {
            previewConversation(session, data, context.sampleRate);
            probeSpokenStop(session, context.sampleRate);
          }
        } else {
          if (result === "done") recognize(session);
          if (result === "idle") { cancel("没有听到说话，本次语音输入已结束。"); resumeWake(); }
        }
      };
      if (background) {
        if (speaking) readyForInterruption();
        else showWake("等待唤醒");
      }
      else {
        display(conversation ? "关闭麦克风" : "正在听", true);
        document.getElementById("interrupt").disabled = false;
        if (session.kind === "native-next") onNextListening?.();
        status(conversation ? "语音对话已开启。说完停顿片刻即可提交。" : "说完停顿片刻，MOSS 自动识别。");
      }
    } catch (error) {
      if (owns(session)) {
        const note = error.name === "NotAllowedError" ? "浏览器未授予麦克风权限，仍可输入文字。" : "麦克风未启动：" + error.message;
        if (background) { disableWake(); showWake(note); }
        status(note);
      }
      reset(session);
    }
  }
  function disableWake() {
    clearTimeout(resumeTimer);
    wake.checked = false;
    interrupt.checked = false;
    interrupt.disabled = true;
    if (current?.kind === "wake") cancel();
    showWake("开启后说“你好，莫斯”，可接着说指令。");
  }
  wake.addEventListener("change", () => {
    if (!wake.checked) return disableWake();
    if (!["localhost","127.0.0.1","[::1]"].includes(location.hostname)) {
      disableWake(); showWake("请在本机打开 MOSS 以启用语音唤醒。"); return;
    }
    interrupt.disabled = false;
    if (speaking && !interrupt.checked) showWake("播报中，暂时停止监听");
    else resumeWake();
  });
  interrupt.addEventListener("change", () => {
    if (!speaking) return;
    if (interrupt.checked) {
      resumeWake();
      readyForInterruption();
    }
    else {
      if (current?.kind === "wake") cancel();
      showWake("播报中，暂时停止监听");
    }
  });
  for (const event of ["pending","start"]) window.addEventListener("moss-speech-"+event, ({detail}) => {
    const newPlayback = !speaking;
    if (newPlayback) {
      captureGeneration++;
      playbackSegments.length = 0;
      finishedSpeech = null;
    }
    speaking = true;
    if (newPlayback && current?.kind === "conversation" && !current.endpoint.heard) resetCapture(current);
    if (current?.kind === "native-next") cancel("开始播报，本次接续录音已结束。");
    if (event === "start" && detail?.text) {
      const from = performance.now();
      playbackSegments.push({from, to:from + detail.seconds*1000, text:normalizeSpoken(detail.text),
                             id:detail.id, category:detail.category});
    }
    clearTimeout(resumeTimer);
    if (!wake.checked) return;
    if (!interrupt.checked) {
      if (current?.kind === "wake") cancel();
      showWake("播报中，暂时停止监听");
    } else {
      if (current?.kind === "wake") {
        if (newPlayback) {
          current.awakeUntil = 0;
          current.next = null;
          resetCapture(current);
        }
      } else resumeWake();
      if (newPlayback) readyForInterruption();
    }
  });
  for (const event of ["end","stop","error","skipped"]) window.addEventListener("moss-speech-"+event, () => {
    if (!speaking) return;
    if (current?.kind === "conversation") {
      speaking = false;
      captureGeneration++;
      current.echoUntil = performance.now() + ECHO_TAIL_MS;
      if (!current.endpoint.heard) resetCapture(current);
      return;
    }
    const session = current?.kind === "wake" && interrupt.checked ? current : null;
    const tail = session && event === "end" && session.endpoint.heard ? capturedClip(session) : null;
    // 自然播完时，保留已开口的短句。它只能打开接话窗口，无法影响下一次播报。
    finishedSpeech = session && event === "end"
      ? {generation:captureGeneration, until:performance.now() + 1800} : null;
    speaking = false;
    captureGeneration++;
    playbackSegments.length = 0;
    if (session) {
      session.awakeUntil = 0;
      session.quietUntil = performance.now() + 250;
      resetCapture(session);
      showWake("等待唤醒");
      if (tail) recognizeWake(session, tail, session.context.sampleRate);
    } else resumeWake(420);
  });
  button.addEventListener("click", () => begin());
  window.addEventListener("pagehide", () => { disableWake(); cancel(); });
  function checkNativeNext() {
    if (current?.kind === "native-next") canContinue(current);
  }
  window.addEventListener("blur", checkNativeNext);
  document.addEventListener("visibilitychange", checkNativeNext);
  document.getElementById("message").addEventListener("input", event => { if (event.isTrusted) checkNativeNext(); });

  async function listenAfterWake(eligible) {
    if (!eligible() || current || speaking || wake.checked) return;
    let permission;
    try { permission = await navigator.permissions?.query({name:"microphone"}); } catch {}
    if (!eligible() || current || speaking || wake.checked) return;
    if (permission?.state !== "granted" || !navigator.locks) {
      status("已唤醒。可点击“开启麦克风”说话。");
      return;
    }
    void begin("native-next", {eligible, permission});
  }

  return {stop:note=>{ disableWake(); cancel(note); }, listenAfterWake,
    stopNativeNext:() => { if (current?.kind === "native-next") cancel("本次接续录音已结束。"); },
    active:() => current?.kind === "conversation" || current?.kind === "native-next"};
}
