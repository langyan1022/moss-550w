import {PRESETS, PRESET_IDS, resolvePalette} from "/conversation-assets/dsh-theme/palettes.js";
import {drawIdentity} from "/conversation-assets/opening-identity.js";
import {activateVoice} from "./agent-voice.js?v=echo-preview-gate";
import {clearWindowReplyDraft, submitWindowReplyByVoice} from "./context-tools.js?v=clipboard-reply-voice";
import {activateSpeech} from "./agent-speech.js?v=v5-erhua";
import {createAudioMonitor} from "./agent-audio-monitor.js?v=fui-signal";
import {monitorMediaSamples} from "/static/agent-media-signal.js?v=fui-signal";
import {installProjection} from "./agent-projection.js?v=projection-keyboard";
import {installHandGesture} from "./assistant-gestures.js?v=scene-lifecycle";
import {mountStaffConsole} from "./staff-console.js?v=staff-editable-english";
import {mountSpeechSpace} from "./speech-space.js?v=speech-space";

const byId = id => document.getElementById(id);
const stage = document.querySelector("main");
const query = new URLSearchParams(location.search);
const lightScene = query.get("scene") === "off";
const sceneLink = byId("scene-mode-link");
let canvas = byId("moss-model");
const emptyCanvas = canvas.cloneNode(false);
const replaceGestureCanvas = installHandGesture({toggle:byId("hand-gesture"),status:byId("hand-gesture-status"),video:byId("hand-gesture-input"),canvas});
let sceneRevision = 0, stopModel, stopEarth, geo, position;
window.addEventListener("moss-geo", ({detail}) => { geo = detail; });
window.addEventListener("moss-browser-position", ({detail}) => { position = detail; });

function setSceneMode(light) {
  stage.toggleAttribute("data-light-scene", light);
  if (!sceneLink) return;
  sceneLink.href = light ? "/?chat=1" : "/?chat=1&scene=off";
  sceneLink.textContent = light ? "载入三维场景" : "打开轻量对话";
}

function loadScene() {
  const revision = ++sceneRevision;
  setSceneMode(false);
  import("/conversation-assets/moss-view.js?entry=scene-lifecycle&lifecycle=1").then(entry => {
    if (revision === sceneRevision) stopModel = entry.mountMossView(canvas);
  }).catch(error => {
    if (revision !== sceneRevision) return;
    canvas.dataset.error = "entry";
    byId("model-load-stage").textContent = "三维模型载入失败，请重新打开三维场景。";
    console.error("550W 三维入口载入失败", error);
  });
  import("/conversation-assets/earth-view.js?v=city-settings&lifecycle=1").then(async entry => {
    const city = (await window.mossCityReady)?.selected();
    if (revision !== sceneRevision) return;
    stopEarth = entry.unmountEarthView;
    entry.mountEarthView({geo, position, city});
  }).catch(error => { if (revision === sceneRevision) console.error("地球视图载入失败", error); });
}

function unloadScene() {
  ++sceneRevision;
  setSceneMode(true);
  stopModel?.();
  stopEarth?.();
  stopModel = stopEarth = undefined;
  const replacement = emptyCanvas.cloneNode(false);
  canvasReady.disconnect();
  canvas.replaceWith(replacement);
  canvas = replacement;
  canvasReady.observe(canvas, {attributes:true, attributeFilter:["data-ready"]});
  replaceGestureCanvas?.(canvas);
  pendingCameraPreset = dials.find(dial => dial.checked)?.value;
  stage.querySelector(".camera-overlay").hidden = true;
  stage.querySelector(".check-indicator").hidden = true;
  byId("model-load-stage").textContent = "载入 550W 三维模型";
  stage.querySelector(".model-load-meter").removeAttribute("value");
  stage.querySelector(".model-load-reading").hidden = true;
  delete stage.dataset.projectionDock;
  stage.style.removeProperty("--projection-lens-room");
  stage.style.removeProperty("--chat-lens-room");
}

setSceneMode(lightScene);
sceneLink?.addEventListener("click", event => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const light = !stage.hasAttribute("data-light-scene");
  const url = new URL(location.href);
  if (light) url.searchParams.set("scene", "off");
  else url.searchParams.delete("scene");
  history.replaceState(history.state, "", url);
  sceneLink.closest("details").open = false;
  if (light) unloadScene();
  else loadScene();
});
if (!lightScene) {
  if (document.hidden) {
    document.addEventListener("visibilitychange", function visible() {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", visible);
      if (!stage.hasAttribute("data-light-scene")) loadScene();
    });
  } else loadScene();
}
const film = byId("opening-film"), identity = byId("opening-identity"), skip = byId("skip-opening");
const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const drawer = byId("dialogue");
const status = text => { byId("status").textContent = text; };
let openingTimer, openingFrame, openingRevision = 0, openingGreeting = false, current;
let pendingCameraPreset;
const silence = new Uint8Array(0);
let thinking = false, microphone = null, speaking = null;
const consolePhaseLabel = byId("console-phase-label");
const consolePhases = {standby:"待机",listen:"正在听",think:"正在处理",speak:"正在播报"};
function updateConsolePhase() {
  const label = stage.dataset.consoleLink !== "online" ? "正在连接" :
    stage.dataset.consoleVoice === "playing" ? consolePhases.speak :
    stage.dataset.consoleVoice === "pending" ? "正在准备播报" :
    consolePhases[stage.dataset.consolePhase];
  if (consolePhaseLabel.textContent !== label) consolePhaseLabel.textContent = label;
}
stage.addEventListener("moss-console-link", updateConsolePhase);
updateConsolePhase();
for (const event of ["pending","start","end","stop","error","skipped"]) {
  window.addEventListener("moss-speech-"+event, () => {
    if (event === "pending" || event === "start") stage.dataset.consoleVoice = event === "pending" ? "pending" : "playing";
    else delete stage.dataset.consoleVoice;
    updateConsolePhase();
    queueMicrotask(updateInterrupt);
  });
}
function updateLens() {
  const detail = speaking || microphone || {phase:thinking ? "think" : "standby", source:"idle", bins:silence, level:0};
  if (stage.dataset.consolePhase !== detail.phase) {
    stage.dataset.consolePhase = detail.phase;
    updateConsolePhase();
  }
  canvas.dispatchEvent(new CustomEvent("moss-signal", {detail}));
}
function sampleLens(phase, source, block, bins, sampleRate) {
  let power = 0;
  for (const sample of block) power += sample * sample;
  const rms = Math.sqrt(power / block.length);
  return {phase, source, bins, sampleRate, dbfs:20 * Math.log10(Math.max(.00001, rms)), level:Math.min(1, rms * 4.5)};
}
const monitor = createAudioMonitor(byId("stage-audio"), canvas.parentElement, reduced);
const speech = window.mossSpeech = activateSpeech(status, (block, bins, sampleRate, analyser) => {
  speaking = block ? sampleLens("speak", "voice", block, bins, sampleRate) : null;
  monitor.sample(1, block, bins, analyser, speaking?.dbfs);
  updateLens();
}, () => voice.stop("已停止上一段语音输入。"));
document.addEventListener("visibilitychange", () => { if (!document.hidden) updateLens(); });
const canvasReady = new MutationObserver(() => {
  updateLens();
  applyCameraPreset();
});
canvasReady.observe(canvas, {attributes:true, attributeFilter:["data-ready"]});

function finishOpening(greet = false) {
  const welcome = greet && openingGreeting;
  openingGreeting = false;
  openingRevision++;
  clearTimeout(openingTimer);
  cancelAnimationFrame(openingFrame);
  delete stage.dataset.opening;
  film.pause();
  identity.hidden = film.hidden = skip.hidden = true;
  if (welcome) staff?.welcomeAfterOpening();
}
function updateOpeningCrop() {
  if (stage.dataset.opening !== "film") return;
  const progress = reduced.matches ? 1 : Math.max(0, Math.min(1, (film.currentTime - .13) / 1.27));
  const remaining = 1 - progress * progress * (3 - 2 * progress);
  film.style.setProperty("--opening-zoom", (1 + .62 * remaining).toFixed(4));
  film.style.setProperty("--opening-light", (1 + .18 * remaining).toFixed(3));
  if (progress < 1) openingFrame = requestAnimationFrame(updateOpeningCrop);
}
async function playOpening(own) {
  if (own !== openingRevision) return;
  stage.dataset.opening = "film";
  identity.hidden = true;
  film.hidden = false;
  if (film.getAttribute("src") !== film.dataset.src || film.error) film.src = film.dataset.src;
  else film.currentTime = 0;
  updateOpeningCrop();
  try { await film.play(); } catch { if (own === openingRevision) finishOpening(); }
}
async function showOpening(explicit = false) {
  finishOpening();
  if (!explicit && (reduced.matches || sessionStorage.getItem("moss.agent.opened"))) return;
  if (explicit) speech.prepareOutput();
  sessionStorage.setItem("moss.agent.opened", "1");
  openingGreeting = true;
  const own = openingRevision;
  byId("opening-position").value = 0;
  stage.dataset.opening = "identity";
  skip.hidden = identity.hidden = false;
  try { await drawIdentity(identity.querySelector("svg")); } catch { finishOpening(); return; }
  if (own !== openingRevision) return;
  openingTimer = setTimeout(() => playOpening(own), reduced.matches ? 650 : 2100);
}
film.addEventListener("ended", () => finishOpening(true));
film.addEventListener("error", () => finishOpening());
film.addEventListener("timeupdate", () => { if (film.duration) byId("opening-position").value = film.currentTime / film.duration; });
skip.addEventListener("click", () => finishOpening(true));
byId("replay-opening").addEventListener("click", () => { byId("replay-opening").closest("details").open = false; showOpening(true); });
document.querySelector(".brand-field").addEventListener("click", () => showOpening(true));
let drawerChange = 0, captionTurnId, spokenCaption, playingMedia;
monitorMediaSamples(monitor.sampleMedia, element => {
  monitor.setMediaPlayback(element);
  if (element !== playingMedia) { playingMedia = element; updateCaption(); }
});
function updateCaption() {
  const turn = current?.turns.at(-1);
  const caption = byId("caption");
  const selection = playingMedia && current?.turns.findLast(t => t.id === playingMedia.dataset.audioTurn)?.media?.selected;
  const mediaTitle = selection?.media_id === playingMedia?.dataset.mediaId && selection
    ? [selection.artist, selection.title].filter(Boolean).join(" · ")
    : playingMedia?.getAttribute("aria-label")?.replace(/^(?:观看|试听)\s*/, "");
  const hasResultPanel = turn?.weather || turn?.weather_choices ||
    turn?.local_overview?.length || turn?.local_overview_partial?.length;
  const fallback = turn && turn.id === captionTurnId && !hasResultPanel && !drawer.hasAttribute("data-open") &&
    (turn.media?.announcement || turn.reply || turn.partial_reply);
  const text = spokenCaption?.text || mediaTitle || fallback || "";
  if (caption.textContent !== text) caption.textContent = text;
  caption.hidden = !text;
}
window.addEventListener("moss-speech-start", ({detail}) => {
  spokenCaption = {id:detail.id, text:detail.caption ?? detail.text};
  updateCaption();
});
window.addEventListener("moss-speech-progress", ({detail}) => {
  if (spokenCaption?.id === detail.id && detail.caption && spokenCaption.text !== detail.caption) {
    spokenCaption.text = detail.caption;
    updateCaption();
  }
});
for (const edge of ["end","stop","error","skipped"])
  window.addEventListener("moss-speech-"+edge, ({detail}) => {
    const wasSpeaking = spokenCaption && spokenCaption.id === detail.id;
    if (wasSpeaking) spokenCaption = null;
    if ((wasSpeaking || edge === "stop") && captionTurnId === detail.id) captionTurnId = undefined;
    updateCaption();
  });
const narrow = matchMedia("(max-width:900px) and (orientation:portrait)");
const projectionPanel = byId("tools-panel");
const mediaReturn = document.createElement("button");
mediaReturn.id = "workspace-media-return";
mediaReturn.type = "button";
mediaReturn.setAttribute("aria-controls","tools-panel");
mediaReturn.textContent = "查看媒体";
mediaReturn.hidden = true;
let mediaParked = false, parkedLog, mediaOpener;

function setWorkspace(mode) {
  const prev = stage.dataset.mobileWorkspace;
  const next = narrow.matches ? mode : undefined;
  const log = byId("log");
  if (prev === "chat" && next !== "chat" && log.clientHeight && drawer.hasAttribute("data-open")) {
    parkedLog = {chat:current?.chat_id, top:log.scrollTop,
      atEnd:log.scrollHeight - log.scrollTop - log.clientHeight < 24};
  }
  if (next) stage.dataset.mobileWorkspace = next;
  else delete stage.dataset.mobileWorkspace;
  const showDrawer = drawer.hasAttribute("data-open") && (!next || next === "chat");
  drawer.inert = !showDrawer;
  projectionPanel.inert = Boolean(next && next !== "media");
  stage.toggleAttribute("data-lore-chat-open", showDrawer);
  byId("history-toggle").setAttribute("aria-expanded", String(showDrawer));
  mediaReturn.hidden = !next || next === "media" || projectionPanel.hidden;
  if (!mediaReturn.hidden) {
    const parent = next === "chat" ? byId("history-state") : stage;
    if (mediaReturn.parentElement !== parent) parent.append(mediaReturn);
    mediaReturn.textContent = projectionPanel.dataset.resourceKind === "music" ? "返回音乐" :
      projectionPanel.dataset.resourceKind === "video" ? "返回视频" : "查看媒体";
  }
  if (showDrawer && parkedLog && (next !== prev || !next)) {
    if (parkedLog.chat === current?.chat_id) log.scrollTop = parkedLog.atEnd ? log.scrollHeight : parkedLog.top;
    parkedLog = null;
  }
  if (current) updateCaption();
}
function mediaPresented(manual, requestedVideo = false) {
  if (!narrow.matches) return;
  if (manual || requestedVideo || (stage.dataset.mobileWorkspace === "stage" && !mediaParked)) {
    const focusClose = manual || (requestedVideo && drawer.contains(document.activeElement));
    if (manual && stage.dataset.mobileWorkspace !== "media") mediaOpener = document.activeElement;
    mediaParked = false;
    setWorkspace("media");
    if (focusClose) byId("tools-close").focus({preventScroll:true});
  } else {
    setWorkspace(stage.dataset.mobileWorkspace || "stage");
  }
}
function syncWorkspace() {
  const mode = stage.dataset.mobileWorkspace;
  const chatOpen = drawer.hasAttribute("data-open");
  if (projectionPanel.hidden) mediaParked = false;
  setWorkspace(mode === "media" && !projectionPanel.hidden ? "media" :
    chatOpen ? "chat" : !projectionPanel.hidden && !mediaParked ? "media" : "stage");
}
function leaveMedia() {
  if (!narrow.matches) return false;
  mediaParked = true;
  setWorkspace(drawer.hasAttribute("data-open") ? "chat" : "stage");
  const previous = mediaOpener;
  mediaOpener = null;
  const target = previous instanceof HTMLButtonElement && previous.isConnected &&
    !previous.closest("[inert]") && previous.getClientRects().length ? previous : mediaReturn;
  if (!target.hidden) target.focus({preventScroll:true});
  return true;
}
mediaReturn.addEventListener("click", () => mediaPresented(true));
new MutationObserver(syncWorkspace).observe(projectionPanel, {attributes:true, attributeFilter:["hidden"]});
narrow.addEventListener("change", () => { mediaParked = false; syncWorkspace(); });

function showChat(show, focus = true) {
  if (show && stage.hasAttribute("data-opening")) finishOpening();
  const revision = ++drawerChange;
  if (show && drawer.hidden) { drawer.hidden = false; drawer.getBoundingClientRect(); }
  drawer.toggleAttribute("data-open", show);
  sessionStorage.setItem("moss.agent.chat-open", show ? "1" : "0");
  if (narrow.matches) mediaParked = !projectionPanel.hidden;
  setWorkspace(show ? "chat" : "stage");
  if (focus) (show ? byId("message") : byId("history-toggle")).focus({preventScroll: true});
  if (!show) {
    const transitions = drawer.getAnimations().map(animation => animation.finished);
    Promise.allSettled(transitions).then(() => { if (drawerChange === revision) drawer.hidden = true; });
  }
}
byId("history-toggle").addEventListener("click", () => showChat(drawer.inert));
byId("close-chat").addEventListener("click", () => showChat(false));
const visibleForWake = () => !document.hidden && document.hasFocus() && !document.querySelector("dialog[open]");
const nativeNext = byId("native-wake-next");
nativeNext.checked = localStorage.getItem("moss.voice.nativeNext") === "on";
nativeNext.addEventListener("change", () => {
  localStorage.setItem("moss.voice.nativeNext", nativeNext.checked ? "on" : "off");
  if (!nativeNext.checked) voice.stopNativeNext();
});
const wakeUrl = new URL(location.href);
let coldWake = /^#native-wake=([A-Za-z0-9_-]{16,128})$/u.exec(wakeUrl.hash)?.[1];
if (wakeUrl.hash.startsWith("#native-wake=")) {
  wakeUrl.hash = "";
  history.replaceState(history.state, "", wakeUrl);
}

function reportWake(action, body) {
  return fetch(`/api/wake/${action}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
}
async function acceptWake(ticket) {
  if (!visibleForWake()) return;
  try {
    const claim = await reportWake("claim", {ticket});
    if (!claim.ok || !visibleForWake()) return;
    const {grant, listen_next:listenNext} = await claim.json();
    if (!visibleForWake()) return;
    const chat = current?.chat_id;
    showChat(true, !drawer.contains(document.activeElement));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (visibleForWake() && drawer.hasAttribute("data-open") && drawer.getClientRects().length) {
      const shown = await reportWake("shown", {ticket, grant});
      if (shown.ok && listenNext && nativeNext.checked && chat) {
        void voice.listenAfterWake(() => current?.chat_id === chat && nativeNext.checked && visibleForWake() &&
          stage.dataset.consoleLink === "online" && drawer.hasAttribute("data-open") && !speech.active() &&
          !byId("message").value.trim() && !current.turns.some(t => ["thinking","saving","pending"].includes(t.status)));
      }
    }
  } catch {
    // 本机接力未收到回执时会走 Windows 打开入口。
  }
}
window.addEventListener("moss-wake-offer", ({detail:ticket}) => { void acceptWake(ticket); });
function acceptColdWake() {
  if (!coldWake || !current?.chat_id || stage.dataset.consoleLink !== "online" || !visibleForWake()) return;
  const ticket = coldWake;
  coldWake = null;
  void acceptWake(ticket);
}
window.addEventListener("focus", acceptColdWake);
document.addEventListener("visibilitychange", acceptColdWake);
stage.addEventListener("moss-console-link", acceptColdWake);
document.addEventListener("keydown", e => {
  // Let the browser dismiss the popover when focus has moved to the projection underneath.
  if (e.key === "Escape" && !e.isComposing && e.keyCode !== 229 &&
      document.querySelector(":is([popover]:popover-open)")) e.stopPropagation();
}, true);
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !e.defaultPrevented && !e.isComposing && e.keyCode !== 229 &&
      !document.querySelector("dialog[open]")) {
    if (stage.hasAttribute("data-opening")) finishOpening();
    else if (stage.dataset.mobileWorkspace === "media") leaveMedia();
    else if (window.mossProjection?.dismissOnEscape?.()) e.preventDefault();
    else if (!drawer.inert) showChat(false);
  }
});
document.addEventListener("pointerdown", event => {
  if (stage.hasAttribute("data-opening") && event.target.closest("#dialogue, #mic, .preferences, #moss-model, #history-toggle")) finishOpening();
}, true);
const dials = [...document.querySelectorAll('input[name="camera-view"]')];
function applyCameraPreset() {
  if (canvas.dataset.ready !== "true" || !pendingCameraPreset) return;
  const preset = pendingCameraPreset;
  pendingCameraPreset = undefined;
  canvas.dispatchEvent(new CustomEvent("moss-view-preset", {detail: preset}));
}
function camera(index) {
  const i = (index + dials.length) % dials.length;
  dials[i].checked = true;
  byId("camera-dial").setAttribute("aria-valuenow", i + 1);
  byId("camera-dial").setAttribute("aria-valuetext", dials[i].parentElement.textContent.trim());
  byId("camera-index").firstChild.textContent = String(i + 1).padStart(2, "0");
  pendingCameraPreset = dials[i].value;
  applyCameraPreset();
}
const dialIndex = () => dials.findIndex(d => d.checked);
dials.forEach((d, i) => d.addEventListener("change", () => camera(i)));
const dial = byId("camera-dial");
let dragY, dragFrom, dragged = false;
dial.addEventListener("pointerdown", e => { dragY = e.clientY - e.clientX; dragFrom = dialIndex(); dragged = false; dial.setPointerCapture(e.pointerId); });
dial.addEventListener("pointermove", e => {
  if (dragY === undefined) return;
  const steps = Math.round((dragY - e.clientY + e.clientX) / 22);
  if (steps) { dragged = true; camera(Math.max(0, Math.min(dials.length - 1, dragFrom + steps))); }
});
dial.addEventListener("lostpointercapture", () => { dragY = undefined; });
dial.addEventListener("click", () => { if (!dragged) camera(dialIndex() + 1); dragged = false; });
dial.addEventListener("wheel", e => { e.preventDefault(); camera(dialIndex() + Math.sign(e.deltaY)); }, {passive: false});
dial.addEventListener("keydown", e => {
  if (["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown"].includes(e.key)) {
    e.preventDefault(); camera(dialIndex() + (["ArrowRight", "ArrowUp"].includes(e.key) ? 1 : -1));
  }
});
byId("reset-view").addEventListener("click", () => camera(0));
byId("fullscreen").addEventListener("click", async () => {
  try { if (document.fullscreenElement) await document.exitFullscreen(); else await stage.requestFullscreen(); }
  catch { status("浏览器未允许全屏。"); }
});
document.addEventListener("fullscreenchange", () => {
  byId("fullscreen").textContent = document.fullscreenElement ? "退出全屏" : "全屏";
  byId("fullscreen").setAttribute("aria-pressed", String(Boolean(document.fullscreenElement)));
});
function theme(name, custom) {
  const base = Object.hasOwn(PRESETS, name) ? name : "zero-interference";
  const palette = resolvePalette({preset: base, accent: custom});
  for (const [property, value] of Object.entries({"--dsc-accent": palette.accent, "--dsc-accent-2": palette.accent2,
    "--dsc-bg": palette.bg, "--dsc-text": palette.text, "--signal": palette.accent})) document.documentElement.style.setProperty(property, value);
  document.documentElement.dataset.palette = custom ? "custom" : base;
  document.querySelector('meta[name="theme-color"]').content = palette.bg;
  byId("theme-name").textContent = custom ? "自定义" : PRESETS[base].label;
  for (const button of byId("theme").children) button.setAttribute("aria-pressed", String(!custom && button.dataset.preset === base));
  byId("use-custom").setAttribute("aria-pressed", String(Boolean(custom)));
  localStorage.setItem("moss.agent.palette", JSON.stringify({base, custom}));
  window.dispatchEvent(new CustomEvent("moss-theme", {detail: {color: palette.accent}}));
}
for (const key of PRESET_IDS) {
  const button = document.createElement("button"); button.type = "button"; button.className = "swatch";
  button.title = PRESETS[key].label; button.setAttribute("aria-label", button.title); button.dataset.preset = key;
  button.style.setProperty("--swatch", PRESETS[key].accent);
  button.addEventListener("click", () => theme(key)); byId("theme").append(button);
}
const custom = () => theme(document.documentElement.dataset.palette, byId("accent").value);
byId("use-custom").addEventListener("click", custom); byId("accent").addEventListener("input", custom);
let palette = {}; try { palette = JSON.parse(localStorage.getItem("moss.agent.palette")) || {}; } catch {}
theme(palette.base, palette.custom);

const picker = byId("agent-model");
const modelNote = byId("agent-model-note");
let selecting = false, loaded = false;
async function models() {
  try {
    const response = await fetch("/api/models", {cache: "no-store"});
    if (!response.ok) throw new Error("模型列表暂不可用。");
    const body = await response.json();
    picker.replaceChildren(...body.models.map(model => {
      const option = new Option(model.label + (model.available ? "" : "（未就绪）"), model.id);
      option.dataset.model = model.model || "";
      if (model.reason) option.title = model.reason;
      option.disabled = !model.available; return option;
    }));
    picker.value = current?.model_id || body.selected; loaded = true; updateModel();
  } catch (e) { modelNote.textContent = e.message; }
}
function updateModel() {
  const running = current?.turns.some(t => t.status === "saving" || t.status === "thinking");
  picker.disabled = !loaded || selecting || Boolean(running);
  if (current?.model_id) picker.value = current.model_id;
  byId("cloud-context-note").hidden = !["deepseek","aliyun","kimi"].includes(current?.model_id);
  modelNote.textContent = running ? "当前轮处理完毕后可以切换。" : (picker.selectedOptions[0]?.dataset.model || "");
}
picker.addEventListener("change", async () => {
  const chosen = picker.value;
  selecting = true; picker.disabled = true;
  try {
    const response = await fetch("/api/select-model", {method: "POST", headers: {"Content-Type":"application/json"}, body:JSON.stringify({id: chosen})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || "模型切换失败");
    if (!current || result.revision >= current.revision) current = result;
  } catch (e) { status(e.message); }
  finally { selecting = false; updateModel(); }
});
document.querySelector(".preferences").addEventListener("toggle", e => { if (e.target.open && !loaded) models(); });
function draw(block, frequencies, sampleRate, analyser) {
  microphone = block ? sampleLens("listen", "mic", block, frequencies, sampleRate) : null;
  monitor.sample(0, block, frequencies, analyser, microphone?.dbfs);
  updateLens();
}
export async function submitRecognizedText(text, calendarTurn, replyTurn) {
  const chatId = current?.chat_id;
  const action = window.mossSubmitSpokenAction?.(text, chatId, calendarTurn, replyTurn);
  if (action) { await action; return; }
  showChat(true, false);
  const submit = window.mossSubmitSpokenMessage;
  const feedback = submit ? await submit(text, chatId, submitWindowReplyByVoice) : "语音未能提交。";
  if (feedback) status(feedback);
}

const voice = activateVoice({status, draw, contextKey: () => current?.chat_id,
  pendingCalendar: () => window.mossPendingCalendar?.(),
  replaceableReply: () => window.mossReplaceableReply?.(),
  targetsPlayback: text => window.mossIsPlaybackControl?.(text),
  submitOverlappingDecision: request => window.mossSubmitOverlappingDecision?.(request),
  onNextListening:() => speech.prepareOutput(),
  onActualSpeech:speech.prepareForSpokenInput,
  onAcceptedWake:speech.prepareForAcceptedWake,
  onConversationGranted:speech.prepareForConversation,
  onInterrupt: () => {
    if (!speech.active()) return false;
    speech.interruptTurn();
    status("语音已打断。");
    updateInterrupt();
    return true;
  }, send: submitRecognizedText});
const optionalSpeechReady = () => Boolean(current) && !document.hidden && document.hasFocus() && !voice.active() &&
  !playingMedia && !current.turns.some(turn => ["thinking","pending","saving"].includes(turn.status)) &&
  !byId("message").value.trim();
const staff = byId("staff-instrument") ? mountStaffConsole({
  playGreeting:text => speech.playWelcome(text, optionalSpeechReady)
}) : null;
if (byId("speech-space-settings")) mountSpeechSpace({
  playPreview:(text, amount, {signal}) => speech.playSpacePreview(text, amount, signal, optionalSpeechReady)
});
byId("interrupt").addEventListener("click", async () => {
  if (speech.active()) {
    speech.interruptTurn();
    status(voice.active() ? "已打断，我在听。" : "语音已打断。");
    updateInterrupt();
    return;
  }
  document.querySelectorAll(".media-audio").forEach(a => a.pause());
  speech.interruptTurn();
  const active = current?.turns.find(t => ["thinking","saving"].includes(t.status));
  if (active) {
    const response = await fetch("/api/cancel", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({turn_id:active.id})});
    if (!response.ok) status("当前轮状态已变化，请查看对话。");
  }
});
function updateInterrupt() {
  const working = current?.turns.some(turn => ["thinking","saving"].includes(turn.status));
  const playingAudio = [...document.querySelectorAll(".media-audio")].some(media => !media.paused && !media.ended && !media.error);
  byId("interrupt").disabled = !working && !speech.active() && !playingAudio;
}
function update(state) {
  const turn = state.turns.at(-1);
  const working = state.turns.some(turn => ["thinking","saving"].includes(turn.status));
  if (!current || current.chat_id !== state.chat_id) {
    if (current) {
      clearWindowReplyDraft();
      voice.stop("已停止上一段语音输入。");
      speech.interruptTurn();
    }
    // A loaded transcript is history unless this page joins an in-flight turn.
    captionTurnId = turn && ["thinking","saving"].includes(turn.status) ? turn.id : undefined;
  } else if (current.turns.at(-1)?.id !== turn?.id) {
    captionTurnId = turn?.id;
  }
  current = state; updateModel();
  acceptColdWake();
  thinking = Boolean(working);
  updateLens();
  updateInterrupt();
  stage.toggleAttribute("data-wait", Boolean(working));
  updateCaption();
  speech.observe(state);
}
window.addEventListener("moss-agent-state", e => update(e.detail));
for (const edge of ["play","playing","pause","ended","emptied","error"]) {
  document.addEventListener(edge, e => {
    if (e.target.matches?.(".media-audio")) updateInterrupt();
  }, true);
}
fetch("/api/state", {cache:"no-store"}).then(r => r.json()).then(s => { if (!current || s.revision >= current.revision) update(s); }).catch(() => {});
installProjection({narrow, mediaPresented, leaveMedia});
showChat(lightScene || query.get("chat") === "1" || sessionStorage.getItem("moss.agent.chat-open") === "1", false);
if (!drawer.hasAttribute("data-open")) showOpening();
