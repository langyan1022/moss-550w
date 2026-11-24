const UPDATE_MS = 10 * 60 * 1000;
const RETRY_MS = 60 * 1000;

function weatherTime(sampledAt, timeZone, full = false) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone, hour:"2-digit", minute:"2-digit", hourCycle:"h23",
      ...(full && {month:"2-digit", day:"2-digit"})
    }).format(new Date(sampledAt));
  } catch {
    return full ? sampledAt.replace("T", " ") : sampledAt.match(/T(\d{2}:\d{2})/)?.[1] || "";
  }
}

function direction(value, positive, negative, space = " ") {
  return Number.isFinite(value) ? `${Math.abs(value).toFixed(2)}°${space}${value < 0 ? negative : positive}` : "";
}

export function mountLocationWeather(tile, {locate, icon, initialCity}) {
  const control = tile.querySelector("#location-weather-control");
  const place = tile.querySelector("#location-weather-place");
  const coordinate = tile.querySelector("#location-weather-coordinate");
  const value = tile.querySelector("#location-weather-value");
  const stamp = tile.querySelector("#location-weather-stamp");
  const detailToggle = tile.querySelector("#location-weather-details-toggle");
  const details = tile.querySelector("#location-weather-details");
  const detailsTitle = details.querySelector("#location-weather-details-title");
  const freshness = details.querySelector("#location-weather-freshness");
  const sampled = details.querySelector("#location-weather-sampled");
  const facts = Object.fromEntries(["point", "feels-like", "humidity", "wind"].map(key =>
    [key, details.querySelector(`#location-weather-${key}`)]));
  const supportsPopover = typeof details.showPopover === "function";
  if (!supportsPopover) details.hidden = true;
  let permission, job, timer, report, position, city;
  let open = true, nextReading = 0, positionStale = false;

  function showPosition() {
    place.textContent = city ? city.district || city.city : positionStale ? "上次定位" : "当前位置";
    place.title = city ? `${city.name} · 手动设定` : "";
    detailsTitle.textContent = city ? `${city.name}天气` : "当前位置天气";
    coordinate.hidden = !position;
    coordinate.textContent = position
      ? `${direction(position.latitude, "N", "S", "")} ${direction(position.longitude, "E", "W", "")}` : "";
  }

  function clearPosition() {
    position = report = null;
    positionStale = false;
  }

  function positionDetails() {
    const bounds = tile.getBoundingClientRect();
    const width = Math.min(320, window.innerWidth - 16);
    const left = Math.max(8, Math.min(bounds.left, window.innerWidth - width - 8));
    const top = Math.ceil(bounds.bottom) + 8;
    details.style.setProperty("--weather-details-left", `${left}px`);
    details.style.setProperty("--weather-details-top", `${top}px`);
  }

  function showDetails(ready, stale) {
    detailToggle.hidden = !ready || !supportsPopover;
    freshness.hidden = !stale;
    if (!ready) {
      if (supportsPopover && details.matches(":popover-open")) details.hidePopover();
      for (const fact of Object.values(facts)) fact.textContent = "—";
      sampled.textContent = "—";
      sampled.removeAttribute("datetime");
      return;
    }
    const {latitude, longitude, apparent_temperature, humidity, wind_speed, sampled_at, time_zone} = report;
    const lat = direction(latitude, "N", "S"), lon = direction(longitude, "E", "W");
    facts.point.textContent = lat && lon ? `${lat} / ${lon}` : "—";
    facts["feels-like"].textContent = Number.isFinite(apparent_temperature) ? `${apparent_temperature.toFixed(1)} °C` : "—";
    facts.humidity.textContent = Number.isFinite(humidity) ? `${humidity}%` : "—";
    facts.wind.textContent = Number.isFinite(wind_speed) ? `${wind_speed} km/h` : "—";
    sampled.dateTime = sampled_at;
    sampled.textContent = weatherTime(sampled_at, time_zone, true);
  }

  details.addEventListener("beforetoggle", event => {
    if (event.newState === "open") positionDetails();
  });
  if (supportsPopover) window.addEventListener("resize", () => {
    if (details.matches(":popover-open")) positionDetails();
  });

  function show(phase) {
    tile.dataset.phase = phase;
    const ready = ["ready", "stale"].includes(phase);
    const states = {idle:city ? "天气待更新" : "天气待定位", locating:"正在定位", loading:"读取天气", denied:"定位未授权",
      error:position && !positionStale ? "天气暂不可用" : "定位暂不可用"};
    showPosition();
    value.replaceChildren();
    stamp.hidden = !ready;
    tile.title = "";
    if (ready) {
      const {temperature, condition, sampled_at:sampledAt} = report;
      if (phase === "stale") {
        const warning = document.createElement("span");
        warning.className = "location-weather-stale";
        warning.textContent = "旧读数";
        value.append(warning);
      } else {
        const glyph = condition && icon(condition);
        if (glyph) value.append(glyph);
      }
      const temperatureLabel = document.createElement("span");
      temperatureLabel.className = "location-temperature";
      if (Number.isFinite(temperature)) {
        temperatureLabel.textContent = `${temperature.toFixed(1)} °C`;
        value.append(temperatureLabel);
      }
      if (condition) {
        const summary = document.createElement("span");
        summary.className = "location-weather-condition";
        summary.textContent = condition;
        summary.title = condition;
        value.append(summary);
      }
      stamp.dateTime = sampledAt;
      stamp.textContent = weatherTime(sampledAt, report.time_zone);
      control.textContent = "刷新";
      control.setAttribute("aria-label", city ? "刷新设定城市天气" : "刷新当前位置天气");
    } else {
      stamp.removeAttribute("datetime");
      stamp.textContent = "";
      value.textContent = states[phase];
      const canRetry = city || permission?.state === "granted";
      control.textContent = phase === "denied" ? "重试" : canRetry ? "刷新" : "开启定位";
      control.setAttribute("aria-label", city ? "刷新设定城市天气" : canRetry ? "刷新当前位置天气" : "为天气开启定位");
      if (phase === "denied") tile.title = "可在浏览器的站点设置中允许定位";
    }
    showDetails(ready, phase === "stale");
    control.disabled = phase === "locating" || phase === "loading";
  }

  function stop() {
    clearTimeout(timer);
    job?.abort();
    job = null;
    tile.removeAttribute("aria-busy");
  }

  async function update(manual = false) {
    if (!open || document.hidden || job || (!city && !manual && permission?.state !== "granted")) return;
    clearTimeout(timer);
    if (!manual && nextReading > Date.now()) {
      timer = setTimeout(update, nextReading - Date.now());
      return;
    }
    const controller = job = new AbortController();
    const {signal} = controller;
    tile.setAttribute("aria-busy", "true");
    let delay = UPDATE_MS;
    if (!report) show(city ? "loading" : "locating");
    try {
      const configured = city;
      let next = position;
      if (!configured) {
        const {status, point} = await locate(manual ? undefined : permission);
        if (signal.aborted) return;
        if (status === "denied") {
          clearPosition();
          delay = 0;
          show("denied");
          return;
        }
        if (!point || status !== "granted") {
          positionStale = Boolean(position);
          throw new Error("定位不可用");
        }
        const round = number => Number(number.toFixed(2));
        next = {latitude:round(point.latitude), longitude:round(point.longitude)};
        if (next.latitude !== position?.latitude || next.longitude !== position?.longitude) report = null;
        position = next;
        positionStale = false;
        if (report) showPosition();
        else show("loading");
      }
      const deadline = setTimeout(() => controller.abort("weather-timeout"), 12000);
      try {
        const res = await fetch("/api/weather/current", {method:"POST", signal, cache:"no-store",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(configured ? {city_id:configured.id} : next)});
        if (!res.ok) throw new Error("天气接口暂时不可用");
        const data = await res.json();
        if (signal.aborted) return;
        const samePlace = configured ? data.city_id === configured.id :
          data.latitude === next.latitude && data.longitude === next.longitude;
        if (!samePlace ||
            typeof data.sampled_at !== "string" || !Number.isFinite(Date.parse(data.sampled_at)) ||
            !(Number.isFinite(data.temperature) || typeof data.condition === "string" && data.condition)) {
          throw new Error("这次缺少实时天气数据");
        }
        report = data;
        show("ready");
      } finally {
        clearTimeout(deadline);
      }
    } catch {
      if (signal.aborted && signal.reason !== "weather-timeout") return;
      delay = RETRY_MS;
      show(report ? "stale" : "error");
    } finally {
      if (job === controller) {
        job = null;
        tile.removeAttribute("aria-busy");
        nextReading = Date.now() + delay;
        if (delay && open && !document.hidden && (city || permission?.state === "granted")) timer = setTimeout(update, delay);
      }
    }
  }

  function permissionChanged() {
    if (city) return;
    if (permission.state === "granted") {
      void update();
    } else {
      stop();
      clearPosition();
      nextReading = 0;
      show(permission.state === "denied" ? "denied" : "idle");
    }
  }

  function setCity(next) {
    if (city?.id === next?.id) return;
    stop();
    clearPosition();
    nextReading = 0;
    city = next;
    tile.toggleAttribute("data-city-configured", Boolean(city));
    if (city) {
      position = {latitude:city.latitude, longitude:city.longitude};
      show("loading");
      void update();
    } else {
      show("idle");
      if (permission) permissionChanged();
    }
  }

  control.addEventListener("click", () => void update(true));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
      if (!report && ["locating", "loading"].includes(tile.dataset.phase)) show("idle");
    } else void update();
  });
  window.addEventListener("pagehide", () => { open = false; stop(); });
  window.addEventListener("pageshow", () => { open = true; void update(); });
  if (navigator.permissions?.query) {
    navigator.permissions.query({name:"geolocation"}).then(result => {
      permission = result;
      permission.addEventListener("change", permissionChanged);
      if (permission.state !== "prompt") permissionChanged();
    }).catch(() => {});
  }
  setCity(initialCity);
  return {setCity};
}
