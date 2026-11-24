const tools = {
  clipboard:"读取剪贴板", screen_text:"选择窗口并识别", screenshot:"选择窗口截图",
  screen_image:"选择窗口画面"
};
const widgets = new WeakMap();
const MAX_VISION_BYTES = 8 * 1024 * 1024;
const REPLY_DRAFT_LIFETIME_MS = 120_000;
const REPLY_DRAFT_VOICE = /^(?:(?:请你?|帮我|麻烦你?)\s*)?(?:回复?(?:一下)?(?:刚才)?[这那]条(?:来信|消息)?|(?:就)?[按用](?:刚才)?[这那]段(?:文字|来信)?(?:生成(?:一条)?回复|拟(?:一句)?回复))(?:吧|好吗)?$/u;
let contextReplyDraft = null;

function make(tag, text) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  return node;
}

function button(text) {
  const node = make("button", text);
  node.type = "button";
  return node;
}

async function captureWindow(stillCurrent, action) {
  const windowOnly = action === "screen_text" || action === "screen_image";
  const stream = await navigator.mediaDevices.getDisplayMedia(windowOnly
    ? {video:{displaySurface:"window"}, audio:false, monitorTypeSurfaces:"exclude"}
    : {video:true, audio:false});
  const video = make("video");
  video.muted = true;
  video.srcObject = stream;
  try {
    if (!stillCurrent()) throw new DOMException("", "AbortError");
    const surface = windowOnly ? stream.getVideoTracks()[0]?.getSettings().displaySurface : null;
    if (action === "screen_text" && surface === "monitor") {
      throw new Error("请选择一个窗口或浏览器标签页来识别文字。");
    }
    if (action === "screen_image" && surface !== "window" && surface !== "browser") {
      throw new Error("请选择浏览器能确认的窗口或标签页。");
    }
    await video.play();
    if (!stillCurrent()) throw new DOMException("", "AbortError");
    if (!video.videoWidth || !video.videoHeight) throw new Error("这个窗口没有返回画面。");
    const maxSide = action === "screen_image" ? 2048 : 4096;
    const scale = Math.min(1, maxSide / Math.max(video.videoWidth, video.videoHeight),
                           Math.sqrt((8 * 1024 * 1024) / (video.videoWidth * video.videoHeight)));
    const canvas = make("canvas");
    canvas.width = Math.max(1, Math.floor(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.floor(video.videoHeight * scale));
    canvas.getContext("2d", {alpha:false}).drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas;
  } finally {
    stream.getTracks().forEach(track => track.stop());
    video.srcObject = null;
  }
}

function selectedText(source) {
  const selection = window.getSelection();
  const selected = source instanceof HTMLTextAreaElement
    ? source.value.slice(source.selectionStart, source.selectionEnd)
    : selection?.rangeCount && source.contains(selection.getRangeAt(0).commonAncestorContainer) ? selection.toString() : "";
  return selected.trim() || (source instanceof HTMLTextAreaElement ? source.value : source.textContent).trim();
}

export async function submitWindowReplyByVoice(text, state) {
  const words = text.normalize("NFKC").trim().replace(/^moss[\s,:]*/iu, "").replace(/[。!?…]+$/u, "").trim();
  if (!REPLY_DRAFT_VOICE.test(words)) return null;
  const draft = contextReplyDraft;
  const last = state?.turns?.at(-1);
  if (!draft || !draft.editor.isConnected || draft.chat !== state?.chat_id ||
      draft.turn !== last?.id || draft.action !== last.context_action || last.status !== "replied" ||
      performance.now() >= draft.expiry) {
    clearWindowReplyDraft();
    return "请先打开当前来信的拟回复编辑框。";
  }
  if (draft.editor.disabled) return "这段来信正在提交。";
  if (draft.submit.disabled) return draft.issue();
  return await draft.send() ? "已提交这段来信，正在拟回复。" : "来信仍在编辑框，可以再试。";
}

export function clearWindowReplyDraft() {
  contextReplyDraft = null;
}

function draftAction(source, origin, generate, sourceTurn) {
  const box = make("div");
  box.className = "context-reply-form";
  const start = button("用这段文字拟回复");
  start.onclick = () => {
    const editor = make("textarea");
    editor.setAttribute("aria-label", "对方来信");
    editor.rows = 4;
    editor.value = selectedText(source);
    const goal = make("textarea");
    goal.setAttribute("aria-label", "回复要求（可选）");
    goal.rows = 2;
    goal.placeholder = "如：周四不行，改周五下午三点";
    const incomingField = make("label");
    incomingField.append(make("span", "对方来信"), editor);
    const goalField = make("label");
    goalField.append(make("span", "回复要求（可选）"), goal);
    const submit = button("生成回复");
    const cancel = button("取消拟回复");
    const hint = make("small");
    const actions = make("div");
    actions.className = "context-reply-actions";
    actions.append(submit, cancel);
    const encoder = new TextEncoder();
    let submission;
    function issue() {
      const incoming = editor.value.trim();
      if (!incoming) return "请填写要回复的来信。";
      const text = "帮我回复：“" + incoming + "”";
      if ([...text].length > 1200 || encoder.encode(text).length > 2700) return "来信过长，请只保留要回复的部分。";
      const requirement = goal.value.trim();
      if ([...requirement].length > 500 || encoder.encode(requirement).length > 1500) return "回复要求过长，请缩短后提交。";
      return "";
    }
    const refresh = () => {
      const error = issue();
      submit.disabled = Boolean(error);
      editor.setAttribute("aria-invalid", String(error.startsWith("来信过长")));
      goal.setAttribute("aria-invalid", String(error.startsWith("回复要求过长")));
      hint.textContent = error || origin + " · 提交后将来信和回复要求交给当前模型。也可说“用这段生成回复”。";
    };
    editor.oninput = goal.oninput = refresh;
    function clearDraft() {
      if (contextReplyDraft?.editor === editor) clearWindowReplyDraft();
    }
    cancel.onclick = () => { clearDraft(); box.replaceChildren(start); start.focus(); };
    async function send() {
      if (submit.disabled || editor.disabled) return false;
      submit.disabled = true;
      editor.disabled = goal.disabled = cancel.disabled = true;
      const incoming = editor.value.trim(), requirement = goal.value.trim();
      if (!submission || submission.incoming !== incoming || submission.requirement !== requirement) {
        submission = {incoming, requirement, id:crypto.randomUUID().replaceAll("-", "")};
      }
      const accepted = await generate(incoming, requirement, submission.id);
      if (accepted) {
        clearDraft();
        box.replaceChildren(make("small", "已提交拟回复。"));
      }
      else { editor.disabled = goal.disabled = cancel.disabled = false; refresh(); }
      return accepted;
    }
    submit.onclick = send;
    contextReplyDraft = {chat:sourceTurn.chat_id, turn:sourceTurn.id, action:sourceTurn.context_action,
                         editor, submit, send, issue, expiry:performance.now() + REPLY_DRAFT_LIFETIME_MS};
    refresh();
    box.replaceChildren(incomingField, goalField, hint, actions);
    editor.focus();
  };
  box.append(start);
  return box;
}

function questionActions(source, options) {
  const box = make("div");
  const ask = button("就这段文字提问"), summary = button("总结这段文字");
  function edit(summarizing) {
    const model = options.model();
    const editor = make("textarea");
    editor.setAttribute("aria-label", "交给模型的窗口文字");
    editor.rows = 4;
    editor.value = selectedText(source);
    const question = make("textarea");
    question.setAttribute("aria-label", "针对窗口文字的问题");
    question.placeholder = "想了解这段文字的什么内容？";
    question.rows = 2;
    question.value = summarizing ? "请简要总结这段窗口文字。" : "";
    const hint = make("small");
    const submit = button(summarizing ? "提交摘要" : "提交问题");
    const cancel = button("取消");
    let pending = false;
    function refresh() {
      const length = [...editor.value.trim()].length;
      const prompt = question.value.trim();
      const longQuestion = [...prompt].length > 1200 || new TextEncoder().encode(prompt).length > 2700;
      submit.disabled = pending || !length || length > 6000 || !prompt || longQuestion;
      hint.textContent = length > 6000 ? "请选择 6000 字以内的窗口文字。" : longQuestion ? "请缩短问题再提交。" :
        "提交后，以下文字和问题将交给" + (model.id === "local" ? "本机模型 " : "云端模型 ") + model.name + "。";
    }
    editor.oninput = question.oninput = refresh;
    cancel.onclick = () => { box.replaceChildren(ask, summary); (summarizing ? summary : ask).focus(); };
    submit.onclick = async () => {
      pending = true;
      refresh();
      editor.disabled = question.disabled = cancel.disabled = true;
      if (await options.send(question.value.trim(), editor.value.trim(), model.id)) {
        box.replaceChildren(ask, summary);
      } else {
        editor.disabled = question.disabled = cancel.disabled = pending = false;
        refresh();
      }
    };
    box.replaceChildren(hint, editor, question, submit, cancel);
    refresh();
    (summarizing ? editor : question).focus();
  }
  ask.onclick = () => edit(false);
  summary.onclick = () => edit(true);
  box.append(ask, summary);
  return box;
}

function showText(container, text, empty, origin, generate, questions, sourceTurn) {
  const result = make("pre", text || empty);
  result.style.whiteSpace = "pre-wrap";
  result.style.overflowWrap = "anywhere";
  result.style.maxHeight = "18rem";
  result.style.overflow = "auto";
  result.dataset.testid = "context-output";
  container.replaceChildren(result);
  if (text?.trim()) {
    if (questions) container.append(questionActions(result, questions));
    container.append(draftAction(result, origin, generate, sourceTurn));
  }
}

function saveScreenshot(canvas, blob, output) {
  canvas.style.width = "100%";
  canvas.style.height = "auto";
  canvas.setAttribute("aria-label", "所选窗口的截图");
  const save = button("保存 PNG");
  save.onclick = () => {
    const link = make("a");
    const url = URL.createObjectURL(blob);
    link.href = url;
    link.download = "MOSS-" + new Date().toISOString().replace(/:/g, "-").slice(0,19) + ".png";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 3000);
  };
  output.replaceChildren(canvas, save);
}

function encodeScreenshot(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1]);
    reader.onerror = () => reject(new Error("无法读取这张截图。"));
    reader.readAsDataURL(blob);
  });
}

function imageQuestionActions(blob, vision, alive, onSubmitted) {
  const box = make("div");
  const ask = button("就这张图提问");
  ask.onclick = () => {
    if (!alive()) return;
    const model = vision?.model();
    const available = model?.id === "aliyun" && model.available;
    const question = make("textarea");
    question.setAttribute("aria-label", "针对窗口画面的问题");
    question.placeholder = "想从画面里了解什么？";
    question.rows = 2;
    const submit = button("发送到阿里云百炼");
    const cancel = button("取消");
    const hint = make("small", available
      ? `预览中的图片和问题将发给阿里云百炼（${model.model}）。图片仅用于本轮问答。`
      : "阿里云百炼视觉当前不可用。");
    const status = make("small");
    status.setAttribute("role", "status");
    let pending = false;
    function refresh() {
      const text = question.value.trim();
      const tooLong = [...text].length > 1200 || new TextEncoder().encode(text).length > 2700;
      submit.disabled = pending || !available || !text || tooLong || blob.size > MAX_VISION_BYTES;
      status.textContent = pending ? "正在提交截图…" : tooLong ? "请缩短问题再提交。" :
        blob.size > MAX_VISION_BYTES ? "截图超过 8 MiB，请选择较小的窗口。" : "";
    }
    question.oninput = refresh;
    cancel.onclick = () => { box.replaceChildren(ask); ask.focus(); };
    submit.onclick = async () => {
      if (!alive() || submit.disabled) return;
      const destination = vision.model();
      if (!destination?.available || destination.id !== model.id || destination.model !== model.model) {
        status.textContent = "视觉模型已更新，请重新打开提问并核对。";
        return;
      }
      const text = question.value.trim();
      pending = question.disabled = cancel.disabled = true;
      refresh();
      let error = "提交未完成，图片和问题仍在这里。";
      try {
        const encoded = await encodeScreenshot(blob);
        if (!alive()) return;
        if (await vision.send(text, encoded, model.id)) {
          if (alive()) onSubmitted();
          return;
        }
      } catch (failure) {
        error = failure?.message || error;
      }
      if (!alive()) return;
      pending = question.disabled = cancel.disabled = false;
      refresh();
      status.textContent = error;
    };
    box.replaceChildren(hint, question, submit, cancel, status);
    refresh();
    question.focus();
  };
  box.append(ask);
  return box;
}

export function mountContextTool(container, turn, generate, questions, vision) {
  const action = turn.context_action;
  const previous = widgets.get(container);
  if (previous && previous.id === turn.id && previous.action === action) return;
  if (previous && previous.id === contextReplyDraft?.turn) clearWindowReplyDraft();
  previous?.abort.abort();
  container.replaceChildren();
  widgets.delete(container);
  if (!Object.hasOwn(tools, action)) return;
  const abort = new AbortController();
  const current = () => container.isConnected && widgets.get(container)?.abort === abort && !abort.signal.aborted;
  widgets.set(container, {id:turn.id, action, abort});
  const trigger = button(tools[action]);
  trigger.dataset.testid = "context-action";
  const output = make("div");
  output.setAttribute("role","status");
  let sequence = 0;
  const clear = button("清除");
  clear.hidden = true;
  function clearOutput() {
    if (contextReplyDraft?.turn === turn.id) clearWindowReplyDraft();
    output.replaceChildren();
  }
  clear.onclick = () => { sequence++; clearOutput(); clear.hidden = true; trigger.disabled = false; };
  container.append(trigger, clear, output);
  trigger.onclick = async () => {
    const request = ++sequence;
    const alive = () => sequence === request && current();
    trigger.disabled = true;
    clear.hidden = true;
    clearOutput();
    try {
      if (action === "clipboard") {
        const text = await navigator.clipboard.readText();
        if (alive()) showText(output, text, "剪贴板中没有文字。", "剪贴板", generate, null, turn);
      } else {
        const canvas = await captureWindow(alive, action);
        if (!alive()) return;
        const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
        if (!blob) throw new Error("无法生成截图。");
        if (action === "screenshot" || action === "screen_image") {
          if (!alive()) return;
          saveScreenshot(canvas, blob, output);
          if (action === "screen_image") {
            canvas.dataset.testid = "window-image-preview";
            canvas.setAttribute("role", "img");
            output.append(imageQuestionActions(blob, vision, alive, () => {
              output.replaceChildren(make("small", "已提交画面提问。"));
              clear.hidden = true;
            }));
          }
        } else {
          output.textContent = "正在本机识别…";
          const response = await fetch("/api/context/ocr", {method:"POST", headers:{"Content-Type":"image/png"},
            body:blob, signal:abort.signal, cache:"no-store"});
          if (!alive()) return;
          if (response.status === 413) throw new Error("截图超过 8 MiB，请选择较小的窗口。");
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "本机文字识别暂不可用。");
          showText(output, result.text, "没有识别到文字。", "窗口识别", generate, questions, turn);
        }
      }
    } catch (error) {
      if (!alive()) return;
      if (action === "clipboard" && (error.name === "NotAllowedError" || error.name === "TypeError")) {
        const area = make("textarea");
        area.placeholder = "在这里粘贴文字（Ctrl+V）";
        area.setAttribute("aria-label", "粘贴剪贴板文字");
        output.replaceChildren(make("span","浏览器未授予剪贴板权限。"), area, draftAction(area, "剪贴板", generate, turn));
        area.focus();
      } else if (error.name === "NotAllowedError" || error.name === "AbortError") {
        output.textContent = "窗口未共享，可重新选择。";
      } else {
        output.textContent = error.message || "这次操作未完成。";
      }
    } finally {
      if (alive()) { trigger.disabled = false; clear.hidden = !output.hasChildNodes(); }
    }
  };
}

window.mossContextTools = {mountContextTool};
