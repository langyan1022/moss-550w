const REFRESH_MS = 5000;
const GIB = 2 ** 30;
const title = "MOSS · 本机状态";
const stylesheet = "/static/device-status.css?v=status-window";
const clockFormat = new Intl.DateTimeFormat("zh-CN", {
  hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23"
});
const setText = (node, value) => { if (node.textContent !== value) node.textContent = value; };
const gib = bytes => (bytes / GIB).toFixed(1);
const clockText = time => clockFormat.format(time);

function deviceFeed() {
  const views = new Map();
  let host, sampleTimer, clockTimer, pending;
  let data = null, phase = "loading";
  const tick = () => {
    const now = new Date();
    for (const view of views.values()) view.clock(now);
  };
  function publish() {
    for (const view of views.values()) {
      view.read(data, phase);
      view.busy?.(Boolean(pending));
    }
  }
  async function refresh() {
    if (!host || pending) return;
    const timerWindow = host;
    timerWindow.clearTimeout(sampleTimer);
    const controller = new AbortController();
    const deadline = timerWindow.setTimeout(() => controller.abort(), 3000);
    pending = controller;
    publish();
    try {
      // A PiP document is about:blank. Fetch from the initiating document instead.
      const response = await fetch("/api/device-status", {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw new Error();
      const next = await response.json();
      if (!Number.isFinite(Date.parse(next.sampled_at))) throw new Error();
      if (pending !== controller) return;
      data = next;
      phase = "connected";
    } catch {
      if (pending === controller) phase = "offline";
    } finally {
      timerWindow.clearTimeout(deadline);
      if (pending === controller) {
        pending = null;
        publish();
        if (host) sampleTimer = host.setTimeout(refresh, REFRESH_MS);
      }
    }
  }
  function updateHost() {
    const active = [...views.keys()].findLast(win => !win.closed && !win.document.hidden);
    if (host === active) return;
    if (host) {
      host.clearTimeout(sampleTimer);
      host.clearInterval(clockTimer);
    }
    pending?.abort();
    pending = null;
    host = active;
    publish();
    if (host) {
      tick();
      clockTimer = host.setInterval(tick, 1000);
      void refresh();
    }
  }
  return {
    watch(win, view) {
      views.set(win, view);
      view.clock(new Date());
      view.read(data, phase);
      view.busy?.(Boolean(pending));
      win.document.addEventListener("visibilitychange", updateHost);
      win.addEventListener("pagehide", () => { views.delete(win); updateHost(); });
      win.addEventListener("pageshow", event => {
        if (event.persisted) { views.set(win, view); updateHost(); }
      });
      updateHost();
    },
    refresh
  };
}

const readings = deviceFeed();

function mountStrip(root) {
  const values = Object.fromEntries([...root.querySelectorAll("[data-main-metric]")]
    .map(row => [row.dataset.mainMetric, row.querySelector("dd")]));
  const state = root.querySelector("#device-strip-state");
  readings.watch(window, {
    clock(now) { setText(values.clock, clockText(now)); },
    read(data, phase) {
      root.dataset.phase = phase;
      setText(state, phase === "connected" ? "在线" : phase === "loading" ? "读取中" : data ? "旧读数" : "未连接");
      state.title = data ? "采样于 " + clockText(new Date(data.sampled_at)) : "";
      if (!data) return;
      const show = (kind, raw, format) => setText(values[kind], Number.isFinite(raw) ? format(raw) : "—");
      show("cpu", data.cpu_percent, value => `${value}%`);
      show("memory", data.memory?.available, value => `${gib(value)}\u00a0GiB`);
      show("disk", data.disk?.free, value => `${gib(value)}\u00a0GiB`);
      if (data.battery?.present === false) setText(values.battery, "无电池");
      else show("battery", data.battery?.percent, value => `${value}%`);
      values.battery.title = data.battery?.charging ? "充电中" : data.battery?.ac === true ? "已接电" :
        data.battery?.ac === false ? "使用电池" : "";
      values.disk.title = data.disk?.drive || "";
    }
  });
}

function readout(label, metric) {
  return `<div class="hud-metric" data-metric="${metric}">
    <dt>${label}</dt>
    <dd class="hud-value">—</dd>
    <dd class="hud-detail">等待读数</dd>
    <dd class="hud-scale" aria-hidden="true" hidden><meter min="0" max="100" value="0"></meter></dd>
  </div>`;
}

function mount(win, mode) {
  const doc = win.document;
  doc.body.classList.add("fui", "device-status-window");
  doc.body.innerHTML = `
    <main id="status-hud" data-window-mode="${mode}">
      <header class="hud-header">
        <h1>本机状态</h1>
        <div class="hud-actions">
          <button id="status-hud-refresh" type="button">刷新</button>
          <button id="status-hud-close" type="button">关闭</button>
          <a id="status-hud-return" href="/" hidden>返回 MOSS</a>
        </div>
      </header>
      <section class="hud-clock" aria-label="本机时间与连接状态">
        <time id="status-hud-clock" aria-label="本机时间"></time>
        <p id="status-hud-connection" role="status">读取中</p>
      </section>
      <dl class="hud-readings">
        ${readout("处理器", "cpu")}
        ${readout("可用内存", "memory")}
        ${readout("电池", "battery")}
        ${readout("系统盘", "disk")}
      </dl>
      <footer class="hud-footer" hidden>采样于 <time id="status-hud-sampled"></time></footer>
    </main>`;

  const byId = id => doc.getElementById(id);
  const hud = byId("status-hud");
  const refresh = byId("status-hud-refresh");
  const state = byId("status-hud-connection");
  const clock = byId("status-hud-clock");
  const sampled = byId("status-hud-sampled");
  const close = byId("status-hud-close");
  const fields = Object.fromEntries([...hud.querySelectorAll("[data-metric]")].map(row => [row.dataset.metric, {
    value:row.querySelector(".hud-value"), detail:row.querySelector(".hud-detail"),
    scale:row.querySelector(".hud-scale"), meter:row.querySelector("meter")
  }]));
  function setReading(kind, value, detail, percent) {
    const field = fields[kind];
    setText(field.value, value);
    setText(field.detail, detail);
    const valid = Number.isFinite(percent) && percent >= 0 && percent <= 100;
    field.scale.hidden = !valid;
    if (valid) field.meter.value = percent;
  }

  function render(data) {
    const cpu = data.cpu_percent;
    setReading("cpu", Number.isFinite(cpu) ? `${cpu}%` : "—",
      Number.isFinite(cpu) ? "系统使用率" : "等待两次采样", cpu);
    const mem = data.memory, disk = data.disk;
    for (const [kind, reading, available] of [["memory", mem, mem?.available], ["disk", disk, disk?.free]]) {
      const valid = reading?.total > 0 && Number.isFinite(available);
      const value = valid ? `${gib(available)} / ${gib(reading.total)}\u00a0GiB` : "—";
      const detail = valid ? (kind === "disk" ? `${disk.drive} · 可用 / 总量` : "可用 / 总量") : "Windows 暂无读数";
      setReading(kind, value, detail, valid ? available / reading.total * 100 : null);
    }
    const battery = data.battery;
    const percent = battery?.percent;
    const level = battery?.present === false ? "无电池" : Number.isFinite(percent) ? `${percent}%` : "—";
    const power = battery?.charging ? "充电中" : battery?.ac === true ? "已接电" :
      battery?.ac === false ? "使用电池" : "Windows 暂无读数";
    setReading("battery", level, power, percent);
    const stamp = new Date(data.sampled_at);
    sampled.dateTime = data.sampled_at;
    setText(sampled, clockText(stamp));
    sampled.parentElement.hidden = false;
  }

  readings.watch(win, {
    clock(now) { clock.dateTime = now.toISOString(); setText(clock, clockText(now)); },
    read(data, phase) {
      hud.dataset.phase = phase;
      setText(state, phase === "connected" ? "已连接" : phase === "loading" ? "读取中" :
        data ? "连接中断 · 上次读数" : "连接中断");
      if (data) render(data);
    },
    busy(value) {
      if (value) refresh.setAttribute("aria-disabled", "true");
      else refresh.removeAttribute("aria-disabled");
    }
  });
  refresh.addEventListener("click", readings.refresh);
  close.addEventListener("click", () => win.close());
  if (mode === "popup" && !win.opener) {
    close.hidden = true;
    byId("status-hud-return").hidden = false;
  }
}

if (document.body.hasAttribute("data-device-status-page")) mount(window, "popup");
const strip = document.getElementById("device-strip");
if (strip) mountStrip(strip);

const trigger = document.getElementById("open-status-window");
if (trigger) {
  const notice = document.getElementById("status-window-notice");
  let child, opening = false, fallback = false;
  trigger.addEventListener("click", async () => {
    if (child && !child.closed) { child.focus(); return; }
    if (opening) return;
    notice.hidden = true;
    if (!fallback && window.documentPictureInPicture?.requestWindow) {
      opening = true;
      try {
        child = await window.documentPictureInPicture.requestWindow({width:392, height:400});
        const pip = child;
        pip.document.documentElement.lang = "zh-CN";
        pip.document.title = title;
        for (const href of ["/conversation-assets/terminal-fonts.css", stylesheet]) {
          const link = pip.document.createElement("link");
          link.rel = "stylesheet";
          link.href = new URL(href, location.origin);
          pip.document.head.append(link);
        }
        pip.addEventListener("pagehide", () => {
          if (child === pip) child = null;
          const restoreFocus = () => {
            if ([document.body, document.documentElement, trigger].includes(document.activeElement)) {
              trigger.focus({preventScroll:true});
            }
          };
          if (document.hasFocus()) restoreFocus();
          else window.addEventListener("focus", restoreFocus, {once:true});
        }, {once:true});
        mount(pip, "pip");
        return;
      } catch {
        child?.close();
        child = null;
        fallback = true;
      } finally {
        opening = false;
      }
    }
    child = window.open("/status", "moss-device-status", "popup,width=392,height=400");
    if (!child) {
      notice.textContent = fallback ? "置顶小窗不可用。再点一次可打开独立状态窗。" :
        "浏览器拦截了状态小窗。允许弹出窗口后可重试。";
      notice.hidden = false;
    }
  });
}
