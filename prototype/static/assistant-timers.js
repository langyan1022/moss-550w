(() => {
  let timers = [];
  let wake;
  let shown = null;
  let state;
  let requested = /^#reminder=(\d+)$/.exec(location.hash)?.[1];
  const dueFormat = new Intl.DateTimeFormat("zh-CN", {
    year:"numeric", month:"long", day:"numeric", hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23"
  });
  const panel = document.createElement("aside");
  panel.className = "assistant-reminder";
  panel.dataset.testid = "timer-notice";
  panel.setAttribute("aria-label", "到点提醒");
  panel.hidden = true;
  const heading = document.createElement("strong");
  const message = document.createElement("p");
  message.setAttribute("role", "status");
  const scheduled = document.createElement("time");
  scheduled.dataset.testid = "timer-due-time";
  scheduled.hidden = true;
  const done = document.createElement("button");
  done.type = "button";
  done.textContent = "知道了";
  done.dataset.testid = "timer-dismiss";
  const error = document.createElement("small");
  error.setAttribute("role", "status");
  const openSource = document.createElement("button");
  openSource.type = "button";
  openSource.className = "assistant-reminder-source";
  openSource.dataset.testid = "timer-source";
  openSource.style.gridArea = "auto / 1";
  const warning = document.createElement("p");
  warning.className = "turn-error";
  warning.hidden = true;
  warning.setAttribute("role", "status");
  document.querySelector("#history-state, .connection")?.after(warning);
  panel.append(heading, message, scheduled, done, openSource, error);
  document.body.append(panel);

  function placePanel() {
    const active = document.activeElement?.closest("dialog:modal") || document.querySelector("dialog:modal");
    const parent = active || document.body;
    if (panel.parentElement === parent) return;
    if (active) {
      const header = active.querySelector(":scope > header");
      if (header) header.after(panel);
      else active.prepend(panel);
    } else parent.append(panel);
    panel.toggleAttribute("data-in-dialog", Boolean(active));
  }

  new MutationObserver(records => {
    if (!panel.hidden && records.some(({target}) => target.tagName === "DIALOG")) placePanel();
  }).observe(document.body, {attributes:true, attributeFilter:["open"], subtree:true});

  function clearRequested() {
    requested = null;
    history.replaceState(null, "", location.pathname + location.search);
  }

  function render() {
    if (!state) return;
    clearTimeout(wake);
    const now = Date.now();
    const linked = requested && timers.find(timer => String(timer.id) === requested);
    const linkedDue = linked && linked.due_at * 1000 <= now;
    const first = linkedDue ? linked : timers[0];
    const remaining = first && first.due_at * 1000 - now;
    if (remaining > 0) wake = setTimeout(render, Math.min(Math.ceil(remaining), 60000));
    if (requested && !linked && (!first || remaining > 0)) {
      shown = "closed";
      heading.textContent = "这条提醒已处理";
      message.textContent = "可以关闭此提示。";
      scheduled.hidden = true;
      scheduled.removeAttribute("datetime");
      error.textContent = "";
      done.disabled = false;
      openSource.hidden = true;
      placePanel();
      panel.hidden = false;
      return;
    }
    if (requested && !linkedDue) clearRequested();
    if (!first || remaining > 0) {
      panel.hidden = true;
      shown = null;
      return;
    }
    if (shown === first.id) return;
    shown = first.id;
    heading.textContent = "提醒时间到了";
    message.textContent = first.label;
    const due = new Date(first.due_at * 1000);
    scheduled.hidden = !Number.isFinite(due.getTime());
    if (!scheduled.hidden) {
      scheduled.dateTime = due.toISOString();
      scheduled.textContent = `原定 ${dueFormat.format(due)}`;
    }
    openSource.hidden = !first.chat_id;
    openSource.disabled = false;
    openSource.textContent = first.media ? "播放" + (first.media.title ? "《" + first.media.title + "》" : "提醒里的媒体") : "查看原对话";
    error.textContent = "";
    done.disabled = false;
    placePanel();
    panel.hidden = false;
  }

  openSource.addEventListener("click", async () => {
    const item = timers.find(timer => timer.id === shown);
    if (!item || openSource.disabled) return;
    openSource.disabled = true;
    try {
      const media = item.media;
      const chat = media?.chat_id || item.chat_id;
      if (state.turns.some(turn => activeStates.has(turn.status))) throw new Error("请先完成当前操作，再打开提醒的原对话。");
      if (state.chat_id !== chat && !await post("switch-chat", {id:chat})) throw new Error("请先完成当前操作，再打开提醒的原对话。");
      const view = views.get(media?.turn_id || item.turn_id);
      if (!view) throw new Error("原对话里已经找不到这条记录。");
      if (document.querySelector("#dialogue")?.hidden) document.querySelector("#history-toggle")?.click();
      view.article.scrollIntoView({block:"center"});
      if (media) {
        const source = state.turns.find(row => row.id === media.turn_id);
        const rows = source?.media?.items || [];
        const index = media.index ?? (rows.findIndex(row => row.media_id === media.media_id) + 1);
        if (rows[index - 1]?.media_id !== media.media_id) throw new Error("原媒体条目已失效。");
        if (!window.mossProjection) throw new Error("播放器尚未就绪，请稍后重试。");
        await view.chooseTrack(rows[index - 1], index);
      }
    } catch (reason) {
      error.textContent = reason.message;
    } finally {
      if (shown === item.id) openSource.disabled = false;
    }
  });

  done.addEventListener("click", async () => {
    if (shown === null || done.disabled) return;
    const id = shown;
    const host = panel.closest("dialog:modal");
    const restoreFocus = host && document.activeElement === done;
    done.disabled = true;
    try {
      if (id === "closed") {
        clearRequested();
        render();
        return;
      }
      const response = await fetch("/api/timer-dismiss", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id})
      });
      if (!response.ok) throw new Error("提醒状态未保存，请重试。");
      if (requested === String(id)) clearRequested();
      timers = timers.filter(timer => timer.id !== id);
      render();
    } catch {
      if (shown === id) {
        error.textContent = "提醒状态未保存，请重试。";
        done.disabled = false;
        if (restoreFocus && document.activeElement === document.body) done.focus();
      }
    } finally {
      if (restoreFocus && host.open && document.activeElement === document.body && (shown !== id || panel.hidden)) {
        host.querySelector('header button[autofocus], header form[method="dialog"] button:not(:disabled)')?.focus();
      }
    }
  });
  window.addEventListener("moss-agent-state", event => {
    state = event.detail;
    warning.textContent = state.timer_windows_error || "";
    warning.hidden = !warning.textContent;
    timers = event.detail.timers || [];
    render();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) render();
  });
})();
