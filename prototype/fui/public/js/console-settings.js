import { onLocaleChange, t } from './i18n.js';

const base = '/api/console-settings';

async function requestApi(url, body, signal) {
  const options = { cache: 'no-store', signal };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  const reply = await fetch(url, options);
  const data = await reply.json().catch(() => ({}));
  if (!reply.ok) throw new Error(data.error || t('console.request_error', { code: reply.status }));
  return data;
}

function request(resource, body, signal) {
  return requestApi(base + '/' + resource, body, signal);
}

function installConsoleSettings() {
  const get = id => document.getElementById(id);
  const dialog = get('dlg-settings');
  const providerSection = get('set-provider-section');
  const librarySection = get('set-library-section');
  if (!dialog || !providerSection || !librarySection) return;

  const providerForm = get('set-provider-form');
  const providerFields = get('set-provider-fields');
  const providerOptions = get('set-provider-choices');
  const providerFeedback = get('set-provider-feedback');
  const availability = get('set-provider-availability');
  const address = providerForm.elements.base_url;
  const model = providerForm.elements.model;
  const apiKey = providerForm.elements.api_key;
  let providers = [];
  let providerId = '';
  let loadingProviders = false;
  let savingProvider = false;

  const sourcesList = get('set-library-sources');
  const libraryForm = get('set-library-form');
  const libraryFields = get('set-library-fields');
  const scanAll = get('set-library-scan-all');
  const refreshLibrary = get('set-library-refresh');
  const libraryFeedback = get('set-library-feedback');
  let library = null;
  let libraryRead;
  let libraryWrite = false;
  let scanPoll;
  let providersShown = false;
  let libraryShown = false;

  const omSection = get('set-online-music-section');
  const omForm = get('set-online-music-form');
  const omFields = get('set-online-music-fields');
  const omFeedback = get('set-online-music-feedback');
  const omEnabled = get('set-om-enabled');
  const omLrclib = get('set-om-lrclib');
  const omEmpty = get('set-om-empty');
  const omOffline = get('set-om-offline');
  const omAudius = get('set-om-audius');
  const omArchive = get('set-om-archive');
  const omRadio = get('set-om-radio');
  const omJamendo = get('set-om-jamendo');
  const omJamendoId = get('set-om-jamendo-id');
  const omJamendoState = get('set-om-jamendo-state');
  const omNavEnabled = get('set-om-nav-enabled');
  const omNavUrl = get('set-om-nav-url');
  const omNavUser = get('set-om-nav-user');
  const omNavPass = get('set-om-nav-pass');
  const omNavState = get('set-om-nav-state');
  const omPingNav = get('set-om-ping-nav');
  const omPingPub = get('set-om-ping-pub');
  let omLoading = false;
  let omSaving = false;
  let omLoaded = false;
  let omDirty = false;
  let omShown = false;
  let omHasConfig = false;
  let providerDirty = false;
  let providersLoaded = false;

  const locationOptions = new URLSearchParams(location.search);
  const demo = locationOptions.get('sim') === '1' || locationOptions.has('beat');
  const canAccess = () => !demo && document.body.dataset.backend !== 'simulated' && document.body.dataset.authRole !== 'C';
  const visible = section => (
    dialog.open
    && section.open
    && !section.closest('[hidden]')
    && (!section.closest('[data-cs-only="true"]') || document.body.dataset.authRole !== 'C')
    && !document.hidden
  );
  const scanning = () => ['queued', 'running', 'scanning'].includes(library?.scan?.state);
  const statusText = new Map();

  function feedback(node, key, { reason, error = false } = {}) {
    statusText.set(node, {key, reason});
    node.textContent = reason || (key ? t(key) : '');
    node.hidden = !node.textContent;
    node.dataset.state = error ? 'crit' : 'data';
  }

  function updateAvailability() {
    const selected = providers.find(row => row.id === providerId);
    if (!selected) {
      feedback(availability);
      return;
    }
    feedback(availability, selected.available ? 'console.ready' : 'console.unavailable',
      {reason: !selected.available ? selected.reason : undefined});
  }

  function fillProvider(id) {
    const selected = providers.find(row => row.id === id);
    if (!selected) return;
    providerId = selected.id;
    address.value = selected.base_url || '';
    model.value = selected.model || '';
    apiKey.value = '';
    updateAvailability();
  }

  function applyProviders(data, id = providerId) {
    if (!Array.isArray(data.providers) || !data.providers.length) {
      throw new Error(t('console.offline'));
    }
    providers = data.providers;
    const preferred = [id, data.selected, providers[0].id].find(key => providers.some(row => row.id === key));
    const choices = providers.map(row => {
      const label = document.createElement('label');
      label.className = 'moss-console-settings__option';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'provider';
      radio.value = row.id;
      radio.checked = row.id === preferred;
      radio.addEventListener('change', () => {
        if (!radio.checked) return;
        fillProvider(radio.value);
        feedback(providerFeedback);
      });
      const name = document.createElement('span');
      name.textContent = row.label;
      label.append(radio, name);
      return label;
    });
    providerOptions.replaceChildren(...choices);
    fillProvider(preferred);
  }

  async function loadProviders() {
    if (loadingProviders || savingProvider) return;
    providerFields.disabled = true;
    if (!canAccess()) {
      feedback(providerFeedback, 'console.offline', {error:true});
      return;
    }
    loadingProviders = true;
    feedback(providerFeedback, 'console.loading');
    try {
      applyProviders(await request('providers'));
      providersLoaded = true;
      providerDirty = false;
      feedback(providerFeedback);
    } catch (error) {
      providers = [];
      feedback(providerFeedback, null, {reason:error.message, error:true});
    } finally {
      loadingProviders = false;
      providerFields.disabled = !canAccess() || providers.length === 0;
    }
  }

  providerForm.addEventListener('input', () => { providerDirty = true; });
  providerForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (savingProvider || loadingProviders || !providerId || !canAccess()) return;
    const body = { id:providerId, base_url:address.value.trim(), model:model.value.trim() };
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
    savingProvider = true;
    providerFields.disabled = true;
    feedback(providerFeedback, 'console.saving');
    try {
      const result = await request('providers', body);
      apiKey.value = '';
      applyProviders(result, body.id);
      feedback(providerFeedback, 'console.saved');
      window.dispatchEvent(new CustomEvent('moss-providers-changed', {detail:result}));
    } catch (error) {
      feedback(providerFeedback, null, {reason:error.message, error:true});
    } finally {
      savingProvider = false;
      providerFields.disabled = !canAccess() || providers.length === 0;
    }
  });

  function renderLibrary() {
    const sources = library?.sources || [];
    const disabled = libraryWrite || scanning() || !canAccess();
    scanAll.disabled = disabled || !sources.length;
    libraryFields.disabled = disabled || !library;
    refreshLibrary.disabled = libraryWrite || !canAccess();
    sourcesList.replaceChildren();
    if (library && !sources.length) {
      const item = document.createElement('li');
      item.className = 'moss-card__meta';
      item.textContent = t('console.no_sources');
      sourcesList.append(item);
    }
    for (const row of sources) {
      const item = document.createElement('li');
      item.className = 'moss-console-settings__source';
      const label = document.createElement('strong');
      label.textContent = row.label;
      const path = document.createElement('code');
      path.textContent = row.path;
      const meta = document.createElement('small');
      meta.className = 'moss-card__meta';
      const details = [t(row.kind === 'nas' ? 'console.nas' : 'console.local')];
      if (row.state === 'ready') {
        details.push(t('console.source_items', {count:row.count ?? 0}));
      } else if (row.state === 'new' || !row.state) {
        details.push(t('console.source_pending'));
      } else {
        details.push(row.error || t('console.source_unavailable'));
      }
      if (row.checked) {
        const checked = new Date(row.checked);
        const stamp = isNaN(checked.getTime()) ? row.checked : checked.toLocaleString(document.documentElement.lang || 'zh-CN');
        details.push(t('console.source_checked', {time:stamp}));
      }
      meta.textContent = details.join(' · ');
      const scan = document.createElement('button');
      scan.type = 'button';
      scan.className = 'fui-btn';
      scan.textContent = t('console.scan_one');
      scan.disabled = disabled;
      scan.addEventListener('click', () => updateLibrary({action:'scan', id:row.id}));
      item.append(label, path, meta, scan);
      sourcesList.append(item);
    }
  }

  function applyLibrary(data, added = false) {
    if (!Array.isArray(data.sources) || !data.scan) throw new Error(t('console.offline'));
    library = data;
    const {state, error} = data.scan;
    const scanMessages = { queued:'console.scan_pending', running:'console.scan_running', scanning:'console.scan_running',
      done:'console.scan_complete', partial:'console.scan_partial', error:'console.scan_failed' };
    feedback(libraryFeedback, added ? 'console.source_added' : scanMessages[state],
      {reason:added ? undefined : error, error:!added && state === 'error'});
  }

  function scheduleScanPoll() {
    clearTimeout(scanPoll);
    if (visible(librarySection) && canAccess() && scanning()) {
      scanPoll = setTimeout(loadLibrary, 850);
    }
  }

  async function loadLibrary() {
    if (libraryRead || libraryWrite || !visible(librarySection)) return;
    if (!canAccess()) {
      feedback(libraryFeedback, 'console.offline', {error:true});
      renderLibrary();
      return;
    }
    const read = libraryRead = new AbortController();
    if (!library) feedback(libraryFeedback, 'console.loading');
    try {
      applyLibrary(await request('library', undefined, read.signal));
    } catch (error) {
      if (!read.signal.aborted) feedback(libraryFeedback, null, {reason:error.message, error:true});
    } finally {
      if (libraryRead === read) libraryRead = null;
      renderLibrary();
      scheduleScanPoll();
    }
  }

  async function updateLibrary(body) {
    if (!canAccess() || libraryWrite || scanning()) return;
    libraryRead?.abort();
    libraryRead = null;
    libraryWrite = true;
    renderLibrary();
    feedback(libraryFeedback, body.action === 'scan' ? 'console.scan_pending' : 'console.saving');
    try {
      const added = body.action === 'add';
      applyLibrary(await request('library', body), added);
      if (added) libraryForm.reset();
    } catch (error) {
      feedback(libraryFeedback, null, {reason:error.message, error:true});
    } finally {
      libraryWrite = false;
      renderLibrary();
      scheduleScanPoll();
    }
  }

  scanAll.addEventListener('click', () => updateLibrary({action:'scan'}));
  refreshLibrary.addEventListener('click', loadLibrary);
  libraryForm.addEventListener('submit', event => {
    event.preventDefault();
    const form = libraryForm.elements;
    void updateLibrary({action:'add', kind:form.kind.value, label:form.label.value.trim(), path:form.path.value.trim()});
  });

  let omHasNavPw = false;
  let omHasJamId = false;

  function normalizeNavUrl(raw) {
    const val = String(raw || '').trim();
    if (!val || /^[a-z][a-z0-9+.-]*:\/\//i.test(val)) return val;
    return `http://${val}`;
  }

  function syncSecretBadges() {
    if (omJamendoState) {
      if (omJamendoId?.value?.trim()) {
        omJamendoState.hidden = false;
        omJamendoState.dataset.state = 'warn';
        omJamendoState.textContent = t('console.om_state_dirty');
      } else if (omHasJamId) {
        omJamendoState.hidden = false;
        omJamendoState.dataset.state = 'ok';
        omJamendoState.textContent = t('console.om_state_saved');
      } else {
        omJamendoState.hidden = true;
      }
    }
    if (omNavState) {
      if (omNavPass?.value) {
        omNavState.hidden = false;
        omNavState.dataset.state = 'warn';
        omNavState.textContent = t('console.om_state_dirty');
      } else if (omHasNavPw) {
        omNavState.hidden = false;
        omNavState.dataset.state = 'ok';
        omNavState.textContent = t('console.om_state_saved');
      } else {
        omNavState.hidden = true;
      }
    }
  }

  function applyOnlineMusic(data) {
    omHasConfig = Boolean(data);
    if (!data) return;
    omLoaded = true;
    omDirty = false;
    if (omEnabled) omEnabled.checked = data.enabled !== false;
    if (omLrclib) omLrclib.checked = data.lyrics_lrclib !== false;
    if (omEmpty) omEmpty.checked = data.fallback_when_empty !== false;
    if (omOffline) omOffline.checked = data.fallback_when_offline !== false;
    if (omAudius) omAudius.checked = data.audius_enabled !== false;
    if (omArchive) omArchive.checked = data.archive_enabled !== false;
    if (omRadio) omRadio.checked = data.radiobrowser_enabled !== false;
    const jam = data.jamendo || {};
    omHasJamId = Boolean(jam.has_client_id);
    if (omJamendo) omJamendo.checked = Boolean(jam.enabled);
    if (omJamendoId) {
      omJamendoId.value = '';
      omJamendoId.placeholder = t(omHasJamId ? 'console.om_secret_saved' : 'console.om_secret_keep');
    }
    const nav = data.navidrome || {};
    omHasNavPw = Boolean(nav.has_password);
    if (omNavEnabled) omNavEnabled.checked = Boolean(nav.enabled);
    if (omNavUrl) omNavUrl.value = nav.url || '';
    if (omNavUser) omNavUser.value = nav.username || '';
    if (omNavPass) {
      omNavPass.value = '';
      omNavPass.placeholder = t(omHasNavPw ? 'console.om_secret_saved' : 'console.om_secret_keep');
    }
    syncSecretBadges();
  }

  async function loadOnlineMusic() {
    if (!omSection || omLoading || omSaving) return;
    if (omFields) omFields.disabled = true;
    if (!canAccess()) {
      feedback(omFeedback, 'console.offline', { error: true });
      return;
    }
    omLoading = true;
    feedback(omFeedback, 'console.loading');
    try {
      applyOnlineMusic(await requestApi('/api/online-music/config'));
      feedback(omFeedback);
    } catch (error) {
      feedback(omFeedback, null, { reason: error.message, error: true });
    } finally {
      omLoading = false;
      if (omFields) omFields.disabled = !canAccess() || !omHasConfig;
    }
  }

  const markOmDirty = () => {
    omDirty = true;
    syncSecretBadges();
    if (!omSaving && !omLoading && omFeedback) {
      feedback(omFeedback, 'console.om_dirty_hint');
    }
  };
  omForm?.addEventListener('input', markOmDirty);
  omForm?.addEventListener('change', markOmDirty);

  omForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (omSaving || omLoading || !canAccess()) return;
    const nav = {
      enabled: Boolean(omNavEnabled?.checked),
      url: normalizeNavUrl(omNavUrl?.value),
      username: String(omNavUser?.value || '').trim(),
    };
    if (omNavPass?.value) nav.password = omNavPass.value;
    const jam = { enabled: Boolean(omJamendo?.checked) };
    if (omJamendoId?.value?.trim()) jam.client_id = omJamendoId.value.trim();
    const payload = {
      enabled: Boolean(omEnabled?.checked),
      lyrics_lrclib: Boolean(omLrclib?.checked),
      fallback_when_empty: Boolean(omEmpty?.checked),
      fallback_when_offline: Boolean(omOffline?.checked),
      audius_enabled: Boolean(omAudius?.checked),
      archive_enabled: Boolean(omArchive?.checked),
      radiobrowser_enabled: Boolean(omRadio?.checked),
      navidrome: nav,
      jamendo: jam,
    };
    omSaving = true;
    if (omFields) omFields.disabled = true;
    feedback(omFeedback, 'console.saving');
    try {
      applyOnlineMusic(await requestApi('/api/online-music/config', payload));
      feedback(omFeedback, 'console.saved');
    } catch (error) {
      feedback(omFeedback, null, { reason: error.message, error: true });
    } finally {
      omSaving = false;
      if (omFields) omFields.disabled = !canAccess();
    }
  });

  async function pingOnlineMusic(target) {
    if (!canAccess() || omLoading || omSaving) return;
    omSaving = true;
    if (omFields) omFields.disabled = true;
    feedback(omFeedback, 'console.om_pinging');
    try {
      const payload = target === 'navidrome'
        ? {
            target: 'navidrome',
            url: normalizeNavUrl(omNavUrl?.value),
            username: String(omNavUser?.value || '').trim(),
            password: omNavPass?.value || '',
          }
        : {
            target: 'public',
            query: 'piano',
            ...(omJamendoId?.value?.trim() ? { jamendo_client_id: omJamendoId.value.trim() } : {}),
            jamendo_enabled: Boolean(omJamendo?.checked),
          };
      const res = await requestApi('/api/online-music/ping', payload);
      if (target === 'navidrome') {
        const detail = [res.type || 'subsonic', res.version].filter(Boolean).join(' v');
        feedback(omFeedback, null, { reason: t('console.om_ping_ok', { detail }) });
      } else if (res.ok) {
        const parts = [`${(res.providers || []).join(', ')} · ${res.sample || res.count}`];
        if (res.jamendo_detail) parts.push(res.jamendo_detail);
        feedback(omFeedback, null, { reason: t('console.om_ping_ok', { detail: parts.join(' | ') }), error: Boolean(res.jamendo_error) });
      } else {
        feedback(omFeedback, null, { reason: res.jamendo_detail || t('console.om_ping_empty'), error: true });
      }
    } catch (error) {
      feedback(omFeedback, null, { reason: error.message, error: true });
    } finally {
      omSaving = false;
      if (omFields) omFields.disabled = !canAccess();
    }
  }

  omPingNav?.addEventListener('click', () => { void pingOnlineMusic('navidrome'); });
  omPingPub?.addEventListener('click', () => { void pingOnlineMusic('public'); });

  const cascadeCheck = get('set-local-cascade');
  let cascadeLoaded = false;
  let cascadeSaving = false;

  async function syncCascade() {
    if (!cascadeCheck || cascadeLoaded || !canAccess()) return;
    try {
      const data = await request('providers');
      cascadeCheck.checked = Boolean(data?.local_cloud_cascade);
      cascadeLoaded = true;
    } catch { /* wait for manual retry */ }
  }

  cascadeCheck?.addEventListener('change', async () => {
    if (!canAccess() || cascadeSaving) return;
    cascadeSaving = true;
    cascadeCheck.disabled = true;
    const nextVal = cascadeCheck.checked;
    feedback(providerFeedback, 'console.saving');
    try {
      const res = await request('providers', { local_cloud_cascade: nextVal });
      cascadeCheck.checked = Boolean(res?.local_cloud_cascade);
      feedback(providerFeedback, 'console.saved');
    } catch (error) {
      cascadeCheck.checked = !nextVal;
      feedback(providerFeedback, null, { reason: error.message, error: true });
    } finally {
      cascadeSaving = false;
      cascadeCheck.disabled = !canAccess();
    }
  });

  function syncVisibility() {
    const showProvider = visible(providerSection);
    const showLibrary = visible(librarySection);
    const showOnlineMusic = Boolean(omSection && visible(omSection));
    if (!dialog.open) {
      apiKey.value = '';
      providerDirty = false;
      providersLoaded = false;
      cascadeLoaded = false;
      if (omNavPass) omNavPass.value = '';
      if (omJamendoId) omJamendoId.value = '';
      omDirty = false;
      omLoaded = false;
      syncSecretBadges();
    }
    if (showProvider && !providersLoaded && !providerDirty) void loadProviders();
    if (showProvider && !cascadeLoaded) void syncCascade();
    if (showLibrary && !libraryShown) void loadLibrary();
    if (showOnlineMusic && !omLoaded && !omDirty) void loadOnlineMusic();
    if (!showLibrary || !canAccess()) {
      clearTimeout(scanPoll);
      libraryRead?.abort();
      libraryRead = null;
    }
    providerFields.disabled = !canAccess() || loadingProviders || savingProvider || !providers.length;
    if (cascadeCheck) cascadeCheck.disabled = !canAccess() || cascadeSaving;
    if (omFields) omFields.disabled = !canAccess() || omLoading || omSaving || !omHasConfig;
    providersShown = showProvider;
    libraryShown = showLibrary;
    omShown = showOnlineMusic;
  }

  new MutationObserver(syncVisibility).observe(dialog, {attributes:true, attributeFilter:['open']});
  providerSection.addEventListener('toggle', syncVisibility);
  librarySection.addEventListener('toggle', syncVisibility);
  omSection?.addEventListener('toggle', syncVisibility);
  window.addEventListener('moss-settings-tab', syncVisibility);
  document.addEventListener('visibilitychange', syncVisibility);
  onLocaleChange(() => {
    for (const [node, value] of statusText) {
      if (!value.reason && value.key) node.textContent = t(value.key);
    }
    if (omJamendoId) omJamendoId.placeholder = t(omHasJamId ? 'console.om_secret_saved' : 'console.om_secret_keep');
    if (omNavPass) omNavPass.placeholder = t(omHasNavPw ? 'console.om_secret_saved' : 'console.om_secret_keep');
    syncSecretBadges();
    updateAvailability();
    renderLibrary();
  });
  syncVisibility();
}

installConsoleSettings();

