/**
 * Interactive Dot-Matrix World / China Map + Cascading Province/City Form Selector.
 *
 * Manual city selection persists in localStorage ('moss-fui.city.v1') and holds STRICTLY
 * HIGHER PRIORITY than automatic browser geolocation / IP coordinates until the operator
 * explicitly restores automatic location.
 */
import { LAND } from '../vendor/fui-geo.js';
import { CITIES, REGIONS, findCityById, findNearestCity, getCitiesByRegion, installCityCatalog, searchCities } from './cities.js';
import { formatCoord, getLocale, t } from './i18n.js';

const STORAGE_KEY = 'moss-fui.city.v1';

const VIEW_BOUNDS = {
  global: { minLon: -180, maxLon: 180, minLat: -62, maxLat: 82, gridStep: 30 },
  china:  { minLon: 72,   maxLon: 136, minLat: 3,   maxLat: 54, gridStep: 10 },
};

// Official Chinese South Sea Islands and maritime features (dot matrix representation)
const SOUTH_SEA_ISLANDS = [
  // 西沙群岛 Xisha Islands (三沙市政府驻地永兴岛及主要岛礁)
  { lon: 112.3338, lat: 16.8325, name: '三沙·永兴岛', en: 'Sansha / Yongxing' },
  { lon: 111.70, lat: 16.52, name: '西沙·永乐群岛', en: 'Xisha / Yongle' },
  { lon: 112.74, lat: 16.06, name: '西沙·浪花礁', en: 'Xisha / Langhua' },
  { lon: 111.20, lat: 16.03, name: '西沙·中建岛', en: 'Xisha / Zhongjian' },
  // 中沙群岛与黄岩岛 Zhongsha Islands & Huangyan Island
  { lon: 117.85, lat: 15.12, name: '中沙·黄岩岛', en: 'Huangyan Island' },
  { lon: 114.40, lat: 15.80, name: '中沙·大环礁', en: 'Zhongsha Atoll' },
  // 东沙群岛 Dongsha Islands
  { lon: 116.72, lat: 20.70, name: '东沙群岛', en: 'Dongsha Islands' },
  // 南沙群岛主要岛礁 Nansha Islands
  { lon: 114.36, lat: 10.38, name: '南沙·太平岛', en: 'Taiping Island' },
  { lon: 112.98, lat: 9.55, name: '南沙·永暑礁', en: 'Yongshu Reef' },
  { lon: 115.53, lat: 9.92, name: '南沙·美济礁', en: 'Meiji Reef' },
  { lon: 114.07, lat: 10.92, name: '南沙·渚碧礁', en: 'Zhubi Reef' },
  { lon: 114.70, lat: 8.85, name: '南沙·华阳礁', en: 'Huayang Reef' },
  { lon: 112.89, lat: 8.86, name: '南沙·南薰礁', en: 'Nanxun Reef' },
  { lon: 114.28, lat: 9.90, name: '南沙·赤瓜礁', en: 'Chigua Reef' },
  // 曾母暗沙 (中国最南领土基点) James Shoal (Southernmost point of China)
  { lon: 112.28, lat: 3.97, name: '曾母暗沙', en: 'Zengmu Ansha' },
  // 钓鱼岛及其附属岛屿 Diaoyu Islands
  { lon: 123.47, lat: 25.75, name: '钓鱼岛', en: 'Diaoyu Island' },
  { lon: 124.57, lat: 25.92, name: '赤尾屿', en: 'Chiwei Yu' },
  { lon: 123.68, lat: 25.87, name: '黄尾屿', en: 'Huangwei Yu' },
];

// Standard South China Sea Nine-Dash Line (中国南海断续线 / 九段线点阵)
const NINE_DASH_LINE = [
  // 1. 北部湾口/莺歌海西南段
  [{ lon: 108.35, lat: 19.60 }, { lon: 108.60, lat: 18.90 }, { lon: 108.85, lat: 18.20 }],
  // 2. 西沙群岛以西/越南中岸外海段
  [{ lon: 109.55, lat: 16.30 }, { lon: 109.90, lat: 15.40 }, { lon: 110.20, lat: 14.50 }],
  // 3. 万安滩西南段
  [{ lon: 110.35, lat: 12.00 }, { lon: 110.25, lat: 11.00 }, { lon: 110.05, lat: 10.00 }],
  // 4. 南沙西南/纳土纳群岛东北段
  [{ lon: 109.70, lat: 7.20 }, { lon: 110.10, lat: 6.20 }, { lon: 110.60, lat: 5.20 }],
  // 5. 曾母暗沙以南 (最南段，折向东)
  [{ lon: 111.60, lat: 3.75 }, { lon: 112.50, lat: 3.70 }, { lon: 113.60, lat: 3.85 }, { lon: 114.70, lat: 4.30 }],
  // 6. 南沙东南/加里曼丹东北外海段
  [{ lon: 116.10, lat: 6.20 }, { lon: 116.70, lat: 7.10 }, { lon: 117.20, lat: 8.00 }],
  // 7. 巴拉望岛以西海域段
  [{ lon: 117.90, lat: 9.80 }, { lon: 118.30, lat: 10.70 }, { lon: 118.65, lat: 11.60 }],
  // 8. 吕宋岛以西/黄岩岛东海域段
  [{ lon: 118.95, lat: 14.10 }, { lon: 119.10, lat: 15.10 }, { lon: 119.15, lat: 16.10 }],
  // 9. 巴士海峡以南/台湾以南海域段
  [{ lon: 120.30, lat: 19.50 }, { lon: 120.80, lat: 20.40 }, { lon: 121.40, lat: 21.30 }],
  // 10. 台湾以东海域段 (国家标准十段线)
  [{ lon: 122.90, lat: 24.60 }, { lon: 123.35, lat: 25.40 }, { lon: 123.80, lat: 26.20 }],
];

function loadSavedCity() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && parsed.mode === 'manual' && Number.isFinite(parsed.lat) && Number.isFinite(parsed.lon)) {
      return parsed;
    }
  } catch { /* ignore */ }
  return null;
}

function saveCity(city) {
  try {
    if (!city) {
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem('moss.weather.city');
    } else {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...city, mode: 'manual' }));
    }
  } catch { /* ignore */ }
}

export function createCityPickerController({ onSelectManualCity, onRestoreAuto } = {}) {
  const canvas = document.getElementById('set-city-map');
  const viewGlobalBtn = document.getElementById('set-city-view-global');
  const viewChinaBtn = document.getElementById('set-city-view-china');
  const searchInput = document.getElementById('set-city-search');
  const resultsEl = document.getElementById('set-city-results');
  const scopeCnBtn = document.getElementById('set-city-scope-cn');
  const scopeIntlBtn = document.getElementById('set-city-scope-intl');
  const regionGrid = document.getElementById('set-city-region-grid');
  const cityGrid = document.getElementById('set-city-grid');
  const cityHeading = document.getElementById('set-city-list-heading');
  const statusBadge = document.getElementById('set-city-priority-badge');
  const currentNameEl = document.getElementById('set-city-current-name');
  const currentMetaEl = document.getElementById('set-city-current-meta');
  const hoverReadoutEl = document.getElementById('set-city-hover');
  const restoreAutoBtn = document.getElementById('set-city-auto-btn');

  let manualCity = loadSavedCity();
  let viewMode = manualCity?.group === 'intl' ? 'global' : 'china';
  let activeScope = manualCity?.group === 'intl' ? 'intl' : 'cn';
  let activeRegion = manualCity?.region || '上海市';
  let hoverCity = null;
  let hoverCoord = null;

  function shortRegionLabel(r) {
    if (!getLocale().startsWith('zh')) return r.en;
    return r.region
      .replace(/维吾尔自治区|回族自治区|壮族自治区|特别行政区|自治区|主要城市|省|市/g, '')
      .trim() || r.region;
  }

  function displayName(city) {
    if (!city) return '';
    return getLocale().startsWith('zh') ? city.name : (city.en || city.name);
  }

  function displayRegion(r) {
    return getLocale().startsWith('zh') ? r.region : (r.en || r.region);
  }

  function project(lon, lat, width, height) {
    const b = VIEW_BOUNDS[viewMode];
    const x = ((lon - b.minLon) / (b.maxLon - b.minLon)) * width;
    const y = ((b.maxLat - lat) / (b.maxLat - b.minLat)) * height;
    return { x, y };
  }

  function unproject(x, y, width, height) {
    const b = VIEW_BOUNDS[viewMode];
    const lon = b.minLon + (x / width) * (b.maxLon - b.minLon);
    const lat = b.maxLat - (y / height) * (b.maxLat - b.minLat);
    return { lon, lat };
  }

  function getVisibleCities() {
    const b = VIEW_BOUNDS[viewMode];
    return CITIES.filter((c) =>
      c.lon >= b.minLon - 1 && c.lon <= b.maxLon + 1 && c.lat >= b.minLat - 1 && c.lat <= b.maxLat + 1,
    );
  }

  function drawMap() {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(240, Math.round(rect.width || 440));
    const height = Math.max(130, Math.round(rect.height || 220));
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const cs = getComputedStyle(canvas);
    const tok = (k, fb) => cs.getPropertyValue(k).trim() || fb;
    const cGrid = tok('--fui-line-2', '#24495b');
    const cLand = tok('--fui-line-3', '#496e80');
    const cData = tok('--fui-data', '#4fd0e6');
    const cOk = tok('--fui-ok', '#35e1a3');
    const cHot = tok('--fui-accent-hot', '#ff6b3d');
    const cHi = tok('--fui-text-hi', '#f1f6f8');

    const b = VIEW_BOUNDS[viewMode];

    // 1. Graticule
    ctx.strokeStyle = cGrid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let lo = Math.ceil(b.minLon / b.gridStep) * b.gridStep; lo <= b.maxLon; lo += b.gridStep) {
      const p = project(lo, b.maxLat, width, height);
      const x = Math.round(p.x) + 0.5;
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
    }
    for (let la = Math.ceil(b.minLat / b.gridStep) * b.gridStep; la <= b.maxLat; la += b.gridStep) {
      const p = project(b.minLon, la, width, height);
      const y = Math.round(p.y) + 0.5;
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
    }
    ctx.stroke();

    // 2. Natural Earth Dot-Matrix Landmass
    const dotSize = viewMode === 'china' ? 3.5 : 2;
    ctx.fillStyle = cLand;
    for (const cell of LAND) {
      if (cell.lon < b.minLon - 2 || cell.lon > b.maxLon + 2 || cell.lat < b.minLat - 2 || cell.lat > b.maxLat + 2) continue;
      const p = project(cell.lon, cell.lat, width, height);
      ctx.fillRect(Math.round(p.x - dotSize / 2), Math.round(p.y - dotSize / 2), dotSize, dotSize);
    }

    // 2b. South China Sea Islands & Maritime Feature Dot Matrix
    for (const isl of SOUTH_SEA_ISLANDS) {
      if (isl.lon < b.minLon - 1 || isl.lon > b.maxLon + 1 || isl.lat < b.minLat - 1 || isl.lat > b.maxLat + 1) continue;
      const p = project(isl.lon, isl.lat, width, height);
      ctx.fillStyle = cLand;
      ctx.fillRect(Math.round(p.x - dotSize / 2), Math.round(p.y - dotSize / 2), dotSize, dotSize);
      if (isl.name.includes('永兴岛') || isl.name.includes('曾母暗沙') || isl.name.includes('钓鱼岛')) {
        ctx.fillStyle = cOk;
        ctx.fillRect(Math.round(p.x - 1), Math.round(p.y - 1), 2.5, 2.5);
      }
    }

    // 2c. South China Sea Nine-Dash Line (中国南海诸岛断续线 / 九段线点阵)
    if (viewMode === 'china' || b.minLat <= 15) {
      ctx.fillStyle = cData;
      for (const seg of NINE_DASH_LINE) {
        for (const pt of seg) {
          if (pt.lon < b.minLon || pt.lon > b.maxLon || pt.lat < b.minLat || pt.lat > b.maxLat) continue;
          const p = project(pt.lon, pt.lat, width, height);
          ctx.fillRect(Math.round(p.x - 1), Math.round(p.y - 1), 2.5, 2.5);
        }
      }
      if (viewMode === 'china') {
        const scPos = project(114.2, 13.5, width, height);
        ctx.fillStyle = cGrid;
        ctx.font = '700 10px "Source Han Sans SC", sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(getLocale().startsWith('zh') ? '南海诸岛' : 'SOUTH SEA ISLANDS', scPos.x, scPos.y);
        ctx.textAlign = 'start';
      }
    }

    // 3. Catalog City Beacons
    const visible = getVisibleCities();
    for (const c of visible) {
      const p = project(c.lon, c.lat, width, height);
      const isSelected = manualCity && c.id === manualCity.id;
      const isHovered = hoverCity && c.id === hoverCity.id;
      if (isSelected || isHovered) continue;
      ctx.fillStyle = c.group === 'cn' ? cData : cHi;
      ctx.globalAlpha = viewMode === 'china' ? 0.65 : 0.48;
      ctx.fillRect(Math.round(p.x - 1), Math.round(p.y - 1), 2.5, 2.5);
    }
    ctx.globalAlpha = 1;

    // 4. Hovered City Target Reticle
    if (hoverCity) {
      const p = project(hoverCity.lon, hoverCity.lat, width, height);
      ctx.strokeStyle = cData;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(p.x - 10, p.y); ctx.lineTo(p.x - 4, p.y);
      ctx.moveTo(p.x + 4, p.y);  ctx.lineTo(p.x + 10, p.y);
      ctx.moveTo(p.x, p.y - 10); ctx.lineTo(p.x, p.y - 4);
      ctx.moveTo(p.x, p.y + 4);  ctx.lineTo(p.x, p.y + 10);
      ctx.stroke();
      ctx.fillStyle = cData;
      ctx.fillRect(p.x - 2, p.y - 2, 4, 4);
    }

    // 5. Locked Manual City Target Marker (Crisp Aerospace Lock Box + Label)
    if (manualCity && manualCity.lon >= b.minLon && manualCity.lon <= b.maxLon && manualCity.lat >= b.minLat && manualCity.lat <= b.maxLat) {
      const p = project(manualCity.lon, manualCity.lat, width, height);
      ctx.strokeStyle = cHot;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(Math.round(p.x - 7) + 0.5, Math.round(p.y - 7) + 0.5, 14, 14);
      ctx.fillStyle = cOk;
      ctx.fillRect(Math.round(p.x - 2.5), Math.round(p.y - 2.5), 5, 5);

      const label = displayName(manualCity);
      ctx.font = '700 12px "Source Han Sans SC", sans-serif';
      const tw = ctx.measureText(label).width;
      const lx = p.x + 12 + tw > width - 6 ? p.x - tw - 12 : p.x + 11;
      const ly = Math.max(14, Math.min(height - 6, p.y + 4));
      ctx.fillStyle = 'rgba(2, 7, 11, 0.85)';
      ctx.fillRect(lx - 4, ly - 11, tw + 8, 15);
      ctx.fillStyle = cHi;
      ctx.fillText(label, lx, ly);
    }
  }

  function populateRegionMatrix() {
    if (scopeCnBtn) scopeCnBtn.setAttribute('aria-selected', String(activeScope === 'cn'));
    if (scopeIntlBtn) scopeIntlBtn.setAttribute('aria-selected', String(activeScope === 'intl'));
    if (!regionGrid) return;
    const list = REGIONS.filter((r) => r.group === activeScope);
    if (!list.some((r) => r.region === activeRegion)) {
      activeRegion = list[0]?.region || '上海市';
    }
    regionGrid.replaceChildren();
    for (const r of list) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-choice-key';
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', String(r.region === activeRegion));
      btn.title = `${displayRegion(r)} (${r.count})`;
      btn.textContent = shortRegionLabel(r);
      btn.addEventListener('click', () => {
        activeRegion = r.region;
        viewMode = r.group === 'intl' ? 'global' : 'china';
        populateRegionMatrix();
        populateCityMatrix();
        syncUi();
      });
      regionGrid.append(btn);
    }
    populateCityMatrix();
  }

  function populateCityMatrix() {
    if (!cityGrid) return;
    const list = getCitiesByRegion(activeRegion);
    if (cityHeading) {
      const regMeta = REGIONS.find((r) => r.region === activeRegion);
      cityHeading.textContent = `${regMeta ? displayRegion(regMeta) : activeRegion} · ${list.length}`;
    }
    cityGrid.replaceChildren();
    for (const c of list) {
      const isSelected = Boolean(manualCity && manualCity.id === c.id);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-choice-key';
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', String(isSelected));
      btn.title = `${c.name} (${c.en}) · ${formatCoord(c.lat, c.lon)}`;
      btn.textContent = displayName(c);
      btn.addEventListener('click', () => selectManualCity(c, { autoSwitchView: false }));
      cityGrid.append(btn);
    }
  }

  function syncUi() {
    if (viewGlobalBtn) viewGlobalBtn.setAttribute('aria-pressed', String(viewMode === 'global'));
    if (viewChinaBtn) viewChinaBtn.setAttribute('aria-pressed', String(viewMode === 'china'));

    if (statusBadge) {
      statusBadge.dataset.state = manualCity ? 'ok' : 'info';
      statusBadge.textContent = t(manualCity ? 'city.locked_badge' : 'city.auto_badge');
    }
    if (currentNameEl) {
      currentNameEl.textContent = manualCity ? displayName(manualCity) : t('city.auto_current');
    }
    if (currentMetaEl) {
      currentMetaEl.textContent = manualCity
        ? `${manualCity.region} · ${formatCoord(manualCity.lat, manualCity.lon)} · ${t('city.priority_note')}`
        : t('city.auto_hint');
    }
    if (restoreAutoBtn) {
      restoreAutoBtn.disabled = !manualCity;
    }
    if (hoverReadoutEl) {
      if (hoverCity) {
        hoverReadoutEl.textContent = `${displayName(hoverCity)} (${hoverCity.region}) · ${formatCoord(hoverCity.lat, hoverCity.lon)}`;
      } else if (hoverCoord) {
        hoverReadoutEl.textContent = formatCoord(hoverCoord.lat, hoverCoord.lon);
      } else {
        hoverReadoutEl.textContent = t('city.map_hint');
      }
    }
    drawMap();
  }

  function selectManualCity(city, { autoSwitchView = false } = {}) {
    if (!city) return;
    manualCity = { ...city, mode: 'manual' };
    saveCity(manualCity);
    activeScope = city.group === 'intl' ? 'intl' : 'cn';
    activeRegion = city.region;
    if (autoSwitchView) {
      viewMode = city.group === 'intl' ? 'global' : 'china';
    }
    populateRegionMatrix();
    syncUi();
    onSelectManualCity?.(manualCity);
  }

  function restoreAutoLocation() {
    manualCity = null;
    saveCity(null);
    populateCityMatrix();
    syncUi();
    onRestoreAuto?.();
  }

  scopeCnBtn?.addEventListener('click', () => {
    activeScope = 'cn';
    viewMode = 'china';
    populateRegionMatrix();
    syncUi();
  });
  scopeIntlBtn?.addEventListener('click', () => {
    activeScope = 'intl';
    viewMode = 'global';
    populateRegionMatrix();
    syncUi();
  });

  // View Switchers (China / Global Dot-Matrix Map)
  viewGlobalBtn?.addEventListener('click', () => {
    viewMode = 'global';
    hoverCity = null;
    syncUi();
  });
  viewChinaBtn?.addEventListener('click', () => {
    viewMode = 'china';
    hoverCity = null;
    syncUi();
  });

  // Canvas Hover & Click-to-Select City
  if (canvas) {
    const locatePointer = (ev) => {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      const coord = unproject(x, y, rect.width, rect.height);
      const pool = getVisibleCities();
      let best = null;
      let bestPx = viewMode === 'china' ? 42 : 34;
      for (const c of pool) {
        const p = project(c.lon, c.lat, rect.width, rect.height);
        const d = Math.hypot(p.x - x, p.y - y);
        if (d < bestPx) {
          bestPx = d;
          best = c;
        }
      }
      if (!best) {
        const near = findNearestCity(coord.lat, coord.lon, pool.length ? pool : CITIES);
        if (near.city && near.distDeg <= (viewMode === 'china' ? 4.5 : 14)) best = near.city;
      }
      return { coord, city: best };
    };

    canvas.addEventListener('pointermove', (ev) => {
      const res = locatePointer(ev);
      hoverCoord = res.coord;
      hoverCity = res.city;
      canvas.style.cursor = res.city ? 'pointer' : 'crosshair';
      syncUi();
    });

    canvas.addEventListener('pointerleave', () => {
      hoverCoord = null;
      hoverCity = null;
      syncUi();
    });

    canvas.addEventListener('click', (ev) => {
      const res = locatePointer(ev);
      if (res.city) {
        selectManualCity(res.city, { autoSwitchView: false });
      }
    });
  }

  // Search Box with Instant Clickable Chips
  searchInput?.addEventListener('input', () => {
    if (!resultsEl) return;
    const q = searchInput.value.trim();
    if (!q) {
      resultsEl.innerHTML = '';
      resultsEl.hidden = true;
      return;
    }
    const hits = searchCities(q, 12);
    resultsEl.hidden = false;
    if (!hits.length) {
      resultsEl.innerHTML = `<span class="moss-card__meta">${escapeHtml(t('city.no_match'))}</span>`;
      return;
    }
    resultsEl.innerHTML = hits.map((c) => `
      <button class="fui-btn moss-city-chip" type="button" data-city-id="${escapeHtml(c.id)}">
        <span>${escapeHtml(displayName(c))}</span>
        <span class="moss-city-chip__reg">${escapeHtml(c.region)}</span>
      </button>`).join('');
  });

  resultsEl?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-city-id]');
    if (!btn) return;
    const city = findCityById(btn.dataset.cityId);
    if (city) {
      selectManualCity(city, { autoSwitchView: true });
      if (searchInput) searchInput.value = '';
      resultsEl.innerHTML = '';
      resultsEl.hidden = true;
    }
  });

  restoreAutoBtn?.addEventListener('click', restoreAutoLocation);

  // Redraw map cleanly when Settings dialog opens (canvas gets real layout dimensions)
  const settingsDlg = document.getElementById('dlg-settings');
  if (settingsDlg && typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => {
      if (settingsDlg.open) drawMap();
    });
    if (canvas) ro.observe(canvas);
  }
  window.addEventListener('moss-settings-tab', (ev) => {
    if (ev.detail === 'location' && settingsDlg?.open) {
      requestAnimationFrame(() => drawMap()); // fui-lint-ignore: R15 one-shot redraw when switching to location tab
    }
  });

  populateRegionMatrix();
  syncUi();

  return {
    getManualCity: () => (manualCity ? { ...manualCity, backendId: findCityById(manualCity.id)?.backendId || manualCity.backendId } : null),
    async loadCatalog() {
      const response = await fetch('/api/cities');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      installCityCatalog(await response.json());
      if (!manualCity) {
        const prior = JSON.parse(localStorage.getItem('moss.weather.city') || 'null')?.id;
        const city = prior && CITIES.find((item) => item.backendId === prior);
        if (city) selectManualCity(city, { autoSwitchView: true });
      }
      populateRegionMatrix();
      syncUi();
    },
    isManualLocked: () => Boolean(manualCity),
    refreshMap: () => syncUi(),
    refreshI18n: () => {
      populateRegionMatrix();
      syncUi();
    },
    scrollIntoView: () => {
      const section = document.getElementById('set-city-section');
      section?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      requestAnimationFrame(() => drawMap()); // fui-lint-ignore: R15 one-shot post-open canvas redraw
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
