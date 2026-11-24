import {createEarthRenderer} from './earth-renderer.js?v=scene-lifecycle';

const cacheKey = 'moss.globe.location.v1';
const eastEight = {latitude:24, longitude:120};
let stopView, lastView, lastGeo;
let initialLocationResolved = false;
let cached;

function valid(point) {
  return point && Number.isFinite(point.latitude) && Math.abs(point.latitude) <= 90 &&
    Number.isFinite(point.longitude) && Math.abs(point.longitude) <= 180;
}

try {
  const saved = JSON.parse(localStorage.getItem(cacheKey));
  if (valid(saved)) cached = saved;
} catch { /* 浏览器可以关闭本地存储。 */ }

function remember(point, timestamp = point?.timestamp) {
  if (!valid(point) || (Number.isFinite(cached?.timestamp) && !(timestamp > cached.timestamp))) return;
  cached = {latitude:+point.latitude.toFixed(1), longitude:+point.longitude.toFixed(1)};
  if (Number.isFinite(timestamp) && timestamp > 0) cached.timestamp = timestamp;
  try { localStorage.setItem(cacheKey, JSON.stringify(cached)); } catch { /* 可在本次页面使用定位。 */ }
}

export function mountEarthView({geo, position, city} = {}) {
  if (stopView) return;

  const view = document.getElementById('earth-view');
  const surface = view.querySelector('.earth-sphere');
  const label = view.querySelector('figcaption span');
  const coordinates = view.querySelector('small');
  const rotationButton = document.getElementById('earth-motion');
  const locateButton = document.getElementById('earth-locate');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const panels = ['dialogue', 'tools-panel', 'lore-panel'].map(id => document.getElementById(id));
  const lifetime = new AbortController();
  const {signal} = lifetime;
  const listeners = {signal};
  remember(position);

  let {latitude, longitude} = lastView || cached || eastEight;
  let pin = lastView ? lastView.pin : cached;
  let paused = reduced.matches || (lastView?.paused ?? true);
  let weatherFocus = lastView?.weatherFocus ?? false;
  let resumeAfterWeather = lastView?.resumeAfterWeather ?? false;
  let renderer, loadError, timer, lastTick = 0, holdUntil = 0, focusRevision = 0;
  let locationRequest = 0, firstVisible = true, rendererRequested = false, configuredCity;

  function visible() {
    return !document.hidden && view.checkVisibility({visibilityProperty:true});
  }

  function draw() {
    renderer?.draw(latitude, longitude, pin);
    const lon = ((longitude + 540) % 360) - 180;
    coordinates.textContent = Math.abs(latitude).toFixed(1) + '°' + (latitude < 0 ? 'S' : 'N') + '  ' +
      Math.abs(lon).toFixed(1) + '°' + (lon < 0 ? 'W' : 'E');
  }

  function tick() {
    if (signal.aborted || paused || !visible()) return;
    const now = performance.now();
    if (now >= holdUntil) {
      if (lastTick) longitude = (longitude + Math.min(125, now - lastTick) * .009) % 360;
      if (lastTick && !weatherFocus) label.textContent = '地球视图';
      draw();
    }
    lastTick = now;
    timer = setTimeout(tick, Math.max(65, holdUntil - now));
  }

  function sync() {
    if (signal.aborted) return;
    clearTimeout(timer);
    lastTick = 0;
    if (firstVisible && visible()) {
      holdUntil = performance.now() + 4500;
      firstVisible = false;
    }
    rotationButton.textContent = paused ? '▷' : 'Ⅱ';
    const description = paused ? '继续地球自转' : '暂停地球自转';
    rotationButton.setAttribute('aria-label', description);
    rotationButton.setAttribute('aria-pressed', String(paused));
    rotationButton.title = description;
    draw();
    if (!paused && visible()) timer = setTimeout(tick, 65);
  }

  function face(point, title, showPin) {
    latitude = point.latitude;
    longitude = point.longitude;
    pin = showPin ? point : null;
    label.textContent = title;
    const measured = point.timestamp;
    label.title = title + (Number.isFinite(measured) ? ' · ' + new Date(measured).toLocaleString('zh-CN') :
      showPin ? '' : ' · UTC+8 中央经线');
    holdUntil = performance.now() + 4500;
    sync();
  }

  function focusGeo(detail) {
    if (!valid(detail)) return;
    lastGeo = detail;
    if (!weatherFocus) resumeAfterWeather = !paused;
    focusRevision++;
    weatherFocus = paused = true;
    face(detail, detail.kind === 'location' ? '设备定位记录' : detail.title, true);
  }

  function configureCity(detail) {
    locationRequest++;
    locateButton.disabled = false;
    configuredCity = valid(detail) ? detail : null;
    if (configuredCity) {
      focusGeo({...configuredCity, kind:'configured', title:configuredCity.name});
      locateButton.title = '查看浏览器定位';
    } else {
      if (lastGeo?.kind === 'configured') lastGeo = null;
      focusRevision++;
      weatherFocus = false;
      face(cached || eastEight, cached ? '上次定位' : '东八区参考视角', Boolean(cached));
    }
  }

  function locate(manual) {
    if (configuredCity && !manual) return;
    const revision = focusRevision, request = ++locationRequest;
    if (manual) locateButton.disabled = true;
    function finish() {
      if (signal.aborted || request !== locationRequest) return false;
      initialLocationResolved = true;
      locateButton.disabled = false;
      return true;
    }
    function returnHome(title) {
      if (weatherFocus) paused = !resumeAfterWeather || reduced.matches;
      weatherFocus = false;
      face(cached || eastEight, title, Boolean(cached));
    }
    function unavailable() {
      if (!finish() || !manual || revision !== focusRevision) return;
      returnHome(cached ? '上次定位' : '东八区');
      locateButton.title = cached ? '定位不可用，显示上次位置' : '定位不可用，显示 UTC+8';
    }
    if (!navigator.geolocation) { unavailable(); return; }
    navigator.geolocation.getCurrentPosition(({coords, timestamp}) => {
      if (!finish() || !valid(coords)) return;
      const detail = {latitude:coords.latitude, longitude:coords.longitude, timestamp};
      window.dispatchEvent(new CustomEvent('moss-browser-position', {detail}));
      if (revision !== focusRevision || (!manual && weatherFocus)) return;
      locateButton.title = '使用浏览器定位';
      returnHome('浏览器定位');
    }, unavailable, {enableHighAccuracy:false, maximumAge:300000, timeout:8000});
  }

  function fitGlobe() {
    if (signal.aborted) return;
    const r = view.getBoundingClientRect();
    view.toggleAttribute('data-covered', panels.some(panel => {
      if (panel.hidden) return false;
      const p = panel.getBoundingClientRect();
      return p.right > r.left - 8 && p.left < r.right + 8 && p.bottom > r.top - 8 && p.top < r.bottom + 8;
    }));
    sync();
    if (rendererRequested || !visible()) return;

    rendererRequested = true;
    createEarthRenderer(surface, signal).then(result => {
      if (signal.aborted) {
        result.dispose();
        return;
      }
      renderer = result;
      firstVisible = true;
      sync();
    }).catch(() => {
      if (signal.aborted) return;
      loadError = document.createElement('span');
      loadError.setAttribute('role', 'status');
      loadError.textContent = '地球地图载入失败';
      surface.append(loadError);
    });
  }

  rotationButton.addEventListener('click', () => {
    focusRevision++;
    paused = !paused;
    if (!paused) {
      holdUntil = 0;
      weatherFocus = false;
      label.textContent = '地球视图';
      label.removeAttribute('title');
    }
    sync();
  }, listeners);
  locateButton.addEventListener('click', () => { focusRevision++; locate(true); }, listeners);
  window.addEventListener('moss-browser-position', ({detail}) => remember(detail), listeners);
  window.addEventListener('moss-geo', ({detail}) => focusGeo(detail), listeners);
  window.addEventListener('moss-city-configured', ({detail}) => configureCity(detail), listeners);
  reduced.addEventListener('change', () => { if (reduced.matches) { paused = true; sync(); } }, listeners);
  surface.addEventListener('earth-redraw', sync, listeners);
  document.addEventListener('visibilitychange', fitGlobe, listeners);
  window.addEventListener('pagehide', event => {
    if (event.persisted) clearTimeout(timer);
    else unmountEarthView();
  }, listeners);
  window.addEventListener('pageshow', fitGlobe, listeners);

  const palette = new MutationObserver(() => { renderer?.setTheme(); sync(); });
  palette.observe(document.documentElement, {attributes:true, attributeFilter:['data-palette','style']});
  const layout = new ResizeObserver(fitGlobe);
  for (const node of [document.documentElement, view, ...panels]) layout.observe(node);
  const panelsChanged = new MutationObserver(fitGlobe);
  panelsChanged.observe(view.closest('main'), {attributes:true, attributeFilter:['data-opening','data-lore-chat-open']});
  for (const panel of panels) panelsChanged.observe(panel, {attributes:true, attributeFilter:['hidden','data-open']});

  stopView = () => {
    lastView = {latitude, longitude, pin, paused, weatherFocus, resumeAfterWeather,
                title:label.textContent, titleDetail:label.title, locateTitle:locateButton.title};
    lifetime.abort();
    clearTimeout(timer);
    palette.disconnect();
    layout.disconnect();
    panelsChanged.disconnect();
    renderer?.dispose();
    loadError?.remove();
    locateButton.disabled = false;
    view.removeAttribute('data-covered');
    view.hidden = true;
  };

  view.hidden = false;
  if (lastView) {
    label.textContent = lastView.title;
    label.title = lastView.titleDetail;
    locateButton.title = lastView.locateTitle;
  } else {
    face(cached || eastEight, cached ? '上次定位' : '东八区参考视角', Boolean(cached));
  }
  if (city || lastGeo?.kind === 'configured') configureCity(city);
  else if (geo !== lastGeo) focusGeo(geo);
  fitGlobe();
  if (!configuredCity && !initialLocationResolved && navigator.permissions) {
    navigator.permissions.query({name:'geolocation'}).then(permission => {
      if (signal.aborted) return;
      if (permission.state === 'granted') locate(false);
      else initialLocationResolved = true;
    }).catch(() => {});
  }
}

export function unmountEarthView() {
  const stop = stopView;
  stopView = null;
  stop?.();
}

if (new URL(import.meta.url).searchParams.get('lifecycle') !== '1') mountEarthView();
