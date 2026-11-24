import {conversationSpeech, explicitRecitation, expressiveText, isVerseCategory, speechSegments} from "./speech-delivery.js?v=v5-erhua";
import {speechRoomAmount} from "./speech-space.js?v=speech-space";

function playbackPlan(text, category, narration) {
  if (!narration) return {sentences:speechSegments(text, category), finalPause:0};
  const sentences = [];
  let pause = 0;
  for (const cue of narration) {
    if (cue.pause_ms) {
      pause += cue.pause_ms;
      continue;
    }
    const spoken = speechSegments(cue.text, category);
    if (!spoken.length) continue;
    spoken[0].pauseBefore = pause;
    pause = 0;
    sentences.push(...spoken);
  }
  return {sentences, finalPause:pause};
}

function waitForPause(ms, signal) {
  return new Promise(resolve => {
    let timer;
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    if (signal.aborted) finish();
    else {
      signal.addEventListener("abort", finish, {once:true});
      timer = setTimeout(finish, ms);
    }
  });
}

function waitForAudioResume(resume, signal) {
  return new Promise(resolve => {
    const timer = setTimeout(finish, 1000);
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    if (signal.aborted) finish();
    else {
      signal.addEventListener("abort", finish, {once:true});
      void resume.then(finish);
    }
  });
}

function leadingReplySilence(audio) {
  const rate = audio.sampleRate;
  let silentFrames = Math.min(audio.length, Math.floor(rate * .12));
  for (let channel = 0; channel < audio.numberOfChannels; channel++) {
    const pcm = audio.getChannelData(channel);
    for (let frame = 0; frame < silentFrames; frame++) {
      if (pcm[frame] === 0) continue;
      silentFrames = frame;
      break;
    }
  }
  // Retain enough exact silence for the existing four-millisecond gain ramp.
  const skipped = silentFrames - Math.ceil(rate * .012);
  return skipped >= rate * .02 ? skipped / rate : 0;
}

function quietEdges(audio) {
  const rate = audio.sampleRate;
  const window = Math.max(1, Math.round(rate * .01));
  const channels = Array.from({length:audio.numberOfChannels}, (_, i) => audio.getChannelData(i));
  const blocks = Math.ceil(audio.length / window);
  function quiet(block) {
    const start = block * window, end = Math.min(start + window, audio.length);
    for (const pcm of channels) {
      let power = 0;
      for (let i = start; i < end; i++) {
        if (Math.abs(pcm[i]) >= .0025) return false;
        power += pcm[i] * pcm[i];
      }
      if (power >= (end - start) * .0005 ** 2) return false;
    }
    return true;
  }
  let first = 0, last = blocks - 1;
  while (first < blocks && quiet(first)) first++;
  while (last > first && quiet(last)) last--;
  if (first >= last) return {head:0, tail:0};
  // 房间混响和轻辅音在任一声道出现，都保留该段及两侧余量。
  const head = Math.min(.18, first * window / rate - .075);
  const tail = Math.min(.32, (audio.length - (last + 1) * window) / rate - .15);
  return {head:head >= .02 ? head : 0, tail:tail >= .02 ? tail : 0};
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
  if (parts.length < 2) return [{at:0, text}];

  // 每 20 毫秒测一次音量，以约 6 kHz 抽样识别停顿。
  const rate = audio.sampleRate, pcm = audio.getChannelData(0).subarray(Math.round(offset * rate));
  const frame = Math.max(1, Math.round(rate * .02)), stride = Math.max(1, Math.floor(rate / 6000));
  const levels = [];
  for (let from = 0; from < pcm.length; from += frame) {
    const to = Math.min(from + frame, pcm.length);
    let power = 0, count = 0;
    for (let i = from; i < to; i += stride) { power += pcm[i] * pcm[i]; count++; }
    levels.push(Math.sqrt(power / count));
  }
  const ordered = [...levels].sort((a,b) => a-b);
  // 允许把换气声和较轻的尾音识别为句间停顿。
  const gate = Math.max(.0002, Math.min(.014, ordered[Math.floor(ordered.length*.9)] * .10));
  const first = levels.findIndex(value => value > gate), last = levels.findLastIndex(value => value > gate);
  if (first < 0 || first === last) return [{at:0, text}];
  const frameSeconds = frame / rate, pauses = [];
  let voiced = first;
  for (let i = first+1; i <= last; i++) {
    if (levels[i] <= gate) continue;
    const duration = (i-voiced-1)*frameSeconds;
    if (duration >= .08) pauses.push({mid:(voiced+1+i)*frameSeconds/2, at:i*frameSeconds-.04});
    voiced = i;
  }
  const weights = parts.map(part => Math.max(1, [...part.replace(/[^\p{L}\p{N}]/gu,"")].length));
  const secondsPerUnit = (last+1-first)*frameSeconds / weights.reduce((sum,n) => sum+n,0);
  let target = first*frameSeconds, startAt = 0, group = 0;
  const cues = [];
  for (let i = 0; i < parts.length-1; i++) {
    target += weights[i]*secondsPerUnit;
    const radius = Math.min(1.5, .5*Math.min(weights[i],weights[i+1])*secondsPerUnit);
    let distance = radius, nearest;
    for (const pause of pauses) {
      const delta = Math.abs(pause.mid-target);
      if (pause.at > startAt && delta <= distance) { nearest = pause; distance = delta; }
    }
    if (!nearest) continue; // 没有明确停顿时，两句合并显示。
    cues.push({at:startAt, text:parts.slice(group,i+1).join("").trim()});
    startAt = nearest.at;
    group = i+1;
  }
  cues.push({at:startAt, text:parts.slice(group).join("").trim()});
  return cues;
}

export function activateSpeech(status, sampleVoice, onChatChange) {
  const speaker = document.getElementById("speaker-state");
  const interrupt = document.getElementById("interrupt");
  const replay = document.createElement("button");
  replay.type = "button";
  replay.textContent = "重播播报";
  replay.hidden = true;
  interrupt.before(replay);
  const volumeControl = document.createElement("label");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = localStorage.getItem("moss.voice.enabled") !== "off";
  volumeControl.append(enabled, " MOSS 语音播报");
  document.querySelector(".preference-panel")?.append(volumeControl);
  let context, resuming, owner, held, prepared, last, player, active = false;
  let openingOutput, optionalSpeechId, optionalSpeechCounter = 0;
  const composer = document.getElementById("message");
  let speechPrepare = false, draftPrepared = false, draftTimer, draftChat, draftBusy = true, composingDraft;
  let speechMicWarm = false, speechMicWarmOnActivity = false, micWarm;
  let importsAt = -Infinity;
  let sampling;
  const observed = new Set();
  function emit(name, detail) {
    window.dispatchEvent(new CustomEvent("moss-speech-" + name, {detail}));
  }
  function stopSampling() {
    clearInterval(sampling);
    sampleVoice(null);
  }

  function unlock() {
    context ??= new AudioContext();
    if (context.state === "running") return;
    const resume = context.resume().catch(() => {});
    resuming = resume;
    void resume.then(() => { if (resuming === resume) resuming = null; });
  }
  function prepareOutput({deferred = false} = {}) {
    if (!enabled.checked) return;
    if (deferred && !context) {
      openingOutput ??= new Promise(resolve => setTimeout(() => {
        prepareOutput();
        openingOutput = undefined;
        resolve();
      }, 0));
      return;
    }
    try { unlock(); }
    catch { /* The playback path reports an unavailable audio context. */ }
  }
  async function prepareOptionalSpeech(canPlay, signal) {
    if (!enabled.checked) return "disabled";
    if (active || signal?.aborted || !canPlay()) return "busy";
    if (context?.state !== "running" && navigator.userActivation?.hasBeenActive !== true) return "activation";
    prepareOutput();
    if (context?.state !== "running" && resuming) {
      await waitForAudioResume(resuming, signal || new AbortController().signal);
    }
    if (!enabled.checked) return "disabled";
    if (active || signal?.aborted || !canPlay()) return "busy";
    if (context?.state !== "running") return "unavailable";
    return "ready";
  }
  async function playWelcome(text, canPlay) {
    const status = await prepareOptionalSpeech(canPlay);
    if (status !== "ready") return status;
    optionalSpeechId = `staff-welcome-${++optionalSpeechCounter}`;
    void render(optionalSpeechId, text, "reply", false, undefined, undefined, true);
    return "started";
  }
  async function playSpacePreview(text, roomAmount, signal, canPlay) {
    const status = await prepareOptionalSpeech(canPlay, signal);
    if (status !== "ready") return status;
    const id = optionalSpeechId = `space-preview-${++optionalSpeechCounter}`;
    function cancel() { if (active && last?.id === id) stop(); }
    signal.addEventListener("abort", cancel, {once:true});
    try {
      return await render(id, text, "reply", false, undefined, undefined, true, roomAmount) === true;
    } finally {
      signal.removeEventListener("abort", cancel);
    }
  }
  function discardPrepared() {
    prepared?.aborter.abort();
    prepared = null;
  }
  function stop() {
    discardPrepared();
    held?.aborter.abort();
    held = null;
    stopSampling();
    if (player) { try { player.stop(); } catch {} player = null; }
    if (active) {
      speaker.textContent = "待机";
      emit("stop", {id:last?.id});
    }
    active = false;
  }
  function stopOptionalSpeech() {
    if (active && last?.id === optionalSpeechId) stop();
  }
  function interruptTurn() {
    owner = null;
    stop();
    replay.hidden = true;
  }
  function prepareForReading(text) {
    const input = (text || "").trimStart();
    return explicitRecitation(input) ||
      /^(?:(?:请(?:你|您)?|帮我|麻烦(?:你|您)?)[\s，,]*)*(?:(?:朗读|朗诵|念|读出|读)(?:一下)?(?:(?:以下|下面)的?(?:这段)?|这段)(?:文字|内容|正文|话)?|把(?:(?:以下|下面)的?(?:这段)?|这段)(?:文字|内容|正文|话)?(?:朗读|朗诵|念|读)给我听)\s*[：:]\s*\S/u.test(input);
  }
  function prepareForDeviceQuery(text) {
    const parts = text.normalize("NFKC").split(/[,、;。!?\n]+/u).map(part => part.trim()).filter(Boolean);
    const clock = /^(?:(?:现在|这会儿|此刻)(?:几点(?:钟)?(?:了|啦)?|的?时间)|(?:今天|今日)(?:是)?(?:几号|几月几[日号]|星期几|周几))[啊呀呢吗么]?$/u;
    const battery = /^(?:(?:我的?|这台)?(?:笔记本(?:电脑)?|电脑)的?)?(?:当前|现在|目前|这会儿)?(?:(?:(?:电池)?电量|电池状态)(?:还有多少|是多少|多少|多大)?|(?:还有|还剩|剩余|剩|有)?多少电(?:量)?|(?:有没有|是否)在充电)[啊呀呢吗么]?$/u;
    const volume = /^(?:(?:当前|现在|目前)的?)?(?:(?:本机|电脑|系统)(?:上|的)?|总)?(?:音量(?:是|有|为)?(?:多大|多少)|声音(?:有)?多大)[啊呀呢吗么]?$/u;
    return parts.length > 0 && parts.every(part => {
      const query = part.replace(/^(?:(?:请你?|帮我|告诉我|顺便|查一下|看看|查看)\s*)+/u, "");
      return clock.test(query) || battery.test(query) || volume.test(query);
    });
  }
  function requestImports() {
    const now = performance.now();
    if (now - importsAt < 8000) return;
    importsAt = now;
    fetch("/api/prepare-speech", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}).catch(() => {
      if (importsAt === now) importsAt = -Infinity;
    });
  }
  function canPrepareFromVoice() {
    return speechPrepare && enabled.checked && !active && !document.hidden && document.hasFocus() &&
      !document.querySelector("dialog[open]:not(#weather-panel[data-auto-result]):not(#local-status-panel[data-auto-result])");
  }
  function prepareForAcceptedWake() {
    if (canPrepareFromVoice() && !draftBusy) requestImports();
  }
  function prepareForSpokenInput(signal) {
    if (!canPrepareFromVoice() || context?.state !== "running") return false;
    if (signal && speechMicWarmOnActivity && !draftBusy) {
      const audibleMedia = [...document.querySelectorAll(".media-audio")].some(media =>
        !media.paused && !media.ended && !media.muted && media.volume > 0);
      if (!audibleMedia && prepareForConversation(signal, true)) return true;
    }
    requestImports();
    return true;
  }
  function releaseConversation(id) {
    void fetch("/api/prepare-speech", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({mic:id, release:true}), keepalive:true}).catch(() => {});
  }
  function stopConversationPreparation() {
    if (!micWarm) return;
    const {id, signal} = micWarm;
    micWarm = null;
    signal.removeEventListener("abort", stopConversationPreparation);
    releaseConversation(id);
  }
  function prepareForConversation(signal, onActivity = false) {
    const now = performance.now();
    if (onActivity && micWarm && now - micWarm.requestedAt >= 25000) stopConversationPreparation();
    if (!speechMicWarm || !enabled.checked || context?.state !== "running" || active ||
        speechMicWarmOnActivity !== onActivity || signal.aborted || micWarm ||
        document.hidden || !document.hasFocus()) return false;
    const lease = {id:crypto.randomUUID().replaceAll("-", ""), signal, requestedAt:now};
    micWarm = lease;
    signal.addEventListener("abort", stopConversationPreparation, {once:true});
    const headers = {"Content-Type":"application/json"};
    if (onActivity) headers["X-MOSS-Voice-Activity"] = "1";
    void fetch("/api/prepare-speech", {method:"POST", headers,
      body:JSON.stringify({mic:lease.id})}).then(response => {
      // 关麦请求可能先到，准备请求完成后再释放同一个租约。
      if (micWarm !== lease) {
        if (response.ok) releaseConversation(lease.id);
      } else if (!response.ok || (onActivity && response.headers.get("X-MOSS-Mic-Prepared") === "0")) {
        stopConversationPreparation();
        if (onActivity) requestImports();
      }
    }).catch(() => {
      if (micWarm === lease) stopConversationPreparation();
    });
    return true;
  }
  function canPrepareDraft() {
    return speechPrepare && enabled.checked && !active && !draftBusy && draftChat &&
      !document.hidden && document.hasFocus() && document.activeElement === composer;
  }
  function clearDraftPreparation() {
    clearTimeout(draftTimer);
    composingDraft = undefined;
  }
  function scheduleDraftPreparation(event) {
    clearTimeout(draftTimer);
    const text = composer.value;
    if (!text.trim()) draftPrepared = false;
    if (event.isComposing) {
      if (event.isTrusted) composingDraft = text;
      return;
    }
    const committed = event.type === "compositionend" && composingDraft === text;
    composingDraft = undefined;
    const chat = draftChat;
    if ((!event.isTrusted && !committed) || draftPrepared || !canPrepareDraft() ||
        !(prepareForReading(text) || prepareForDeviceQuery(text))) return;
    draftTimer = setTimeout(() => {
      if (chat !== draftChat || text !== composer.value || !canPrepareDraft()) return;
      draftPrepared = true;
      requestImports();
    }, 550);
  }
  composer?.addEventListener("input", scheduleDraftPreparation);
  composer?.addEventListener("compositionend", scheduleDraftPreparation);
  composer?.addEventListener("blur", clearDraftPreparation);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) return;
    stopOptionalSpeech();
    clearDraftPreparation();
    stopConversationPreparation();
    if (prepared?.localReading) discardPrepared();
  });
  enabled.addEventListener("change", () => {
    localStorage.setItem("moss.voice.enabled", enabled.checked ? "on" : "off");
    clearDraftPreparation();
    draftPrepared = false;
    if (enabled.checked) unlock();
    else {
      stopConversationPreparation();
      stop();
    }
  });
  function userAction(action, payload, state, {deferOutput = false} = {}) {
    clearDraftPreparation();
    draftPrepared = false;
    if (["cancel","new-chat","switch-chat"].includes(action)) {
      if (action !== "cancel") onChatChange?.();
      interruptTurn(); last = null; return;
    }
    if (action !== "message" && action !== "approve") return;
    stop();
    if (!deferOutput) prepareOutput();
    replay.hidden = true;
    owner = {chat:state.chat_id, id:payload.turn_id, text:payload.text, previous:new Set(state.turns.map(t => t.id))};
    // 预导入请求不传文字。真正收到播报请求后才加载权重。
    if (action === "message" && enabled.checked && state.speech_prepare === true && prepareForReading(payload.text)) {
      owner.importsRequested = true;
      requestImports();
    }
  }
  function owns(turn, chat) {
    if (!owner || owner.chat !== chat) return false;
    if (!owner.id && turn.user === owner.text && !owner.previous.has(turn.id)) owner.id = turn.id;
    return owner.id === turn.id;
  }
  function voiceBody(fields, roomAmount) {
    return JSON.stringify(roomAmount === 100 ? fields : {...fields, room_amount:roomAmount});
  }
  function requestVoice(text, category, preferCached, signal, roomAmount) {
    return fetch("/api/speak", {method:"POST", headers:{"Content-Type":"application/json"},
      body:voiceBody({text, category, prefer_cached:preferCached}, roomAmount), signal});
  }
  function prepareLeadingSpeech(turn) {
    if (!turn.partial_reply) {
      discardPrepared();
      return;
    }
    if (owner.prepared || !enabled.checked || context?.state !== "running" ||
        turn.narration || explicitRecitation(turn.user)) return;
    const reading = turn.source === "context" ? turn.local_overview_speech_lead : null;
    if (reading && (document.hidden || !document.hasFocus())) return;
    if (!reading && turn.source !== "holo") return;
    const {category, text, expression} = conversationSpeech(reading ? {
      ...turn, status:"replied", local_overview:[reading]
    } : {...turn, reply:turn.partial_reply}, []);
    if (category !== "reply" && category !== "warm") return;
    // Raw errors and readings that could not be verified are left to the completed turn.
    if (reading && text === reading.reply) return;
    const segments = speechSegments(reading ? text : turn.partial_reply, category);
    const closedParagraph = Boolean(reading) || /\r?\n[ \t]*\r?\n[ \t]*$/u.test(turn.partial_reply);
    const leading = segments[0];
    if (!leading || (!closedParagraph && segments.length < 2) || !/[。！？!?；;.…]["”’」』）】》]*\s*$/u.test(leading.spoken)) return;
    const first = expressiveText(leading.spoken, expression);
    const aborter = new AbortController();
    const roomAmount = speechRoomAmount();
    const response = requestVoice(first, category, false, aborter.signal, roomAmount).then(async value => {
      if (value.status === 200 && value.headers.get("Content-Type")?.startsWith("audio/wav")) return value;
      await value.body?.cancel();
      return null;
    }).catch(() => null);
    owner.prepared = true;
    // 只提前合成，正式播报仍以本轮最终首段及空间档位一致为准。
    prepared = {id:turn.id, category, first, aborter, response, roomAmount, localReading:Boolean(reading)};
  }
  async function render(id, text, category = "reply", replaying = false, expression, narration, allowEdgeTrim,
                        roomAmount = speechRoomAmount()) {
    if (!enabled.checked || !text) return;
    const key = [id, category, text, expression, narration && JSON.stringify(narration)].join("\n");
    if (held?.key === key) return held.promise;
    if (!replaying && observed.has(key)) return;
    const {sentences, finalPause} = playbackPlan(text, category, narration);
    const first = sentences[0] && expressiveText(sentences[0].spoken, expression);
    const trim = allowEdgeTrim && category === "reply" && !expression &&
      sentences.length > 1 && first === sentences[0].spoken;
    const trimLead = allowEdgeTrim && category === "reply" && !expression && !narration && !replaying;
    const lead = !narration && !replaying && context?.state === "running" && prepared?.id === id &&
      prepared.category === category && prepared.first === first && prepared.roomAmount === roomAmount ? prepared : null;
    if (lead) prepared = null;
    stop();
    observed.add(key);
    const aborter = lead?.aborter ?? new AbortController();
    last = {id, text, key, category, expression, narration, allowEdgeTrim};
    replay.hidden = true;
    active = true;
    interrupt.disabled = false;
    speaker.textContent = "合成中";
    status("正在合成 MOSS 播报…");
    emit("pending", {id});
    let markStarted;
    let skipped = false;
    const started = new Promise(resolve => { markStarted = resolve; });
    const run = async () => {
      const ducked = new Map();
      function duckMusic(audio) {
        if (ducked.has(audio)) return;
        const original = audio.volume, level = Math.min(original, .15);
        let adjusted = false;
        const observeVolume = () => { if (audio.volume !== level) adjusted = true; };
        const restore = () => {
          if (ducked.get(audio) !== restore) return;
          ducked.delete(audio);
          audio.removeEventListener("volumechange", observeVolume);
          if (!adjusted && audio.volume === level) audio.volume = original;
        };
        audio.addEventListener("volumechange", observeVolume);
        ducked.set(audio, restore);
        audio.volume = level;
        return restore;
      }
      function restoreMusic() {
        for (const restore of ducked.values()) restore();
      }
      aborter.signal.addEventListener("abort", restoreMusic, {once:true});
      try {
        if (openingOutput) await openingOutput;
        if (context?.state !== "running" && resuming) {
          await waitForAudioResume(resuming, aborter.signal);
        }
        if (aborter.signal.aborted) return;
        if (context?.state !== "running") throw new Error("点击重播播报以启用声音。");
        const allowsPair = isVerseCategory(category) || ["reply", "warm"].includes(category);
        let batchAvailable = false;
        let batchFailed = false;
        async function prepare(index) {
          const {spoken, caption} = sentences[index];
          const voiceText = index === 0 && !isVerseCategory(category) ? expressiveText(spoken, expression) : spoken;
          const request = preferCached => requestVoice(voiceText, category, preferCached, aborter.signal, roomAmount);
          const automatic = category === "media" && !replaying && sentences.length === 1;
          const earlyResponse = index === 0 ? await lead?.response : null;
          if (aborter.signal.aborted) throw aborter.signal.reason;
          const resp = earlyResponse ?? await request(automatic);
          if (!resp.ok) throw new Error((await resp.json()).error || "MOSS 语音合成失败。");
          batchAvailable = allowsPair && resp.headers.get("X-MOSS-Batch-Capable") === "1";
          const shortCue = resp.headers.get("X-MOSS-Announcement") === "short";
          if (shortCue) {
            // 开播回执和通用短报幕都完成后，再为重播生成完整曲名。
            const warm = event => {
              if (event.detail.turnId !== id) return;
              window.removeEventListener("moss-media-start", warm);
              const finished = held?.key === key ? held.promise : null;
              void finished?.then(() => {
                if (!aborter.signal.aborted) return request(false).then(r => r.body?.cancel());
              }).catch(() => {});
            };
            window.addEventListener("moss-media-start", warm, {signal:aborter.signal});
          }
          const audio = await context.decodeAudioData(await resp.arrayBuffer());
          return {audio, caption, shortCue, cacheOnly:automatic};
        }
        async function preparePair(index) {
          const parts = sentences.slice(index, index+2);
          function singleSegments() {
            if (aborter.signal.aborted) throw aborter.signal.reason;
            batchFailed = true;
            return null;
          }
          const resp = await fetch("/api/speak-batch", {method:"POST", signal:aborter.signal,
            headers:{"Content-Type":"application/json"}, body:voiceBody({category, texts:parts.map(p=>p.spoken)}, roomAmount)});
          if ([404, 405, 503].includes(resp.status)) return singleSegments();
          if (!resp.ok) throw new Error((await resp.json()).error || "MOSS 后续段落合成失败。");
          try {
            const files = await resp.formData();
            const waves = parts.map((_, i) => files.get(String(i)));
            if (!waves.every(wav => wav instanceof Blob && wav.size)) return singleSegments();
            const prepared = await Promise.all(waves.map(async (wav, i) => ({
              audio:await context.decodeAudioData(await wav.arrayBuffer()),
              caption:parts[i].caption, shortCue:false
            })));
            batchAvailable = resp.headers.get("X-MOSS-Batch-Capable") === "1";
            return prepared;
          } catch {
            return singleSegments();
          }
        }
        function prefetch(promise) {
          const slot = {ready:false};
          slot.done = promise.then(value => { slot.value = value; }, error => { slot.error = error; })
            .finally(() => { slot.ready = true; });
          return slot;
        }
        const queue = sentences.length ? [prefetch(prepare(0))] : [];
        let audioEnd = null;
        async function pauseIfNeeded(ms) {
          const elapsed = audioEnd === null ? 0 : Math.max(0, (context.currentTime - audioEnd) * 1000);
          if (ms <= elapsed) return;
          speaker.textContent = "停顿";
          await waitForPause(ms - elapsed, aborter.signal);
        }
        function fillQueue(index, playingSeconds) {
          if (queue.length || index >= sentences.length) return;
          const parts = sentences.slice(index,index+2);
          if (batchAvailable && !batchFailed && parts.length === 2 && playingSeconds >= 2.5 &&
              parts.every(p=>p.spoken.length<=160) && parts[0].spoken.length + parts[1].spoken.length <= 240) {
            const pair = preparePair(index);
            const first = pair.then(value => value ? value[0] : prepare(index));
            const second = pair.then(value => value ? value[1] : first.then(() => prepare(index+1)));
            queue.push(prefetch(first), prefetch(second));
          } else {
            queue.push(prefetch(prepare(index)));
          }
        }
        for (let index=0; index<sentences.length; index++) {
          const next = queue.shift();
          if (index && !next.ready) {
            speaker.textContent = "合成中";
            emit("pending", {id, sentence:index+1, count:sentences.length});
          }
          await next.done;
          if (aborter.signal.aborted) return;
          if (next.error) throw next.error;
          const {audio, caption, shortCue, cacheOnly} = next.value;
          if (skipped) {
            status("已跳过本次报幕，可以手动重播。");
            emit("skipped", {id});
            return;
          }
          fillQueue(index+1, audio.duration);
          if (sentences[index].pauseBefore) {
            await pauseIfNeeded(sentences[index].pauseBefore);
            if (aborter.signal.aborted) return;
          }
          const src = context.createBufferSource();
          player = src;
          src.buffer = audio;
          const analyser = context.createAnalyser();
          analyser.fftSize = 4096;
          const edges = trim && !shortCue ? quietEdges(audio) : {head:0, tail:0};
          const nextSentence = sentences[index+1];
          const offset = sentences[index].pauseBefore ? 0 :
            index ? edges.head : trimLead && !shortCue ? leadingReplySilence(audio) : 0;
          const tail = nextSentence && !nextSentence.pauseBefore ? edges.tail : 0;
          const gain = offset || tail ? context.createGain() : null;
          if (gain) src.connect(gain).connect(analyser);
          else src.connect(analyser);
          analyser.connect(context.destination);
          const samples = new Float32Array(analyser.fftSize);
          const frequencies = new Uint8Array(analyser.frequencyBinCount);
          const ended = new Promise(resolve => { src.onended = resolve; });
          for (const audio of document.querySelectorAll(".media-audio")) if (!audio.paused) duckMusic(audio);
          const clock = context.currentTime;
          const fade = .004;
          if (offset) {
            gain.gain.setValueAtTime(0, clock);
            gain.gain.linearRampToValueAtTime(1, clock + fade);
          }
          src.start(0, offset);
          const duration = audio.duration - offset;
          audioEnd = clock + duration;
          markStarted({cacheOnly, duckTarget: audio => {
            if (!aborter.signal.aborted && player === src) return duckMusic(audio);
          }});
          speaker.textContent = "播报中";
          status("MOSS 正在播报");
          const fullCaption = shortCue ? "为你播放。" : caption;
          const cues = timedCaptions(fullCaption, category, audio, offset);
          let cue = 0;
          const timing = {id, seconds:duration, sentence:index+1, count:sentences.length,
                          text:fullCaption, shortCue, category};
          const successor = queue[0];
          if (tail && successor) {
            // 按原计划播放到下一段解码完成，避免合成尚未完成时截断当前混响。
            void successor.done.then(() => {
              if (successor.error || aborter.signal.aborted || player !== src) return;
              const stopAt = Math.max(clock + duration - tail, context.currentTime + .02);
              if (stopAt >= audioEnd - .015) return;
              gain.gain.setValueAtTime(1, stopAt - fade);
              gain.gain.linearRampToValueAtTime(0, stopAt);
              src.stop(stopAt);
              audioEnd = stopAt;
              timing.seconds = stopAt - clock;
            });
          }
          emit("start", {...timing, caption:cues[0].text});
          let lastProgress = -1;
          function sample() {
            analyser.getFloatTimeDomainData(samples);
            analyser.getByteFrequencyData(frequencies);
            sampleVoice(samples, frequencies, context.sampleRate, analyser);
            const position = Math.min(timing.seconds, context.currentTime - clock);
            const previous = cue;
            while (cue+1 < cues.length && position >= cues[cue+1].at) cue++;
            if (cue !== previous || position - lastProgress >= .24) {
              emit("progress", {...timing, position, caption:cues[cue].text});
              lastProgress = position;
            }
          }
          sampling = setInterval(sample, 60);
          sample();
          await ended;
          src.disconnect();
          gain?.disconnect();
          analyser.disconnect();
          if (player === src) { player = null; stopSampling(); }
          if (aborter.signal.aborted) return;
        }
        if (finalPause) {
          await pauseIfNeeded(finalPause);
          if (aborter.signal.aborted) return;
        }
        status("播报结束。");
        emit("end", {id});
        return true;
      } catch (e) {
        if (!aborter.signal.aborted) {
          status("MOSS 播报未完成。" + e.message);
          emit("error", {id, message:e.message});
        }
      } finally {
        markStarted(false);
        aborter.signal.removeEventListener("abort", restoreMusic);
        restoreMusic();
        if (!aborter.signal.aborted) {
          active = false;
          speaker.textContent = "待机";
          replay.hidden = false;
        }
      }
    };
    held = {key, aborter, started, skip:() => { skipped = true; }, promise:run()};
    return held.promise;
  }
  function observe(state) {
    if (draftChat !== state.chat_id) {
      clearDraftPreparation();
      draftPrepared = false;
      draftChat = state.chat_id;
    }
    draftBusy = state.turns.some(turn => ["thinking", "pending", "saving"].includes(turn.status));
    speechPrepare = state.speech_prepare === true;
    speechMicWarmOnActivity = state.speech_mic_warm_on_activity === true;
    speechMicWarm = state.speech_mic_warm === true && (!speechMicWarmOnActivity || state.model_id !== "local");
    if (!speechMicWarm) stopConversationPreparation();
    const index = owner?.id ? state.turns.findIndex(item => item.id === owner.id) : state.turns.length - 1;
    const turn = state.turns[index];
    if (prepared && (prepared.id !== turn?.id || owner?.chat !== state.chat_id)) discardPrepared();
    if (!turn || !owns(turn, state.chat_id)) return;
    if (turn.status === "cancelled") { stop(); return; }
    if (turn.status === "thinking") {
      if (speechPrepare && !owner.importsRequested && turn.source === "holo" &&
          enabled.checked && context?.state === "running" && !document.hidden) {
        owner.importsRequested = true;
        requestImports();
      }
      prepareLeadingSpeech(turn);
    }
    const history = index === state.turns.length - 1 ? state.turns : state.turns.slice(0, index + 1);
    const {category, text, expression, narration} = conversationSpeech(turn, history);
    const allowEdgeTrim = !turn.media && !turn.transport && !["media", "transport"].includes(turn.source);
    if (text) render(turn.id, text, category, false, expression, narration, allowEdgeTrim);
    else if (turn.status !== "thinking") discardPrepared();
  }
  replay.addEventListener("click", () => {
    if (!last) return;
    unlock();
    observed.delete(last.key);
    stop();
    render(last.id, last.text, last.category, true, last.expression, last.narration, last.allowEdgeTrim);
  });
  document.getElementById("mic").addEventListener("click", () => {
    stopOptionalSpeech();
    if (enabled.checked) unlock();
  });
  window.addEventListener("pagehide", () => { clearDraftPreparation(); stopConversationPreparation(); stop(); });
  return {userAction, observe, stop, interruptTurn, prepareForSpokenInput, prepareForAcceptedWake, prepareForConversation,
    prepareOutput, playWelcome, playSpacePreview, stopFor(id) {
    if (last?.id === id) stop();
    else if (prepared?.id === id) discardPrepared();
  }, active:() => active,
    beforeMedia:async ({turnId, chatId, title, announcement, allowCachedOverlap = false}) => {
      if (!owner || owner.id !== turnId || owner.chat !== chatId) return;
      if (!held || last?.id !== turnId) render(turnId, announcement || `为你播放《${title}》。`, "media");
      const current = last?.id === turnId ? held : null;
      if (!current) return;
      const speaking = await Promise.race([current.started, new Promise(r => setTimeout(() => r(false), 1200))]);
      if (!speaking) { current.skip(); return; }
      if (allowCachedOverlap && speaking.cacheOnly) return speaking.duckTarget;
      await current.promise;
    }};
}
