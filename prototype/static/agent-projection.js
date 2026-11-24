import {bindProjection} from "/conversation-assets/projection-motion.js";
import {mossLegacyMusic} from "/static/agent-legacy-music.js?v=fui-signal";
import {mossLegacyVideo} from "/static/agent-video.js";
import {restoreMediaSource} from "./agent-transport.js";

export function installProjection({narrow, mediaPresented, leaveMedia}) {
  const get = id => document.getElementById(id);
  const panel = get("tools-panel"), results = get("tool-results"), player = get("tool-player");
  const pin = get("tools-pin");
  let active, audible, generation = 0, sent, voiceChat, pendingSelection, pendingStart, desktopOpener;
  const audioViews = new WeakMap();
  const transport = get("projection-transport"), now = get("music-now"), voice = get("projection-speech");
  const clock = t => Number.isFinite(t) ? Math.floor(t/60)+":"+String(Math.floor(t%60)).padStart(2,"0") : "";
  const phaseLabel = document.createElement("span"), elapsed = document.createElement("span");
  elapsed.setAttribute("aria-hidden","true"); transport.append(phaseLabel,elapsed);
  function updateTransport(phase, title, kind = active?.projected?.selected?.kind) {
    const label = (kind === "video" ? "视频" : "音乐") + " · " + phase + (title ? "《" + title + "》" : "");
    if (phaseLabel.textContent !== label) phaseLabel.textContent = label;
    const playing = audible?.audio && !audible.audio.paused && audible !== active;
    const badge = playing ? (audible.audio.localName === "video" ? "视频" : "音乐") + " · 正在播放《" + audible.projected.selected.title + "》" : label;
    if (now.textContent !== badge) now.textContent = badge;
    transport.hidden = now.hidden = false;
    const a = active?.audio;
    const progress = !pendingSelection && title && a && Number.isFinite(a.duration) ? "　" + clock(a.currentTime) + " / " + clock(a.duration) : "";
    if (elapsed.textContent !== progress) elapsed.textContent = progress;
  }
  function music(view, phase) {
    if (view !== active || pendingSelection) return;
    if (phase) view.transportState = phase;
    updateTransport(view.transportState || "已选中",view.projected?.selected?.title);
  }
  const key = value => String(value || "").normalize("NFKC").trim().toLowerCase();
  function showPending(text,turns=[]) {
    if (/[？?]|(?:叫什么|谁唱|哪首|哪一首|什么歌|别|不要|不想|是否|能否|保存|草稿)/u.test(text)) return;
    const label = /^([^·•\n，,。！？?]{1,72}?)\s+[·•]\s+([^·•\n，,。！？?]{1,48})$/u.exec(text.trim());
    const artist = /^(?:(?:请|麻烦|帮我)\s*)*(?:(?<play>(?:给我)?来(?:一[首手]|几首|首))|(?:只看看|只看|看看|只列出|列出))\s*[^，,；;。！？?\n]{1,64}的(?:歌|歌曲|音乐)[。！!]?$/u.exec(text.trim());
    const items = turns.findLast(t=>t.media?.items?.length)?.media.items || [];
    const ordinal = items.length && /^(?:(?:换|换成|切换到)\s*)?第?\s*[零一二两三四五六七八九十\d]+\s*(?:首|个|条)?[。！!]?$/u.test(text.trim());
    if (!label && !ordinal && !artist && !/^(?:(?:请|麻烦|那就|帮我|给我|再)\s*)*(?:我?想听|我?要听|我听|试听|播放|换(?:一首|一个|成|到|首|个)|下(?:一)?首|上一首|放这|就这首|我?想看)/u.test(text)) return;
    const video = /视频|影片|电影|想看《/u.test(text) || (ordinal && items[0]?.kind === "video");
    const known = label && items.some(i=>key(i.title)===key(label[1]) && key(i.artist)===key(label[2]));
    if (artist ? artist.groups.play : !label || known) generation++;
    pendingSelection = true;
    voice.hidden = true;
    motion.show({manual:true,timed:false});
    panel.setAttribute("data-media-pending","");
    get("tool-title").textContent = video ? "影像资料" : "音乐检索";
    mediaPresented(false);
    updateTransport(video ? "正在打开视频…" : known || (!artist && !label && items.length) ? "正在选曲…" : "正在检索曲目…",label?.[1],video ? "video" : "audio");
  }
  const motion = bindProjection(panel, {onHidden:() => {
    desktopOpener = null;
    active?.audio?.pause();
    if (player.querySelector("iframe")) player.replaceChildren();
    pin.setAttribute("aria-pressed","false");
    pin.textContent = "固定";
  }});
  function advancePlayback() {
    pendingStart?.aborter?.abort();
    return ++generation;
  }
  function manual() {
    advancePlayback();
    window.mossSpeech?.stop?.();
  }
  function close() {
    advancePlayback();
    pendingSelection = false;
    active?.audio?.pause();
    motion.hide({immediate:true});
  }
  function reset() {
    close();
    sent = null; active = null; audible = null; newestTurn = null;
    results.replaceChildren(); player.replaceChildren();
    delete panel.dataset.turnId;
    transport.hidden = voice.hidden = now.hidden = true;
    speechNotes.clear(); shortCues.clear();
  }
  function releaseOtherMedia(current, switchingTrack) {
    for (const other of document.querySelectorAll(".media-audio")) {
      if (other === current || (!switchingTrack && !other.paused)) continue;
      if (switchingTrack) other.pause();
      const source = other.getAttribute("src");
      if (!source || (other.preload === "none" && !other.readyState && !other.currentSrc)) continue;
      // 释放历史音轨的下载连接，保留手动重听时的播放位置。
      other.dataset.parkedSrc = source;
      other.dataset.parkedTime = String(other.ended ? 0 : other.currentTime);
      other.preload = "none";
      other.removeAttribute("src");
      other.load();
      const view = audioViews.get(other);
      if (view?.disposePlayer) {
        view.disposePlayer();
        view.disposePlayer = null;
        view.playerControls = view.playerHeading = null;
      }
    }
  }
  function focusable(element) {
    return element instanceof HTMLElement && element.isConnected && !element.closest("[hidden],[inert]") &&
      !element.matches(":disabled") && element.getClientRects().length > 0;
  }
  function present(explicit) {
    mediaPresented(explicit);
    if (!explicit || narrow.matches) return;
    const trigger = document.activeElement;
    if (trigger !== document.body && !panel.contains(trigger) && focusable(trigger)) desktopOpener = trigger;
    desktopOpener ??= active?.projectButton || now;
    get("tools-close").focus({preventScroll:true});
  }
  function open(view, explicit = false) {
    restoreMediaSource(view.audio);
    if (active !== view && active?.audio?.localName === "video") active.audio.pause();
    if (active !== view) releaseOtherMedia(view.audio, false);
    active = view;
    pendingSelection = false;
    panel.dataset.turnId = view.article.dataset.turnId;
    const spoken = speechNotes.get(panel.dataset.turnId);
    voice.hidden = !spoken;
    if (spoken) displaySpeech(panel.dataset.turnId,spoken);
    const {items,selected} = view.projected;
    const kind = selected?.kind || items[0]?.kind;
    get("tool-title").textContent = {audio:"音乐检索", video:"影像资料", image:"图片检索"}[kind] || "媒体检索";
    get("tool-note").textContent = items.length + " 条结果";
    panel.dataset.resourceKind = {audio:"music",video:"video",image:"image"}[kind] || "search";
    panel.setAttribute("data-media-list","");
    motion.show({manual:true,timed:!narrow.matches});
    present(explicit);
    results.replaceChildren();
    for (const [index,item] of items.entries()) {
      const row = document.createElement("article");
      row.className = "media-choice";
      const playable = ["audio","video"].includes(item.kind) && /^[a-f0-9]{32}$/.test(item.media_id);
      const external = item.kind === "video" && item.source_kind === "online" && Boolean(item.url);
      const choice = document.createElement(playable || external ? "button" : "div");
      if (playable) {
        choice.type = "button";
        choice.dataset.testid = "track-choice";
        choice.addEventListener("click", () => view.chooseTrack(item,index + 1));
      }
      if (external) {
        choice.type = "button";
        choice.addEventListener("click", () => {
          manual();
          for (const media of document.querySelectorAll(".media-audio")) media.pause();
          player.replaceChildren(mossLegacyVideo.createExternal(item).root);
          results.querySelector("[aria-current]")?.removeAttribute("aria-current");
          row.setAttribute("aria-current","true");
          motion.renew();
        });
      }
      const title = document.createElement("span"); title.textContent = item.title;
      const detail = document.createElement("small");
      const peers = items.filter(i => key(i.title)===key(item.title) && key(i.artist)===key(item.artist));
      let version = "";
      if (peers.length>1 && item.path) {
        const parts = item.path.split(/[\\/]/);
        version = parts.at(-1);
        if (peers.some(i=>i.media_id!==item.media_id && key(i.path?.split(/[\\/]/).at(-1))===key(version)))
          version = parts.slice(-2).join("/");
      }
      detail.textContent = item.subtitle
        ? [cueTime(item.subtitle.start_s) + "–" + cueTime(item.subtitle.end_s), item.subtitle.excerpt || item.subtitle.text].join(" · ")
        : [item.artist,version || {local:"本机文件",nas:"网络存储"}[item.source_kind]].filter(Boolean).join(" · ");
      if (item.kind === "audio") {
        if (detail.textContent) detail.prepend(" · ");
        detail.prepend(mossLegacyMusic.availability(item));
      }
      choice.title = [item.title,detail.textContent].filter(Boolean).join(" · ");
      choice.append(title, detail); row.append(choice); results.append(row);
      if (selected?.media_id && selected.media_id === item.media_id &&
          JSON.stringify(selected.subtitle || null) === JSON.stringify(item.subtitle || null)) row.setAttribute("aria-current","true");
    }
    if (view.audio) {
      if (!view.playerControls) {
        if (kind === "video") {
          view.playerHeading = document.createElement("div");
          view.playerHeading.className = "player-heading";
          const name = document.createElement("strong"); name.textContent = selected.title;
          view.playerHeading.append(name);
          view.playerControls = mossLegacyVideo.create(selected,view.audio).root;
        } else {
          const mounted = mossLegacyMusic.mount(view.audio,{items,selected,mode:view.projected.mode,
            chooseTrack:(item,n)=>view.chooseTrack(item,n),clipStart:view.bounds?.start,
            clipSeconds:view.bounds ? view.bounds.end - view.bounds.start : undefined});
          view.playerHeading = mounted.heading;
          view.playerControls = mounted.controls;
          view.disposePlayer = mounted.dispose;
        }
      }
      if (player.lastElementChild !== view.playerControls) player.replaceChildren(view.playerHeading,view.playerControls);
      if ((!view.projected.recoveredFrom || explicit) && view.audio.readyState < 1 && view.audio.preload === "none")
        view.audio.preload = "metadata";
      music(view);
    } else {
      player.replaceChildren();
      transport.hidden = now.hidden = true;
    }
  }
  function feedback(view,text) {
    const label = view.playerControls?.querySelector(".player-feedback");
    if (label) label.textContent = text;
  }
  function promptPlay(view) {
    open(view);
    music(view,"等待点击播放");
    panel.setAttribute("data-media-pending","");
    feedback(view,"点击播放键开始播放。");
  }
  function cueTime(seconds) {
    const ms = Math.round(seconds * 1000);
    const m = Math.floor(ms / 60000);
    const hours = m >= 60 ? Math.floor(m / 60) + ":" : "";
    return hours + String(m % 60).padStart(2,"0") + ":" + String(Math.floor(ms / 1000) % 60).padStart(2,"0") +
      "." + String(ms % 1000).padStart(3,"0");
  }
  function prepare(view,turn,items,selected,mayOpen) {
    if (!items.length && !selected) return;
    const rows = items.length ? items : [selected];
    const key = JSON.stringify([rows.map(i=>[i.media_id || i.url,i.subtitle]), selected?.media_id,selected?.subtitle,
      view.audio?.getAttribute("src") || view.audio?.dataset.parkedSrc,turn.media?.play_mode]);
    if (key === view.projectionKey) return;
    view.projectionKey = key;
    view.disposePlayer?.();
    view.disposePlayer = null;
    view.playerControls = null;
    view.projected = {items:rows,selected,mode:turn.media?.play_mode,recoveredFrom:turn.media?.recovered_from};
    if (view.audio) audioViews.set(view.audio, view);
    if (!view.projectButton) {
      const btn = document.createElement("button");
      btn.className = "projection-link"; btn.type = "button"; btn.dataset.testid = "open-media";
      btn.textContent = "查看媒体结果"; btn.setAttribute("aria-controls","tools-panel");
      btn.addEventListener("click", () => open(view,true));
      view.answers.append(btn); view.projectButton = btn;
    }
    if (mayOpen || (view === active && !panel.hidden)) open(view);
  }
  function arm(text,chatId,turns) {
    if (navigator.userActivation?.isActive) { sent = {text,chatId}; showPending(text,turns); }
  }
  get("mic").addEventListener("click",() => {
    if (navigator.userActivation?.isActive) voiceChat = document.querySelector("#turns").dataset.chat;
  });
  function fromVoice(text,chatId,turns) {
    if (voiceChat === chatId) { sent = {text,chatId}; showPending(text,turns); }
    voiceChat = null;
  }
  function claim(turn) {
    if (!sent || sent.chatId !== turn.chat_id || sent.text !== turn.user) return false;
    sent = null;
    return true;
  }
  function hasPendingAudio(entry) {
    const audio = entry.view.audio;
    return audio?.isConnected && (audio === entry.audio ||
      entry.view.projected?.recoveredFrom === entry.target.media_id);
  }
  function holdPending(chatId) {
    const entry = pendingStart;
    if (!entry || entry.chatId !== chatId || entry.generation !== generation ||
        !hasPendingAudio(entry)) return null;
    let unblock;
    const gate = new Promise(resolve => { unblock = resolve; });
    entry.gates.add(gate);
    if (entry.starting && !entry.interrupted) {
      // Keep native playback pending until the message decides whether to cancel or release it.
      entry.interrupted = true;
      entry.audio.pause();
    }
    const release = () => { entry.gates.delete(gate); unblock(); };
    return {target:entry.target, release, cancel() {
      if (entry !== pendingStart || entry.generation !== generation ||
          !hasPendingAudio(entry)) return false;
      advancePlayback();
      pendingStart = null;
      entry.view.playRequested = false;
      window.mossSpeech?.stopFor?.(entry.target.turn_id);
      if (active === entry.view) {
        panel.removeAttribute("data-media-pending");
        music(entry.view,"已取消待播放");
        feedback(entry.view,"已取消待播放。");
      }
      release();
      return true;
    }};
  }
  function waitForMedia(audio, event, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(signal.reason); return; }
      if (audio.error) { reject(new Error("备用音轨暂不可用")); return; }
      const listeners = new AbortController();
      const finish = (error) => {
        clearTimeout(timer);
        listeners.abort();
        if (error) reject(error);
        else resolve();
      };
      const unavailable = () => finish(new Error("备用音轨暂不可用"));
      const timer = setTimeout(unavailable, 5000);
      audio.addEventListener(event, () => finish(), {signal:listeners.signal});
      audio.addEventListener("error", unavailable, {signal:listeners.signal});
      signal.addEventListener("abort", () => finish(signal.reason), {signal:listeners.signal});
    });
  }
  async function seekBeforePlaying(audio, offset, signal) {
    if (audio.readyState < HTMLMediaElement.HAVE_METADATA) await waitForMedia(audio, "loadedmetadata", signal);
    if (signal.aborted) throw signal.reason;
    if (!Number.isFinite(audio.duration) || offset >= audio.duration) throw new Error("备用音轨的播放进度无效");
    audio.currentTime = offset;
    if (audio.seeking) await waitForMedia(audio, "seeked", signal);
    if (Math.abs(audio.currentTime - offset) > .25) throw new Error("未能定位到中断位置");
  }
  function recoverInterrupted(view, turn, audio, playId) {
    const position = audio.currentTime;
    const selected = turn?.media?.selected;
    if (pendingStart || view.audio !== audio || !audio.isConnected || !turn?.media?.autoplay || turn.media.recovered_from ||
        turn.media.play_mode !== "track" || selected?.kind !== "audio" || !selected.artist ||
        selected.artist_source === "folder" || selected.media_id !== audio.dataset.mediaId ||
        !Number.isFinite(position) || position <= 0 || audio.ended) return false;
    const entry = {view, audio, chatId:turn.chat_id, generation:advancePlayback(), gates:new Set(),
      target:{turn_id:turn.id,media_id:selected.media_id}, aborter:new AbortController()};
    pendingStart = entry;
    music(view, "正在核对备用音轨");
    panel.setAttribute("data-media-pending","");
    void (async () => {
      const timer = setTimeout(() => entry.aborter.abort(), 6000);
      try {
        const next = await window.mossRecoverTrack?.(turn, selected.media_id,
          {play_id:playId, position_s:position}, entry.aborter.signal);
        const current = () => entry === pendingStart && entry.generation === generation && hasPendingAudio(entry);
        while (current() && entry.gates.size) await Promise.all(entry.gates);
        if (!current()) return;
        const offset = next?.media?.resume_position_s;
        const replacement = next?.media?.selected;
        if (replacement && Number.isFinite(offset) && offset > 0 &&
            replacement.media_id !== selected.media_id && view.audio?.dataset.mediaId === replacement.media_id) {
          clearTimeout(timer);
          await play(view,next,replacement,false,offset);
        } else {
          music(view,"播放中断");
          feedback(view,"备用音轨暂不可用。");
        }
      } finally {
        clearTimeout(timer);
        if (pendingStart === entry) {
          pendingStart = null;
          panel.removeAttribute("data-media-pending");
        }
      }
    })();
    return true;
  }
  async function play(view,turn,selected,announce,offset) {
    const audio = view.audio;
    const fullTrack = selected.kind === "audio" && turn.media?.play_mode === "track";
    const localTrack = fullTrack && selected.source_kind === "local";
    restoreMediaSource(audio);
    if (selected.kind === "video") mediaPresented(false, true);
    const own = advancePlayback();
    view.playRequested = true;
    releaseOtherMedia(audio, true);
    if (["track","video"].includes(turn.media?.play_mode)) audio.preload = "auto";
    panel.setAttribute("data-media-pending","");
    const entry = {view, audio, chatId:turn.chat_id, generation:own, gates:new Set(),
      target:{turn_id:turn.id, media_id:selected.media_id}, starting:false, interrupted:false};
    if (offset !== undefined) entry.aborter = new AbortController();
    if (announce || fullTrack) pendingStart = entry;
    let duckTarget, restoreTarget;
    try {
      if (announce) {
        music(view,"准备播放");
        feedback(view,"准备播报曲名…");
        duckTarget = await window.mossSpeech?.beforeMedia?.({turnId:turn.id,chatId:turn.chat_id,title:selected.title,
          announcement:turn.media?.announcement, allowCachedOverlap:localTrack});
      }
      if (entry.aborter) {
        music(view,"正在定位续播位置");
        await seekBeforePlaying(audio,offset,entry.aborter.signal);
      }
      do {
        while (own === generation && entry.gates.size) await Promise.all(entry.gates);
        if (!fullTrack && pendingStart === entry) pendingStart = null;
        if (own !== generation || !audio.isConnected) return;
        if (entry.interrupted) {
          view.playRequested = true;
          panel.setAttribute("data-media-pending","");
        }
        music(view, offset === undefined ? "正在载入" : "正在续播");
        if (selected.subtitle && view.bounds) {
          await mossLegacyVideo.locateClip(audio, view.bounds);
          if (own !== generation || !audio.isConnected) return;
        }
        entry.interrupted = false;
        entry.starting = fullTrack;
        restoreTarget = duckTarget?.(audio);
        try {
          await audio.play();
        } catch (error) {
          if (error.name !== "AbortError" || !entry.interrupted) throw error;
        } finally {
          entry.starting = false;
        }
        if (entry.interrupted) {
          restoreTarget?.();
          restoreTarget = null;
        }
      } while (entry.interrupted);
      if (offset !== undefined && own === generation && !audio.paused) feedback(view,"从 " + clock(offset) + " 继续播放。");
    }
    catch (error) {
      restoreTarget?.();
      if (error.name === "AbortError") return;
      if (own !== generation || !audio.isConnected) return;
      if (error.name !== "NotAllowedError" && turn.media?.retry_track_source && !audio.played.length) {
        const replacement = await window.mossRecoverTrack?.(turn, selected.media_id);
        while (own === generation && entry.gates.size) await Promise.all(entry.gates);
        if (own !== generation) return;
        if (replacement && view.audio?.dataset.mediaId === replacement.media.selected.media_id) {
          await play(view, replacement, replacement.media.selected, false);
          return;
        }
        if (view.audio !== audio || !audio.isConnected) {
          if (active === view && hasPendingAudio(entry)) {
            view.playRequested = false;
            panel.removeAttribute("data-media-pending");
            music(view, "可试听");
          }
          return;
        }
      }
      if (error.name === "RangeError") {
        panel.removeAttribute("data-media-pending");
        music(view, error.message);
        audio.dispatchEvent(new CustomEvent("moss-playback-rejected", {detail:error.name}));
        return;
      }
      audio.dispatchEvent(new CustomEvent("moss-playback-rejected",{detail:error.name}));
      open(view);
      music(view,error.name === "NotAllowedError" ? "等待点击播放" : offset === undefined ? "播放失败" : "播放中断");
      feedback(view,error.name === "NotAllowedError" ? "浏览器需要你点击播放键开始播放。" : "当前媒体无法播放，请检查文件和浏览器支持的格式。");
    } finally {
      if (pendingStart === entry) pendingStart = null;
    }
  }
  pin.addEventListener("click",()=> {
    const pinned = pin.getAttribute("aria-pressed") !== "true";
    pin.setAttribute("aria-pressed",String(pinned)); pin.textContent = pinned ? "已固定" : "固定";
    motion.hold(pinned);
  });
  function dismiss() {
    const opener = desktopOpener;
    desktopOpener = null;
    if (leaveMedia()) {
      if (active?.audio?.localName === "video") active.audio.pause();
      return;
    }
    const restoreFocus = Boolean(opener || panel.contains(document.activeElement));
    close();
    if (restoreFocus) [opener, active?.projectButton, get("history-toggle")].find(focusable)?.focus({preventScroll:true});
  }
  function dismissOnEscape() {
    if (narrow.matches || panel.hidden || !desktopOpener) return false;
    dismiss();
    return true;
  }
  get("tools-close").addEventListener("click", dismiss);
  narrow.addEventListener("change", () => {
    if (!panel.hidden) motion.show({manual:true, timed:!narrow.matches});
  });
  now.addEventListener("click",()=>{
    if (audible && !audible.audio.paused) open(audible,true);
    else if (active) open(active,true);
    else { motion.show({manual:true,timed:!narrow.matches}); present(true); }
  });
  for (const [name, state] of Object.entries({loadstart:"正在载入",loadedmetadata:null,play:"准备播放",waiting:"缓冲中",stalled:"缓冲中",playing:"正在播放",
      pause:"已暂停",ended:"播放结束",error:"播放失败",timeupdate:null,durationchange:null})) {
    document.addEventListener(name,e => {
      if (name === "playing" && e.target.paused) return;
      if (name === "play" && e.target.matches?.(".media-audio")) {
        if (pendingStart?.aborter && pendingStart.audio !== e.target) advancePlayback();
        releaseOtherMedia(e.target, true);
      }
      if (name === "playing" && audioViews.has(e.target)) audible = audioViews.get(e.target);
      if (active && audible?.audio === e.target && active !== audible && ["playing","pause","ended","error"].includes(name)) music(active);
      if (active?.audio !== e.target) return;
      if (name !== "play" && active.transportState === "已取消待播放") return;
      if (name === "play") active.playRequested = true;
      if (name === "pause" || name === "ended") active.playRequested = false;
      if (name === "pause" && e.target.error?.code > 1) return;
      if (name === "error" && e.target.error?.code > 1 && e.target.played.length) {
        active.playRequested = false;
        music(active,"播放中断");
        return;
      }
      if (!active.playRequested && e.target.paused && ["loadstart","waiting","stalled"].includes(name)) music(active,"正在读取时长");
      else if (!active.playRequested && e.target.paused && name === "loadedmetadata") music(active,"可试听");
      else music(active,state);
    },true);
  }
  const speechNotes = new Map();
  const shortCues = new Set();
  let newestTurn;
  function displaySpeech(id,text) {
    speechNotes.set(id,text);
    const row = document.querySelector('[data-turn-id="'+CSS.escape(id)+'"] .assistant-body');
    if (row) {
      let note = row.querySelector(".speech-progress");
      if (!note) { note = document.createElement("small"); note.className = "speech-progress"; row.append(note); }
      note.textContent = text;
    }
    if (!pendingSelection && id === panel.dataset.turnId) { voice.textContent=text; voice.hidden=false; }
  }
  for (const [name,label] of Object.entries({"pending":"正在合成…","start":"正在播报","end":"播报结束","error":"无法播报","stop":"已停止","skipped":"已跳过迟到的报幕"})) {
    window.addEventListener("moss-speech-"+name, e => {
      if (!e.detail?.id) return;
      if (name === "start") {
        if (e.detail.shortCue) shortCues.add(e.detail.id); else shortCues.delete(e.detail.id);
      }
      if (name === "end" && /无法播报|已停止|已跳过/.test(speechNotes.get(e.detail.id) || "")) return;
      const phase = shortCues.has(e.detail.id) && name === "start" ? "正在简短报幕" :
                    shortCues.has(e.detail.id) && name === "end" ? "简短报幕结束" : label;
      displaySpeech(e.detail.id, "MOSS 语音 · " + (e.detail.interrupted ? "已停止" : phase) +
                    (name === "error" && e.detail.message ? "　"+e.detail.message : ""));
    });
  }
  window.addEventListener("moss-speech-progress",e => {
    if (e.detail?.id) displaySpeech(e.detail.id,"MOSS 语音 · "+(e.detail.shortCue ? "简短报幕" : "正在播报")+"　"+clock(e.detail.position)+" / "+clock(e.detail.seconds));
  });
  window.addEventListener("moss-agent-state",e=>{
    const t = e.detail.turns.at(-1);
    newestTurn = t?.id;
    if (pendingSelection && t?.reply && sent?.text === t.user && !t.media?.selected) {
      pendingSelection = false;
      panel.removeAttribute("data-media-pending");
      updateTransport("没有可播放的曲目");
    }
  });
  new MutationObserver(() => {
    if (newestTurn && get("speaker-state").textContent === "合成中") displaySpeech(newestTurn,"MOSS 语音 · 正在合成…");
  }).observe(get("speaker-state"),{childList:true,subtree:true,characterData:true});
  window.addEventListener("moss-media-start", e => {
    if (e.detail.turnId === pendingStart?.target.turn_id && pendingStart.starting &&
        !pendingStart.interrupted && !pendingStart.audio.paused) pendingStart = null;
    if (e.detail.turnId === active?.audio?.dataset.audioTurn) panel.setAttribute("data-media-pending","");
  });
  window.addEventListener("moss-media-pause", e => {
    if (e.detail.turnId === active?.audio?.dataset.audioTurn && !pendingStart?.aborter) {
      panel.removeAttribute("data-media-pending");
      motion.renew();
    }
  });
  panel.addEventListener("keydown",e=>{if(e.key === "Escape"){
    e.preventDefault();
    e.stopPropagation();
    dismiss();
  }});
  get("interrupt").addEventListener("click", advancePlayback, true);
  window.addEventListener("pagehide",close);
  window.mossProjection = {prepare, open, close, reset, manual, arm, fromVoice, claim, play, promptPlay, feedback, holdPending, recoverInterrupted, dismissOnEscape,
    cancel(view) { if (active === view) { advancePlayback(); view.audio?.pause(); } }};
  window.dispatchEvent(new Event("moss-projection-ready"));
}
