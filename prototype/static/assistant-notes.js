const dialog = document.querySelector("#notes-panel");
const editor = document.querySelector("#notes-editor");
const body = document.querySelector("#notes-body");
const label = document.querySelector("#notes-label");
const cancel = document.querySelector("#notes-cancel");
const filter = document.querySelector("#notes-filter");
const query = document.querySelector("#notes-query");
const archive = document.querySelector("#notes-archive");
const status = document.querySelector("#notes-status");
const list = document.querySelector("#notes-list");
const more = document.querySelector("#notes-more");
let editing = null;
let cursor = null;
let loadController;
let saving = false;

async function read(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "便签请求未完成。");
  return data;
}

function button(text, handler, accessibleName) {
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = text;
  control.setAttribute("aria-label", accessibleName);
  control.addEventListener("click", handler);
  return control;
}

function begin(note = null) {
  editing = note;
  body.value = note?.text || "";
  label.textContent = note ? `编辑 ${note.id} 号便签` : "新便签";
  cancel.hidden = !note;
}

function render(note) {
  const item = document.createElement("li");
  const number = document.createElement("small");
  const text = document.createElement("p");
  number.textContent = `${note.id} 号`;
  text.textContent = note.text;
  item.append(number, text);
  if (note.archived) {
    item.append(button("恢复", () => update(note, "restore"), `恢复 ${note.id} 号便签`));
  } else {
    item.append(
      button("编辑", () => {
        if (saving) return;
        begin(note);
        body.focus();
      }, `编辑 ${note.id} 号便签`),
      button("归档", () => update(note, "archive"), `归档 ${note.id} 号便签`),
    );
  }
  return item;
}

async function load({append = false, message = ""} = {}) {
  loadController?.abort();
  const controller = new AbortController();
  loadController = controller;
  const params = new URLSearchParams({
    q: query.value, archived: archive.checked ? "1" : "0",
    before: append ? String(cursor) : "0",
  });
  more.disabled = true;
  try {
    const page = await fetch("/api/notes?" + params, {cache: "no-store", signal: controller.signal}).then(read);
    if (controller.signal.aborted) return;
    if (!append) list.replaceChildren();
    list.append(...page.items.map(render));
    cursor = page.before;
    more.hidden = !cursor;
    status.textContent = message || (list.childElementCount ? "" : "此处没有便签。");
  } catch (error) {
    if (!controller.signal.aborted) {
      list.replaceChildren();
      more.hidden = true;
      status.textContent = message ? message + "列表刷新失败。" : "暂时无法读取便签。";
    }
  } finally {
    if (!controller.signal.aborted) more.disabled = false;
  }
}

async function update(note = null, action = "", text = null) {
  if (saving) return;
  saving = true;
  editor.inert = true;
  status.textContent = "正在保存…";
  try {
    const payload = note ? {version: note.version, action, ...(text === null ? {} : {text})} : {text};
    const response = await fetch(note ? `/api/notes/${note.id}` : "/api/notes", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
    if (response.status === 409) {
      const error = await response.json();
      await load({message: error.error});
      return;
    }
    const {item} = await read(response);
    if (text !== null || editing?.id === item.id) begin();
    const message = action === "archive" ? "已归档。" : action === "restore" ? "已恢复。" : `已保存 ${item.id} 号便签。`;
    await load({message});
  } catch (error) {
    status.textContent = error.message || "暂时无法保存便签。";
  } finally {
    saving = false;
    editor.inert = false;
  }
}

document.querySelector('[data-panel="notes-panel"]').addEventListener("click", () => load());
dialog.addEventListener("close", () => loadController?.abort());
editor.addEventListener("submit", event => {
  event.preventDefault();
  if (body.value.trim()) update(editing, "edit", body.value);
});
cancel.addEventListener("click", () => {
  begin();
  body.focus();
});
filter.addEventListener("submit", event => {
  event.preventDefault();
  load();
});
archive.addEventListener("change", () => load());
more.addEventListener("click", () => load({append: true}));
