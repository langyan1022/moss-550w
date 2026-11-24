/**
 * Left Rail Telemetry + Earth Track & Live Site Weather + Bottom Dock Audio/Log/Reminders/Media.
 *
 * Uses vendor/fui-geo.js (dot-matrix globe, flyTo, attachGlobeControls) and
 * vendor/fui-audio.js (drawEnvelope, pushEnvelope, level).
 */
import { attachGlobeControls, drawGlobe, flyTo } from '../vendor/fui-geo.js';
import { createOrbit, drawEnvelope, drawOrbit, drawSpectrum, holdPeaks, pushEnvelope, toBands } from '../vendor/fui-audio.js';
import { fixed, formatClock, formatCoord, t, weatherCondition } from './i18n.js';

function computeTimeLevel(time) {
  if (!time?.length) return { peak: 0, rms: 0, rmsDb: -Infinity };
  const isFloat = time instanceof Float32Array || time instanceof Float64Array;
  let peak = 0;
  let sum = 0;
  for (let i = 0; i < time.length; i++) {
    const v = isFloat ? time[i] : (time[i] - 128) / 128;
    const a = Math.abs(v);
    if (a > peak) peak = a;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / time.length);
  return { peak: Math.min(1, peak), rms, rmsDb: rms > 1e-6 ? 20 * Math.log10(rms) : -Infinity };
}

const GIB = 1024 ** 3;

export function setGauge(el, unit) {
  if (!el) return;
  const clamped = Math.min(1, Math.max(0, Number.isFinite(unit) ? unit : 0));
  const segments = Number(getComputedStyle(el).getPropertyValue('--segments')) || 16;
  const prev = Number(el.style.getPropertyValue('--fui-value')) || 0;
  const snapped = Math.round(clamped * segments) / segments;
  el.style.setProperty('--fui-steps', String(Math.max(1, Math.round(Math.abs(snapped - prev) * segments))));
  el.style.setProperty('--fui-value', snapped.toFixed(3));
  el.setAttribute('aria-valuenow', String(Math.round(clamped * 100)));
  if (!el.hasAttribute('data-ready')) {
    requestAnimationFrame(() => el.setAttribute('data-ready', '')); // fui-lint-ignore: R15 one-shot post-first-paint flag
  }
}

function setStateAttr(el, state) {
  if (!el) return;
  if (state && state !== 'idle') el.setAttribute('data-state', state);
  else el.removeAttribute('data-state');
}

export function createTelemetryController({
  fetchLocalWeather,
  onOpenOverview,
  onOpenForecast,
  onOpenCityPicker,
  onDismissTimer,
  onOpenTimerSource,
  onSelectMedia,
  onGetLogSource,
}) {
  // Hardware telemetry elements (v3 Station Meter Rows)
  const cpuRow = document.getElementById('hw-cpu-row');
  const cpuVal = document.getElementById('hw-cpu-val');
  const cpuGauge = document.getElementById('hw-cpu-gauge');
  const cpuTemp = document.getElementById('hw-cpu-temp');
  const gpuRow = document.getElementById('hw-gpu-row');
  const gpuName = document.getElementById('hw-gpu-name');
  const gpuVal = document.getElementById('hw-gpu-val');
  const gpuGauge = document.getElementById('hw-gpu-gauge');
  const gpuTemp = document.getElementById('hw-gpu-temp');
  const ramRow = document.getElementById('hw-ram-row');
  const ramVal = document.getElementById('hw-ram-val');
  const ramGauge = document.getElementById('hw-ram-gauge');
  const batRow = document.getElementById('hw-bat-row');
  const batVal = document.getElementById('hw-bat-val');
  const batGauge = document.getElementById('hw-bat-gauge');
  const diskRow = document.getElementById('hw-disk-row');
  const diskVal = document.getElementById('hw-disk-val');
  const diskGauge = document.getElementById('hw-disk-gauge');
  const hwMeta = document.getElementById('hw-meta');
  const hwBadge = document.getElementById('hw-badge');
  const hwInspectBtn = document.getElementById('hw-inspect');
  const hwPopoutBtn = document.getElementById('hw-popout');
  const tagSiteCodeEl = document.getElementById('tag-site-code');

  // Earth track & site weather elements
  const globeCanvas = document.getElementById('geo-globe');
  const geoCallout = document.getElementById('geo-callout');
  const geoCoord = document.getElementById('geo-coord');
  const geoSiteLabel = document.getElementById('geo-site-label');
  const geoSiteId = document.getElementById('geo-site-id');
  const geoLocateBtn = document.getElementById('geo-locate');
  const geoSpinBtn = document.getElementById('geo-spin');
  const geoRefreshBtn = document.getElementById('geo-refresh');
  const geoForecastBtn = document.getElementById('geo-forecast');
  const weatherTemp = document.getElementById('geo-weather-temp');
  const weatherCond = document.getElementById('geo-weather-cond');
  const weatherFacts = document.getElementById('geo-weather-facts');

  // Bottom dock tabs & panes
  const dockTabs = [...document.querySelectorAll('[data-dock-tab]')];
  const dockPanes = [...document.querySelectorAll('[data-dock-pane]')];
  const logBody = document.getElementById('runtime-log-body');
  const logWrap = document.getElementById('log-scroll-wrap') || logBody?.closest('.fui-table-wrap') || null;
  const logOledRx = document.getElementById('log-oled-rx');
  const logOledSeq = document.getElementById('log-oled-seq');
  const logFollowBtn = document.getElementById('log-follow-btn');
  const logSourceBadge = document.getElementById('log-source-badge');
  const remCountEl = document.getElementById('dock-rem-count');
  const remListEl = document.getElementById('dock-reminders-list');
  const remEmptyEl = document.getElementById('dock-reminders-empty');
  const mediaCountEl = document.getElementById('dock-media-count');
  const mediaListEl = document.getElementById('dock-media-list');
  const mediaEmptyEl = document.getElementById('dock-media-empty');

  // Audio channel strips
  const micBadge = document.getElementById('strip-mic-badge');
  const micCanvas = document.getElementById('strip-mic-wave');
  const micNoSig = document.getElementById('strip-mic-nosig');
  const micDbEl = document.getElementById('strip-mic-db');
  const mossBadge = document.getElementById('strip-moss-badge');
  const mossCanvas = document.getElementById('strip-moss-wave');
  const mossNoSig = document.getElementById('strip-moss-nosig');
  const mossDbEl = document.getElementById('strip-moss-db');

  const micHistory = new Float32Array(120);
  const mossHistory = new Float32Array(120);
  const micPeaks = new Float32Array(22);
  const mossPeaks = new Float32Array(22);
  const standbyBands = Float32Array.from({ length: 22 }, (_, i) => 0.04 + 0.025 * Math.sin(i * 0.65));
  const micOrbit = createOrbit(22);
  const mossOrbit = createOrbit(22);
  let micLastTick = performance.now();
  let mossLastTick = performance.now();
  let lastMicFrame = null;
  let lastMossFrame = null;
  let channelAudioStyle = (() => {
    try {
      const saved = localStorage.getItem('moss-fui.channel-wave');
      if (['fui', 'bars', 'orbit'].includes(saved)) return saved;
    } catch { /* ignore */ }
    return 'bars';
  })();
  let lastDbWrite = 0;
  let logFollow = true;
  let oledFrameSeq = 0;
  let oledPulseTimer = 0;
  const OLED_PRESETS = ['mono-white', 'ssd-dual', 'ice-cyan', 'amber', 'phosphor-green', 'moss-red'];
  let oledPreset = (() => {
    try {
      const saved = localStorage.getItem('moss-fui.oled-preset');
      if (OLED_PRESETS.includes(saved)) return saved;
    } catch { /* ignore */ }
    return 'mono-white';
  })();

  function setOledPreset(next, { persist = true } = {}) {
    oledPreset = OLED_PRESETS.includes(next) ? next : 'mono-white';
    if (persist) {
      try { localStorage.setItem('moss-fui.oled-preset', oledPreset); } catch { /* ignore */ }
    }
    window.dispatchEvent(new CustomEvent('moss-oled-preset', { detail: oledPreset }));
  }
  window.addEventListener('storage', (ev) => {
    if (ev.key === 'moss-fui.oled-preset') setOledPreset(ev.newValue || 'mono-white', { persist: false });
  });
  setOledPreset(oledPreset, { persist: false });

  let lastDevice = null;
  let lastDeviceStale = false;
  let lastDeviceLive = false;
  let lastHwSeverity = 'idle';
  let cpuAlertStreak = { warnStreak: 0, critStreak: 0 };
  let logRows = [];

  let activeReminders = [];
  let activeMediaQueue = { turnId: null, chatId: null, items: [], selectedId: null };

  // Earth track & site weather state (default Beijing / UTC+8 reference)
  const qs = new URLSearchParams(location.search);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const globeView = { lon: 116.4074, lat: 39.9042 };
  let globeSpinEnabled = true;
  let globeFrozen = qs.get('paused') === '1';
  let globeDragging = false;
  let globeHoldUntil = 0;
  let globeRaf = 0;
  let globeLastTs = 0;
  let globePulsePhase = 0;
  let manualCity = null;
  let site = {
    id: 'REF-08',
    labelKey: 'geo.ref_site',
    customLabel: null,
    lat: 39.9042,
    lon: 116.4074,
    state: 'ok',
  };
  let siteWeather = null;
  let weatherLoading = false;

  /* ── Hardware Telemetry Renderer (dynamic multi-tier color coding by live value band) ── */
  function evaluateHardware(data) {
    if (!data) {
      return {
        severity: 'idle',
        cpuState: 'idle', ramState: 'idle', batState: 'idle', diskState: 'idle',
        cpuGaugeState: 'data', ramGaugeState: 'data', batGaugeState: 'data', diskGaugeState: 'data',
      };
    }
    const cpu = data.cpu_percent;
    const ramFreeGb = data.memory ? data.memory.available / GIB : null;
    const ramTotGb = data.memory ? data.memory.total / GIB : null;
    const ramUsedRatio = ramFreeGb !== null && ramTotGb ? 1 - ramFreeGb / ramTotGb : null;
    const batPct = data.battery?.present !== false ? data.battery?.percent : null;
    const diskFreeGb = data.disk ? data.disk.free / GIB : null;
    const diskTotGb = data.disk ? data.disk.total / GIB : null;
    const diskUsedRatio = diskFreeGb !== null && diskTotGb ? 1 - diskFreeGb / diskTotGb : null;

    // Alert severity (drives panel badge & Level-2 banner — strictly real thresholds)
    const cpuState = cpu >= 90 ? 'crit' : cpu >= 80 ? 'warn' : 'idle';
    const ramState = ramFreeGb !== null ? (ramFreeGb <= 2 ? 'crit' : ramFreeGb <= 4 ? 'warn' : 'idle') : 'idle';
    const batState = batPct !== null && !data.battery?.ac ? (batPct <= 10 ? 'crit' : batPct <= 20 ? 'warn' : 'idle') : 'idle';
    const diskState = diskFreeGb !== null ? (diskFreeGb <= 15 ? 'crit' : diskFreeGb <= 25 ? 'warn' : 'idle') : 'idle';

    // Data-level gauge colors (ok = healthy headroom, data = active normal, warn = elevated, crit = critical)
    const cpuGaugeState = !Number.isFinite(cpu) ? 'data'
      : cpu >= 90 ? 'crit' : cpu >= 75 ? 'warn' : cpu >= 40 ? 'data' : 'ok';
    const ramGaugeState = ramUsedRatio === null ? 'data'
      : (ramFreeGb <= 2 || ramUsedRatio >= 0.90) ? 'crit'
        : (ramFreeGb <= 4 || ramUsedRatio >= 0.75) ? 'warn'
          : ramUsedRatio < 0.30 ? 'ok' : 'data';
    const batGaugeState = batPct === null ? 'idle'
      : batPct <= 15 ? 'crit' : batPct <= 35 ? 'warn' : batPct >= 75 ? 'ok' : 'data';
    const diskGaugeState = diskUsedRatio === null ? 'data'
      : (diskFreeGb <= 15 || diskUsedRatio >= 0.88) ? 'crit'
        : (diskFreeGb <= 40 || diskUsedRatio >= 0.65) ? 'warn'
          : diskUsedRatio < 0.45 ? 'ok' : 'data';

    const list = [cpuState, ramState, batState, diskState];
    const severity = list.includes('crit') ? 'crit' : list.includes('warn') ? 'warn' : 'idle';
    return {
      severity,
      cpuState, ramState, batState, diskState,
      cpuGaugeState, ramGaugeState, batGaugeState, diskGaugeState,
    };
  }

  function showLoad(value, gauge, stale) {
    const known = Number.isFinite(value);
    setGauge(gauge, known ? value / 100 : 0);
    if (!known) gauge.removeAttribute('aria-valuenow');
    if (known && !stale) gauge.removeAttribute('aria-valuetext');
    else gauge.setAttribute('aria-valuetext', known ? t('hw.last_reading') + ' · ' + fixed(value, 0) + ' %' : t('hw.no_signal'));
  }

  function renderProcessorReadings(data, stale) {
    const cpu = data?.cpu_percent;
    const gpu = data?.gpu;
    const temperature = value => Number.isFinite(value) ? fixed(value, 0) + ' °C' : '—';
    cpuVal.textContent = Number.isFinite(cpu) ? fixed(cpu, 0) + ' %' : '—';
    cpuTemp.textContent = temperature(data?.cpu?.temperature_c);
    gpuVal.textContent = Number.isFinite(gpu?.percent) ? fixed(gpu.percent, 0) + ' %' : '—';
    gpuTemp.textContent = temperature(gpu?.temperature_c);
    if (typeof gpu?.name === 'string' && gpu.name) gpuName.title = gpu.name;
    else gpuName.removeAttribute('title');
    showLoad(cpu, cpuGauge, stale);
    showLoad(gpu?.percent, gpuGauge, stale);
    const gpuState = Number.isFinite(gpu?.percent) ? (stale ? 'warn' : 'data') : 'idle';
    setStateAttr(gpuRow, gpuState);
    setStateAttr(gpuGauge, gpuState);
  }

  function renderDevice(data, { stale = false, live = false, refreshOnly = false } = {}) {
    lastDevice = data;
    lastDeviceStale = stale;
    if (!refreshOnly) lastDeviceLive = Boolean(live);
    renderProcessorReadings(data, stale);
    const ev = evaluateHardware(stale ? null : data);
    if (!data || stale) {
      cpuAlertStreak = { warnStreak: 0, critStreak: 0 };
      lastHwSeverity = ev.severity;
    } else {
      const effectiveLive = refreshOnly ? lastDeviceLive : Boolean(live);
      const db = computeDebouncedCpuState(data.cpu_percent, cpuAlertStreak, {
        live: effectiveLive,
        advance: !refreshOnly,
      });
      if (!refreshOnly) cpuAlertStreak = { warnStreak: db.warnStreak, critStreak: db.critStreak };
      const alertList = [db.state, ev.ramState, ev.batState, ev.diskState];
      ev.severity = alertList.includes('crit') ? 'crit' : alertList.includes('warn') ? 'warn' : 'idle';
      lastHwSeverity = ev.severity;
    }

    if (!data) {
      ramVal.textContent = '—';
      batVal.textContent = '—';
      diskVal.textContent = '—';
      setStateAttr(cpuRow, 'idle');
      setStateAttr(cpuGauge, 'idle');
      setStateAttr(ramRow, 'idle');
      setStateAttr(ramGauge, 'idle');
      setStateAttr(batRow, 'idle');
      setStateAttr(batGauge, 'idle');
      setStateAttr(diskRow, 'idle');
      setStateAttr(diskGauge, 'idle');
      setGauge(ramGauge, 0);
      setGauge(batGauge, 0);
      setGauge(diskGauge, 0);
      if (hwBadge) {
        hwBadge.dataset.state = 'idle';
        hwBadge.textContent = t('hw.no_signal');
      }
      if (hwMeta) hwMeta.textContent = t('hw.no_signal');
      return ev;
    }

    const cpuState = stale && Number.isFinite(data.cpu_percent) ? 'warn' : ev.cpuState;
    setStateAttr(cpuRow, cpuState);
    setStateAttr(cpuGauge, cpuState);

    const ramFreeGb = data.memory ? data.memory.available / GIB : null;
    const ramTotGb = data.memory ? data.memory.total / GIB : null;
    ramVal.textContent = ramFreeGb !== null ? `${fixed(ramFreeGb, 1)} G` : '—';
    setGauge(ramGauge, ramFreeGb !== null && ramTotGb ? 1 - ramFreeGb / ramTotGb : 0);
    setStateAttr(ramRow, ev.ramState);
    setStateAttr(ramGauge, ev.ramState);

    const bat = data.battery;
    if (bat && bat.present === false) {
      batVal.textContent = 'AC';
      setGauge(batGauge, 1);
      setStateAttr(batRow, 'idle');
      setStateAttr(batGauge, 'idle');
    } else if (bat && Number.isFinite(bat.percent)) {
      batVal.textContent = `${fixed(bat.percent, 0)} %`;
      setGauge(batGauge, bat.percent / 100);
      setStateAttr(batRow, ev.batState);
      setStateAttr(batGauge, ev.batState);
    } else {
      batVal.textContent = '—';
      setGauge(batGauge, 0);
      setStateAttr(batRow, 'idle');
      setStateAttr(batGauge, 'idle');
    }

    const diskFreeGb = data.disk ? data.disk.free / GIB : null;
    const diskTotGb = data.disk ? data.disk.total / GIB : null;
    diskVal.textContent = diskFreeGb !== null ? `${fixed(diskFreeGb, 0)} G` : '—';
    setGauge(diskGauge, diskFreeGb !== null && diskTotGb ? 1 - diskFreeGb / diskTotGb : 0);
    setStateAttr(diskRow, ev.diskState);
    setStateAttr(diskGauge, ev.diskState);

    if (hwBadge) {
      hwBadge.dataset.state = stale ? 'warn' : ev.severity === 'idle' ? 'ok' : ev.severity;
      hwBadge.textContent = stale
        ? t('hw.last_reading')
        : ev.severity === 'crit'
          ? t('state.critical')
          : ev.severity === 'warn'
            ? t('state.degraded')
            : t('state.nominal');
    }
    if (hwMeta) {
      const power = bat?.charging ? t('hw.charging') : bat?.ac ? t('hw.ac_on') : t('hw.ac_off');
      const stamp = data.sampled_at ? formatClock(data.sampled_at) : '--:--:--';
      hwMeta.textContent = `${power} · ${stamp}`;
    }
    return ev;
  }

  /* ── Dot-Matrix Globe + Continuous 3D Orbital Spin + Real-Time Site Weather ── */
  let lastCityFitKey = '';
  let lastCoordKey = '';

  function formatStackedCoord(lat, lon) {
    const [latPart = '', lonPart = ''] = formatCoord(lat, lon).split(/\s+/);
    return lonPart ? `LAT ${latPart}\nLON ${lonPart}` : formatCoord(lat, lon);
  }

  function fitCityLabel(force = false) {
    if (!geoSiteLabel) return;
    const text = (geoSiteLabel.textContent || '').trim();
    const box = geoSiteLabel.parentElement;
    const avail = Math.floor((box?.clientWidth || geoSiteLabel.clientWidth || 0) - 2);
    if (!text || avail < 40) return;
    const key = `${text}|${avail}`;
    if (!force && key === lastCityFitKey) return;
    lastCityFitKey = key;

    const charCount = [...text].length;
    const track = charCount <= 2 ? '0.1em' : charCount <= 4 ? '0.05em' : '0.02em';
    geoSiteLabel.style.setProperty('--moss-city-track', track);

    let lo = 14;
    let hi = 46;
    let best = lo;
    const prevMaxW = geoSiteLabel.style.maxWidth;
    geoSiteLabel.style.maxWidth = 'none';
    for (let i = 0; i < 8; i += 1) {
      const mid = (lo + hi) / 2;
      geoSiteLabel.style.setProperty('--moss-city-size', `${mid.toFixed(1)}px`);
      if (geoSiteLabel.getBoundingClientRect().width <= avail) {
        best = mid;
        lo = mid;
      } else {
        hi = mid;
      }
    }
    geoSiteLabel.style.maxWidth = prevMaxW;
    geoSiteLabel.style.setProperty('--moss-city-size', `${Math.max(14, Math.floor(best * 2) / 2).toFixed(1)}px`);
  }

  function redrawGlobe() {
    if (!globeCanvas) return;
    const pulseRing = globeSpinEnabled && !globeFrozen && !reduced.matches
      ? 8 + 4 * (0.5 + 0.5 * Math.sin(globePulsePhase))
      : 10;
    drawGlobe(globeCanvas, {
      lon: globeView.lon,
      lat: globeView.lat,
      markers: [{ id: site.id, lon: site.lon, lat: site.lat, state: site.state, selected: true, ring: pulseRing }],
    });
    const coordKey = `${site.lat.toFixed(4)},${site.lon.toFixed(4)}`;
    if (geoCoord && coordKey !== lastCoordKey) {
      lastCoordKey = coordKey;
      geoCoord.textContent = formatStackedCoord(site.lat, site.lon);
    }
    const labelText = site.customLabel || t(site.labelKey);
    if (geoSiteLabel && geoSiteLabel.textContent !== labelText) {
      geoSiteLabel.textContent = labelText;
    }
    fitCityLabel();
    if (tagSiteCodeEl && tagSiteCodeEl.textContent !== labelText) tagSiteCodeEl.textContent = labelText;
    if (geoSiteId && geoSiteId.textContent !== site.id) geoSiteId.textContent = site.id;
    if (geoCallout) geoCallout.textContent = `${site.id} · ${formatCoord(site.lat, site.lon)}`;
  }

  function syncGlobeSpinUi() {
    if (!geoSpinBtn) return;
    geoSpinBtn.setAttribute('aria-pressed', String(globeSpinEnabled));
    geoSpinBtn.textContent = t(globeSpinEnabled ? 'geo.spin_on' : 'geo.spin_off');
  }

  function syncGlobeLoop() {
    const shouldRun = Boolean(globeCanvas) && globeSpinEnabled && !globeFrozen && !reduced.matches && !document.hidden;
    if (!shouldRun) {
      cancelAnimationFrame(globeRaf);
      globeRaf = 0;
      redrawGlobe();
      return;
    }
    if (globeRaf) return;
    globeLastTs = performance.now();
    const step = (now) => {
      if (!globeSpinEnabled || globeFrozen || reduced.matches || document.hidden) {
        globeRaf = 0;
        redrawGlobe();
        return;
      }
      const dt = Math.min(0.05, Math.max(0.001, (now - globeLastTs) / 1000));
      globeLastTs = now;
      globePulsePhase += dt * 3.2;
      if (!globeDragging && now >= globeHoldUntil) {
        // Smooth eastward 3D planetary rotation (~10 deg/s) with subtle axial tilt sway
        globeView.lon = ((globeView.lon + dt * 10 + 180) % 360) - 180;
        globeView.lat = 18 * Math.sin(globePulsePhase * 0.08) + site.lat * 0.35;
      }
      redrawGlobe();
      globeRaf = requestAnimationFrame(step); // fui-lint-ignore: R15 user-requested continuous 3D Earth rotation; pauses when frozen, hidden, dragged, or reduced-motion
    };
    globeRaf = requestAnimationFrame(step); // fui-lint-ignore: R15 user-requested continuous 3D Earth rotation; pauses when frozen, hidden, dragged, or reduced-motion
  }

  function renderSiteWeather() {
    if (!weatherTemp || !weatherCond || !weatherFacts) return;
    if (weatherLoading) {
      weatherTemp.innerHTML = `<span class="fui-num">—</span><span class="fui-readout__unit">°C</span>`;
      weatherCond.textContent = t('geo.weather_loading');
      weatherFacts.textContent = '';
      if (geoForecastBtn) geoForecastBtn.disabled = true;
      return;
    }
    if (!siteWeather?.current) {
      weatherTemp.innerHTML = `<span class="fui-num">—</span><span class="fui-readout__unit">°C</span>`;
      weatherCond.textContent = t('geo.weather_na');
      weatherFacts.textContent = '';
      if (geoForecastBtn) geoForecastBtn.disabled = true;
      return;
    }
    const c = siteWeather.current;
    const tempStr = Number.isFinite(c.temperature) ? fixed(c.temperature, 1) : '—';
    weatherTemp.innerHTML = `<span class="fui-num">${tempStr}</span><span class="fui-readout__unit">°C</span>`;
    weatherCond.textContent = weatherCondition(c.code, c.condition);

    const facts = [];
    if (Number.isFinite(c.wind_speed)) facts.push(`${t('geo.wind')} ${fixed(c.wind_speed, 0)} km/h`);
    if (Number.isFinite(c.humidity)) facts.push(`${t('geo.humidity')} ${fixed(c.humidity, 0)}%`);
    if (Number.isFinite(c.precipitation_probability)) facts.push(`${t('geo.precip')} ${fixed(c.precipitation_probability, 0)}%`);
    weatherFacts.textContent = facts.join(' · ');
    if (geoForecastBtn) geoForecastBtn.disabled = false;
  }

  async function loadSiteWeather({ silent = false } = {}) {
    if (!fetchLocalWeather) return;
    const target = site;
    weatherLoading = !silent && !siteWeather;
    renderSiteWeather();
    try {
      const data = await fetchLocalWeather(target.lat, target.lon);
      if (site !== target) return;
      siteWeather = data;
      weatherLoading = false;
      renderSiteWeather();
    } catch {
      if (site !== target) return;
      weatherLoading = false;
      renderSiteWeather();
    }
  }

  function focusSite({ lat, lon, id = 'FIX-01', labelKey = 'geo.query_site', customLabel = null, weather = null, fetchWeather = true }) {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const coordChanged = lat !== site.lat || lon !== site.lon;
    if (coordChanged) siteWeather = null;
    weatherLoading = false;
    site = { id, labelKey, customLabel, lat, lon, state: 'ok' };
    if (weather) {
      siteWeather = {
        source: weather.source || 'Open-Meteo',
        city: weather.city || customLabel,
        latitude: lat,
        longitude: lon,
        current: {
          temperature: weather.temperature ?? weather.temperature_max ?? weather.temperature_min,
          code: weather.code,
          condition: weather.condition,
          humidity: weather.humidity,
          wind_speed: weather.wind_speed,
          precipitation_probability: weather.precipitation_probability,
        },
        days: weather.days || [],
        hourly: weather.hourly || [],
      };
      renderSiteWeather();
    } else if (coordChanged) {
      renderSiteWeather();
    }
    globeHoldUntil = performance.now() + 3200;
    flyTo(globeView, { lon, lat }, redrawGlobe);
    if (fetchWeather && !weather) loadSiteWeather({ silent: false });
  }

  function setManualCity(city, { fetchWeather = true } = {}) {
    if (!city || !Number.isFinite(city.lat) || !Number.isFinite(city.lon)) return;
    manualCity = { ...city };
    const coordChanged = city.lat !== site.lat || city.lon !== site.lon;
    if (coordChanged) siteWeather = null;
    site = {
      id: `${city.code || 'CITY'} · LOCK`,
      labelKey: 'geo.ref_site',
      customLabel: city.name,
      lat: city.lat,
      lon: city.lon,
      state: 'ok',
    };
    if (coordChanged) renderSiteWeather();
    globeHoldUntil = performance.now() + 3200;
    flyTo(globeView, { lon: city.lon, lat: city.lat }, redrawGlobe);
    if (fetchWeather) loadSiteWeather({ silent: false });
  }

  function clearManualCity() {
    manualCity = null;
    siteWeather = null;
    site = {
      id: 'REF-08',
      labelKey: 'geo.ref_site',
      customLabel: null,
      lat: 39.9042,
      lon: 116.4074,
      state: 'ok',
    };
    globeHoldUntil = performance.now() + 2400;
    flyTo(globeView, { lon: site.lon, lat: site.lat }, redrawGlobe);
    renderSiteWeather();
  }

  geoSiteLabel?.addEventListener('click', () => onOpenCityPicker?.());

  geoSpinBtn?.addEventListener('click', () => {
    globeSpinEnabled = !globeSpinEnabled;
    syncGlobeSpinUi();
    syncGlobeLoop();
  });

  function locateBrowser({ interactive = false } = {}) {
    // Manual city strictly takes precedence over automatic geolocation coordinates
    if (manualCity) {
      globeHoldUntil = performance.now() + 3200;
      flyTo(globeView, { lon: manualCity.lon, lat: manualCity.lat }, redrawGlobe);
      addLog({
        time: Date.now(),
        source: 'GEO',
        text: t('city.locked_log', { city: manualCity.name }),
        level: 'info',
      });
      if (interactive) onOpenCityPicker?.();
      return Promise.resolve(null);
    }
    if (!navigator.geolocation) {
      if (weatherCond) weatherCond.textContent = t('geo.locate_denied');
      return Promise.resolve(null);
    }
    geoLocateBtn.disabled = true;
    if (geoSiteLabel) {
      geoSiteLabel.textContent = t('geo.locate_busy');
      fitCityLabel(true);
    }
    return new Promise(resolve => navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        geoLocateBtn.disabled = false;
        if (manualCity) return resolve(null);
        browserLocation = { latitude: coords.latitude, longitude: coords.longitude };
        browserLocationAt = Date.now();
        focusSite({
          lat: coords.latitude,
          lon: coords.longitude,
          id: 'LOC-01',
          labelKey: 'geo.browser_site',
          fetchWeather: true,
        });
        resolve(browserLocation);
      },
      () => {
        geoLocateBtn.disabled = false;
        redrawGlobe();
        if (!manualCity && weatherCond) weatherCond.textContent = t('geo.locate_denied');
        resolve(null);
      },
      { enableHighAccuracy: false, timeout: 8000, maximumAge: 300000 },
    ));
  }

  let browserLocation = null;
  let browserLocationAt = 0;
  geoLocateBtn?.addEventListener('click', () => locateBrowser({ interactive: true }));

  geoRefreshBtn?.addEventListener('click', () => loadSiteWeather({ silent: false }));
  geoForecastBtn?.addEventListener('click', () => {
    if (siteWeather) onOpenForecast?.(siteWeather, site.customLabel || t(site.labelKey));
  });

  if (globeCanvas) {
    attachGlobeControls(globeCanvas, globeView, () => {
      globeHoldUntil = performance.now() + 2400;
      redrawGlobe();
    });
    const endDrag = () => {
      if (globeDragging) {
        globeDragging = false;
        globeHoldUntil = performance.now() + 2400;
      }
    };
    globeCanvas.addEventListener('pointerdown', () => { globeDragging = true; });
    window.addEventListener('pointerup', endDrag);
    window.addEventListener('pointercancel', endDrag);
    window.addEventListener('blur', endDrag);
    new ResizeObserver(redrawGlobe).observe(globeCanvas);
    if (geoSiteLabel?.parentElement) {
      new ResizeObserver(() => fitCityLabel(true)).observe(geoSiteLabel.parentElement);
    }
    document.fonts?.ready?.then(() => fitCityLabel(true));
    document.addEventListener('visibilitychange', syncGlobeLoop);
    reduced.addEventListener('change', syncGlobeLoop);
    syncGlobeSpinUi();
    redrawGlobe();
    syncGlobeLoop();
  }

  /* ── Dual Audio Channel Strips (MIC = crit red when live, MOSS = ok emerald when live / warn amber when muted) ── */
  let mossMuted = false;

  function setMossMuted(muted) {
    mossMuted = Boolean(muted);
    if (!mossCanvas || mossCanvas.hidden) updateAudioChannel('moss', null);
  }

  function renderChannelCanvas(canvas, isMic, hist, frame) {
    if (!canvas) return;
    const isActive = Boolean(frame);
    const chState = isMic ? (isActive ? 'crit' : 'warn') : (!isActive && mossMuted ? 'crit' : undefined);
    const chInk = isMic ? (isActive ? 'hi' : 'dim') : (isActive ? 'data' : 'dim');

    if (channelAudioStyle === 'fui' && isActive) {
      drawEnvelope(canvas, hist, { state: chState });
      return;
    }

    const now = performance.now();
    const dt = Math.min(0.1, Math.max(0.01, (now - (isMic ? micLastTick : mossLastTick)) / 1000));
    if (isMic) micLastTick = now;
    else mossLastTick = now;

    // Build 22-band voice-range FFT spectrum (80 Hz–8 kHz) with perceptual lift
    let bands;
    if (!isActive) {
      bands = standbyBands;
      (isMic ? micPeaks : mossPeaks).fill(0);
    } else if (frame?.freq?.length) {
      bands = toBands(frame.freq, frame.sampleRate || 48000, 22, { fMin: 80, fMax: 8000 });
      for (let i = 0; i < bands.length; i++) {
        bands[i] = Math.min(1, bands[i] > 0.01 ? Math.pow(bands[i], 0.72) : bands[i]);
      }
    } else {
      bands = new Float32Array(22);
      const stride = Math.max(1, Math.floor(hist.length / 22));
      for (let i = 0; i < 22; i++) {
        let peak = 0;
        for (let j = i * stride; j < Math.min(hist.length, (i + 1) * stride); j++) {
          if (hist[j] > peak) peak = hist[j];
        }
        bands[i] = Math.min(1, peak * 1.15);
      }
    }

    const peaks = isActive ? holdPeaks(isMic ? micPeaks : mossPeaks, bands, dt, 0.85) : undefined;
    if (channelAudioStyle === 'orbit' && isActive) {
      drawOrbit(canvas, isMic ? micOrbit : mossOrbit, {
        bands,
        dt: 0.032,
        layout: 'line',
        waves: 2,
        glow: true,
        particles: true,
        ambient: false,
        ink: chInk,
        state: chState,
      });
    } else {
      drawSpectrum(canvas, bands, {
        style: 'bars',
        segments: 7,
        gap: 2,
        ink: chInk,
        state: chState,
        peaks,
      });
    }
  }

  function setChannelAudioStyle(style) {
    channelAudioStyle = ['fui', 'bars', 'orbit'].includes(style) ? style : 'bars';
    try { localStorage.setItem('moss-fui.channel-wave', channelAudioStyle); } catch { /* ignore */ }
    renderChannelCanvas(micCanvas, true, micHistory, lastMicFrame);
    renderChannelCanvas(mossCanvas, false, mossHistory, lastMossFrame);
  }

  function updateAudioChannel(channel, frame) {
    const isMic = channel === 'mic';
    if (isMic) lastMicFrame = frame || null;
    else lastMossFrame = frame || null;
    const badge = isMic ? micBadge : mossBadge;
    const canvas = isMic ? micCanvas : mossCanvas;
    const noSig = isMic ? micNoSig : mossNoSig;
    const dbEl = isMic ? micDbEl : mossDbEl;
    const hist = isMic ? micHistory : mossHistory;
    const rowEl = badge?.closest('.fui-voice');

    if (!frame) {
      hist.fill(0);
      if (canvas) canvas.hidden = false;
      if (noSig) noSig.hidden = true;
      if (badge) {
        badge.classList.remove('fui-badge--solid');
        if (!isMic && mossMuted) {
          badge.dataset.state = 'crit';
          badge.textContent = t('audio.muted');
        } else {
          badge.dataset.state = isMic ? 'warn' : 'data';
          badge.textContent = t('audio.standby');
        }
      }
      if (rowEl) rowEl.dataset.channelState = !isMic && mossMuted ? 'muted' : 'standby';
      if (dbEl) dbEl.textContent = !isMic && mossMuted ? 'MUTE' : '—';
      renderChannelCanvas(canvas, isMic, hist, null);
      return;
    }

    if (canvas) canvas.hidden = false;
    if (noSig) noSig.hidden = true;
    if (badge) {
      badge.classList.add('fui-badge--solid');
      badge.dataset.state = isMic ? 'crit' : 'ok';
      badge.textContent = t(isMic ? 'audio.capturing' : 'audio.speaking');
    }
    if (rowEl) rowEl.dataset.channelState = isMic ? 'capturing' : 'speaking';
    if (frame.envelopeHistory) {
      hist.set(frame.envelopeHistory.subarray(0, hist.length));
      renderChannelCanvas(canvas, isMic, hist, frame);
      if (dbEl) dbEl.textContent = `${fixed(frame.db ?? -14.2, 1)} dB`;
      return;
    }
    const lv = computeTimeLevel(frame.time);
    pushEnvelope(hist, lv.peak);
    renderChannelCanvas(canvas, isMic, hist, frame);
    const now = performance.now();
    if (dbEl && now - lastDbWrite > 180) {
      lastDbWrite = now;
      dbEl.textContent = Number.isFinite(lv.rmsDb) ? `${fixed(lv.rmsDb, 1)} dB` : '—';
    }
  }

  for (const chCanvas of [micCanvas, mossCanvas]) {
    if (chCanvas) {
      new ResizeObserver(() => {
        if (chCanvas === micCanvas) renderChannelCanvas(micCanvas, true, micHistory, lastMicFrame);
        else renderChannelCanvas(mossCanvas, false, mossHistory, lastMossFrame);
      }).observe(chCanvas);
    }
  }

  /* ── Bottom Dock Tabs + Live Scrolling Backend Log + Reminders + Media Queue ── */
  function setDockTab(tabId) {
    for (const btn of dockTabs) btn.setAttribute('aria-selected', String(btn.dataset.dockTab === tabId));
    for (const pane of dockPanes) pane.hidden = pane.dataset.dockPane !== tabId;
    if (tabId === 'audio-log' && logFollow && logWrap) {
      logWrap.scrollTop = logWrap.scrollHeight;
    }
  }
  dockTabs.forEach((btn) => btn.addEventListener('click', () => setDockTab(btn.dataset.dockTab)));

  function syncFollowBtn() {
    if (!logFollowBtn) return;
    logFollowBtn.setAttribute('aria-pressed', String(logFollow));
    logFollowBtn.dataset.state = logFollow ? 'ok' : 'warn';
    logFollowBtn.textContent = t(logFollow ? 'log.follow_on' : 'log.follow_off');
  }

  logFollowBtn?.addEventListener('click', () => {
    logFollow = !logFollow;
    syncFollowBtn();
    if (logFollow && logWrap) logWrap.scrollTop = logWrap.scrollHeight;
  });

  logWrap?.addEventListener('scroll', () => {
    const nearBottom = logWrap.scrollHeight - logWrap.scrollTop - logWrap.clientHeight <= 24;
    if (!nearBottom && logFollow) {
      logFollow = false;
      syncFollowBtn();
    } else if (nearBottom && !logFollow) {
      logFollow = true;
      syncFollowBtn();
    }
  });

  function updateLogSourceUi(info) {
    if (!info) return;
    const activeName = info.runtime_log ? info.runtime_log.split(/[\\/]/).at(-1) : 'moss-runtime.log';
    if (logSourceBadge) {
      logSourceBadge.textContent = `LIVE · ${activeName}`;
      logSourceBadge.title = info.runtime_log || '';
    }
  }

  async function refreshLogSource() {
    if (!onGetLogSource) return;
    try { updateLogSourceUi(await onGetLogSource()); } catch { /* ignore */ }
  }

  function formatLogStamp(ts) {
    const d = new Date(ts || Date.now());
    const pad2 = (n) => String(n).padStart(2, '0');
    const pad3 = (n) => String(n).padStart(3, '0');
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}.${pad3(d.getMilliseconds())}`;
  }

  function buildLogRow(row, fresh = false) {
    const tr = document.createElement('tr');
    tr.className = fresh ? 'moss-log-row moss-log-row--fresh' : 'moss-log-row';
    tr.dataset.level = row.level;
    const solid = row.level === 'crit' ? ' fui-badge--solid' : '';
    const srcLabel = row.file ? `${row.source}:${row.file}` : row.source;
    tr.innerHTML = `<td class="fui-num moss-log-ts">${formatLogStamp(row.time)}</td>`
      + `<td class="fui-num moss-log-src">${escapeHtml(srcLabel)}</td>`
      + `<td class="fui-table__text moss-log-msg">${escapeHtml(row.text)}</td>`
      + `<td><span class="fui-badge${solid}" data-state="${row.level}">${escapeHtml(t(`level.${row.level}`))}</span></td>`;
    return tr;
  }

  function pulseOledRx() {
    oledFrameSeq += 1;
    if (logOledSeq) logOledSeq.textContent = `RX #${String(oledFrameSeq % 1000).padStart(3, '0')}`;
    if (logOledRx) {
      logOledRx.dataset.pulse = 'true';
      clearTimeout(oledPulseTimer);
      oledPulseTimer = setTimeout(() => { if (logOledRx) delete logOledRx.dataset.pulse; }, 220);
    }
  }

  function addLog(entry) {
    const row = {
      time: entry.time || Date.now(),
      source: entry.source || 'SYS',
      text: entry.text || '',
      level: ['ok', 'info', 'warn', 'crit'].includes(entry.level) ? entry.level : 'info',
      file: entry.file || null,
    };
    logRows.push(row);
    if (logRows.length > 160) logRows.shift();
    if (logBody) {
      logBody.append(buildLogRow(row, true));
      while (logBody.children.length > 160) logBody.firstElementChild.remove();
      if (logFollow && logWrap) logWrap.scrollTop = logWrap.scrollHeight;
    }
    pulseOledRx();
    const lastEl = document.getElementById('footer-last-event');
    if (lastEl) lastEl.textContent = `${formatClock(row.time)} ${row.source}`;
  }

  function renderLog() {
    if (!logBody) return;
    logBody.replaceChildren(...logRows.map((row) => buildLogRow(row, false)));
    if (logFollow && logWrap) logWrap.scrollTop = logWrap.scrollHeight;
    const lastEl = document.getElementById('footer-last-event');
    const newest = logRows.at(-1);
    if (lastEl && newest) {
      lastEl.textContent = `${formatClock(newest.time)} ${newest.source}`;
    }
  }
  syncFollowBtn();

  function renderReminders(timers = []) {
    activeReminders = timers;
    if (remCountEl) remCountEl.textContent = String(timers.length);
    if (!remListEl || !remEmptyEl) return;
    remListEl.replaceChildren();
    remEmptyEl.hidden = timers.length > 0;
    const now = Date.now();
    for (const item of timers) {
      const dueMs = (item.due_at || 0) * 1000;
      const isDue = dueMs <= now;
      const li = document.createElement('li');
      li.className = 'moss-modal-row';
      const main = document.createElement('div');
      main.className = 'moss-modal-row__main';
      main.innerHTML = `<p class="moss-modal-row__text">${escapeHtml(item.label)}</p><span class="moss-modal-row__meta fui-num">${t('rem.due')} ${formatClock(dueMs)}</span>`;
      const actions = document.createElement('div');
      actions.className = 'moss-row moss-row--tight';
      if (isDue) {
        const badge = document.createElement('span');
        badge.className = 'fui-badge';
        badge.dataset.state = 'warn';
        badge.textContent = t('level.warn');
        actions.append(badge);
      }
      if (item.chat_id) {
        const openBtn = document.createElement('button');
        openBtn.type = 'button';
        openBtn.className = 'fui-btn';
        openBtn.textContent = t('rem.open');
        openBtn.addEventListener('click', () => onOpenTimerSource?.(item));
        actions.append(openBtn);
      }
      const dismissBtn = document.createElement('button');
      dismissBtn.type = 'button';
      dismissBtn.className = 'fui-btn';
      dismissBtn.textContent = t('rem.dismiss');
      dismissBtn.addEventListener('click', () => onDismissTimer?.(item.id));
      actions.append(dismissBtn);
      li.append(main, actions);
      remListEl.append(li);
    }
  }

  function renderMediaQueue({ turnId, chatId, items = [], selectedId = null } = {}) {
    activeMediaQueue = { turnId, chatId, items, selectedId };
    if (mediaCountEl) mediaCountEl.textContent = String(items.length);
    if (!mediaListEl || !mediaEmptyEl) return;
    mediaListEl.replaceChildren();
    mediaEmptyEl.hidden = items.length > 0;
    items.forEach((item, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-hud-track';
      btn.setAttribute('aria-current', String(item.media_id === selectedId));
      const artist = item.artist ? ` · ${item.artist}` : '';
      btn.innerHTML = `<span class="fui-num">${String(idx + 1).padStart(2, '0')}</span><span class="moss-hud-track__name">${escapeHtml(item.title || item.filename)}${escapeHtml(artist)}</span><span class="fui-badge" data-state="idle">${escapeHtml((item.kind || 'AUDIO').toUpperCase())}</span>`;
      btn.addEventListener('click', () => onSelectMedia?.({ chatId, turnId, item, index: idx + 1 }));
      mediaListEl.append(btn);
    });
  }

  hwInspectBtn?.addEventListener('click', () => onOpenOverview?.());
  hwPopoutBtn?.addEventListener('click', () => openStatusPopout());

  function openStatusPopout() {
    window.open('/status', 'moss-status-window', 'width=440,height=660,noopener');
  }

  updateAudioChannel('mic', null);
  updateAudioChannel('moss', null);

  return {
    renderDevice,
    getDevice: () => lastDevice,
    isDeviceStale: () => lastDeviceStale,
    getHardwareSeverity: () => lastHwSeverity,
    focusSite,
    setManualCity,
    clearManualCity,
    locateBrowser,
    getBrowserLocation: () => Date.now() - browserLocationAt < 300000 ? browserLocation : null,
    isManualCityLocked: () => Boolean(manualCity),
    loadSiteWeather,
    getSiteWeather: () => siteWeather,
    updateAudioChannel,
    setChannelAudioStyle,
    getChannelAudioStyle: () => channelAudioStyle,
    setOledPreset,
    getOledPreset: () => oledPreset,
    setMossMuted,
    addLog,
    refreshLogSource,
    renderReminders,
    renderMediaQueue,
    setDockTab,
    openStatusPopout,
    setFrozen(next) {
      globeFrozen = Boolean(next);
      syncGlobeLoop();
    },
    refreshI18n() {
      renderDevice(lastDevice, { stale: lastDeviceStale, live: lastDeviceLive, refreshOnly: true });
      syncGlobeSpinUi();
      lastCoordKey = '';
      redrawGlobe();
      fitCityLabel(true);
      renderSiteWeather();
      syncFollowBtn();
      renderLog();
      renderReminders(activeReminders);
      renderMediaQueue(activeMediaQueue);
    },
  };
}

export function computeDebouncedCpuState(cpuPercent, streak = {}, { live = false, thresholdSamples = 3, advance = true } = {}) {
  const rawState = !Number.isFinite(cpuPercent) ? 'idle' : cpuPercent >= 90 ? 'crit' : cpuPercent >= 80 ? 'warn' : 'idle';
  if (!live) {
    return {
      state: rawState,
      rawState,
      warnStreak: rawState === 'idle' ? 0 : thresholdSamples,
      critStreak: rawState === 'crit' ? thresholdSamples : 0,
    };
  }
  const prevWarn = Number(streak?.warnStreak || 0);
  const prevCrit = Number(streak?.critStreak || 0);
  const warnStreak = advance ? (rawState !== 'idle' ? prevWarn + 1 : 0) : prevWarn;
  const critStreak = advance ? (rawState === 'crit' ? prevCrit + 1 : 0) : prevCrit;
  const state = critStreak >= thresholdSamples ? 'crit' : warnStreak >= thresholdSamples ? 'warn' : 'idle';
  return { state, rawState, warnStreak, critStreak };
}

function escapeHtml(str) {
  return String(str ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}
