/**
 * Native <dialog class="fui-dialog"> Overlay Manager.
 * Handles History, Long-Term Memory, Notes CRUD, Local Status Overview,
 * 3-Day & Hourly Weather Forecast, Jev Native Draft Handoff, Tool Catalog, and Settings.
 */
import { fixed, formatClock, getLocale, t, weatherCondition } from './i18n.js';
import { rankMicDevices } from './mic-selector.js';
import { safeUuid } from './safe-web.js';

const TOOL_PROMPTS = [
  { key: '刘德华的冰雨', labelZh: '点歌 · 刘德华的冰雨', labelEn: 'Music · Andy Lau' },
  { key: '现在几点了？', labelZh: '查询本机时间', labelEn: 'Query local clock' },
  { key: '现在几点、电脑还有多少电、音量多大？', labelZh: '时间、电量与系统音量', labelEn: 'Clock, battery & volume' },
  { key: '电脑怎么这么卡', labelZh: '硬件负载诊断', labelEn: 'System load diagnosis' },
  { key: '20分钟后提醒我休息', labelZh: '设定 20 分钟休息提醒', labelEn: 'Set 20-min reminder' },
  { key: '明天上海的天气怎么样？', labelZh: '查询城市天气预报', labelEn: 'Query city weather' },
  { key: '帮我保存一份本地草稿，内容逐字写：明天核对清单', labelZh: '创建并核验本地草稿', labelEn: 'Create verified draft' },
  { key: '列出媒体库', labelZh: '查看本机媒体库索引', labelEn: 'List media library' },
  { key: '列出 MCP 工具', labelZh: '查看开放的 MCP 工具', labelEn: 'List MCP tools' },
  { key: '执行 PowerShell：Get-Date', labelZh: '执行 PowerShell 命令', labelEn: 'Run PowerShell command' },
  { key: '帮我回同事：明天几点开会？我的意思是下午三点。', labelZh: 'Jev 辅助拟定回复', labelEn: 'Draft colleague reply' },
  { key: '朗诵王之涣的《登鹳雀楼》', labelZh: '550W 原声朗诵诗词', labelEn: 'Recite classical poetry' },
];

export function createModalsController({
  api,
  voice,
  stage,
  dialogue,
  telemetry,
  hudPopups,
  opening,
  onSelectModel,
  onSelectPersonality,
}) {
  const localized = (zh, en) => getLocale().startsWith('zh') ? zh : en;
  const closeDialog = (dlg) => {
    if (!dlg || !dlg.open || dlg.dataset.leaving || dlg.dataset.required === 'true') return;
    dlg.dataset.leaving = 'true';
    const done = () => {
      clearTimeout(timer);
      dlg.removeEventListener('animationend', onEnd);
      delete dlg.dataset.leaving;
      if (dlg.open) dlg.close();
    };
    const onEnd = (e) => { if (e.target === dlg) done(); };
    dlg.addEventListener('animationend', onEnd);
    const timer = setTimeout(done, 150);
  };

  document.addEventListener('click', (e) => {
    const closer = e.target.closest('[data-dialog-close]');
    if (closer) closeDialog(closer.closest('dialog'));
  });
  document.addEventListener('cancel', (e) => {
    if (e.target instanceof HTMLDialogElement) {
      e.preventDefault();
      if (e.target.dataset.required === 'true') return;
      closeDialog(e.target);
    }
  }, true);

  /* ── History Dialog (Session Switcher + Full-Text Search + 1-Click Markdown Export) ── */
  const historyDlg = document.getElementById('dlg-history');
  const historyList = document.getElementById('history-list');
  const historySearchForm = document.getElementById('history-search-form');
  const historySearchInput = document.getElementById('history-search-input');
  const historySearchClear = document.getElementById('history-search-clear');
  const historySearchStatus = document.getElementById('history-search-status');
  let historySearchTimer = 0;
  let historySearchSeq = 0;

  function renderHistoryChats(state) {
    if (!historyList) return;
    if (historySearchStatus) { historySearchStatus.hidden = true; historySearchStatus.textContent = ''; }
    const chats = state?.chats || [];
    historyList.replaceChildren();
    if (!chats.length) {
      historyList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${escapeHtml(t('modal.history_empty'))}</span></li>`;
      return;
    }
    const isLive = api.getMode() === 'live';
    for (const chat of chats) {
      const li = document.createElement('li');
      li.className = 'moss-modal-row moss-history-row';
      const isCurrent = chat.id === state.chat_id;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fui-btn moss-history-row__main';
      btn.setAttribute('aria-pressed', String(isCurrent));
      btn.innerHTML = `<span>${escapeHtml(chat.title || chat.id)}</span>${isCurrent ? `<span class="fui-badge" data-state="ok">${escapeHtml(t('modal.current'))}</span>` : ''}`;
      btn.addEventListener('click', async () => {
        if (!isCurrent) await api.postAction('switch-chat', { id: chat.id }).catch(() => {});
        closeDialog(historyDlg);
      });
      li.append(btn);
      if (isLive) {
        const exp = document.createElement('a');
        exp.className = 'fui-btn moss-history-row__exp';
        exp.href = `/api/chats/${encodeURIComponent(chat.id)}/export`;
        exp.download = `moss-chat-${chat.id.slice(0, 12)}.md`;
        exp.textContent = t('modal.history_export');
        exp.dataset.exportChat = chat.id;
        li.append(exp);
      }
      historyList.append(li);
    }
  }

  async function runHistorySearch(rawQuery) {
    const seq = ++historySearchSeq;
    const q = String(rawQuery || '').trim();
    if (historySearchClear) historySearchClear.hidden = !q;
    if (!q) {
      renderHistoryChats(api.getState());
      return;
    }
    if (api.getMode() !== 'live') {
      const state = api.getState();
      const filtered = (state?.chats || []).filter((c) => `${c.title || ''} ${c.id}`.toLowerCase().includes(q.toLowerCase()));
      renderHistoryChats({ ...state, chats: filtered });
      return;
    }
    try {
      const resp = await fetch(`/api/chats/search?q=${encodeURIComponent(q)}`, { cache: 'no-store' });
      if (!resp.ok || seq !== historySearchSeq) return;
      const data = await resp.json();
      if (seq !== historySearchSeq) return;
      const hits = data.hits || [];
      if (historySearchStatus) {
        historySearchStatus.hidden = false;
        historySearchStatus.textContent = hits.length
          ? t('modal.history_hits', { n: hits.length })
          : t('modal.history_no_match');
      }
      historyList.replaceChildren();
      if (!hits.length) {
        historyList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${escapeHtml(t('modal.history_no_match'))}</span></li>`;
        return;
      }
      for (const hit of hits) {
        const li = document.createElement('li');
        li.className = 'moss-modal-row moss-history-row';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'fui-btn moss-history-hit';
        const stamp = hit.created_at ? String(hit.created_at).slice(0, 16).replace('T', ' ') : '';
        btn.innerHTML = `<div class="moss-modal-row__main"><p class="moss-modal-row__text"></p><span class="moss-modal-row__meta fui-num"></span></div>`;
        btn.querySelector('.moss-modal-row__text').textContent = hit.snippet || hit.user || '';
        btn.querySelector('.moss-modal-row__meta').textContent = `${hit.chat_title || hit.chat_id}${stamp ? ` · ${stamp}` : ''}`;
        btn.addEventListener('click', async () => {
          const curChat = api.getState()?.chat_id;
          if (hit.chat_id && hit.chat_id !== curChat) {
            await api.postAction('switch-chat', { id: hit.chat_id }).catch(() => {});
          }
          closeDialog(historyDlg);
          if (hit.turn_id) setTimeout(() => dialogue.scrollToTurn?.(hit.turn_id), 180);
        });
        li.append(btn);
        if (hit.chat_id) {
          const exp = document.createElement('a');
          exp.className = 'fui-btn moss-history-row__exp';
          exp.href = `/api/chats/${encodeURIComponent(hit.chat_id)}/export`;
          exp.download = `moss-chat-${hit.chat_id.slice(0, 12)}.md`;
          exp.textContent = t('modal.history_export');
          exp.dataset.exportChat = hit.chat_id;
          li.append(exp);
        }
        historyList.append(li);
      }
    } catch { /* ignore transient search errors */ }
  }

  historySearchForm?.addEventListener('submit', (e) => {
    e.preventDefault();
    clearTimeout(historySearchTimer);
    void runHistorySearch(historySearchInput?.value);
  });
  historySearchInput?.addEventListener('input', () => {
    clearTimeout(historySearchTimer);
    const val = historySearchInput.value;
    if (historySearchClear) historySearchClear.hidden = !val.trim();
    historySearchTimer = setTimeout(() => void runHistorySearch(val), 220);
  });
  historySearchClear?.addEventListener('click', () => {
    ++historySearchSeq;
    clearTimeout(historySearchTimer);
    if (historySearchInput) historySearchInput.value = '';
    if (historySearchClear) historySearchClear.hidden = true;
    renderHistoryChats(api.getState());
  });

  function renderHistory(state) {
    if (historySearchInput?.value?.trim()) return;
    renderHistoryChats(state);
  }

  /* ── Memory Dialog ── */
  const memoryDlg = document.getElementById('dlg-memory');
  const memoryList = document.getElementById('memory-list');
  const memoryForm = document.getElementById('memory-form');
  const memoryInput = document.getElementById('memory-input');
  const memoryBackend = document.getElementById('memory-backend');

  function renderMemories(state) {
    if (!memoryList) return;
    if (memoryBackend) {
      memoryBackend.textContent = `BACKEND · ${(state?.memory_backend || 'sqlite').toUpperCase()}`;
    }
    const items = state?.memories || [];
    memoryList.replaceChildren();
    if (!items.length) {
      memoryList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${escapeHtml(t('modal.memory_empty'))}</span></li>`;
      return;
    }
    for (const m of items) {
      const li = document.createElement('li');
      li.className = 'moss-modal-row';
      const main = document.createElement('div');
      main.className = 'moss-modal-row__main';
      const rawTs = m.updated_at || m.created_at;
      const parsedTs = rawTs ? new Date(rawTs) : null;
      const date = parsedTs && !Number.isNaN(parsedTs.getTime()) ? parsedTs.toISOString().slice(0, 10) : '';
      main.innerHTML = `<p class="moss-modal-row__text"></p><span class="moss-modal-row__meta fui-num">${escapeHtml((m.source || 'memory').toUpperCase())}${date ? ` · ${date}` : ''}</span>`;
      main.querySelector('.moss-modal-row__text').textContent = m.text;
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'fui-btn fui-btn--danger';
      del.textContent = t('modal.delete');
      del.addEventListener('click', () => api.postAction('forget', { id: m.id }).catch(() => {}));
      li.append(main, del);
      memoryList.append(li);
    }
  }
  memoryForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const draft = memoryInput?.value;
    if (!draft?.trim()) return;
    try {
      await api.postAction('remember', { text: draft.trim() });
      if (memoryInput.value === draft) memoryInput.value = '';
    } catch (error) {
      memoryInput.setCustomValidity(error.message);
      memoryInput.reportValidity();
      memoryInput.setCustomValidity('');
    }
  });

  /* ── Notes Dialog ── */
  const notesDlg = document.getElementById('dlg-notes');
  const notesForm = document.getElementById('notes-form');
  const notesBody = document.getElementById('notes-body');
  const notesLabel = document.getElementById('notes-label');
  const notesCancelEdit = document.getElementById('notes-cancel-edit');
  const notesQuery = document.getElementById('notes-query');
  const notesArchiveToggle = document.getElementById('notes-archive-toggle');
  const notesFilterForm = document.getElementById('notes-filter');
  const notesList = document.getElementById('notes-list');
  const notesStatus = document.getElementById('notes-status');
  let editingNote = null;
  let notesLoadSeq = 0;

  function setEditingNote(note = null) {
    editingNote = note;
    if (notesBody) notesBody.value = note?.text || '';
    if (notesLabel) notesLabel.textContent = note ? t('modal.notes_edit', { id: note.id }) : t('modal.notes_new');
    if (notesCancelEdit) notesCancelEdit.hidden = !note;
  }

  async function loadNotes() {
    if (!notesList) return;
    const seq = ++notesLoadSeq;
    try {
      const page = await api.fetchNotes({
        q: notesQuery?.value || '',
        archived: Boolean(notesArchiveToggle?.checked),
      });
      if (seq !== notesLoadSeq) return;
      notesList.replaceChildren();
      if (!page.items?.length) {
        notesList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${escapeHtml(t('modal.notes_empty'))}</span></li>`;
        return;
      }
      for (const note of page.items) {
        const li = document.createElement('li');
        li.className = 'moss-modal-row';
        const main = document.createElement('div');
        main.className = 'moss-modal-row__main';
        main.innerHTML = `<span class="moss-modal-row__meta fui-num">#${note.id}</span><p class="moss-modal-row__text"></p>`;
        main.querySelector('.moss-modal-row__text').textContent = note.text;
        const btns = document.createElement('div');
        btns.className = 'moss-row moss-row--tight';
        if (note.archived) {
          const restore = document.createElement('button');
          restore.type = 'button';
          restore.className = 'fui-btn';
          restore.textContent = t('modal.restore');
          restore.addEventListener('click', async () => {
            try {
              await api.saveNote({ note, action: 'restore' });
              await loadNotes();
            } catch (error) { if (notesStatus) notesStatus.textContent = error.message; }
          });
          btns.append(restore);
        } else {
          const edit = document.createElement('button');
          edit.type = 'button';
          edit.className = 'fui-btn';
          edit.textContent = t('modal.edit');
          edit.addEventListener('click', () => { setEditingNote(note); notesBody?.focus(); });
          const arch = document.createElement('button');
          arch.type = 'button';
          arch.className = 'fui-btn';
          arch.textContent = t('modal.archive');
          arch.addEventListener('click', async () => {
            try {
              await api.saveNote({ note, action: 'archive' });
              await loadNotes();
            } catch (error) { if (notesStatus) notesStatus.textContent = error.message; }
          });
          btns.append(edit, arch);
        }
        li.append(main, btns);
        notesList.append(li);
      }
      if (notesStatus) notesStatus.textContent = '';
    } catch (err) {
      if (notesStatus) notesStatus.textContent = err.message;
    }
  }

  notesForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const draft = notesBody?.value;
    const editing = editingNote;
    if (!draft?.trim()) return;
    try {
      await api.saveNote({ note: editing, action: 'edit', text: draft.trim() });
      if (editingNote === editing && notesBody.value === draft) setEditingNote(null);
      await loadNotes();
    } catch (err) {
      if (notesStatus) notesStatus.textContent = err.message;
    }
  });
  notesCancelEdit?.addEventListener('click', () => setEditingNote(null));
  notesFilterForm?.addEventListener('submit', (e) => { e.preventDefault(); loadNotes(); });
  notesArchiveToggle?.addEventListener('change', () => loadNotes());

  /* ── Local Status Overview Dialog ── */
  const statusDlg = document.getElementById('dlg-status');
  const statusList = document.getElementById('dlg-status-list');
  function openLocalStatus(turn = null) {
    if (!statusDlg || !statusList) return;
    statusList.replaceChildren();
    const items = turn?.local_overview || [];
    if (items.length) {
      for (const item of items) {
        statusList.insertAdjacentHTML('beforeend', `<dt class="fui-label">${escapeHtml(item.kind.toUpperCase())}</dt><dd class="fui-num">${escapeHtml(item.reply)}</dd>`);
      }
    } else {
      const dev = telemetry.getDevice();
      const GIB = 1024 ** 3;
      const reading = (value, suffix, digits = 0) => Number.isFinite(value)
        ? fixed(value, digits) + suffix
        : '—';
      const cpuTemperature = reading(dev?.cpu?.temperature_c, ' °C', 1);
      const rows = [
        [t('hw.cpu'), reading(dev?.cpu_percent, ' %')],
        [t('hw.cpu_temperature'), [cpuTemperature, dev?.cpu?.source].filter(Boolean).join(' · ')],
        [t('hw.gpu'), [dev?.gpu?.name, reading(dev?.gpu?.percent, ' %')].filter(Boolean).join(' · ')],
        [t('hw.gpu_temperature'), reading(dev?.gpu?.temperature_c, ' °C', 1)],
        [t('hw.ram'), dev?.memory ? `${fixed(dev.memory.available / GIB, 1)} / ${fixed(dev.memory.total / GIB, 1)} GiB` : '—'],
        [t('hw.battery'), dev?.battery?.present === false ? t('hw.no_battery') : dev?.battery?.percent !== undefined ? `${fixed(dev.battery.percent, 0)} %` : '—'],
        [t('hw.disk'), dev?.disk ? `${dev.disk.drive} ${fixed(dev.disk.free / GIB, 1)} / ${fixed(dev.disk.total / GIB, 1)} GiB` : '—'],
      ];
      if (dev && telemetry.isDeviceStale()) {
        rows.push([t('hw.last_reading'), dev.sampled_at ? formatClock(dev.sampled_at) : '—']);
      }
      for (const [k, v] of rows) {
        statusList.insertAdjacentHTML('beforeend', `<dt class="fui-label">${escapeHtml(k)}</dt><dd class="fui-num">${escapeHtml(v)}</dd>`);
      }
    }
    statusDlg.showModal();
  }

  /* ── Weather Forecast Dialog (3-Day + Hourly Open-Meteo) ── */
  const weatherDlg = document.getElementById('dlg-weather');
  const weatherTitle = document.getElementById('dlg-weather-title');
  const weatherBody = document.getElementById('dlg-weather-body');
  function openForecast(data, label = '') {
    if (!weatherDlg || !weatherBody || !data) return;
    if (weatherTitle) weatherTitle.textContent = label ? `${label} · ${t('modal.weather_title')}` : t('modal.weather_title');
    const cur = data.current || data;
    const tempVal = Number.isFinite(cur.temperature)
      ? `${fixed(cur.temperature, 1)} °C`
      : Number.isFinite(cur.temperature_min) && Number.isFinite(cur.temperature_max)
        ? `${fixed(cur.temperature_min, 0)}–${fixed(cur.temperature_max, 0)} °C`
        : '—';

    let html = `
      <dl class="fui-dialog__list">
        <dt class="fui-label">${escapeHtml(t('modal.weather_cond'))}</dt><dd>${escapeHtml(weatherCondition(cur.code, cur.condition))}</dd>
        <dt class="fui-label">${escapeHtml(t('modal.weather_temp'))}</dt><dd class="fui-num">${escapeHtml(tempVal)}</dd>
        ${Number.isFinite(cur.wind_speed) ? `<dt class="fui-label">${escapeHtml(t('geo.wind'))}</dt><dd class="fui-num">${fixed(cur.wind_speed, 1)} km/h</dd>` : ''}
        ${Number.isFinite(cur.humidity) ? `<dt class="fui-label">${escapeHtml(t('geo.humidity'))}</dt><dd class="fui-num">${fixed(cur.humidity, 0)} %</dd>` : ''}
        ${Number.isFinite(cur.precipitation_probability) ? `<dt class="fui-label">${escapeHtml(t('modal.weather_precip'))}</dt><dd class="fui-num">${fixed(cur.precipitation_probability, 0)} %</dd>` : ''}
      </dl>`;

    if (data.days?.length) {
      html += `<div class="fui-divider" role="separator"><span>${escapeHtml(t('modal.weather_daily'))}</span></div>
        <div class="fui-table-wrap"><table class="fui-table fui-table--compact">
          <thead><tr><th>${escapeHtml(t('modal.weather_date'))}</th><th>${escapeHtml(t('modal.weather_cond'))}</th><th>${escapeHtml(t('modal.weather_temp'))}</th><th>${escapeHtml(t('modal.weather_precip'))}</th></tr></thead>
          <tbody>${data.days.map((d) => `<tr>
            <td class="fui-num">${escapeHtml(d.date)}</td>
            <td class="fui-table__text">${escapeHtml(weatherCondition(d.code, d.condition))}</td>
            <td class="fui-num">${fixed(d.temperature_min, 0)}–${fixed(d.temperature_max, 0)} °C</td>
            <td class="fui-num">${Number.isFinite(d.precipitation_probability) ? `${fixed(d.precipitation_probability, 0)}%` : '—'}</td>
          </tr>`).join('')}</tbody>
        </table></div>`;
    }

    if (data.hourly?.length) {
      html += `<div class="fui-divider" role="separator"><span>${escapeHtml(t('modal.weather_hourly'))}</span></div>
        <div class="fui-table-wrap"><table class="fui-table fui-table--compact">
          <thead><tr><th>${escapeHtml(t('modal.weather_time'))}</th><th>${escapeHtml(t('modal.weather_cond'))}</th><th>${escapeHtml(t('modal.weather_temp'))}</th><th>${escapeHtml(t('modal.weather_precip'))}</th></tr></thead>
          <tbody>${data.hourly.slice(0, 8).map((h) => `<tr>
            <td class="fui-num">${escapeHtml((h.time || '').split('T').at(-1) || '—')}</td>
            <td class="fui-table__text">${escapeHtml(weatherCondition(h.code, h.condition))}</td>
            <td class="fui-num">${Number.isFinite(h.temperature) ? `${fixed(h.temperature, 1)} °C` : '—'}</td>
            <td class="fui-num">${Number.isFinite(h.precipitation_probability) ? `${fixed(h.precipitation_probability, 0)}%` : '—'}</td>
          </tr>`).join('')}</tbody>
        </table></div>`;
    }
    html += `<p class="moss-card__meta">${escapeHtml(t('modal.weather_source'))}</p>`;
    weatherBody.innerHTML = html;
    weatherDlg.showModal();
  }

  /* ── Jev Draft Handoff Dialog ── */
  const jevDlg = document.getElementById('dlg-jev');
  const jevPreview = document.getElementById('jev-preview');
  const jevStatus = document.getElementById('jev-status');
  const jevConfirm = document.getElementById('jev-confirm');
  let jevPayload = null;

  function openJevHandoff(payload) {
    if (!jevDlg || jevDlg.open || !payload.chatId || !payload.turnId || !payload.text?.trim()) return;
    jevPayload = { ...payload, operationId: safeUuid(), bytes: new TextEncoder().encode(payload.text).length };
    if (jevPreview) jevPreview.textContent = payload.text;
    if (jevStatus) jevStatus.textContent = '';
    if (jevConfirm) jevConfirm.disabled = false;
    jevDlg.showModal();
  }
  jevDlg?.addEventListener('close', () => { jevPayload = null; });
  jevConfirm?.addEventListener('click', async () => {
    const submitted = jevPayload;
    if (!submitted || jevConfirm.disabled) return;
    jevConfirm.disabled = true;
    if (jevStatus) jevStatus.textContent = localized('正在等待草稿窗回执…', 'Waiting for the draft window…');
    try {
      const result = await api.submitJevHandoff({ chat_id: submitted.chatId, turn_id: submitted.turnId, operation_id: submitted.operationId });
      if (result.status !== 'filled' || result.bytes !== submitted.bytes) {
        throw new Error(localized('填入结果尚未核实，请查看本机草稿窗。', 'Fill could not be verified. Check the local draft window.'));
      }
      if (jevPayload === submitted && jevStatus) {
        jevStatus.textContent = localized('已填入本机草稿窗并核实正文。', 'Filled and verified in the local draft window.');
      }
    } catch (err) {
      if (jevPayload === submitted) {
        if (jevStatus) jevStatus.textContent = err.message;
        jevConfirm.disabled = false;
      }
    }
  });

  /* ── Tools Catalog Dialog ── */
  const toolsDlg = document.getElementById('dlg-tools');
  const toolsGrid = document.getElementById('tools-grid');
  function renderToolsGrid() {
    if (!toolsGrid) return;
    const isZh = document.documentElement.lang.startsWith('zh');
    toolsGrid.replaceChildren();
    for (const item of TOOL_PROMPTS) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fui-btn';
      btn.textContent = isZh ? item.labelZh : item.labelEn;
      btn.addEventListener('click', () => {
        closeDialog(toolsDlg);
        dialogue.fillComposer(item.key, { submit: false });
      });
      toolsGrid.append(btn);
    }
  }

  /* ── Settings Dialog (5-Tab 550W System Config Console + tactile matrices — zero clunky native <select>) ── */
  const settingsDlg = document.getElementById('dlg-settings');
  const settingsTabs = [...(settingsDlg?.querySelectorAll('[data-set-tab]') || [])];
  const settingsPanes = [...(settingsDlg?.querySelectorAll('[data-set-pane]') || [])];
  const oledGrid = document.getElementById('set-oled-grid');
  let activeSettingsTab = 'model';

  function selectSettingsTab(tabId) {
    const valid = settingsPanes.some((p) => p.dataset.setPane === tabId) ? tabId : 'model';
    activeSettingsTab = valid;
    for (const btn of settingsTabs) {
      btn.setAttribute('aria-selected', String(btn.dataset.setTab === valid));
    }
    for (const pane of settingsPanes) {
      pane.hidden = pane.dataset.setPane !== valid;
    }
    window.dispatchEvent(new CustomEvent('moss-settings-tab', { detail: valid }));
  }
  for (const btn of settingsTabs) {
    btn.addEventListener('click', () => selectSettingsTab(btn.dataset.setTab));
  }

  function syncOledPresetButtons(preset = telemetry.getOledPreset?.() || 'mono-white') {
    if (!oledGrid) return;
    for (const btn of oledGrid.querySelectorAll('[data-oled-preset]')) {
      btn.setAttribute('aria-checked', String(btn.dataset.oledPreset === preset));
    }
  }
  if (oledGrid) {
    for (const btn of oledGrid.querySelectorAll('[data-oled-preset]')) {
      btn.addEventListener('click', () => {
        telemetry.setOledPreset?.(btn.dataset.oledPreset);
        syncOledPresetButtons(btn.dataset.oledPreset);
      });
    }
    window.addEventListener('moss-oled-preset', (ev) => syncOledPresetButtons(ev.detail));
    syncOledPresetButtons();
  }
  const modelGrid = document.getElementById('set-model-grid');
  const cloudNote = document.getElementById('set-cloud-note');
  const cloudCountCheck = document.getElementById('set-cloud-count');
  const personalityGrid = document.getElementById('set-personality-grid');
  const personalityStatus = document.getElementById('set-personality-status');
  const ttsCheck = document.getElementById('set-tts');
  const residentCheck = document.getElementById('set-voice-resident');
  const residentStatus = document.getElementById('set-voice-resident-status');
  const micList = document.getElementById('set-mic-list');
  const micStatus = document.getElementById('set-mic-status');
  const wakeCheck = document.getElementById('set-wake');
  const bargeCheck = document.getElementById('set-barge');
  const stageAudioGroup = document.getElementById('set-stage-audio-group');
  const channelAudioGroup = document.getElementById('set-channel-audio-group');
  const scene3dCheck = document.getElementById('set-scene-3d');
  const simModeCheck = document.getElementById('set-sim-mode');
  const replayBtn = document.getElementById('set-replay-opening');
  const popoutBtn = document.getElementById('set-open-status');

  /* ── Tab 07: Soul Constitution, Beliefs & User Persona Archive ── */
  const soulDistillBtn = document.getElementById('set-soul-distill-btn');
  const soulExportBtn = document.getElementById('set-soul-export-btn');
  const soulDownloadBtn = document.getElementById('set-soul-download-btn');
  const soulStatsEl = document.getElementById('set-soul-stats');
  const soulStatusEl = document.getElementById('set-soul-status');
  const soulExportPreview = document.getElementById('set-soul-export-preview');
  const soulBeliefForm = document.getElementById('set-soul-belief-form');
  const soulBeliefScope = document.getElementById('set-soul-belief-scope');
  const soulBeliefInput = document.getElementById('set-soul-belief-input');
  const soulBeliefsList = document.getElementById('set-soul-beliefs-list');
  const soulTraitForm = document.getElementById('set-soul-trait-form');
  const soulTraitCat = document.getElementById('set-soul-trait-cat');
  const soulTraitKey = document.getElementById('set-soul-trait-key');
  const soulTraitVal = document.getElementById('set-soul-trait-val');
  const soulProfileList = document.getElementById('set-soul-profile-list');
  const soulPinnedTitle = document.getElementById('set-soul-pinned-title');
  const soulOpenMemoryBtn = document.getElementById('set-soul-open-memory-btn');

  let lastSoulSnap = null;
  let lastExportBundle = '';

  function showSoulStatus(msg, isError = false) {
    if (!soulStatusEl) return;
    soulStatusEl.hidden = !msg;
    soulStatusEl.textContent = msg || '';
    soulStatusEl.dataset.state = isError ? 'crit' : 'ok';
  }

  async function soulJson(url, options = {}) {
    const resp = await fetch(url, {
      cache: 'no-store',
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  function renderSoulArchive(snap) {
    if (!snap) return;
    lastSoulSnap = snap;
    const st = snap.stats || {};
    if (soulStatsEl) {
      soulStatsEl.textContent = t('soul.stats_line', {
        p: st.profile_count ?? 0,
        b: st.active_beliefs ?? 0,
        c: st.constitution_count ?? 0,
        m: st.memory_count ?? 0,
      });
    }
    if (soulPinnedTitle) {
      soulPinnedTitle.textContent = t('soul.pinned_title', { n: st.memory_count ?? (snap.memories?.length || 0) });
    }
    if (soulBeliefsList) {
      const beliefs = snap.beliefs || [];
      soulBeliefsList.replaceChildren(...beliefs.map((b) => {
        const li = document.createElement('li');
        li.className = 'moss-modal-row';
        const isCore = Boolean(b.pinned) || b.scope === 'constitution';
        const badgeState = isCore ? 'info' : (b.status === 'active' ? 'ok' : 'warn');
        const main = document.createElement('div');
        main.className = 'moss-modal-row__main';
        main.innerHTML = `<p class="moss-modal-row__text"><span class="fui-badge" data-state="${badgeState}">${escapeHtml(isCore ? t('soul.core_tag') : b.scope.toUpperCase())}</span> ${escapeHtml(b.statement)}</p>`
          + `<span class="moss-modal-row__meta fui-num">EVIDENCE x${b.evidence_count ?? 1} · CONF ${Number(b.confidence ?? 0.85).toFixed(2)} · ${escapeHtml(String(b.status || 'active').toUpperCase())}</span>`;
        li.append(main);
        if (!isCore) {
          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'fui-btn';
          const nextStatus = b.status === 'active' ? 'retired' : 'active';
          toggleBtn.textContent = nextStatus === 'retired' ? t('soul.belief_retire') : t('soul.belief_activate');
          toggleBtn.addEventListener('click', async () => {
            if (api.getMode() !== 'live') return;
            try {
              const res = await soulJson('/api/soul/beliefs', { method: 'POST', body: JSON.stringify({ id: b.id, status: nextStatus }) });
              renderSoulArchive(res.soul);
            } catch (err) { showSoulStatus(err.message, true); }
          });
          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'fui-btn fui-btn--danger';
          delBtn.textContent = t('modal.delete');
          delBtn.addEventListener('click', async () => {
            if (api.getMode() !== 'live') return;
            try {
              const res = await soulJson(`/api/soul/beliefs/${b.id}`, { method: 'DELETE' });
              renderSoulArchive(res.soul);
            } catch (err) { showSoulStatus(err.message, true); }
          });
          li.append(toggleBtn, delBtn);
        }
        return li;
      }));
    }
    if (soulProfileList) {
      const traits = snap.profile || [];
      if (!traits.length) {
        soulProfileList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${escapeHtml(t('soul.profile_empty'))}</span></li>`;
      } else {
        soulProfileList.replaceChildren(...traits.map((tr) => {
          const li = document.createElement('li');
          li.className = 'moss-modal-row';
          const main = document.createElement('div');
          main.className = 'moss-modal-row__main';
          main.innerHTML = `<p class="moss-modal-row__text"><span class="fui-badge" data-state="info">${escapeHtml(tr.category)}</span> <strong class="fui-num">${escapeHtml(tr.key)}</strong> · ${escapeHtml(tr.value)}</p>`
            + `<span class="moss-modal-row__meta fui-num">SRC ${escapeHtml(tr.source)} · SAL ${Number(tr.salience ?? 1).toFixed(2)} · HIT ${tr.access_count ?? 0}</span>`;
          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'fui-btn fui-btn--danger';
          delBtn.textContent = t('modal.delete');
          delBtn.addEventListener('click', async () => {
            if (api.getMode() !== 'live') return;
            try {
              const res = await soulJson(`/api/soul/profile/${encodeURIComponent(tr.key)}`, { method: 'DELETE' });
              renderSoulArchive(res.soul);
            } catch (err) { showSoulStatus(err.message, true); }
          });
          li.append(main, delBtn);
          return li;
        }));
      }
    }
  }

  async function refreshSoulArchive() {
    if (api.getMode() !== 'live') {
      renderSoulArchive({
        beliefs: [{ id: 0, scope: 'constitution', pinned: 1, evidence_count: 99, confidence: 1.0, status: 'active', statement: localized('事实与工具回执最高优先（仿真示例）', 'Truth & tool receipts first (sim)') }],
        profile: [{ key: 'work_city', value: 'Beijing', category: 'location', source: 'distill', salience: 1.1, access_count: 3 }],
        memories: api.getState()?.memories || [],
        stats: { profile_count: 1, active_beliefs: 1, constitution_count: 1, memory_count: (api.getState()?.memories || []).length },
      });
      return;
    }
    try {
      const snap = await soulJson('/api/soul');
      renderSoulArchive(snap);
    } catch (err) { showSoulStatus(err.message, true); }
  }

  window.addEventListener('moss-settings-tab', (ev) => {
    if (ev.detail === 'soul') void refreshSoulArchive();
  });

  soulDistillBtn?.addEventListener('click', async () => {
    if (api.getMode() !== 'live') { showSoulStatus(t('soul.sim_readonly'), true); return; }
    soulDistillBtn.disabled = true;
    try {
      const res = await soulJson('/api/soul/distill', { method: 'POST', body: '{}' });
      renderSoulArchive(res.soul);
      const sm = res.consolidation?.world_model?.soul_maintenance || {};
      showSoulStatus(t('soul.distill_done', {
        t: sm.traits_updated ?? 0,
        e: sm.episodes_indexed ?? 0,
        g: sm.beliefs_graduated ?? 0,
        o: sm.pruned_orphans ?? 0,
      }));
    } catch (err) { showSoulStatus(err.message, true); }
    finally { soulDistillBtn.disabled = false; }
  });

  soulExportBtn?.addEventListener('click', async () => {
    if (api.getMode() !== 'live') { showSoulStatus(t('soul.sim_readonly'), true); return; }
    soulExportBtn.disabled = true;
    try {
      const res = await soulJson('/api/soul/export');
      lastExportBundle = res.combined_markdown || '';
      if (soulExportPreview) {
        soulExportPreview.hidden = !lastExportBundle;
        soulExportPreview.textContent = lastExportBundle;
      }
      if (soulDownloadBtn) soulDownloadBtn.hidden = !lastExportBundle;
      showSoulStatus('');
    } catch (err) { showSoulStatus(err.message, true); }
    finally { soulExportBtn.disabled = false; }
  });

  soulDownloadBtn?.addEventListener('click', () => {
    if (!lastExportBundle) return;
    const blob = new Blob([lastExportBundle], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `moss-soul-archive-${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  soulBeliefForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (api.getMode() !== 'live') { showSoulStatus(t('soul.sim_readonly'), true); return; }
    const stmt = soulBeliefInput?.value?.trim();
    if (!stmt) { showSoulStatus(t('soul.field_required'), true); return; }
    const submitBtn = soulBeliefForm.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await soulJson('/api/soul/beliefs', {
        method: 'POST',
        body: JSON.stringify({ scope: soulBeliefScope?.value || 'habit', statement: stmt }),
      });
      if (soulBeliefInput) soulBeliefInput.value = '';
      renderSoulArchive(res.soul);
      showSoulStatus('');
    } catch (err) { showSoulStatus(err.message, true); }
    finally { if (submitBtn) submitBtn.disabled = false; }
  });

  soulTraitForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (api.getMode() !== 'live') { showSoulStatus(t('soul.sim_readonly'), true); return; }
    const key = soulTraitKey?.value?.trim();
    const value = soulTraitVal?.value?.trim();
    if (!key || !value) { showSoulStatus(t('soul.field_required'), true); return; }
    const submitBtn = soulTraitForm.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await soulJson('/api/soul/profile', {
        method: 'POST',
        body: JSON.stringify({ category: soulTraitCat?.value || 'preference', key, value, salience: 1.25 }),
      });
      if (soulTraitKey) soulTraitKey.value = '';
      if (soulTraitVal) soulTraitVal.value = '';
      renderSoulArchive(res.soul);
      showSoulStatus('');
    } catch (err) { showSoulStatus(err.message, true); }
    finally { if (submitBtn) submitBtn.disabled = false; }
  });

  soulOpenMemoryBtn?.addEventListener('click', () => {
    closeDialog(settingsDlg);
    renderMemories(api.getState());
    memoryDlg?.showModal();
  });

  let lastModelsBody = null;
  let selectingModel = false;
  const modelStatus = document.createElement('p');
  modelStatus.className = 'moss-card__meta';
  modelStatus.setAttribute('role', 'status');
  modelStatus.hidden = true;
  modelGrid?.after(modelStatus);

  function renderModels(body) {
    if (body) lastModelsBody = body;
    const data = body || lastModelsBody;
    if (!modelGrid || !data?.models) return;
    const selectedId = data.selected || data.models[0]?.id || '';
    modelGrid.replaceChildren();
    for (const m of data.models) {
      const isSelected = m.id === selectedId;
      const isCloud = ['deepseek', 'aliyun', 'kimi'].includes(m.id);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-choice-key moss-choice-key--model';
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', String(isSelected));
      btn.disabled = !m.available || selectingModel;
      btn.innerHTML = `
        <span class="moss-choice-key__name">${escapeHtml(m.label)}</span>
        <span class="moss-choice-key__tag fui-num">${m.available ? (isCloud ? 'CLOUD' : 'LOCAL') : 'N/A'}</span>`;
      btn.addEventListener('click', async () => {
        if (isSelected || selectingModel) return;
        selectingModel = true;
        modelStatus.hidden = false;
        modelStatus.textContent = localized('正在切换模型…', 'Switching model…');
        renderModels();
        try {
          const state = await onSelectModel?.(m.id);
          if (state?.model_id !== m.id) throw new Error(localized('模型切换尚未核实。', 'Model switch was not confirmed.'));
          lastModelsBody = { ...lastModelsBody, selected: state.model_id };
          modelStatus.hidden = true;
        } catch (error) { modelStatus.textContent = error.message; }
        finally {
          selectingModel = false;
          renderModels();
          modelGrid.querySelector('[aria-checked="true"]')?.focus({ preventScroll: true });
        }
      });
      modelGrid.append(btn);
    }
    if (cloudNote) cloudNote.hidden = !['deepseek', 'aliyun', 'kimi'].includes(selectedId);
  }

  let lastPersonalityState = null;
  let selectingPersonality = false;
  function renderPersonalities(state = null) {
    if (state?.personalities?.length) {
      lastPersonalityState = {
        personality_id: state.personality_id || 'partner',
        personalities: state.personalities,
      };
    } else if (state?.personality_id && lastPersonalityState) {
      lastPersonalityState = { ...lastPersonalityState, personality_id: state.personality_id };
    }
    const data = lastPersonalityState;
    if (!personalityGrid || !data?.personalities?.length) return;
    const isZh = getLocale().startsWith('zh');
    const activeId = data.personality_id || 'partner';
    personalityGrid.replaceChildren();
    for (const item of data.personalities) {
      const isSelected = item.id === activeId;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-personality-card';
      btn.dataset.personality = item.id;
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', String(isSelected));
      btn.disabled = selectingPersonality;
      const label = isZh ? item.label : (item.label_en || item.label);
      const desc = isZh ? (item.desc || item.description || '') : (item.desc_en || item.description_en || item.desc || item.description || '');
      const sample = isZh ? (item.sample || '') : (item.sample_en || item.sample || '');
      btn.innerHTML = `
        <span class="moss-personality-card__head">
          <span class="moss-personality-card__code fui-num">${escapeHtml(item.code || item.id.toUpperCase())}</span>
          <strong class="moss-personality-card__title">${escapeHtml(label)}</strong>
        </span>
        <span class="moss-personality-card__desc">${escapeHtml(desc)}</span>
        ${sample ? `<span class="moss-personality-card__sample">${escapeHtml(sample)}</span>` : ''}`;
      btn.addEventListener('click', async () => {
        if (isSelected || selectingPersonality) return;
        selectingPersonality = true;
        renderPersonalities();
        if (personalityStatus) personalityStatus.textContent = localized('正在切换性格预设…', 'Switching personality…');
        try {
          const next = await onSelectPersonality?.(item.id);
          const confirmed = next?.personality_id || item.id;
          lastPersonalityState = {
            ...lastPersonalityState,
            personality_id: confirmed,
            ...(next?.personalities?.length ? { personalities: next.personalities } : {}),
          };
          if (personalityStatus) {
            personalityStatus.textContent = t('set.personality_switched', { label });
          }
        } catch (error) {
          if (personalityStatus) personalityStatus.textContent = error.message;
        } finally {
          selectingPersonality = false;
          renderPersonalities();
          personalityGrid.querySelector('[aria-checked="true"]')?.focus({ preventScroll: true });
        }
      });
      personalityGrid.append(btn);
    }
  }

  let micPopulateSeq = 0;

  async function populateMics() {
    if (!micList) return;
    const seq = ++micPopulateSeq;
    const rawDevices = await voice.listMicDevices();
    if (seq !== micPopulateSeq) return;
    const devices = rawDevices.some((d) => typeof d.score === 'number') ? rawDevices : rankMicDevices(rawDevices);
    const activeId = voice.getMicDevice();
    const liveInfo = voice.getActiveMicInfo?.() || null;
    const bestDevice = devices[0] || null;
    const items = [
      { id: '', label: t('set.mic_default'), code: 'AUTO', badges: [] },
      ...devices.map((d, idx) => {
        const badges = [];
        if (d.recommended) badges.push(t('set.mic_badge_best'));
        if (d.tier === 'dedicated') badges.push(t('set.mic_badge_ext'));
        else if (d.virtual) badges.push(t('set.mic_badge_virtual'));
        if (liveInfo?.deviceId && liveInfo.deviceId === d.deviceId) badges.push(t('set.mic_badge_active'));
        return {
          id: d.deviceId,
          label: d.label || `Mic ${d.deviceId.slice(0, 6)}`,
          code: `IN-${String(idx + 1).padStart(2, '0')}`,
          badges,
        };
      }),
    ];
    micList.replaceChildren();
    for (const item of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-choice-row';
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', String(item.id === activeId));
      const tagHtml = item.badges.map((b) => `<span class="fui-badge" data-state="${b === t('set.mic_badge_virtual') ? 'warn' : 'ok'}">${escapeHtml(b)}</span>`).join(' ');
      btn.innerHTML = `<span class="moss-choice-row__code fui-num">${escapeHtml(item.code)}</span>`
        + `<span class="moss-choice-row__label">${escapeHtml(item.label)} ${tagHtml}</span>`;
      btn.addEventListener('click', () => {
        voice.setMicDevice(item.id);
        for (const sib of micList.querySelectorAll('[role="radio"]')) {
          sib.setAttribute('aria-checked', String(sib === btn));
        }
        void populateMics();
      });
      micList.append(btn);
    }
    if (micStatus) {
      if (!activeId) {
        const pickedLabel = liveInfo?.label || bestDevice?.label || localized('系统默认', 'System default');
        micStatus.textContent = t('set.mic_status_auto', { label: pickedLabel });
      } else {
        const chosen = devices.find((d) => d.deviceId === activeId);
        const liveMatch = liveInfo?.deviceId === activeId ? liveInfo.label : '';
        const chosenLabel = chosen?.label || liveMatch || activeId.slice(0, 8);
        micStatus.textContent = t('set.mic_status_pinned', { label: chosenLabel });
      }
    }
  }
  navigator.mediaDevices?.addEventListener?.('devicechange', () => {
    if (settingsDlg?.open) void populateMics();
  });
  window.addEventListener('moss-mic-changed', () => {
    if (settingsDlg?.open) void populateMics();
  });

  if (cloudCountCheck) {
    cloudCountCheck.checked = dialogue.getShowCloudCounts();
    cloudCountCheck.addEventListener('change', () => dialogue.setShowCloudCounts(cloudCountCheck.checked));
  }
  function syncResidentStatus() {
    if (residentCheck && voice.getVoiceResident) {
      residentCheck.checked = voice.getVoiceResident();
      residentCheck.disabled = !voice.getTtsEnabled();
    }
    if (residentStatus && voice.getSessionWarmStatus) {
      const status = voice.getSessionWarmStatus();
      residentStatus.value = t(`set.voice_resident_${status}`);
    }
  }
  if (ttsCheck) {
    ttsCheck.checked = voice.getTtsEnabled();
    ttsCheck.addEventListener('change', () => {
      voice.setTtsEnabled(ttsCheck.checked);
      syncResidentStatus();
    });
  }
  if (residentCheck && voice.setVoiceResident) {
    residentCheck.checked = voice.getVoiceResident();
    residentCheck.addEventListener('change', () => {
      voice.setVoiceResident(residentCheck.checked);
      syncResidentStatus();
    });
  }
  window.addEventListener('moss-speech-warm-state', syncResidentStatus);
  syncResidentStatus();
  wakeCheck?.addEventListener('change', () => voice.setWakeEnabled(wakeCheck.checked));
  bargeCheck?.addEventListener('change', () => voice.setBargeInEnabled(bargeCheck.checked));

  function syncAudioWaveButtons() {
    const curStage = stage.getAudioStyle?.() || 'spectrum';
    if (stageAudioGroup) {
      for (const b of stageAudioGroup.querySelectorAll('[data-stage-audio]')) {
        b.setAttribute('aria-checked', String(b.dataset.stageAudio === curStage));
      }
    }
    const curCh = telemetry.getChannelAudioStyle?.() || 'bars';
    if (channelAudioGroup) {
      for (const b of channelAudioGroup.querySelectorAll('[data-channel-audio]')) {
        b.setAttribute('aria-checked', String(b.dataset.channelAudio === curCh));
      }
    }
  }
  if (stageAudioGroup) {
    for (const btn of stageAudioGroup.querySelectorAll('[data-stage-audio]')) {
      btn.addEventListener('click', () => {
        stage.setAudioStyle(btn.dataset.stageAudio);
        syncAudioWaveButtons();
      });
    }
  }
  if (channelAudioGroup) {
    for (const btn of channelAudioGroup.querySelectorAll('[data-channel-audio]')) {
      btn.addEventListener('click', () => {
        const next = btn.dataset.channelAudio;
        telemetry.setChannelAudioStyle?.(next);
        hudPopups?.setChannelAudioStyle?.(next);
        syncAudioWaveButtons();
      });
    }
  }
  syncAudioWaveButtons();
  scene3dCheck?.addEventListener('change', () => stage.setSceneEnabled(scene3dCheck.checked));
  simModeCheck?.addEventListener('change', () => api.forceSimulated(simModeCheck.checked));
  replayBtn?.addEventListener('click', () => {
    closeDialog(settingsDlg);
    opening.start(true);
  });
  popoutBtn?.addEventListener('click', () => {
    closeDialog(settingsDlg);
    telemetry.openStatusPopout();
  });

  function open(name, opts = {}) {
    if (name === 'history') { renderHistory(api.getState()); historyDlg?.showModal(); }
    else if (name === 'memory') { renderMemories(api.getState()); memoryDlg?.showModal(); }
    else if (name === 'notes') { loadNotes(); notesDlg?.showModal(); }
    else if (name === 'tools') { renderToolsGrid(); toolsDlg?.showModal(); }
    else if (name === 'settings') {
      selectSettingsTab(opts?.tab || activeSettingsTab);
      syncOledPresetButtons();
      populateMics();
      syncResidentStatus();
      if (simModeCheck) simModeCheck.checked = api.getMode() === 'simulated';
      settingsDlg?.showModal();
    }
  }

  return {
    open,
    selectSettingsTab,
    renderHistory,
    renderMemories,
    renderModels,
    renderPersonalities,
    syncSelectedModel(id) {
      if (id && lastModelsBody && lastModelsBody.selected !== id) renderModels({ ...lastModelsBody, selected: id });
    },
    openLocalStatus,
    openForecast,
    openJevHandoff,
    refreshI18n() {
      renderHistory(api.getState());
      renderMemories(api.getState());
      renderPersonalities(api.getState());
      renderSoulArchive(lastSoulSnap);
      renderToolsGrid();
      syncResidentStatus();
      void populateMics();
    },
  };
}

function escapeHtml(str) {
  return String(str ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}
