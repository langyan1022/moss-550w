(() => {
  const panel = document.getElementById("jev-handoff-panel");
  if (!panel) return;
  const preview = document.getElementById("jev-handoff-preview");
  const status = document.getElementById("jev-handoff-status");
  const error = document.getElementById("jev-handoff-error");
  const close = document.getElementById("jev-handoff-close");
  const confirm = document.getElementById("jev-handoff-confirm");
  let review = null;

  function open({chatId, turnId, text, trigger}) {
    if (document.querySelector("dialog[open]") || !chatId || !turnId || typeof text !== "string" || !text.trim()) return false;
    review = {chatId, turnId, trigger, bytes:new TextEncoder().encode(text).length};
    preview.textContent = text;
    status.textContent = error.textContent = "";
    status.hidden = error.hidden = true;
    close.textContent = "取消";
    close.disabled = confirm.disabled = false;
    panel.dataset.phase = "review";
    panel.showModal();
    return true;
  }

  function reset() {
    review = null;
    preview.textContent = "";
    if (panel.open) panel.close();
  }

  panel.addEventListener("cancel", event => {
    if (panel.dataset.phase === "pending") event.preventDefault();
  });

  panel.addEventListener("close", () => {
    if (panel.open) return;
    const trigger = review?.trigger;
    reset();
    if (trigger?.isConnected && !trigger.disabled && !document.querySelector("dialog[open]")) {
      trigger.focus({preventScroll:true});
    }
  });

  confirm.addEventListener("click", async () => {
    const submitted = review;
    if (!submitted || !panel.open || panel.dataset.phase !== "review") return;
    close.textContent = "关闭";
    status.textContent = "正在填写本机草稿窗…";
    status.hidden = false;
    status.focus({preventScroll:true});
    confirm.disabled = close.disabled = true;
    panel.dataset.phase = "pending";

    let failure = "未收到可核实的填写回执，请查看本机草稿窗。";
    let filled = false;
    try {
      const response = await fetch("/api/context/jev-handoff", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({chat_id:submitted.chatId, turn_id:submitted.turnId, operation_id:crypto.randomUUID()})
      });
      const result = await response.json();
      filled = response.ok && result.status === "filled" && result.bytes === submitted.bytes;
      if (!response.ok && typeof result.error === "string" && result.error.trim()) failure = result.error;
    } catch {
      // 提交后连接可能中断，填写结果以本机窗口为准。
    }
    if (review !== submitted || !panel.open) return;
    if (filled) {
      status.textContent = "已填入本机草稿窗。";
    } else {
      status.hidden = true;
      error.textContent = failure;
      error.hidden = false;
    }
    panel.dataset.phase = filled ? "filled" : "error";
    close.disabled = false;
    close.focus({preventScroll:true});
  });

  window.mossJevHandoff = {open, reset};
})();
