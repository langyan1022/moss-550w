import assert from "node:assert/strict";
import {activateSpeech} from "./static/agent-speech.js";

class Element extends EventTarget {
  append(...children) { this.children = children; }
  before(element) { this.previous = element; }
}

function fixture({holdSecondRequest = false, holdMicPreparation = false} = {}) {
  const nodes = Object.fromEntries(
    ["speaker-state", "interrupt", "message", "mic", "preferences"].map(key => [key, new Element()])
  );
  const resumes = [], requests = [], preparations = [], preparationKeepAlive = [], events = [];
  let releaseSecondRequest, finishMicPreparation;
  globalThis.window = new EventTarget();
  globalThis.document = Object.assign(new EventTarget(), {
    getElementById: id => nodes[id],
    createElement: () => new Element(),
    querySelector: () => nodes.preferences,
    querySelectorAll: () => [],
    hidden: false,
    hasFocus: () => true
  });
  Object.defineProperty(globalThis, "localStorage", {configurable:true, value:{getItem:() => null, setItem() {}}});
  globalThis.fetch = async (url, options) => {
    if (url === "/api/prepare-speech") {
      const payload = JSON.parse(options.body);
      preparations.push(payload);
      preparationKeepAlive.push(Boolean(options.keepalive));
      if (holdMicPreparation && payload.mic && !payload.release) {
        await new Promise(resolve => { finishMicPreparation = resolve; });
      }
      return {ok:true,status:204};
    }
    assert.equal(url, "/api/speak");
    requests.push(JSON.parse(options.body));
    if (holdSecondRequest && requests.length === 2) {
      await new Promise(resolve => { releaseSecondRequest = resolve; });
    }
    return {ok:true, headers:new Headers(), arrayBuffer:async () => new ArrayBuffer(0)};
  };
  globalThis.AudioContext = class {
    state = "suspended";
    sampleRate = 16000;
    currentTime = 0;
    resume() {
      return new Promise(resolve => resumes.push(() => { this.state = "running"; resolve(); }));
    }
    async decodeAudioData() {
      assert.equal(this.state, "running");
      return {duration:.01};
    }
    createBufferSource() {
      const source = {connect() {}, disconnect() {}, stop() { source.onended(); },
        start:() => { assert.equal(this.state, "running"); queueMicrotask(() => source.onended()); }};
      return source;
    }
    createAnalyser() {
      return {fftSize:32, frequencyBinCount:16, connect() {}, disconnect() {},
        getFloatTimeDomainData() {}, getByteFrequencyData() {}};
    }
  };
  const speech = activateSpeech(() => {}, () => {});
  for (const name of ["pending", "stop", "start", "end", "error"])
    window.addEventListener("moss-speech-" + name, event => events.push([name, event.detail.id]));
  const replay = nodes.interrupt.previous;
  const enabled = nodes.preferences.children[0].children[0];
  function submit(id, reply, chat = "qa-chat") {
    const initial = {chat_id:chat, turns:[]};
    const text = "测试请求 " + id;
    const final = {chat_id:chat, turns:[{id, user:text, status:"replied", reply, source:"context"}]};
    speech.userAction("message", {text}, initial);
    speech.observe(final);
    speech.observe(final);
  }
  return {speech, replay, enabled, composer:nodes.message, resumes, requests, preparations, preparationKeepAlive,
    events, submit, finishSecondRequest:() => releaseSecondRequest(), finishMicPreparation:() => finishMicPreparation()};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

{
  const f = fixture({holdMicPreparation:true});
  const state = {chat_id:"qa-chat", turns:[], speech_prepare:true, speech_mic_warm:true};
  const mic = new AbortController();
  f.speech.prepareOutput();
  f.resumes.shift()();
  f.speech.prepareForConversation(mic.signal);
  assert.deepEqual(f.preparations, []);
  f.speech.observe(state);
  document.hidden = true;
  f.speech.prepareForConversation(mic.signal);
  assert.deepEqual(f.preparations, []);
  document.hidden = false;
  f.speech.prepareForConversation(mic.signal);
  f.speech.prepareForConversation(mic.signal);
  const [{mic:id}] = f.preparations;
  assert.match(id, /^[0-9a-f]{32}$/);
  assert.deepEqual(f.preparations, [{mic:id}]);
  mic.abort();
  assert.deepEqual(f.preparations, [{mic:id}, {mic:id,release:true}]);
  f.finishMicPreparation();
  await settle();
  assert.deepEqual(f.preparations, [{mic:id}, {mic:id,release:true}, {mic:id,release:true}]);
  assert.deepEqual(f.preparationKeepAlive, [false,true,true]);
  assert.deepEqual(f.requests, []);
}

{
  for (const release of [
    f => { document.hidden = true; document.dispatchEvent(new Event("visibilitychange")); },
    f => { f.enabled.checked = false; f.enabled.dispatchEvent(new Event("change")); },
    f => { f.speech.observe({turns:[], speech_mic_warm:false}); },
    f => { window.dispatchEvent(new Event("pagehide")); }
  ]) {
    const f = fixture();
    const mic = new AbortController();
    f.speech.observe({turns:[], speech_mic_warm:true});
    f.speech.prepareOutput();
    f.resumes.shift()();
    f.speech.prepareForConversation(mic.signal);
    await settle();
    const [{mic:id}] = f.preparations;
    release(f);
    document.hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    mic.abort();
    assert.deepEqual(f.preparations, [{mic:id}, {mic:id,release:true}]);
    assert.deepEqual(f.requests, []);
  }
}

{
  const f = fixture({holdMicPreparation:true});
  const mic = new AbortController();
  f.speech.observe({turns:[], speech_mic_warm:true});
  f.speech.prepareOutput();
  f.resumes.shift()();
  f.speech.prepareForConversation(mic.signal);
  f.submit("active-after-preparation", "这轮仍然播报。");
  mic.abort();
  f.finishMicPreparation();
  await settle();
  assert.deepEqual(f.requests.map(row => row.text), ["这轮仍然播报。"]);
  assert.deepEqual(f.events, [["pending", "active-after-preparation"], ["start", "active-after-preparation"],
    ["end", "active-after-preparation"]]);
}

{
  const f = fixture();
  f.submit("fast", "电量六十三。");
  await settle();
  assert.deepEqual(f.events, [["pending", "fast"]]);
  assert.deepEqual(f.requests, []);
  f.resumes.shift()();
  await settle();
  assert.deepEqual(f.events, [["pending", "fast"], ["start", "fast"], ["end", "fast"]]);
  assert.deepEqual(f.requests.map(row => row.text), ["电量六十三。"]);
}

{
  const f = fixture();
  f.submit("stale", "上一轮。");
  f.speech.userAction("switch-chat", {}, {chat_id:"qa-other", turns:[]});
  f.submit("latest", "下一轮。", "qa-other");
  f.resumes.splice(0).forEach(resume => resume());
  await settle();
  assert.deepEqual(f.requests.map(row => row.text), ["下一轮。"]);
  assert.deepEqual(f.events, [["pending", "stale"], ["stop", "stale"],
    ["pending", "latest"], ["start", "latest"], ["end", "latest"]]);
}

{
  const f = fixture();
  f.submit("muted", "这句应当取消。");
  f.enabled.checked = false;
  f.enabled.dispatchEvent(new Event("change"));
  f.resumes.shift()();
  await settle();
  assert.deepEqual(f.requests, []);
  assert.deepEqual(f.events, [["pending", "muted"], ["stop", "muted"]]);
}

{
  const f = fixture();
  const failed = new Promise(resolve => window.addEventListener("moss-speech-error", resolve, {once:true}));
  f.submit("late", "点重播再听。");
  await failed;
  assert.equal(f.replay.hidden, false);
  assert.deepEqual(f.requests, []);
  f.replay.dispatchEvent(new Event("click"));
  assert.equal(f.resumes.length, 2);
  f.resumes.pop()();
  await settle();
  assert.deepEqual(f.events, [["pending", "late"], ["error", "late"],
    ["pending", "late"], ["start", "late"], ["end", "late"]]);
  assert.deepEqual(f.requests.map(row => row.text), ["点重播再听。"]);
}

{
  const state = {chat_id:"qa-chat",turns:[],speech_prepare:true};
  for (const text of ["请帮我朗读以下文字：潮", "请把下面这段读给我听：潮", "麻烦您帮我朗诵这段正文:潮"]) {
    const f = fixture();
    f.speech.userAction("message", {text}, state);
    assert.deepEqual(f.preparations, [{}], text);
    assert.deepEqual(f.requests, [], text);
  }
  for (const text of ["“请帮我朗读以下文字：潮”", "请不要把下面这段读给我听：潮",
      "请先翻译下面这段再读给我听：潮", "请把下面这段读给我听", "请把下面这段读给我听："]) {
    const f = fixture();
    f.speech.userAction("message", {text}, state);
    assert.deepEqual(f.preparations, [], text);
  }
  const f = fixture();
  f.speech.observe(state);
  f.composer.value = "  请把下面这段读给我听：潮";
  const input = new Event("input");
  Object.defineProperty(input, "isTrusted", {value:true});
  f.composer.dispatchEvent(input);
  assert.deepEqual(f.preparations, []);
  await new Promise(resolve => setTimeout(resolve, 600));
  assert.deepEqual(f.preparations, [{}]);
  assert.deepEqual(f.requests, []);
}

{
  const f = fixture({holdSecondRequest:true});
  const state = {chat_id:"qa-chat", turns:[], speech_prepare:false};
  const text = "第一句很短。" + "海山水月星云天风".repeat(10) + "。";
  const user = "请朗读以下文字：[pause 600ms]" + text;
  f.speech.userAction("message", {text:user}, state);
  f.speech.observe({...state, turns:[{id:"pause", user, status:"replied", reply:text, source:"narration",
    narration:{category:"reply", cues:[{pause_ms:600}, {text}]}}]});
  const ended = new Promise(resolve => window.addEventListener("moss-speech-end", resolve, {once:true}));
  const startedWaiting = performance.now();
  f.resumes.shift()();
  await settle();
  assert.deepEqual(f.events, [["pending", "pause"]]);
  assert.equal(f.requests.length, 2, "Submit the next clip during the requested pause.");
  f.finishSecondRequest();
  await ended;
  assert(performance.now() - startedWaiting >= 570);
  assert.deepEqual(f.events, [["pending", "pause"], ["start", "pause"], ["start", "pause"], ["end", "pause"]]);
}

console.log("Speech unlock, preparation and explicit-pause prefetch passed");
