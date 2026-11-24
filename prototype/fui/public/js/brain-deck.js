/**
 * MOSS Brain Control Deck (#dlg-brain) + Proactive Pulse Toast + Camera Vision-to-SAG
 * + Safe Declarative Generative FUI Widget Renderer (sliders, comparison tables, checklists, metrics).
 */
import { t } from './i18n.js';

function esc(str) {
  return String(str ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

/* ── Safe Recursive-Descent Arithmetic Evaluator for Generative FUI Calculators (zero eval) ── */
function safeEvalExpr(expr, vars) {
  const src = String(expr || '').replaceAll('**', '^').replaceAll('Math.', '').trim();
  if (!src || src.length > 260) return NaN;
  const tokens = [];
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (/\s/.test(ch)) { i += 1; continue; }
    if ('+-*/()^,'.includes(ch)) { tokens.push({ type: 'op', val: ch }); i += 1; continue; }
    if (/[0-9.]/.test(ch)) {
      let j = i;
      while (j < src.length && /[0-9.]/.test(src[j])) j += 1;
      tokens.push({ type: 'num', val: Number(src.slice(i, j)) });
      i = j;
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      let j = i;
      while (j < src.length && /[A-Za-z0-9_]/.test(src[j])) j += 1;
      tokens.push({ type: 'id', val: src.slice(i, j) });
      i = j;
      continue;
    }
    return NaN;
  }
  let pos = 0;
  const peek = () => tokens[pos];
  const take = () => tokens[pos++];

  function parseExpr() {
    let left = parseTerm();
    while (peek()?.type === 'op' && (peek().val === '+' || peek().val === '-')) {
      const op = take().val;
      const right = parseTerm();
      left = op === '+' ? left + right : left - right;
    }
    return left;
  }
  function parseTerm() {
    let left = parsePow();
    while (peek()?.type === 'op' && (peek().val === '*' || peek().val === '/')) {
      const op = take().val;
      const right = parsePow();
      left = op === '*' ? left * right : (right === 0 ? NaN : left / right);
    }
    return left;
  }
  function parsePow() {
    let base = parseUnary();
    if (peek()?.type === 'op' && peek().val === '^') {
      take();
      base = Math.pow(base, parsePow());
    }
    return base;
  }
  function parseUnary() {
    if (peek()?.type === 'op' && (peek().val === '+' || peek().val === '-')) {
      const op = take().val;
      const v = parseUnary();
      return op === '-' ? -v : v;
    }
    return parsePrimary();
  }
  function parsePrimary() {
    const tok = peek();
    if (!tok) return NaN;
    if (tok.type === 'num') { take(); return tok.val; }
    if (tok.type === 'id') {
      take();
      if (peek()?.type === 'op' && peek().val === '(') {
        take();
        const args = [];
        if (peek()?.val !== ')') {
          args.push(parseExpr());
          while (peek()?.val === ',') { take(); args.push(parseExpr()); }
        }
        if (peek()?.val === ')') take();
        if (tok.val === 'pow') return Math.pow(args[0], args[1]);
        if (tok.val === 'sqrt') return Math.sqrt(args[0]);
        if (tok.val === 'round') return Math.round(args[0]);
        if (tok.val === 'min') return Math.min(...args);
        if (tok.val === 'max') return Math.max(...args);
        if (tok.val === 'abs') return Math.abs(args[0]);
        return NaN;
      }
      return Number(vars[tok.val] ?? NaN);
    }
    if (tok.type === 'op' && tok.val === '(') {
      take();
      const val = parseExpr();
      if (peek()?.val === ')') take();
      return val;
    }
    return NaN;
  }
  try {
    const out = parseExpr();
    return Number.isFinite(out) ? out : NaN;
  } catch {
    return NaN;
  }
}

export function renderGenerativeWidget(widget) {
  if (!widget || typeof widget !== 'object') return null;
  const spec = widget.spec || {};
  const kind = widget.kind || 'metrics';
  const wrap = document.createElement('section');
  wrap.className = 'moss-gen-widget';
  wrap.dataset.widgetKind = kind;
  wrap.innerHTML = `<header class="moss-gen-widget__head">`
    + `<span class="fui-badge" data-state="info">FUI · ${esc(kind.toUpperCase())}</span>`
    + `<strong class="moss-gen-widget__title">${esc(widget.title || 'FUI Widget')}</strong>`
    + (widget.subtitle ? `<span class="moss-gen-widget__sub">${esc(widget.subtitle)}</span>` : '')
    + `</header><div class="moss-gen-widget__body"></div>`;
  const body = wrap.querySelector('.moss-gen-widget__body');

  if (kind === 'calculator' && Array.isArray(spec.inputs)) {
    const state = {};
    const outEls = [];
    const controls = document.createElement('div');
    controls.className = 'moss-gen-calc__inputs';
    const outputsWrap = document.createElement('div');
    outputsWrap.className = 'moss-gen-calc__outputs';

    const recompute = () => {
      for (const item of outEls) {
        const val = safeEvalExpr(item.expr, state);
        const formatted = Number.isFinite(val)
          ? (Math.abs(val) >= 100 ? val.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) : val.toFixed(2))
          : '—';
        item.valEl.textContent = formatted;
      }
    };

    for (const inp of spec.inputs.slice(0, 8)) {
      const id = String(inp.id || 'x');
      const min = Number.isFinite(Number(inp.min)) ? Number(inp.min) : 0;
      const max = Number.isFinite(Number(inp.max)) ? Number(inp.max) : 100;
      const step = Number.isFinite(Number(inp.step)) && Number(inp.step) > 0 ? Number(inp.step) : 1;
      const init = Number.isFinite(Number(inp.value)) ? Number(inp.value) : min;
      state[id] = init;

      const row = document.createElement('label');
      row.className = 'moss-gen-slider';
      row.innerHTML = `<span class="moss-gen-slider__meta"><span>${esc(inp.label || id)}</span><strong class="fui-num">${esc(init)}</strong></span>`
        + `<input type="range" min="${min}" max="${max}" step="${step}" value="${init}">`;
      const range = row.querySelector('input');
      const readout = row.querySelector('strong');
      range.addEventListener('input', () => {
        state[id] = Number(range.value);
        readout.textContent = range.value;
        recompute();
      });
      controls.append(row);
    }

    for (const out of (spec.outputs || []).slice(0, 6)) {
      const card = document.createElement('div');
      card.className = 'moss-gen-kpi';
      card.innerHTML = `<span class="moss-gen-kpi__label">${esc(out.label || '')}</span>`
        + `<span class="moss-gen-kpi__val fui-num">—</span>`
        + (out.unit ? `<span class="moss-gen-kpi__unit">${esc(out.unit)}</span>` : '');
      outEls.push({ expr: out.expr, valEl: card.querySelector('.moss-gen-kpi__val') });
      outputsWrap.append(card);
    }
    body.append(controls, outputsWrap);
    recompute();
    return wrap;
  }

  if (kind === 'comparison' && Array.isArray(spec.columns) && Array.isArray(spec.rows)) {
    const tblWrap = document.createElement('div');
    tblWrap.className = 'fui-table-wrap moss-gen-table-wrap';
    const ths = spec.columns.slice(0, 8).map(c => `<th>${esc(c)}</th>`).join('');
    const trs = spec.rows.slice(0, 12).map(row => (
      `<tr>${(Array.isArray(row) ? row : []).slice(0, 8).map(cell => `<td>${esc(cell)}</td>`).join('')}</tr>`
    )).join('');
    tblWrap.innerHTML = `<table class="fui-table"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
    body.append(tblWrap);
    return wrap;
  }

  if (kind === 'checklist' && Array.isArray(spec.items)) {
    const list = document.createElement('ul');
    list.className = 'moss-gen-checklist';
    for (const it of spec.items.slice(0, 15)) {
      const li = document.createElement('li');
      const checked = it?.done ? ' checked' : '';
      const text = typeof it === 'string' ? it : (it?.text || '');
      li.innerHTML = `<label class="moss-check"><input type="checkbox"${checked}><span>${esc(text)}</span></label>`;
      list.append(li);
    }
    body.append(list);
    return wrap;
  }

  // Default: KPI metrics grid
  const grid = document.createElement('div');
  grid.className = 'moss-gen-calc__outputs';
  for (const it of (spec.items || []).slice(0, 8)) {
    const card = document.createElement('div');
    card.className = 'moss-gen-kpi';
    if (it?.state) card.dataset.state = it.state;
    card.innerHTML = `<span class="moss-gen-kpi__label">${esc(it?.label || '')}</span>`
      + `<span class="moss-gen-kpi__val fui-num">${esc(it?.value ?? '—')}</span>`
      + (it?.unit ? `<span class="moss-gen-kpi__unit">${esc(it.unit)}</span>` : '');
    grid.append(card);
  }
  body.append(grid);
  return wrap;
}

/* ── Brain Control Deck Controller (#dlg-brain) + Proactive Toast + Vision-to-SAG ── */
async function apiJson(url, options = {}) {
  const resp = await fetch(url, {
    cache: 'no-store',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

export function createBrainDeckController({
  getMode,
  isPaused,
  onSendPrompt,
  onSpeakBriefing,
  onAddLog,
}) {
  const dlg = document.getElementById('dlg-brain');
  const openBtn = document.getElementById('strip-brain-btn');
  const openBadge = document.getElementById('strip-brain-badge');
  const tabs = [...document.querySelectorAll('[data-brain-tab]')];
  const panes = [...document.querySelectorAll('[data-brain-pane]')];

  // Telemetry summary outputs
  const kpiRsi = document.getElementById('brain-kpi-rsi');
  const kpiSag = document.getElementById('brain-kpi-sag');
  const kpiPulse = document.getElementById('brain-kpi-pulse');
  const kpiDream = document.getElementById('brain-kpi-dream');

  // RSI tab elements
  const rsiRunBtn = document.getElementById('brain-rsi-run');
  const rsiStatusEl = document.getElementById('brain-rsi-status');
  const rsiListEl = document.getElementById('brain-rsi-list');

  // SAG tab elements
  const sagEntitiesEl = document.getElementById('brain-sag-entities');
  const sagSearchForm = document.getElementById('brain-sag-search-form');
  const sagSearchInput = document.getElementById('brain-sag-search-input');
  const sagSearchResult = document.getElementById('brain-sag-search-result');
  const sagIngestForm = document.getElementById('brain-sag-ingest-form');
  const sagTitleInput = document.getElementById('brain-sag-title');
  const sagContentInput = document.getElementById('brain-sag-content');
  const sagFileInput = document.getElementById('brain-sag-file');
  const sagIngestMsg = document.getElementById('brain-sag-ingest-msg');

  // Pulse & Dream tab elements
  const pulseEnableEl = document.getElementById('brain-pulse-enabled');
  const pulseDndEl = document.getElementById('brain-pulse-dnd');
  const briefingBtn = document.getElementById('brain-briefing-btn');
  const dreamBtn = document.getElementById('brain-dream-btn');
  const briefingBox = document.getElementById('brain-briefing-box');
  const worldModelBox = document.getElementById('brain-world-model');

  // Jobs & Skills tab elements
  const jobForm = document.getElementById('brain-job-form');
  const jobKindEl = document.getElementById('brain-job-kind');
  const jobTargetEl = document.getElementById('brain-job-target');
  const jobsListEl = document.getElementById('brain-jobs-list');
  const skillsListEl = document.getElementById('brain-skills-list');
  const skillForm = document.getElementById('brain-skill-form');
  const skillNameEl = document.getElementById('brain-skill-name');
  const skillPromptEl = document.getElementById('brain-skill-prompt');

  // IoT & Physical 550W Hardware tab elements
  const haForm = document.getElementById('brain-ha-form');
  const haUrlEl = document.getElementById('brain-ha-url');
  const haTokenEl = document.getElementById('brain-ha-token');
  const haModeEl = document.getElementById('brain-ha-mode');
  const haEnabledEl = document.getElementById('brain-ha-enabled');
  const haMsgEl = document.getElementById('brain-ha-msg');
  const haRefreshBtn = document.getElementById('brain-ha-refresh');
  const haDevicesEl = document.getElementById('brain-ha-devices');
  const hwModeButtons = [...document.querySelectorAll('[data-hw-mode]')];
  const hwGestureButtons = [...document.querySelectorAll('[data-hw-gesture]')];
  const hwYawEl = document.getElementById('brain-hw-yaw');
  const hwPitchEl = document.getElementById('brain-hw-pitch');
  const hwStatusEl = document.getElementById('brain-hw-status');
  const mobileLinkEl = document.getElementById('brain-mobile-link');

  // Proactive Toast Strip on main console
  const toastEl = document.getElementById('pulse-toast');
  const toastBadge = document.getElementById('pulse-toast-badge');
  const toastTitle = document.getElementById('pulse-toast-title');
  const toastBody = document.getElementById('pulse-toast-body');
  const toastDismissBtn = document.getElementById('pulse-toast-dismiss');
  const toastOpenBtn = document.getElementById('pulse-toast-open');

  // Camera Vision-to-SAG button
  const camSagBtn = document.getElementById('rail-cam-sag');
  const camVideo = document.getElementById('rail-cam-video');

  let overview = null;
  let activeAlertId = null;

  function setTab(name) {
    for (const tab of tabs) tab.setAttribute('aria-selected', String(tab.dataset.brainTab === name));
    for (const pane of panes) pane.hidden = pane.dataset.brainPane !== name;
  }
  for (const tab of tabs) {
    tab.addEventListener('click', () => setTab(tab.dataset.brainTab));
  }

  function renderOverview(data) {
    if (!data) return;
    overview = data;
    const rsi = data.rsi || {};
    const sag = data.sag || {};
    const soul = data.soul || {};
    const pulse = data.pulse || {};
    const wm = data.world_model;

    if (kpiRsi) kpiRsi.textContent = `${rsi.active_count ?? 0} / DPO ${rsi.dpo_export_count ?? 0}`;
    if (kpiSag) kpiSag.textContent = `${sag.event_count ?? 0} · ${sag.entity_count ?? 0}E · ${sag.edge_count ?? 0}H`;
    if (kpiPulse) kpiPulse.textContent = pulse.config?.dnd ? t('brain.pulse_dnd') : (pulse.config?.enabled ? t('brain.pulse_on') : t('brain.pulse_off'));
    if (kpiDream) kpiDream.textContent = wm ? `GEN #${wm.generation_id}` : 'STANDBY';

    const activeAlerts = (pulse.alerts || []).filter(a => !a.dismissed);
    if (openBadge) {
      openBadge.hidden = activeAlerts.length === 0;
      openBadge.textContent = String(activeAlerts.length);
    }
    const latest = activeAlerts[0];
    if (toastEl && latest && latest.id !== activeAlertId) {
      activeAlertId = latest.id;
      toastEl.hidden = false;
      toastEl.dataset.state = latest.level || 'info';
      if (toastBadge) {
        toastBadge.dataset.state = latest.level || 'info';
        toastBadge.textContent = latest.kind?.toUpperCase() || 'PULSE';
      }
      if (toastTitle) toastTitle.textContent = latest.title || '';
      if (toastBody) toastBody.textContent = latest.body || '';
    } else if (toastEl && !latest) {
      toastEl.hidden = true;
      activeAlertId = null;
    }

    // 1. RSI lessons
    if (rsiStatusEl) {
      rsiStatusEl.textContent = `生效 ${rsi.active_count ?? 0} 条 · 淘汰 ${rsi.retired_count ?? 0} 条 · DPO 样本 ${rsi.dpo_export_count ?? 0} 条`;
    }
    if (rsiListEl) {
      const items = rsi.lessons || [];
      if (!items.length) {
        rsiListEl.innerHTML = `<li class="moss-card__meta">${esc(t('brain.rsi_empty'))}</li>`;
      } else {
        rsiListEl.replaceChildren(...items.map(l => {
          const li = document.createElement('li');
          li.className = 'moss-modal-row';
          li.innerHTML = `<div class="moss-modal-row__main">`
            + `<p class="moss-modal-row__text"><span class="fui-badge" data-state="info">L#${l.id} ${esc(l.scope)}</span> ${esc(l.rule)}</p>`
            + `<span class="moss-modal-row__meta fui-num">CONF ${Number(l.confidence).toFixed(2)} · HIT ${l.applied_count} · OK ${l.helped_count} · FAIL ${l.failed_count}</span>`
            + `</div>`;
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'fui-btn';
          btn.textContent = t('brain.retire');
          btn.addEventListener('click', async () => {
            await fetch(`/api/rsi/lessons/${l.id}`, { method: 'DELETE' });
            await refresh();
          });
          li.append(btn);
          return li;
        }));
      }
    }

    // 2. SAG top entities
    if (sagEntitiesEl) {
      const ents = sag.top_entities || [];
      if (!ents.length) {
        sagEntitiesEl.innerHTML = `<span class="moss-card__meta">${esc(t('brain.sag_empty'))}</span>`;
      } else {
        sagEntitiesEl.replaceChildren(...ents.map(e => {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'fui-key fui-key--sm';
          b.textContent = `${e.name} (${e.degree})`;
          b.addEventListener('click', () => {
            if (sagSearchInput) sagSearchInput.value = e.name;
            sagSearchForm?.requestSubmit();
          });
          return b;
        }));
      }
    }

    // 3. Pulse config & World model
    if (pulseEnableEl) pulseEnableEl.checked = Boolean(pulse.config?.enabled);
    if (pulseDndEl) pulseDndEl.checked = Boolean(pulse.config?.dnd);
    if (worldModelBox) {
      if (!wm?.world_model) {
        worldModelBox.innerHTML = `<p class="moss-card__meta">${esc(t('brain.dream_empty'))}</p>`;
      } else {
        const m = wm.world_model;
        const conflictsHtml = (m.conflicts || []).length
          ? `<div class="moss-brain-conflicts"><strong>待核对冲突 (${m.conflicts.length})</strong><ul>${m.conflicts.map(c => `<li>${esc(c.detail)}</li>`).join('')}</ul></div>`
          : '';
        worldModelBox.innerHTML = `<p class="moss-card__meta">${esc(wm.summary)}</p>`
          + `<div class="moss-brain-kv">`
          + `<div><span class="fui-label">活跃项目</span><span>${esc((m.active_projects || []).join(' · ') || '—')}</span></div>`
          + `<div><span class="fui-label">协作人物</span><span>${esc((m.key_collaborators || []).join(' · ') || '—')}</span></div>`
          + `<div><span class="fui-label">音乐偏好</span><span>${esc((m.media_taste || []).map(x => `${x.artist}(${x.plays})`).join(' · ') || '—')}</span></div>`
          + `<div><span class="fui-label">关注主题</span><span>${esc((m.focus_topics || []).join(' · ') || '—')}</span></div>`
          + `<div><span class="fui-label">灵魂档案</span><span>${esc(`画像 ${soul.profile_count ?? 0} · 信念 ${soul.active_beliefs ?? 0} · 记忆 ${soul.memory_count ?? 0}`)}</span></div>`
          + `</div>${conflictsHtml}`;
      }
    }

    // 4. Jobs & Skills
    if (jobsListEl) {
      const jobs = data.jobs || [];
      if (!jobs.length) {
        jobsListEl.innerHTML = `<li class="moss-card__meta">${esc(t('brain.jobs_empty'))}</li>`;
      } else {
        jobsListEl.replaceChildren(...jobs.slice(0, 8).map(j => {
          const li = document.createElement('li');
          li.className = 'moss-modal-row';
          const st = j.status === 'done' ? 'ok' : j.status === 'error' ? 'crit' : 'warn';
          li.innerHTML = `<div class="moss-modal-row__main">`
            + `<p class="moss-modal-row__text"><span class="fui-badge" data-state="${st}">J#${j.id} ${j.progress}%</span> ${esc(j.title)}</p>`
            + `<span class="moss-modal-row__meta">${esc(j.step_text)}</span>`
            + `</div>`;
          return li;
        }));
      }
    }
    if (skillsListEl) {
      const skills = data.skills || [];
      skillsListEl.replaceChildren(...skills.map(sk => {
        const li = document.createElement('li');
        li.className = 'moss-modal-row';
        li.innerHTML = `<div class="moss-modal-row__main"><p class="moss-modal-row__text">${esc(sk.name)}</p><span class="moss-modal-row__meta">${esc(sk.description || sk.prompt)}</span></div>`;
        const runBtn = document.createElement('button');
        runBtn.type = 'button';
        runBtn.className = 'fui-btn fui-btn--primary';
        runBtn.textContent = t('brain.skill_run');
        runBtn.addEventListener('click', async () => {
          void apiJson('/api/skills', { method: 'POST', body: JSON.stringify({ mark_used: sk.id }) }).catch(() => {});
          dlg?.close();
          onSendPrompt?.(sk.prompt);
        });
        li.append(runBtn);
        return li;
      }));
    }

    // 5. IoT & Physical 550W
    const iot = data.iot || {};
    const icfg = iot.config || {};
    if (haUrlEl && document.activeElement !== haUrlEl) haUrlEl.value = icfg.ha_url || '';
    if (haModeEl && document.activeElement !== haModeEl) haModeEl.value = icfg.ha_mode || 'dry_run';
    if (haEnabledEl) haEnabledEl.checked = Boolean(icfg.ha_enabled);
    if (haTokenEl && icfg.ha_token_configured) haTokenEl.placeholder = `已保存令牌 (${icfg.ha_token_hint}) · 留空保持不变`;
    const frame = iot.hardware_frame || {};
    if (hwStatusEl) {
      const sub = iot.hardware_stream_subscribers ?? 0;
      hwStatusEl.textContent = `帧 #${frame.seq ?? 0} · 灯态 ${frame.mode || 'standby'} · 舵机 (${frame.servo?.yaw_deg ?? 0}°, ${frame.servo?.pitch_deg ?? 0}°) · 订阅终端 ${sub}`;
    }
    if (mobileLinkEl && data.mobile_url && /^(\/|https?:\/\/)/i.test(String(data.mobile_url))) {
      mobileLinkEl.href = data.mobile_url;
      mobileLinkEl.textContent = data.mobile_url;
    }
  }

  async function refresh() {
    if (getMode?.() !== 'live') return null;
    try {
      const data = await apiJson('/api/brain/overview');
      renderOverview(data);
      return data;
    } catch {
      return null;
    }
  }

  async function refreshHaDevices() {
    if (!haDevicesEl || getMode?.() !== 'live') return;
    try {
      const res = await apiJson('/api/ha/entities');
      const items = res.entities || [];
      haDevicesEl.replaceChildren(...items.slice(0, 10).map(e => {
        const li = document.createElement('li');
        li.className = 'moss-modal-row';
        const domain = e.entity_id.split('.')[0];
        li.innerHTML = `<div class="moss-modal-row__main"><p class="moss-modal-row__text">${esc(e.friendly_name)} <span class="fui-num">(${esc(e.entity_id)})</span></p>`
          + `<span class="moss-modal-row__meta fui-num">${esc(res.mode.toUpperCase())} · STATE: ${esc(e.state)}</span></div>`;
        if (['light', 'switch', 'fan'].includes(domain)) {
          const tgl = document.createElement('button');
          tgl.type = 'button';
          tgl.className = 'fui-btn';
          tgl.textContent = t('brain.ha_toggle');
          tgl.addEventListener('click', async () => {
            try {
              await apiJson('/api/ha/service', {
                method: 'POST',
                body: JSON.stringify({ entity_id: e.entity_id, service: 'toggle' }),
              });
              await refreshHaDevices();
            } catch (err) {
              if (haMsgEl) haMsgEl.textContent = err.message;
            }
          });
          li.append(tgl);
        }
        return li;
      }));
    } catch (err) {
      haDevicesEl.innerHTML = `<li class="moss-card__meta">${esc(err.message)}</li>`;
    }
  }

  // Event bindings
  openBtn?.addEventListener('click', () => {
    if (!dlg) return;
    if (!dlg.open) dlg.showModal();
    void refresh();
    void refreshHaDevices();
  });
  toastOpenBtn?.addEventListener('click', () => openBtn?.click());
  toastDismissBtn?.addEventListener('click', async () => {
    if (activeAlertId) {
      await apiJson(`/api/proactive/alerts/${activeAlertId}/dismiss`, { method: 'POST', body: '{}' }).catch(() => {});
    }
    if (toastEl) toastEl.hidden = true;
    activeAlertId = null;
    void refresh();
  });

  rsiRunBtn?.addEventListener('click', async () => {
    rsiRunBtn.disabled = true;
    try {
      const res = await apiJson('/api/rsi/iterate', { method: 'POST', body: '{}' });
      if (rsiStatusEl) rsiStatusEl.textContent = res.cycle?.summary || 'RSI 复盘完成';
      await refresh();
    } catch (err) {
      if (rsiStatusEl) rsiStatusEl.textContent = err.message;
    } finally {
      rsiRunBtn.disabled = false;
    }
  });

  sagSearchForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = (sagSearchInput?.value || '').trim();
    if (!q || !sagSearchResult) return;
    sagSearchResult.textContent = '正在执行混合向量 + 动态超边检索…';
    try {
      const res = await apiJson('/api/sag/query', { method: 'POST', body: JSON.stringify({ query: q, limit: 6, hops: 1 }) });
      sagSearchResult.textContent = res.evidence_text || '未检索到直接命中或超边关联条目。';
    } catch (err) {
      sagSearchResult.textContent = err.message;
    }
  });

  sagIngestForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!sagIngestMsg) return;
    const fpath = (sagFileInput?.value || '').trim();
    const title = (sagTitleInput?.value || '').trim();
    const content = (sagContentInput?.value || '').trim();
    try {
      if (fpath) {
        const res = await apiJson('/api/sag/ingest', { method: 'POST', body: JSON.stringify({ file_path: fpath }) });
        sagIngestMsg.textContent = `已导入文件：新增 ${res.ingested} 个 SAG 知识节点。`;
        if (sagFileInput) sagFileInput.value = '';
      } else {
        const res = await apiJson('/api/sag/ingest', { method: 'POST', body: JSON.stringify({ title, content }) });
        sagIngestMsg.textContent = `已入库节点 E#${res.event?.id}（关联 ${(res.event?.entities || []).length} 个实体）。`;
        if (sagTitleInput) sagTitleInput.value = '';
        if (sagContentInput) sagContentInput.value = '';
      }
      await refresh();
    } catch (err) {
      sagIngestMsg.textContent = err.message;
    }
  });

  const savePulseCfg = async () => {
    try {
      await apiJson('/api/proactive/config', {
        method: 'POST',
        body: JSON.stringify({ enabled: Boolean(pulseEnableEl?.checked), dnd: Boolean(pulseDndEl?.checked) }),
      });
      await refresh();
    } catch (err) {
      onAddLog?.({ time: Date.now(), source: 'PULSE', text: err.message, level: 'warn' });
    }
  };
  pulseEnableEl?.addEventListener('change', savePulseCfg);
  pulseDndEl?.addEventListener('change', savePulseCfg);

  briefingBtn?.addEventListener('click', async () => {
    briefingBtn.disabled = true;
    try {
      const b = await apiJson('/api/proactive/briefing', { method: 'POST', body: '{}' });
      if (briefingBox) briefingBox.textContent = `${b.title}：${b.text}`;
      try {
        onSpeakBriefing?.(b.text);
      } catch {
        /* never overwrite rendered briefing text if speech hook fails */
      }
    } catch (err) {
      if (briefingBox) briefingBox.textContent = err.message;
    } finally {
      briefingBtn.disabled = false;
    }
  });

  dreamBtn?.addEventListener('click', async () => {
    dreamBtn.disabled = true;
    try {
      const res = await apiJson('/api/brain/consolidate', { method: 'POST', body: '{}' });
      if (res.overview) renderOverview(res.overview);
    } catch (err) {
      if (worldModelBox) worldModelBox.textContent = err.message;
    } finally {
      dreamBtn.disabled = false;
    }
  });

  jobForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const kind = jobKindEl?.value || 'web_research';
    const target = (jobTargetEl?.value || '').trim();
    if (!target) return;
    try {
      await apiJson('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({
          kind,
          title: target.slice(0, 48),
          topic: kind === 'web_research' ? target : '',
          path: kind === 'folder_digest' ? target : '',
        }),
      });
      if (jobTargetEl) jobTargetEl.value = '';
      await refresh();
    } catch (err) {
      onAddLog?.({ time: Date.now(), source: 'JOB', text: err.message, level: 'warn' });
    }
  });

  skillForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = (skillNameEl?.value || '').trim();
    const prompt = (skillPromptEl?.value || '').trim();
    if (!name || !prompt) return;
    try {
      await apiJson('/api/skills', { method: 'POST', body: JSON.stringify({ name, prompt }) });
      if (skillNameEl) skillNameEl.value = '';
      if (skillPromptEl) skillPromptEl.value = '';
      await refresh();
    } catch (err) {
      onAddLog?.({ time: Date.now(), source: 'SKILL', text: err.message, level: 'warn' });
    }
  });

  haForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await apiJson('/api/iot/config', {
        method: 'POST',
        body: JSON.stringify({
          ha_url: haUrlEl?.value || '',
          ha_token: haTokenEl?.value || '',
          ha_mode: haModeEl?.value || 'dry_run',
          ha_enabled: Boolean(haEnabledEl?.checked),
        }),
      });
      if (haTokenEl) haTokenEl.value = '';
      if (haMsgEl) haMsgEl.textContent = 'Home Assistant 配置已保存。';
      await refresh();
      await refreshHaDevices();
    } catch (err) {
      if (haMsgEl) haMsgEl.textContent = err.message;
    }
  });
  haRefreshBtn?.addEventListener('click', () => void refreshHaDevices());

  for (const btn of hwModeButtons) {
    btn.addEventListener('click', async () => {
      try {
        await apiJson('/api/hardware/moss/command', {
          method: 'POST',
          body: JSON.stringify({ mode: btn.dataset.hwMode }),
        });
        await refresh();
      } catch (err) {
        if (hwStatusEl) hwStatusEl.textContent = err.message;
      }
    });
  }
  for (const btn of hwGestureButtons) {
    btn.addEventListener('click', async () => {
      try {
        await apiJson('/api/hardware/moss/command', {
          method: 'POST',
          body: JSON.stringify({
            gesture: btn.dataset.hwGesture,
            yaw_deg: Number(hwYawEl?.value || 0),
            pitch_deg: Number(hwPitchEl?.value || 0),
          }),
        });
        await refresh();
      } catch (err) {
        if (hwStatusEl) hwStatusEl.textContent = err.message;
      }
    });
  }

  // Camera Vision-to-SAG capture button
  camSagBtn?.addEventListener('click', async () => {
    if (!camVideo || camVideo.hidden || !camVideo.videoWidth) {
      onAddLog?.({ time: Date.now(), source: 'SAG', text: '请先开启光学摄像头再执行识图入库。', level: 'warn' });
      return;
    }
    camSagBtn.disabled = true;
    try {
      const c = document.createElement('canvas');
      c.width = Math.min(1280, camVideo.videoWidth);
      c.height = Math.round(c.width * (camVideo.videoHeight / camVideo.videoWidth));
      c.getContext('2d').drawImage(camVideo, 0, 0, c.width, c.height);
      const b64 = c.toDataURL('image/png').replace(/^data:image\/png;base64,/, '');
      const res = await apiJson('/api/context/vision-to-sag', {
        method: 'POST',
        body: JSON.stringify({ image: b64 }),
      });
      onAddLog?.({ time: Date.now(), source: 'SAG', text: `镜头识图已入库 E#${res.event?.id}：${(res.summary || '').slice(0, 70)}`, level: 'ok' });
      await refresh();
    } catch (err) {
      onAddLog?.({ time: Date.now(), source: 'SAG', text: `识图入库失败：${err.message}`, level: 'warn' });
    } finally {
      camSagBtn.disabled = false;
    }
  });

  // Gentle 20s poll for Proactive Pulse alerts & background job completions (skips when hidden/paused)
  setInterval(() => { // fui-lint-ignore: R15 polls proactive pulse alerts and background job progress; skips when hidden or paused
    if (!document.hidden && !isPaused?.() && getMode?.() === 'live') {
      void refresh();
    }
  }, 20000);

  return {
    refresh,
    open: () => openBtn?.click(),
  };
}
