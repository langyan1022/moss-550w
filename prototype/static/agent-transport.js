export function restoreMediaSource(node) {
  const source = node?.dataset.parkedSrc;
  if (!source) return;
  const seconds = Number(node.dataset.parkedTime);
  delete node.dataset.parkedSrc;
  delete node.dataset.parkedTime;
  node.src = source;
  if (seconds > 0) node.currentTime = seconds;
}

function startOfClip(node) {
  const fragment = new URL(node.currentSrc || node.src).hash;
  const match = /^#t=(?:npt:)?(\d+(?:\.\d+)?)(?:,|$)/.exec(fragment);
  return match ? Number(match[1]) : 0;
}

document.addEventListener("play", event => {
  if (event.target.matches?.(".media-audio")) delete event.target.dataset.mossStopped;
}, true);
document.addEventListener("seeked", event => {
  const node = event.target;
  if (node.dataset?.mossStopped && Math.abs(node.currentTime - startOfClip(node)) > .1) delete node.dataset.mossStopped;
}, true);

function withDeadline(promise, milliseconds) {
  let timer;
  const limit = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("播放器操作超时")), milliseconds);
  });
  return Promise.race([promise, limit]).finally(() => clearTimeout(timer));
}

async function operate(instruction, current) {
  const {node} = current();
  function matches() {
    const {node: actual, snapshot} = current();
    return node?.isConnected && actual === node && snapshot?.turn_id === instruction.target_turn &&
      snapshot.media_id === instruction.media_id;
  }
  if (!matches()) return {event:"unavailable"};
  try {
    restoreMediaSource(node);
    if (instruction.action === "resume") {
      await withDeadline(node.play(), 8000);
    } else {
      node.pause();
      if (instruction.action === "stop") {
        const start = startOfClip(node);
        if (Math.abs(node.currentTime - start) > .025) {
          const controller = new AbortController();
          const settled = new Promise(resolve => node.addEventListener("seeked", resolve, {once:true, signal:controller.signal}));
          try {
            node.currentTime = start;
            if (node.seeking) await withDeadline(settled, 3000);
          } finally { controller.abort(); }
        }
        if (Math.abs(node.currentTime - start) > .1) return {event:"failed"};
        node.dataset.mossStopped = "1";
      }
    }
    if (!matches()) return {event:"unavailable"};
    if (instruction.action === "resume" ? node.paused || node.ended || node.error : !node.paused) return {event:"failed"};
    const event = {pause:"paused", resume:"playing", stop:"stopped"}[instruction.action];
    return {event, position:node.currentTime};
  } catch (error) {
    if (!matches()) return {event:"unavailable"};
    if (instruction.action === "resume") node.pause();
    return {event:error.name === "NotAllowedError" ? "blocked" : "failed"};
  }
}

async function apply(state, requestId, current, pending) {
  const turn = state.turns?.at(-1);
  const instruction = turn?.transport;
  if (!requestId || turn?.status !== "player_requested" || instruction?.request_id !== requestId) return state;
  let result;
  if (instruction.queued) {
    const target = pending?.target;
    if (target?.turn_id !== instruction.queued.turn_id || target.media_id !== instruction.queued.media_id || !pending.cancel()) {
      result = {event:"unavailable"};
    } else {
      result = instruction.control_live ? await operate(instruction, current) : {event:"queue_cancelled"};
      result.cancelled_queue = true;
    }
  } else {
    result = await operate(instruction, current);
  }
  const body = JSON.stringify({turn_id:turn.id, chat_id:turn.chat_id, request_id:requestId,
    target_turn:instruction.target_turn, media_id:instruction.media_id, ...result});
  const send = () => fetch("/api/transport-result", {method:"POST", headers:{"Content-Type":"application/json"},body});
  let response;
  try { response = await send(); } catch { response = await send(); }
  const next = await response.json();
  if (!response.ok) throw new Error(next.error || "无法登记播放器回执。");
  return next;
}

window.mossTransport = {apply};
