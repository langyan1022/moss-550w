const storageKey = "moss.weather.city";
const svgNS = "http://www.w3.org/2000/svg";
const collator = new Intl.Collator("zh-CN", {numeric:true});
const worldwide = [0, 0, 360, 180];
const china = [251, 35, 72, 40];
const mapHelp = "点击地图标记，或使用下方的地点选择。";

const markup = `
  <header class="city-settings-header">
    <div><h2 id="city-settings-title">当前城市</h2><span lang="en">LOCATION</span></div>
    <form method="dialog"><button type="submit">关闭</button></form>
  </header>
  <div class="city-settings-body">
    <dl class="city-settings-source"><dt>目前使用</dt><dd id="city-current-source">自动定位</dd></dl>
    <div class="city-map-selector" role="group" aria-label="地图范围">
      <button type="button" data-city-map="world" aria-pressed="true">全球</button>
      <button type="button" data-city-map="china" aria-pressed="false">中国</button>
    </div>
    <svg id="city-world-map" viewBox="0 0 360 180" role="img" aria-label="世界点阵地图" aria-describedby="city-map-hint">
      <defs>
        <mask id="city-land-mask" maskUnits="userSpaceOnUse" x="0" y="0" width="360" height="180">
          <image id="city-map-land-image" width="360" height="180"/>
        </mask>
      </defs>
      <path class="city-map-grid" d="M30 0v180M60 0v180M90 0v180M120 0v180M150 0v180M180 0v180M210 0v180M240 0v180M270 0v180M300 0v180M330 0v180M0 30h360M0 60h360M0 90h360M0 120h360M0 150h360"/>
      <rect class="city-map-land" width="360" height="180" mask="url(#city-land-mask)"/>
      <g id="city-map-markers"></g>
    </svg>
    <div class="city-map-reading"><span id="city-map-hint">${mapHelp}</span><span id="city-map-coordinates"></span></div>
    <form id="city-location-form">
      <fieldset id="city-location-fields" disabled>
        <legend class="city-form-legend">地点选择</legend>
        <div class="city-location-columns">
          <label>范围<select id="city-location-mode">
            <option value="domestic">国内地区</option><option value="international">国际城市</option>
          </select></label>
          <label class="city-domestic-field">省级地区<select id="city-province"></select></label>
          <label class="city-domestic-field">城市／地区<select id="city-prefecture"></select></label>
          <label class="city-domestic-field">区县<select id="city-district"></select></label>
          <label class="city-international-field" hidden>国家／地区<select id="city-country"></select></label>
          <label class="city-international-field" hidden>城市<select id="city-international"></select></label>
        </div>
      </fieldset>
      <output id="city-selection" aria-live="polite"></output>
      <div class="city-settings-actions">
        <button id="city-clear" type="button" hidden>恢复自动定位</button>
        <button id="city-retry" type="button" hidden>重试</button>
        <button id="city-save" type="submit" disabled>设为当前城市</button>
      </div>
    </form>
    <p id="city-settings-feedback" role="status" hidden></p>
  </div>`;

function validId(id) {
  return typeof id === "string" && id.length > 0 && id.length <= 128 && !/\s/u.test(id);
}

export function savedCityId() {
  try {
    const id = JSON.parse(localStorage.getItem(storageKey))?.id;
    return validId(id) ? id : null;
  } catch { return null; }
}

function validEntry(entry) {
  return entry && validId(entry.id) && typeof entry.name === "string" && entry.name.trim() &&
    Number.isFinite(entry.latitude) && Math.abs(entry.latitude) <= 90 &&
    Number.isFinite(entry.longitude) && Math.abs(entry.longitude) <= 180;
}

function groupBy(entries, key) {
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry[key])) groups.set(entry[key], []);
    groups.get(entry[key]).push(entry);
  }
  return groups;
}

function setOptions(select, items, placeholder, value = "") {
  select.replaceChildren(new Option(placeholder, ""), ...items.map(([id, label]) => new Option(label, id)));
  select.value = value;
  select.disabled = !items.length;
}

function sortedKeys(groups) {
  return [...groups.keys()].sort(collator.compare).map(key => [key, key]);
}

function coords(entry) {
  const degree = (value, positive, negative) => `${Math.abs(value).toFixed(2)}°${value < 0 ? negative : positive}`;
  return `${degree(entry.latitude, "N", "S")}  ${degree(entry.longitude, "E", "W")}`;
}

function mapPosition(entry) { return {x:entry.longitude + 180, y:90 - entry.latitude}; }

function cityCatalog(data) {
  if (!Array.isArray(data?.domestic) || !Array.isArray(data?.international)) throw new Error("城市列表格式错误");
  const domestic = data.domestic.filter(entry => validEntry(entry) && typeof entry.province === "string" &&
    entry.province && typeof entry.city === "string" && entry.city);
  const international = data.international.filter(entry => validEntry(entry) && typeof entry.country === "string" && entry.country);
  if (!domestic.length && !international.length) throw new Error("城市列表为空");
  return {
    international, provinces:groupBy(domestic, "province"), countries:groupBy(international, "country"),
    ids:new Map([...domestic, ...international].map(entry => [entry.id, entry]))
  };
}

export function mountCitySettings({onChange} = {}) {
  const button = document.getElementById("city-configure");
  const dialog = document.getElementById("city-settings");
  if (!button || !dialog) return {ready:Promise.resolve(null), selected:() => null, open:() => {}};
  const byId = id => dialog.querySelector(`#${id}`);
  dialog.innerHTML = markup;
  dialog.setAttribute("aria-labelledby", "city-settings-title");
  const form = byId("city-location-form"), fields = byId("city-location-fields");
  const mode = byId("city-location-mode"), province = byId("city-province"), city = byId("city-prefecture");
  const district = byId("city-district"), country = byId("city-country"), international = byId("city-international");
  const map = byId("city-world-map"), markers = byId("city-map-markers"), hint = byId("city-map-hint");
  const mapButtons = [...dialog.querySelectorAll("[data-city-map]")];
  const source = byId("city-current-source"), preview = byId("city-selection"), feedback = byId("city-settings-feedback");
  const save = byId("city-save"), clear = byId("city-clear"), retry = byId("city-retry");
  let selection = null, draft = null;
  let catalog, pending, region = "world", shownMarkers = [];

  function showFeedback(text) {
    feedback.textContent = text;
    feedback.hidden = !text;
  }

  function publish(entry, force = false) {
    const changed = selection?.id !== entry?.id;
    selection = entry;
    source.textContent = entry ? `${entry.name} · 手动设定` : savedCityId() ? "正在核对已设城市" : "自动定位";
    clear.hidden = !entry && !savedCityId();
    if (changed || force) onChange?.(entry);
    return entry;
  }

  async function loadCatalog() {
    if (catalog) return catalog;
    if (!pending) pending = (async () => {
      const response = await fetch("/api/cities", {signal:AbortSignal.timeout(5000)});
      if (!response.ok) throw new Error("城市列表暂不可用");
      catalog = cityCatalog(await response.json());
      return catalog;
    })().finally(() => { pending = null; });
    return pending;
  }

  async function reconcileSelection() {
    const id = savedCityId();
    if (!id) return publish(null);
    let entry = null;
    clear.hidden = false;
    try {
      if (catalog) entry = catalog.ids.get(id) || null;
      else {
        const response = await fetch(`/api/cities?id=${encodeURIComponent(id)}`, {signal:AbortSignal.timeout(5000)});
        if (response.status !== 404) {
          if (!response.ok) throw new Error("已设城市暂不可用");
          const {city} = await response.json();
          if (!validEntry(city) || city.id !== id) throw new Error("地点记录格式错误");
          entry = city;
        }
      }
    }
    catch {
      if (savedCityId() === id) source.textContent = "已设城市暂不可用";
      return selection;
    }
    if (savedCityId() !== id) return selection;
    if (!entry) {
      try { localStorage.removeItem(storageKey); } catch { /* Keep the dialog usable when storage is unavailable. */ }
    }
    return publish(entry);
  }

  function domesticOptions(place) {
    setOptions(province, sortedKeys(catalog.provinces), "请选择省级地区", place?.province);
    const cities = groupBy(catalog.provinces.get(province.value) || [], "city");
    setOptions(city, sortedKeys(cities), "请选择城市／地区", place?.city);
    const entries = cities.get(city.value) || [];
    const base = entries.find(item => !item.district);
    const countyOptions = entries.filter(item => item.district).sort((a, b) => collator.compare(a.district, b.district));
    setOptions(district, countyOptions.map(item => [item.id, item.district]), base ? "全市／地区" : "请选择区县",
      place?.district ? place.id : "");
    return base;
  }

  function foreignOptions(place) {
    setOptions(country, sortedKeys(catalog.countries), "所有国家／地区", place?.country);
    const entries = catalog.countries.get(country.value) || catalog.international;
    setOptions(international, [...entries].sort((a, b) => collator.compare(a.name, b.name))
      .map(item => [item.id, country.value ? item.name : `${item.name}（${item.country}）`]), "请选择城市", place?.id);
  }

  function showMode(value) {
    mode.value = value;
    for (const label of dialog.querySelectorAll(".city-domestic-field")) label.hidden = value !== "domestic";
    for (const label of dialog.querySelectorAll(".city-international-field")) label.hidden = value !== "international";
  }

  function drawMap() {
    const isChina = region === "china";
    const [minX, minY, width, height] = isChina ? china : worldwide;
    map.setAttribute("viewBox", `${minX} ${minY} ${width} ${height}`);
    map.setAttribute("aria-label", isChina ? "中国城市点阵地图" : "世界城市点阵地图");
    for (const control of mapButtons) control.setAttribute("aria-pressed", String(control.dataset.cityMap === region));
    markers.replaceChildren();
    shownMarkers = [];
    if (!catalog) return;

    const representatives = [...catalog.provinces.values()].map(entries => entries.find(entry => !entry.district) || entries[0]);
    const currentProvince = catalog.provinces.get(province.value) || [];
    const candidates = isChina ? [...currentProvince.filter(item => !item.district), ...representatives] :
      [...catalog.international, ...representatives];
    const cells = new Set();
    const spread = isChina ? 2.6 : 15;
    const radius = isChina ? .42 : 1.9;
    for (const entry of [...(draft ? [draft] : []), ...candidates]) {
      const {x, y} = mapPosition(entry);
      if (x <= minX || x >= minX + width || y <= minY || y >= minY + height) continue;
      const cell = `${Math.floor((x - minX) / spread)}:${Math.floor((y - minY) / spread)}`;
      if (cells.has(cell)) continue;
      cells.add(cell);
      const point = document.createElementNS(svgNS, "circle");
      point.setAttribute("cx", x);
      point.setAttribute("cy", y);
      point.setAttribute("r", radius);
      point.dataset.cityId = entry.id;
      if (entry.id === draft?.id) {
        point.classList.add("city-map-selected");
        const ring = document.createElementNS(svgNS, "circle");
        ring.setAttribute("cx", x);
        ring.setAttribute("cy", y);
        ring.setAttribute("r", radius * 2.1);
        ring.classList.add("city-map-ring");
        markers.append(ring);
      }
      markers.append(point);
      shownMarkers.push({entry, x, y});
    }
  }

  function setDraft(entry) {
    draft = entry;
    hint.textContent = mapHelp;
    const current = entry?.id === selection?.id;
    preview.textContent = entry ? `${current ? "已应用" : "待设定"} ${entry.name}` : "";
    byId("city-map-coordinates").textContent = entry ? coords(entry) : "";
    save.disabled = !entry || current;
    drawMap();
  }

  function choose(entry) {
    if (entry?.country) {
      showMode("international");
      foreignOptions(entry);
    } else {
      showMode("domestic");
      domesticOptions(entry);
    }
    setDraft(entry);
  }

  function closest(event) {
    const matrix = map.getScreenCTM();
    if (!matrix) return null;
    let result = null, limit = event.pointerType === "touch" ? 24 : 15;
    for (const mark of shownMarkers) {
      const pixel = new DOMPoint(mark.x, mark.y).matrixTransform(matrix);
      const distance = Math.hypot(event.clientX - pixel.x, event.clientY - pixel.y);
      if (distance < limit) { limit = distance; result = mark.entry; }
    }
    return result;
  }

  async function populate() {
    fields.disabled = save.disabled = true;
    retry.hidden = true;
    dialog.setAttribute("aria-busy", "true");
    showFeedback(catalog ? "" : "正在读取城市列表…");
    try {
      await loadCatalog();
      await reconcileSelection();
      if (!dialog.open) return;
      region = selection?.country || !selection ? "world" : "china";
      choose(selection);
      fields.disabled = false;
      showFeedback("");
    } catch {
      retry.hidden = false;
      showFeedback("城市列表暂不可用，请重试。");
    } finally {
      dialog.removeAttribute("aria-busy");
    }
  }

  function open() {
    if (dialog.open) return;
    const land = byId("city-map-land-image");
    if (!land.hasAttribute("href")) land.setAttribute("href", "/conversation-assets/city-map-land.svg?v=city-settings");
    dialog.showModal();
    void populate();
  }

  function commit(entry) {
    try {
      if (entry) localStorage.setItem(storageKey, JSON.stringify({id:entry.id}));
      else localStorage.removeItem(storageKey);
    } catch {
      showFeedback("当前浏览器禁止保存城市设置。");
      return;
    }
    publish(entry, true);
    dialog.close();
  }

  button.addEventListener("click", event => {
    const menu = event.currentTarget.closest("details");
    if (menu) menu.open = false;
    open();
  });
  dialog.addEventListener("close", () => {
    const menu = button.closest("details");
    if (menu && !menu.open && (!document.activeElement || document.activeElement === document.body)) menu.querySelector("summary")?.focus();
  });
  for (const control of mapButtons) control.addEventListener("click", () => {
    region = control.dataset.cityMap;
    drawMap();
  });
  map.addEventListener("click", event => {
    const place = closest(event);
    if (place) choose(place);
  });
  map.addEventListener("pointermove", event => {
    const place = closest(event);
    map.classList.toggle("city-map-hit", Boolean(place));
    hint.textContent = place?.name || mapHelp;
  });
  map.addEventListener("pointerleave", () => { hint.textContent = mapHelp; });
  mode.addEventListener("change", () => {
    region = mode.value === "domestic" ? "china" : "world";
    showMode(mode.value);
    if (mode.value === "domestic") domesticOptions(null);
    else foreignOptions(null);
    setDraft(null);
  });
  province.addEventListener("change", () => {
    region = "china";
    domesticOptions({province:province.value});
    setDraft(null);
  });
  city.addEventListener("change", () => {
    const base = domesticOptions({province:province.value, city:city.value});
    setDraft(base || null);
  });
  district.addEventListener("change", () => {
    const base = catalog.provinces.get(province.value)?.find(item => item.city === city.value && !item.district);
    setDraft(catalog.ids.get(district.value) || base || null);
  });
  country.addEventListener("change", () => {
    foreignOptions({country:country.value});
    setDraft(null);
  });
  international.addEventListener("change", () => setDraft(catalog.ids.get(international.value) || null));
  retry.addEventListener("click", () => void populate());
  clear.addEventListener("click", () => commit(null));
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (draft) commit(draft);
  });
  window.addEventListener("storage", event => {
    if (event.key === storageKey || event.key === null) void reconcileSelection();
  });
  const ready = reconcileSelection();
  return {ready, selected:() => selection, open};
}
