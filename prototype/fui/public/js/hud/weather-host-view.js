/**
 * HUD card view renderers for Weather projection, Host Telemetry, and Due Reminders.
 */
import { fixed, formatClock, formatCoord, t, weatherCondition } from '../i18n.js';
import { escapeHtml, GIB, localizeCity, weatherGlyphSvg } from './utils.js';

export function populateWeatherCard(card, titleEl, body, { onOpenForecastModal }) {
  const w = card.turn?.weather || {};
  const city = localizeCity(w.city);
  titleEl.textContent = city ? `${city} · ${t('hud.weather_title')}` : t('hud.weather_title');

  const tempHero = Number.isFinite(w.temperature)
    ? `${fixed(w.temperature, 1)}`
    : Number.isFinite(w.temperature_min) && Number.isFinite(w.temperature_max)
      ? `${fixed(w.temperature_min, 0)}–${fixed(w.temperature_max, 0)}`
      : '—';
  const condText = weatherCondition(w.code, w.condition);
  const precip = Number.isFinite(w.precipitation_probability) ? w.precipitation_probability : 0;
  const coords = Number.isFinite(w.latitude) && Number.isFinite(w.longitude)
    ? formatCoord(w.latitude, w.longitude)
    : '—';

  const daysHtml = (w.days || []).slice(0, 3).map((d) => `
    <div class="moss-hud-day">
      <span class="fui-num">${escapeHtml((d.date || '').slice(5))}</span>
      <span class="moss-hud-day__cond">${escapeHtml(weatherCondition(d.code, d.condition))}</span>
      <span class="fui-num">${fixed(d.temperature_min, 0)}–${fixed(d.temperature_max, 0)}°C</span>
      <span class="fui-num moss-hud-day__pop">${Number.isFinite(d.precipitation_probability) ? `${fixed(d.precipitation_probability, 0)}%` : '—'}</span>
    </div>`).join('');

  body.innerHTML = `
    <div class="moss-hud-weather-hero">
      ${weatherGlyphSvg(w.code, w.condition)}
      <div class="moss-hud-weather-main">
        <div class="moss-hud-weather-city">${escapeHtml(city)}</div>
        <div class="moss-weather-temp-row">
          <div class="moss-hud-weather-temp fui-num">${escapeHtml(tempHero)}<span class="fui-readout__unit">°C</span></div>
          <span class="moss-hud-weather-cond">${escapeHtml(condText)}</span>
        </div>
        <span class="fui-coord fui-num">${escapeHtml(coords)}</span>
      </div>
    </div>
    <div class="moss-hud-weather-metrics">
      <div class="moss-hud-metric">
        <div class="fui-gauge-head">
          <span class="fui-label">${escapeHtml(t('modal.weather_precip'))}</span>
          <span class="fui-num">${fixed(precip, 0)} %</span>
        </div>
        <div class="fui-gauge" data-state="${precip >= 60 ? 'warn' : 'data'}" style="--segments: 16; --fui-value: ${(precip / 100).toFixed(3)}" role="meter" aria-label="Precipitation" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(precip)}"></div>
      </div>
      ${Number.isFinite(w.wind_speed) ? `<div class="moss-hud-kv"><span class="fui-label">${escapeHtml(t('geo.wind'))}</span><span class="fui-num">${fixed(w.wind_speed, 1)} km/h</span></div>` : ''}
    </div>
    ${daysHtml ? `<div class="moss-hud-days">${daysHtml}</div>` : ''}
    <div class="moss-hud-foot">
      <span class="fui-num moss-card__meta">${escapeHtml(w.source || 'Open-Meteo')}</span>
      <button class="fui-btn" type="button" data-open-forecast>${escapeHtml(t('hud.full_forecast'))}</button>
    </div>`;

  body.querySelector('[data-open-forecast]')?.addEventListener('click', () => onOpenForecastModal?.(w, city));
}

export function updateHostHardware(el, device, deviceStale) {
  const readings = {
    'host-cpu-load': [device?.cpu_percent, '%'],
    'host-cpu-temperature': [device?.cpu?.temperature_c, ' °C', device?.cpu?.source],
    'host-gpu-load': [device?.gpu?.percent, '%', device?.gpu?.name],
    'host-gpu-temperature': [device?.gpu?.temperature_c, ' °C', device?.gpu?.name],
  };
  for (const [name, [value, unit, source]] of Object.entries(readings)) {
    const node = el.querySelector(`[data-${name}]`);
    if (!node) continue;
    node.textContent = !deviceStale && Number.isFinite(value) ? fixed(value, 0) + unit : '—';
    node.title = source || '';
  }
  const signal = el.querySelector('[data-host-signal]');
  if (signal) signal.hidden = Boolean(device && !deviceStale);
}

export function populateHostCard(card, titleEl, body, { telemetry, isDeviceStale, onOpenLocalStatusModal, onOpenPopout }) {
  const turn = card.turn || {};
  const items = turn.local_overview?.length ? turn.local_overview : (turn.local_overview_partial || []);
  const isReading = !turn.local_overview?.length && Boolean(turn.local_overview_partial?.length);
  titleEl.textContent = t(isReading ? 'hud.host_reading' : 'hud.host_title');

  const now = new Date();
  const pad2 = (n) => String(n).padStart(2, '0');
  const dev = telemetry?.getDevice?.();
  const ramFree = dev?.memory ? dev.memory.available / GIB : null;
  const ramTot = dev?.memory ? dev.memory.total / GIB : null;
  const bat = dev?.battery?.present !== false ? dev?.battery?.percent : null;
  const diskFree = dev?.disk ? dev.disk.free / GIB : null;

  const chronoHtml = `
    <div class="moss-hud-chrono">
      <span class="fui-label" lang="en">CURRENT HOST TIME</span>
      <div class="moss-chrono moss-chrono--compact fui-num">
        <span class="moss-chrono__major">${now.getFullYear()}</span><span class="moss-chrono__unit">Y</span>
        <span class="moss-chrono__major">${pad2(now.getMonth() + 1)}</span><span class="moss-chrono__unit">M</span>
        <span class="moss-chrono__major">${pad2(now.getDate())}</span><span class="moss-chrono__unit">D</span>
        <span class="moss-chrono__gap" aria-hidden="true"></span>
        <span class="moss-chrono__minor">${pad2(now.getHours())}</span><span class="moss-chrono__unit">H</span>
        <span class="moss-chrono__minor">${pad2(now.getMinutes())}</span><span class="moss-chrono__unit">MIN</span>
        <span class="moss-chrono__minor moss-chrono__sec">${pad2(now.getSeconds())}</span><span class="moss-chrono__unit">S</span>
      </div>
    </div>`;

  const kpiHtml = `
    <div class="moss-kpi-grid">
      <div class="moss-kpi" data-accent="data">
        <span class="moss-kpi__en" lang="en">CPU LOAD</span>
        <strong class="moss-kpi__val fui-num" data-host-cpu-load>—</strong>
        <span class="moss-kpi__zh">${escapeHtml(t('hw.cpu'))} · <span class="fui-num" data-host-cpu-temperature>—</span></span>
      </div>
      <div class="moss-kpi" data-accent="data">
        <span class="moss-kpi__en" lang="en">GPU LOAD</span>
        <strong class="moss-kpi__val fui-num" data-host-gpu-load>—</strong>
        <span class="moss-kpi__zh">GPU · <span class="fui-num" data-host-gpu-temperature>—</span></span>
      </div>
      <div class="moss-kpi" data-accent="ok">
        <span class="moss-kpi__en" lang="en">RAM FREE</span>
        <strong class="moss-kpi__val fui-num">${ramFree !== null ? fixed(ramFree, 1) : '—'}<span class="moss-kpi__unit">/${ramTot !== null ? fixed(ramTot, 0) : '—'}G</span></strong>
        <span class="moss-kpi__zh">${escapeHtml(t('hw.ram'))}</span>
      </div>
      <div class="moss-kpi" data-accent="warn">
        <span class="moss-kpi__en" lang="en">BATTERY</span>
        <strong class="moss-kpi__val fui-num">${Number.isFinite(bat) ? fixed(bat, 0) : 'AC'}<span class="moss-kpi__unit">${Number.isFinite(bat) ? '%' : ''}</span></strong>
        <span class="moss-kpi__zh">${escapeHtml(t('hw.battery'))}</span>
      </div>
      <div class="moss-kpi" data-accent="intel">
        <span class="moss-kpi__en" lang="en">DISK FREE</span>
        <strong class="moss-kpi__val fui-num">${diskFree !== null ? fixed(diskFree, 1) : '—'}<span class="moss-kpi__unit">G</span></strong>
        <span class="moss-kpi__zh">${escapeHtml(t('hw.disk'))}</span>
      </div>
    </div>`;

  const listHtml = items.map((it) => `
    <div class="moss-hud-kv-row">
      <span class="fui-badge" data-state="${it.status === 'error' ? 'warn' : 'data'}">${escapeHtml(it.label || (it.kind || 'SYS').toUpperCase())}</span>
      <span class="moss-hud-kv-row__val">${escapeHtml(it.reply || '')}</span>
    </div>`).join('');

  body.innerHTML = `
    ${chronoHtml}
    ${kpiHtml}
    <span class="moss-card__meta" data-host-signal hidden>${escapeHtml(t('hw.no_signal'))}</span>
    ${listHtml ? `<div class="moss-hud-kv-list">${listHtml}</div>` : ''}
    <div class="moss-hud-foot">
      <button class="fui-btn" type="button" data-host-modal>${escapeHtml(t('hw.inspect'))}</button>
      <button class="fui-btn" type="button" data-host-popout>${escapeHtml(t('hw.popout'))}</button>
    </div>`;

  updateHostHardware(body, dev, isDeviceStale());
  body.querySelector('[data-host-modal]')?.addEventListener('click', () => onOpenLocalStatusModal?.(turn));
  body.querySelector('[data-host-popout]')?.addEventListener('click', () => onOpenPopout?.());
}

export function renderDueReminders(reminderStackEl, timers = [], { onLocateTurn, onDismissTimer }) {
  if (!reminderStackEl) return;
  const now = Date.now();
  const due = timers.filter((tm) => (tm.due_at || 0) * 1000 <= now);
  reminderStackEl.replaceChildren();
  reminderStackEl.hidden = due.length === 0;
  for (const item of due.slice(0, 2)) {
    const card = document.createElement('div');
    card.className = 'moss-hud-reminder';
    card.dataset.state = 'warn';
    card.setAttribute('role', 'alert');
    card.innerHTML = `
      <div class="moss-hud-reminder__head">
        <span class="fui-badge fui-badge--solid" data-state="warn">${escapeHtml(t('hud.rem_badge'))}</span>
        <time class="fui-num">${formatClock((item.due_at || 0) * 1000)}</time>
      </div>
      <p class="moss-hud-reminder__text">${escapeHtml(item.label)}</p>
      <div class="moss-hud-foot">
        ${item.turn_id ? `<button class="fui-btn" type="button" data-rem-locate>${escapeHtml(t('rem.open'))}</button>` : ''}
        ${document.body?.dataset?.authRole === 'C' ? '' : `<button class="fui-btn fui-btn--primary" type="button" data-rem-dismiss>${escapeHtml(t('rem.dismiss'))}</button>`}
      </div>`;
    card.querySelector('[data-rem-locate]')?.addEventListener('click', () => onLocateTurn?.(item.turn_id));
    card.querySelector('[data-rem-dismiss]')?.addEventListener('click', () => onDismissTimer?.(item.id));
    reminderStackEl.append(card);
  }
}
