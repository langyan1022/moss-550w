"use strict";

const composer = document.querySelector("#message");
const send = document.querySelector('[data-testid="send"]');
const conversation = document.querySelector("#conversation, #log");
const list = document.querySelector("#turns");
const welcome = document.querySelector("#welcome");
const errorBanner = document.querySelector("#error");
const hint = document.querySelector("#input-hint");
const memoryInput = document.querySelector("#memory-input");
const memoryAdd = document.querySelector('[data-testid="memory-add"]');
const newChat = document.querySelector("#new-chat");
const localStatusPanel = document.querySelector("#local-status-panel");
const localStatusTitle = document.querySelector("#local-status-title");
const localStatusReadings = document.querySelector("#local-status-readings");
const localStatusProgress = document.querySelector("#local-status-progress");
const localStatusFooter = document.querySelector("#local-status-footer");
const localStatusTime = document.querySelector("#local-status-time");
const localStatusCancel = document.querySelector("#local-status-cancel");
const quickLocalStatus = document.querySelector("#quick-local-status");
const weatherPanel = document.querySelector("#weather-panel");
const weatherTitle = document.querySelector("#weather-title");
const weatherContent = document.querySelector("#weather-content");
const weatherVoiceStatus = document.querySelector("#weather-voice-status");
const weatherLocationButton = document.querySelector("#weather-use-location");
const weatherLocationFeedback = document.querySelector("#weather-location-feedback");
const consoleStage = document.querySelector("main[data-console-link]");
const visionExample = document.querySelector("[data-vision-example]");
const localStatusLabels = {clock:"时间",battery:"电量",volume:"系统音量",cpu:"CPU",memory:"内存",
  disk:"磁盘",uptime:"运行时长",device:"设备状态",reminders:"提醒",calendar:"日程",weather:"天气",system_media:"系统播放器",
  mcp:"工具结果"};
const activeStates = new Set(["thinking", "pending", "saving"]);
const calendarApprovalWords = new Map([
  ["calendar_create", "确认创建"], ["calendar_reschedule", "确认改期"], ["calendar_cancel", "确认取消"]
]);
const calendarDeclineWords = new Map([
  ["calendar_create", ["取消日程", "不创建了", "不用安排了"]],
  ["calendar_reschedule", ["取消改期", "不改了"]],
  ["calendar_cancel", ["保留这项日程", "保留日程", "不取消了"]]
]);
const views = new Map();
const drafts = new Map();
let latest = {revision: -1, turns: []};
let connected = false;
let posting = false;
let historyKey = "";
let memoryKey = "";
let memoryExpiryTimer;
let awaitingPlaybackSnapshot = true;
const consumedPlayback = new Set();
let lastStartedAudio = null;
let lastStartedAtUs = 0;
let submittedMedia = null;
let localStatusRequest = null;
let localStatusView = null;
let weatherRequest = null;
let weatherView = null;
let informationSpeech = null;
let weatherPosition = null;
let weatherLocationRequest = null;
let weatherLocationEpoch = 0;
let composerEditRevision = 0;
let composerComposing = false;
const browserPositionMaxAge = 300000;
const weatherPositionTimeout = 8000;
const weatherPositionQuickWait = 800;

function pagePlayer() {
  const last = lastStartedAudio;
  if (!last || last.chat !== latest.chat_id || !last.node.isConnected || last.node.dataset.mediaId !== last.id)
    return {state:"idle"};
  const {node} = last;
  let state = "playing";
  if (node.error) state = "failed";
  else if (node.ended) state = "ended";
  else if (node.paused) state = node.dataset.mossStopped === "1" ? "stopped" : "paused";
  else if (node.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) state = "buffering";
  return {state, turn_id:node.dataset.audioTurn, media_id:last.id};
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

const informationSpeechLabels = {pending:"正在准备播报", start:"正在播报", error:"语音未能播放"};
const localStatusProgressLabels = {reading:"读取中", tool:"等待工具结果", complete:"读取完成", partial:"部分查询未完成"};
const hasMcpReading = items => items.some(item => item.kind === "mcp");
function informationSpeechPhase(view) {
  return view?.automatic && informationSpeech?.id === view.id ? informationSpeech.phase : null;
}

function canStopInformationSpeech(phase) {
  return phase === "pending" || phase === "start";
}

function updateInformationProgress(panel) {
  const local = panel === localStatusPanel;
  const view = informationView(panel);
  const status = local ? localStatusProgress : weatherVoiceStatus;
  if (!status) return;
  const phase = informationSpeechPhase(view);
  if (phase) status.dataset.voicePhase = phase;
  else delete status.dataset.voicePhase;
  const readings = local && view ? localStatusProgressLabels[panel.dataset.phase] : "";
  const label = informationSpeechLabels[phase] || readings;
  status.hidden = !label;
  setText(status, label);
  const stop = panel.querySelector("[data-stop-information-speech]");
  if (!stop) return;
  const canStop = canStopInformationSpeech(phase);
  if (!canStop && panel.open && document.activeElement === stop)
    panel.querySelector('form[method="dialog"] button[type="submit"]')?.focus();
  stop.hidden = !canStop;
}

for (const panel of [localStatusPanel, weatherPanel]) {
  panel?.querySelector("[data-stop-information-speech]")?.addEventListener("click", () => {
    const view = informationView(panel);
    if (panel.open && canStopInformationSpeech(informationSpeechPhase(view)))
      window.mossSpeech?.stopFor(view.id);
  });
}

for (const event of ["pending", "start", "error", "end", "stop", "skipped"]) {
  window.addEventListener("moss-speech-" + event, ({detail}) => {
    if (!detail?.id || (event !== "pending" && informationSpeech?.id !== detail.id)) return;
    informationSpeech = informationSpeechLabels[event] ? {id:detail.id, phase:event} : null;
    for (const panel of [localStatusPanel, weatherPanel]) {
      if (panel?.open) updateInformationProgress(panel);
    }
  });
}

function localReading({kind, reply}) {
  const clock = kind === "clock" && /^(.*)[，,]\s*(\d\d:\d\d:\d\d)\s*。?$/u.exec(reply);
  if (clock) return {value:clock[2],detail:clock[1],numeric:true};
  const percent = kind === "battery" ? /^电量\s*(\d+%)（([^）]+)）[。.]?$/u.exec(reply)
    : kind === "volume" ? /^系统音量为\s*(\d+%)(?:[，,]\s*(.+?))?[。.]?$/u.exec(reply) : null;
  if (percent) return {value:percent[1],detail:percent[2],numeric:true,level:parseInt(percent[1], 10)};
  return {value:reply};
}

function createLocalStatusReading(item, condition) {
  const row = element("div", "status-reading");
  row.dataset.readingKind = item.kind;
  row.toggleAttribute("data-reading-error", item.kind === "mcp" && item.status === "error");
  const {value, detail, numeric, level} = localReading(item);
  const valueElement = element("dd", "status-reading-value", value);
  valueElement.toggleAttribute("data-text-reading", !numeric);
  const toolLabel = item.kind === "mcp" && typeof item.label === "string" ? item.label.trim() : "";
  const label = element("dt", "", toolLabel || localStatusLabels[item.kind] || "读数");
  if (condition) {
    const icon = weatherIcon(condition);
    if (icon) label.prepend(icon);
  }
  row.append(label, valueElement);
  if (detail) {
    const note = element("dd", "status-reading-detail", detail);
    note.toggleAttribute("data-muted", item.kind === "volume" && detail === "当前已静音");
    row.append(note);
  }
  if (level >= 0 && level <= 100) {
    const scale = element("dd", "status-reading-scale");
    scale.setAttribute("aria-hidden", "true");
    scale.style.setProperty("--status-level", `${Math.floor(level / 5) * 5}%`);
    row.append(scale);
  }
  return {item, condition, row};
}

function updateLocalStatusView(turn, items) {
  const previous = localStatusView.readings;
  const readings = items.map(item => {
    const condition = item.kind === "weather" ? turn.weather?.condition : undefined;
    const index = previous.findIndex(entry =>
      entry.item.kind === item.kind && entry.item.reply === item.reply && entry.item.label === item.label &&
      entry.item.status === item.status && entry.condition === condition);
    return index < 0 ? createLocalStatusReading(item, condition) : previous.splice(index, 1)[0];
  });
  for (const {row} of previous) row.remove();
  for (const [index, {row}] of readings.entries()) {
    const current = localStatusReadings.children[index];
    if (current !== row) localStatusReadings.insertBefore(row, current || null);
  }
  localStatusView.readings = readings;
  if (localStatusTitle) {
    const sections = ["本机状态"];
    if (items.some(item => item.kind === "calendar")) sections.push("日程");
    if (items.some(item => item.kind === "weather")) sections.push("天气");
    if (turn.mcp_request || hasMcpReading(items)) sections.push("工具结果");
    const title = sections.length === 1 ? sections[0] : `${sections.slice(0, -1).join("、")}和${sections.at(-1)}`;
    setText(localStatusTitle, title);
  }
  const pending = turn.status === "thinking";
  const waitingForTool = pending && turn.mcp_request && !hasMcpReading(items);
  const failedTool = items.some(item => item.kind === "mcp" && item.status === "error");
  localStatusPanel.dataset.phase = waitingForTool ? "tool" : pending ? "reading" : failedTool ? "partial" : "complete";
  updateInformationProgress(localStatusPanel);
  if (localStatusCancel) {
    if (!pending && document.activeElement === localStatusCancel)
      localStatusPanel.querySelector('form[method="dialog"] button[type="submit"]')?.focus();
    localStatusCancel.hidden = !pending;
  }
  const when = !pending && turn.replied_at && new Date(turn.replied_at);
  const dated = when && !Number.isNaN(when.valueOf());
  localStatusFooter.hidden = !dated;
  if (dated) {
    localStatusTime.dateTime = turn.replied_at;
    setText(localStatusTime, when.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}));
  } else {
    localStatusTime.removeAttribute("datetime");
    setText(localStatusTime, "");
  }
}

function openLocalStatus(turn, trigger, items = turn.local_overview, automatic = false) {
  const openPanel = document.querySelector("dialog[open]");
  if (!localStatusPanel || !items?.length || (openPanel && openPanel !== localStatusPanel)) return;
  localStatusReadings.replaceChildren();
  localStatusView = {id:turn.id, trigger, readings:[], automatic};
  localStatusPanel.toggleAttribute("data-auto-result", automatic);
  updateLocalStatusView(turn, items);
  if (!localStatusPanel.open) localStatusPanel.showModal();
}

function informationView(panel) {
  if (panel === localStatusPanel) return localStatusView;
  if (panel === weatherPanel) return weatherView;
  return null;
}

function composerHasSelection() {
  return document.activeElement === composer && composer.selectionStart !== composer.selectionEnd;
}

function deferAutomaticPanel(request) {
  if (composerComposing || composerHasSelection() ||
      (composerEditRevision !== request.composerRevision && (composer.value || document.activeElement === composer)))
    request.deferPanel = true;
  return request.deferPanel;
}

function prepareAutomaticPanel(target, request) {
  const opened = document.querySelectorAll("dialog[open]");
  const origin = request.panelOrigin;
  if (!origin) return opened.length === 0 && !deferAutomaticPanel(request);
  // 只接替说话时看到的自动结果，用户中途关闭或另开详情后就留在对话里。
  if (!origin.view?.automatic || opened.length !== 1 || opened[0] !== origin.panel ||
      informationView(origin.panel) !== origin.view) return false;
  if (origin.panel !== target) origin.panel.close();
  return true;
}

function restoreDialogFocus(trigger) {
  if (document.querySelector("dialog[open]")) return;
  const target = [trigger, composer, document.querySelector("#history-toggle")]
    .find(node => node?.isConnected && !node.disabled && node.getClientRects().length);
  target?.focus();
}

localStatusPanel?.addEventListener("close", () => {
  if (localStatusPanel.open) return;
  const closed = localStatusView;
  localStatusView = null;
  localStatusPanel.removeAttribute("data-auto-result");
  delete localStatusPanel.dataset.phase;
  if (localStatusCancel) localStatusCancel.hidden = true;
  updateInformationProgress(localStatusPanel);
  if (closed && localStatusRequest?.id === closed.id) localStatusRequest = null;
  if (closed) restoreDialogFocus(closed.trigger);
});

function syncLocalStatus(state, canOpen) {
  const request = localStatusRequest;
  if (!request?.id || request.chat !== state.chat_id) return;
  const turn = state.turns.findLast(item => item.id === request.id);
  if (!turn) return;
  const items = turn.status === "thinking" ? turn.local_overview_partial : turn.local_overview;
  if (localStatusView?.id === turn.id) {
    if (!localStatusPanel.open) localStatusRequest = null;
    else if (items?.length) updateLocalStatusView(turn, items);
    else if (!activeStates.has(turn.status)) localStatusPanel.close();
  } else if (localStatusPanel && canOpen && !document.hidden && items?.length &&
             prepareAutomaticPanel(localStatusPanel, request)) {
    openLocalStatus(turn, request.trigger, items, true);
  }
  if (!activeStates.has(turn.status)) localStatusRequest = null;
}

localStatusCancel?.addEventListener("click", () => {
  const request = localStatusRequest;
  if (!request?.id || request.id !== localStatusView?.id || request.chat !== latest.chat_id || !localStatusPanel.open) return;
  const turn = latest.turns.findLast(item => item.id === request.id);
  if (turn?.status === "thinking" && turn.local_overview_partial?.length)
    void post("cancel", {turn_id:turn.id}, undefined, {turnOnly:true});
});

const temperatureText = value => Number.isFinite(value) ? `${value} °C` : "";
const precipitationText = value => Number.isFinite(value) ? `${value}%` : "";

function weatherTemperature(row) {
  if (Number.isFinite(row.temperature)) return temperatureText(row.temperature);
  const {temperature_min:min, temperature_max:max} = row;
  if (Number.isFinite(min) && Number.isFinite(max) && min !== max) return `${min} 至 ${max} °C`;
  return temperatureText(min ?? max);
}

const weatherCloud = "M6 15h12a3 3 0 0 0 0-6h-1a5 5 0 0 0-9.7-1A3.6 3.6 0 0 0 6 15Z";
const weatherIcons = {
  sun:"M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  partly:"M9 2v2M2 9h2m.2-4.8L5.6 5.6m6.8-.5A4 4 0 0 0 5.1 10 " + weatherCloud,
  cloud:weatherCloud,
  rain:weatherCloud + "M7 18l-1 3m6-3-1 3m6-3-1 3",
  snow:weatherCloud + "M7 18v3m-1.5-1.5h3m7.5-1.5v3m-1.5-1.5h3",
  thunder:weatherCloud + "m13 16-2.5 3H14l-2 3",
  fog:weatherCloud + "M4 18h16m-13 3h10",
};

function weatherIcon(condition) {
  const kind = /雷/.test(condition) ? "thunder" : /雪/.test(condition) ? "snow" :
    /雨/.test(condition) ? "rain" : /雾/.test(condition) ? "fog" :
    /多云|大致晴/.test(condition) ? "partly" : /晴/.test(condition) ? "sun" : /阴/.test(condition) ? "cloud" : "";
  if (!kind) return null;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("weather-icon");
  svg.dataset.weatherIcon = kind;
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d",weatherIcons[kind]);
  svg.append(path);
  return svg;
}

function weatherDescription(row, className) {
  const label = element("span", className);
  const icon = weatherIcon(row.condition);
  if (icon) label.append(icon);
  label.append(row.condition || "—");
  return label;
}

function hasWeatherReadings(turn) {
  const weather = turn.weather;
  return turn.status === "replied" && weather?.source === "Open-Meteo" && Boolean(
    weather.condition || weatherTemperature(weather) || Number.isFinite(weather.precipitation_probability) ||
    Number.isFinite(weather.wind_speed) || weather.days?.length || weather.hourly?.length);
}

function weatherTable(label, rows, timeLabel) {
  const section = element("section", "weather-section");
  const table = element("table", "weather-table");
  const header = table.createTHead().insertRow();
  for (const text of [timeLabel, "天气", "气温", "降水概率"]) {
    const cell = element("th", "", text);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const reading of rows) {
    const row = body.insertRow();
    const date = reading.date || reading.time?.split("T").at(-1) || "—";
    const condition = element("td");
    condition.append(weatherDescription(reading,"weather-table-condition"));
    row.append(element("td","",date), condition,
      element("td","",weatherTemperature(reading) || "—"),
      element("td","",precipitationText(reading.precipitation_probability) || "—"));
  }
  section.append(element("h3", "", label), table);
  return section;
}

function openWeather(turn, trigger, automatic = false) {
  if (!weatherPanel || !weatherTitle || !weatherContent || !hasWeatherReadings(turn)) return;
  const openPanel = document.querySelector("dialog[open]");
  if (openPanel && openPanel !== weatherPanel) return;
  const weather = turn.weather;
  const date = weather.time?.replace("T", " ") || weather.date;
  const overview = element("div", "weather-overview");
  const period = [date, weather.part_of_day].filter(Boolean).join(" · ");
  if (period) overview.append(element("p", "weather-period", period));
  if (weather.condition) overview.append(weatherDescription(weather,"weather-condition"));
  const temperature = weatherTemperature(weather);
  if (temperature) overview.append(element("p", "weather-temperature", temperature));

  const metrics = element("dl", "weather-metrics");
  const wind = Number.isFinite(weather.wind_speed) ? `${weather.wind_speed} 公里/小时` : "";
  const rainfall = Number.isFinite(weather.precipitation) ? `${weather.precipitation} 毫米` : "";
  for (const [label, value] of [["降水概率", precipitationText(weather.precipitation_probability)], ["降水量", rainfall], ["风速", wind]]) {
    if (!value) continue;
    const metric = element("div", "weather-metric");
    metric.append(element("dt", "", label), element("dd", "", value));
    if (label === "降水概率") {
      const gauge = element("dd","weather-probability-gauge");
      const probability = Math.max(0,Math.min(100,weather.precipitation_probability));
      gauge.style.setProperty("--weather-fill", Math.floor(probability/5)*5 + "%");
      gauge.setAttribute("aria-hidden","true");
      metric.append(gauge);
    }
    metrics.append(metric);
  }
  const source = element("p", "weather-source");
  const sourceLink = element("a", "", "天气数据由 Open-Meteo 提供");
  sourceLink.href = "https://open-meteo.com/";
  sourceLink.target = "_blank";
  sourceLink.rel = "noopener";
  source.append(sourceLink);
  const focusWasInReadings = weatherContent.contains(document.activeElement);
  setText(weatherTitle, weather.city ? `${weather.city}天气` : "天气预报");
  weatherContent.replaceChildren();
  if (overview.children.length) weatherContent.append(overview);
  if (metrics.children.length) weatherContent.append(metrics);
  if (weather.hourly?.length) weatherContent.append(weatherTable("逐小时", weather.hourly, "时间"));
  if (weather.days?.length) weatherContent.append(weatherTable("三天预报", weather.days, "日期"));
  weatherContent.append(source);
  weatherView = {id:turn.id, trigger:trigger || composer, automatic};
  weatherPanel.toggleAttribute("data-auto-result", automatic);
  updateInformationProgress(weatherPanel);
  if (!weatherPanel.open) weatherPanel.showModal();
  else if (focusWasInReadings) weatherPanel.querySelector("#weather-close")?.focus();
}

weatherPanel?.addEventListener("close", () => {
  if (weatherPanel.open) return;
  const closed = weatherView;
  weatherView = null;
  weatherPanel.removeAttribute("data-auto-result");
  updateInformationProgress(weatherPanel);
  if (closed) restoreDialogFocus(closed.trigger);
});

function syncWeather(state, canOpen) {
  const request = weatherRequest;
  if (!request?.id || request.chat !== state.chat_id) return;
  const turn = state.turns.findLast(item => item.id === request.id);
  if (!turn || activeStates.has(turn.status)) return;
  weatherRequest = null;
  if (!canOpen || document.hidden) return;
  if (turn.local_overview?.some(item => item.kind === "weather")) return;
  if (turn.browser_position_available && request.devicePosition) {
    window.dispatchEvent(new CustomEvent("moss-geo", {detail:{...request.devicePosition, kind:"browser", title:"浏览器当前位置"}}));
  } else if (weatherPanel && hasWeatherReadings(turn) && prepareAutomaticPanel(weatherPanel, request)) {
    openWeather(turn, request.trigger, true);
  }
}

function asksDeviceLocation(text) {
  return /^(?:(?:MOSS|莫斯)[,、\s]+)?(?:(?:请问|请|帮我|麻烦你?|给我|(?:你能)?告诉我|查一下|查查|查看|看看|你知道)\s*)*(?:(?:(?:我|本机|设备|浏览器)(?:的?(?:当前|现在|此刻))?|(?:当前|现在|此刻))的?(?:地理位置|定位点|定位|位置|所在地|经纬度|坐标)(?:是?在?(?:哪里|哪儿|哪)|是什么|是多少|什么|多少|的坐标|的经纬度)?|我(?:现在|当前|此刻)?(?:在|位于)(?:哪里|哪儿|哪))(?:吗|么|呢)?(?:,\s*(?:你(?:不)?知道(?:么|吗)?|能告诉我吗))?[。?!\s]*$/iu.test(text.normalize("NFKC").trim());
}

function pendingWeatherUsesBrowser(text) {
  const turn = latest.turns.at(-1);
  return turn?.status === "replied" && turn.weather_clarification && Date.now() <= Date.parse(turn.weather_clarification.expires_at) &&
    /^(?:我(?:的)?)?(?:当前的?(?:定位点|定位|位置)|现在的?位置|本机位置|所在地)(?:附近)?$|^(?:我这[边里儿]|这里|这儿|附近)$/u.test(
      text.normalize("NFKC").trim().replace(/[。?!\s]+$/u, ""));
}

function usesBrowserWeatherLocation(text) {
  if (!text || /不要查|不用查|别查|无需查|翻译|复述|举例|解释天气|草稿/u.test(text)) return false;
  const last = latest.turns.at(-1);
  if (last?.status === "replied" && Date.now() <= Date.parse(last.weather_retry_browser_until) &&
      /^(?:需要|要|要的|好|好的|好啊|行|可以|可以啊|(?:请|那就)?(?:再查|重查|再试|重试)(?:一次|一遍|一下)?)(?:吧|呀)?[。!?]*$/u.test(text.normalize("NFKC").trim())) return true;
  const lead = /^(?:(?:请问|请你?|帮我|麻烦你?|给我|告诉我|我想知道|查一查|查一下|查询|查看|查查|查|看一下|看看|看)[,\s]*)+/u;
  const phrase = text.normalize("NFKC").trim().replace(/^(?:MOSS|莫斯)[\s,、]+/iu, "").replace(/[。!?\s]+$/u, "");
  const query = phrase.replace(lead, "");
  const weatherTerms = /天气|气温|温度|下雨|下雪|降水|带伞|穿什么|怎么穿|冷不冷|热不热|几度|多少度/u;
  const place = /^(?:(?:今天|明天|后天|现在|当前|这会儿)[,\s]*)?(?:我(?:现在所在(?:的)?地方|的?(?:当前的?(?:定位点|定位|位置)|本机位置|所在地)|现在的位置|这边|这里|这儿)|当前的?(?:定位点|定位|位置)|本机位置|现在的位置|这边|这里|这儿|附近|本地|当地)/u;
  // Require complete read-only clauses when a reminder is mentioned.
  if (!/提醒|闹钟|倒计时|计时器|叫我|通知我/u.test(query) && place.test(query) && weatherTerms.test(query)) return true;
  if (pendingWeatherUsesBrowser(text)) return true;
  const weatherQuestion = /^(?:今天|明天|后天|现在|当前|这会儿|未来三天|这三天|今晚|明早|明晚)?(?:凌晨|早上|上午|中午|下午|傍晚|晚间|晚上|夜里)?的?(?:(?:天气(?:预报|情况)?|气温|温度)(?:怎么样|如何|怎样|多少|是多少|有多少|多少度|几度|呢|好吗)?|(?:会不会|会|要)?(?:下雨|下雪)(?:吗|么|不)?|(?:出门)?(?:(?:需不需要|要不要|需要|要|用不用)?带伞(?:吗|么)?|穿什么|怎么穿)|冷不冷|热不热|几度|多少度)$/u;
  if (query.split(/[,、]/u).every(part => weatherQuestion.test(part.trim().replace(/^我(?:们)?/u, "")))) return true;
  const localReading = /^(?:(?:我(?:的)?|这台)?(?:电脑|本机|系统)的?)?(?:现在|当前|这会儿|此刻)?(?:几点(?:钟)?(?:了|啦)?|时间|日期|今天星期几|电池(?:状态)?|(?:还(?:有|剩)|剩余)?多少电(?:量)?|(?:电量|音量|CPU(?:占用率)?|(?:可用)?内存|(?:系统盘|磁盘)(?:空间|可用)?)(?:是多少|有多少|多少|多大|怎么样)?)[啊呀呢吗么]?$/iu;
  const muteReading = /^(?:现在|当前|目前|这会儿)?(?:(?:(?:我(?:的|这台)?|这台)?电脑|本机|系统|扬声器|默认(?:音频)?输出(?:设备)?)(?:的?(?:音量|声音|音频))?|音量)(?:现在|当前|目前|这会儿)?(?:静音(?:了)?(?:吗|么|没(?:有)?)|(?:有没有|是否|是不是|有无)(?:开启|进入)?静音(?:了)?|(?:是|处于)静音(?:的|状态)?(?:吗|么)|静不静音|静音状态)$/u;
  const reminderType = "(?:提醒|闹钟|倒计时|计时器)";
  const reminderAmount = "(?:哪些|多少(?:个)?|几个|什么)";
  const reminderDay = "(?:今天|明天|后天|今早|今晚|明早|明晚)(?:凌晨|早上|上午|中午|下午|晚上)?";
  const reminderReading = new RegExp(`^(?:我?(?:现在)?还有${reminderAmount}${reminderType}|${reminderType}列表|` +
    `${reminderDay}\\s*的?\\s*(?:我的?)?(?:还?有${reminderAmount})?\\s*${reminderType}(?:列表|(?:有)?(?:哪些|什么))?)$`, "u");
  const reminderList = new RegExp(`^(?:(?:请你?|帮我|麻烦你?|给我|告诉我)\\s*)*(?:查看|查询|列出|显示|看看)(?:一下)?` +
    `(?:我的?)?(?:当前|待办|全部|所有)?${reminderType}(?:列表)?$`, "u");
  const isLocalReading = clause => {
    const part = clause.replace(lead, "");
    return localReading.test(part) || muteReading.test(part) || reminderReading.test(part) || reminderList.test(clause);
  };
  const nearbyWeather = clause => {
    const part = clause.replace(lead, "");
    return (place.test(part) || !/^(?:天气(?:预报|情况)?|气温|温度)$/u.test(part)) &&
      weatherQuestion.test(part.replace(place, "").trim());
  };
  const clauses = phrase.split(/[,、;。!?]+|\s*(?:顺便|另外|然后|以及|再查一下|再看看|和)\s*/u)
    .map(clause => clause.trim()).filter(Boolean);
  if (clauses.some(isLocalReading) && clauses.some(nearbyWeather) &&
      clauses.every(clause => isLocalReading(clause) || nearbyWeather(clause))) return true;
  if (clauses.length === 1) {
    for (const edge of query.matchAll(/几点(?:钟)?(?:了|啦)?[啊呀呢吗么]?|静(?:不静)?音(?:的|了|状态)?(?:吗|么|没有?)?|(?:多少|多大)(?:电(?:量)?|内存|空间)?[啊呀呢吗么]?/gu)) {
      const cut = edge.index + edge[0].length;
      if (isLocalReading(query.slice(0, cut).trim()) && nearbyWeather(query.slice(cut).trim())) return true;
    }
  }
  const prior = latest.turns.findLast(turn => turn.status === "replied" && (turn.weather || turn.weather_choices));
  if (prior?.weather?.location_kind !== "browser") return false;
  const followup = query.replace(/^(?:回到|说回|再看看|再看)?[,\s]*(?:那边|那里|那儿|(?:之前|上次|刚才)(?:查的)?(?:那(?:个|组))?(?:地方|位置|地点)|那么?)[,\s]*/u, "");
  return weatherQuestion.test(followup) || /^(?:今天|明天|后天|现在|当前|凌晨|早上|上午|中午|下午|傍晚|晚间|晚上|夜里)(?:的呢|的话呢|怎么样|如何|呢)?$/u.test(followup);
}

function validWeatherPosition({latitude, longitude} = {}) {
  return Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180;
}

window.addEventListener("moss-browser-position", ({detail}) => {
  if (!detail || !validWeatherPosition(detail) || !Number.isFinite(detail.timestamp) || detail.timestamp <= 0) return;
  if (!weatherPosition || detail.timestamp >= weatherPosition.timestamp) {
    const {latitude, longitude, timestamp} = detail;
    weatherPosition = {point:{latitude, longitude},timestamp};
  }
});

function readWeatherPosition(signal, permission, maximumAge) {
  if (!navigator.geolocation) return Promise.resolve({status:"unavailable"});
  return new Promise(resolve => {
    let finished = false;
    function finish(status, point) {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      signal.removeEventListener("abort", cancel);
      permission?.removeEventListener("change", permissionChanged);
      if (status === "denied") weatherPosition = null;
      resolve({status, point});
    }
    function cancel() { finish("cancelled"); }
    function permissionChanged() {
      if (permission.state !== "granted") finish("denied");
    }
    const deadline = setTimeout(() => finish("timeout"), weatherPositionTimeout);
    signal.addEventListener("abort", cancel, {once:true});
    permission?.addEventListener("change", permissionChanged);
    try {
      navigator.geolocation.getCurrentPosition(({coords, timestamp}) => {
        if (finished) return;
        if (permission && permission.state !== "granted") { finish("denied"); return; }
        if (!validWeatherPosition(coords)) { finish("unavailable"); return; }
        const {latitude, longitude} = coords;
        window.dispatchEvent(new CustomEvent("moss-browser-position", {detail:{latitude, longitude, timestamp}}));
        finish("granted", {latitude, longitude});
      }, error => finish(error.code === 1 ? "denied" : error.code === 3 ? "timeout" : "unavailable"),
      {enableHighAccuracy:false, maximumAge, timeout:weatherPositionTimeout});
    } catch (error) {
      finish(error.name === "SecurityError" ? "denied" : "unavailable");
    }
  });
}

function pendingWeatherPosition(permission, maximumAge = browserPositionMaxAge) {
  if (!weatherLocationRequest) {
    const controller = new AbortController();
    const request = {controller, result:readWeatherPosition(controller.signal, permission, maximumAge)};
    weatherLocationRequest = request;
    request.result.then(() => {
      if (weatherLocationRequest === request) weatherLocationRequest = null;
    });
  }
  return weatherLocationRequest.result;
}

let ambientWeather;
const cityReady = document.getElementById("city-settings")
  ? import("/static/city-settings.js?v=city-settings").then(async ({mountCitySettings}) => {
    const settings = mountCitySettings({onChange(city) {
      ambientWeather?.setCity(city);
      window.dispatchEvent(new CustomEvent("moss-city-configured", {detail:city}));
    }});
    await settings.ready;
    return settings;
  }).catch(() => null) : Promise.resolve(null);
window.mossCityReady = cityReady;

const weatherTile = document.getElementById("location-weather");
if (weatherTile) {
  Promise.all([import("/static/location-weather.js?v=city-settings"), cityReady]).then(([{mountLocationWeather}, settings]) => {
    ambientWeather = mountLocationWeather(weatherTile, {
      icon: weatherIcon,
      locate: permission => pendingWeatherPosition(permission, 0),
      initialCity: settings?.selected()
    });
  }).catch(() => {
    weatherTile.dataset.phase = "error";
    setText(weatherTile.querySelector("#location-weather-value"), "天气组件加载失败");
  });
}

window.addEventListener("pagehide", () => {
  weatherLocationEpoch++;
  weatherLocationRequest?.controller.abort();
  weatherLocationRequest = null;
  weatherPosition = null;
});

function showWeatherLocationStatus(status) {
  if (!weatherLocationFeedback) return;
  const messages = {locating:"正在获取当前位置…", granted:"已获取当前位置，可查询附近天气。",
    denied:"浏览器未授予定位权限，可在站点设置中开启。", timeout:"定位超时，可重试或告诉我所在的区县。",
    unavailable:"设备暂时无法定位，可重试或告诉我所在的区县。"};
  weatherLocationFeedback.dataset.state = status;
  weatherLocationFeedback.hidden = false;
  setText(weatherLocationFeedback, messages[status]);
}

weatherLocationButton?.addEventListener("click", async () => {
  showWeatherLocationStatus("locating");
  weatherLocationButton.disabled = true;
  const {status} = await pendingWeatherPosition();
  weatherLocationButton.disabled = false;
  if (status !== "cancelled") showWeatherLocationStatus(status);
  else if (weatherLocationFeedback) weatherLocationFeedback.hidden = true;
});

function browserWeatherLocation(text, locateDevice, beforeLookup) {
  if (!(locateDevice || usesBrowserWeatherLocation(text)) || !navigator.permissions?.query || !navigator.geolocation) return null;
  beforeLookup?.();
  const wait = locateDevice ? weatherPositionTimeout : weatherPositionQuickWait;
  const page = weatherLocationEpoch;
  return new Promise(resolve => {
    let finished = false;
    function complete(point) {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      resolve(point);
    }
    const deadline = setTimeout(() => complete(null), wait);
    Promise.resolve().then(() => navigator.permissions.query({name:"geolocation"})).then(async permission => {
      if (finished) return;
      if (page !== weatherLocationEpoch) { complete(null); return; }
      if (permission.state !== "granted") { weatherPosition = null; complete(null); return; }
      if (!weatherLocationRequest && weatherPosition && Date.now() - weatherPosition.timestamp <= browserPositionMaxAge) {
        complete(weatherPosition.point);
        return;
      }
      // A slow fix can serve the next request without rerunning this one.
      const {point} = await pendingWeatherPosition(permission);
      complete(page === weatherLocationEpoch && permission.state === "granted" ? point || null : null);
    }).catch(() => complete(null));
  });
}

function showError(message) {
  const panelError = document.querySelector("dialog[open] .panel-error");
  for (const node of [errorBanner, ...document.querySelectorAll(".panel-error")]) {
    const visible = Boolean(message) && node === (panelError || errorBanner);
    node.hidden = !visible;
    setText(node, visible ? message : "");
  }
}

const playbackTarget = "(?:(?:当前(?:的)?|这(?:首|段|个))?(?:音乐|歌曲|歌|音频|视频|影片|短片|电影|播放器)|这(?:一)?首)";
const playbackControl = "(?:暂停|停一下|停一停|停下|停止|关掉|关闭|继续|恢复|接着)";
const playbackVerb = "(?:播放|放|播)";
const playbackModifier = String.raw`\s*(?:一下|下)?\s*`;
const explicitPlaybackControl = new RegExp(
  String.raw`^(?:(?:MOSS|莫斯|摩斯)\s*[,，、]\s*)?(?:(?:请你?|麻烦你?|帮我|给我|先|那就)\s*)*(?:` +
  String.raw`${playbackControl}(?:${playbackVerb}(?:${playbackModifier}${playbackTarget})?|${playbackModifier}${playbackTarget})|` +
  String.raw`(?:把|将|让)?${playbackTarget}\s*(?:先)?${playbackControl}${playbackVerb}?|` +
  String.raw`(?:别|不要|不用)(?:再|继续)?${playbackVerb}\s*(?:这(?:一)?(?:首(?:歌曲|歌|曲子)?|段)了?|${playbackTarget}了|了)` +
  String.raw`)(?:一下|一会儿|一会)?(?:吧|啦)?\s*[。！!]?\s*$`, "iu");
window.mossIsPlaybackControl = text => explicitPlaybackControl.test(text.trim());

const replyCorrection = /^(?:(?:MOSS|莫斯|摩斯)[\s,，、]+)?(?:(?:请你?|麻烦你?)\s*)?(?:先)?(?:停止(?:继续)?(?:当前的?|这轮的?|本轮的?)?(?:回答|回复)|别(?:继续)?(?:回答|回复)(?:了)?|取消(?:这轮|本轮|当前)(?:回答|回复)|算了)[\s,，、。;；!！]*(?:改问|我改问|改成问|换(?:一)?个问题)[\s,，、。:：;；!！]*(\S[\s\S]*\S)$/iu;

function replacementQuestion(text) {
  const next = replyCorrection.exec(text.trim())?.[1];
  return next && next.length <= 1200 && new TextEncoder().encode(next).length <= 2700 ? next : null;
}

function replacementCommand(text, turn) {
  return turn?.status === "thinking" && turn.conversation_replaceable === true ?
    {action:"replace-reply", body:{turn_id:turn.id, chat_id:latest.chat_id, conversation_only:true}, text} : null;
}

function calendarApproval(turn) {
  return calendarApprovalWords.get(turn?.desktop_confirmation?.action);
}

function approvalAnswer(text) {
  return text.trim().replace(/[。！!]+$/u, "").trimEnd();
}

function composeAction(text, active = latest.turns.find(turn => activeStates.has(turn.status))) {
  if (!text) return null;
  const replacement = replacementQuestion(text);
  if (active && replacement) return replacementCommand(replacement, active);
  const answer = text.normalize("NFKC").replace(/^(?:MOSS|莫斯)[\s,、]+/iu, "")
    .replace(/[。!]+$/u, "").trim().replace(/^(?:请你?|麻烦你?)\s*/u, "").replace(/(?:吧|呀|啊|呗)$/u, "");
  const replyStop = /^(?:停止(?:回复|播报)|(?:先)?别(?:继续)?(?:说|播报)了|不用(?:继续)?说了|不要继续说了)$/u.test(answer);
  if (replyStop && (answer.includes("播报") ||
      (active?.status !== "thinking" && !(active?.status === "saving" && active.receipt)))) {
    return {action:"stop-speech"};
  }
  const playback = explicitPlaybackControl.test(text);
  if (!active || (playback && active.source !== "media" && !active.media_in_flight)) {
    return {action:"message", body:{text, chat_id:latest.chat_id}, playback};
  }
  const calendar = calendarApproval(active);
  const confirmations = calendar ? [calendar, "确认执行", "确认"] :
    active.desktop_confirmation ? ["确认执行", "确认"] : ["确认保存", "确认"];
  if (active.status === "pending" && confirmations.includes(approvalAnswer(text))) {
    return {action:"approve", body:{turn_id:active.id}, draftApproval:!active.desktop_confirmation};
  }
  if (active.status === "saving" && active.desktop_confirmation && active.desktop_confirmation.action !== "powershell") return null;
  const stopTurn = /^(?:取消(?:这轮|本轮|当前请求|当前任务|操作|草稿)?|停止(?:这轮|本轮|当前请求|当前任务)?)$/u.test(answer);
  const declineDraft = active.status === "pending" && ["算了", "取消保存", "不保存了", "不用保存了", "别保存了"].includes(answer);
  const wait = active.status === "thinking" && /^(?:先)?(?:停一下|等一下|等等|稍等一下)$/u.test(answer);
  const declineCalendar = active.status === "pending" &&
    calendarDeclineWords.get(active.desktop_confirmation?.action)?.includes(answer);
  if (replyStop || stopTurn || declineDraft || declineCalendar || wait) {
    return {action:"cancel", body:{turn_id:active.id}, turnOnly:replyStop || wait};
  }
  return null;
}

function controls() {
  const active = latest.turns.find(turn => activeStates.has(turn.status));
  send.disabled = !connected || posting || !composeAction(composer.value.trim());
  if (quickLocalStatus) quickLocalStatus.disabled = !connected || !latest.chat_id || posting || Boolean(active);
  if (localStatusCancel) localStatusCancel.disabled = !connected || posting;
  if (visionExample) visionExample.hidden = !latest.vision_model;
  const action = active?.desktop_confirmation?.action;
  const calendar = calendarApproval(active);
  const hints = {
    pending: calendar ? `请输入“${calendar}”或“取消”` :
      action ? "请输入“确认执行”或“取消”" : "请输入“确认保存”或“取消”，也可使用上方按钮",
    thinking: active?.conversation_replaceable ? "可以说“停止”或“算了，换个问题”" : "输入“停止”可结束本轮回复",
    saving: action === "powershell" ? "输入“停止”可结束 PowerShell 进程" :
      calendar ? "等待本机日程回执" : action ? "等待 Windows 回执" :
      active?.receipt ? "输入“停止”可结束回复，保留草稿" : "输入“取消”可停止保存"
  };
  setText(hint, posting ? "正在提交…" : (hints[active?.status] || "Enter 发送 · Shift + Enter 换行"));
  for (const button of list.querySelectorAll("button")) {
    if (button.closest(".context-widget") || button.dataset.testid === "copy-reply") continue;
    button.disabled = posting || !connected || Boolean(active && button.dataset.testid === "retry-music");
  }
  const switchingBlocked = !connected || posting || active?.status === "saving" || active?.status === "thinking";
  for (const button of document.querySelectorAll("#tool-results .media-choice button")) button.disabled = !connected || posting || Boolean(active);
  newChat.disabled = switchingBlocked;
  for (const button of document.querySelectorAll(".history-entry")) button.disabled = switchingBlocked;
  for (const button of document.querySelectorAll(".memory-remove")) button.disabled = posting || !connected;
  memoryAdd.disabled = !connected || posting || !memoryInput.value.trim();
}

function setConnection(online, text) {
  connected = online;
  if (consoleStage) {
    consoleStage.dataset.consoleLink = online ? "online" : "connecting";
    consoleStage.dispatchEvent(new Event("moss-console-link"));
  }
  document.querySelector(".connection").classList.toggle("online", online);
  setText(document.querySelector("#connection-label"), text);
  controls();
}

function makeCard(type, title) {
  const box = element("div", "message-card " + type);
  const head = element("div", "card-head");
  const body = element("p", "card-body");
  head.append(element("strong", "", title));
  box.append(head, body);
  return {box, head, body};
}

function createTurn(turn) {
  const article = element("article", "turn");
  article.dataset.testid = "turn";
  article.dataset.turnId = turn.id;
  const byline = element("div", "user-line");
  const date = element("time");
  byline.append(date, element("span", "", "你"));
  const user = element("p", "user-message message-text");
  const agent = element("div", "assistant-label", "MOSS");
  const body = element("div", "assistant-body");
  const status = element("p", "turn-status");
  status.dataset.testid = "status";
  status.setAttribute("role", "status");
  const cards = element("div");
  const actions = element("div", "actions");
  const answers = element("div");
  body.append(status, cards, actions, answers);
  article.append(byline, user, agent, body);
  return {article, date, user, status, cards, actions, answers, assistantBody: body, actionKey: null};
}

function statusText(turn) {
  switch (turn.status) {
    case "thinking": return turn.holo_tools?.includes("draft_chat_reply") ? "正在为这条消息拟回复…" : "正在处理你的消息…";
    case "pending": return turn.desktop_confirmation?.action === "calendar_create" ? "请核对日程时间。" :
      calendarApproval(turn) ? "请核对本机日程变更。" :
      turn.desktop_confirmation ? "这项操作需要你确认。" :
      turn.verify_only ? "草稿等待核验。" : "这份草稿需要你确认。";
    case "saving": return calendarApproval(turn) ? "正在写入并核实本机日程…" :
      turn.desktop_confirmation ? "正在等待 Windows 回执…" :
      turn.receipt ? "正在生成文字回复…" : turn.verify_only ? "正在核验草稿…" : "正在保存草稿…";
    case "saved": return "草稿已保存。";
    case "cancelled": return turn.verify_only ? "已结束核验，草稿的保存结果尚未确认。" :
      turn.receipt ? "文字回复已停止，草稿已保留。" : "本轮已取消。";
    case "error": return turn.receipt ? "草稿已保留，文字回复中断。" : "本轮未完成。";
    case "replied": return "";
    default: return "正在更新状态…";
  }
}

function actionButton(testId, label, primary, id) {
  const button = element("button", primary ? "action-primary" : "action-secondary", label);
  button.type = "button";
  button.dataset.testid = testId;
  button.addEventListener("click", () => post(testId, {turn_id: id}));
  return button;
}

function renderActions(view, turn) {
  const key = turn.status + (turn.receipt ? "-receipt" : "") + (turn.verify_only ? "-verify" : "");
  if (view.actionKey === key) return;
  view.actionKey = key;
  view.actions.replaceChildren();
  if (turn.status === "pending") {
    const calendar = calendarApproval(turn);
    const approved = calendar ??
      (turn.desktop_confirmation ? "确认执行" : turn.verify_only ? "重新核验" : "确认保存");
    const cancelled = turn.desktop_confirmation?.action === "calendar_cancel" ? "保留这项日程" :
      turn.desktop_confirmation ? "取消操作" : turn.verify_only ? "结束核验" : "取消草稿";
    view.actions.append(actionButton("approve", approved, true, turn.id));
    view.actions.append(actionButton("cancel", cancelled, false, turn.id));
  } else if (turn.status === "saving" || turn.status === "thinking") {
    if (turn.desktop_confirmation) {
      if (turn.desktop_confirmation.action === "powershell") {
        view.actions.append(actionButton("cancel", "停止 PowerShell", false, turn.id));
      }
      return;
    }
    const label = turn.verify_only ? "结束核验" : turn.status === "saving" && !turn.receipt ? "取消保存" : "停止回复";
    view.actions.append(actionButton("cancel", label, false, turn.id));
  }
}

function pauseMedia() {
  window.mossProjection?.close();
  for (const audio of document.querySelectorAll(".media-audio")) audio.pause();
}

function renderAudio(view, turn, mayPlay) {
  const native = (turn.native_receipts || []).findLast(record =>
    record.result && (record.tool === "search_media" || (record.tool === "play_media" && record.result.status === "played")));
  const media = turn.media;
  const projection = window.mossProjection;
  const nativeSearch = (turn.native_receipts || []).findLast(record => record.tool === "search_media");
  const items = media?.show_audio === false ? [] : (media?.items || nativeSearch?.result?.matches || []);
  let selected = media?.show_audio !== false ? media?.selected : null;
  let clip = media?.autoplay ? media.playback : null;
  let fullTrack = media?.autoplay && ["track","video"].includes(media.play_mode);
  if (native?.tool === "play_media") {
    clip = native.result;
    selected = {...clip, kind:"audio"};
  } else if (native?.tool === "search_media") {
    selected = native.result.matches?.find(match => match.media_id === native.result.selected_id) || selected;
    clip = null;
  }
  view.chooseTrack = async (item,number) => {
    if (!connected || posting || latest.turns.some(t => activeStates.has(t.status))) return;
    const text = "播放第 " + number + (item.kind === "video" ? " 个视频" : " 首");
    projection.manual();
    projection.arm(text, latest.chat_id, latest.turns);
    projection.feedback(view,"正在选择《" + item.title + "》…");
    view.audio?.pause();
    const ok = await post("media-select", {chat_id:latest.chat_id,turn_id:turn.id,media_id:item.media_id,index:number},text);
    if (!ok) projection.feedback(view,errorBanner.textContent || "该媒体暂时无法打开。");
  };
  if (view.audio && (view.audio.localName !== selected?.kind || view.audio.dataset.mediaId !== selected.media_id)) {
    view.disposePlayer?.(); view.disposePlayer = null;
    view.audio.pause(); view.audio.remove(); view.audio = null; view.playerControls = null;
  }
  if (!["audio","video"].includes(selected?.kind) || !/^[a-f0-9]{32}$/.test(selected.media_id)) {
    projection?.prepare(view,turn,items,selected,mayPlay);
    return;
  }
  if (!view.audio) {
    const node = element(selected.kind, "media-audio");
    node.controls = true;
    node.preload = "none";
    node.dataset.testid = "media-" + selected.kind;
    node.dataset.audioTurn = turn.id;
    const reported = new Set();
    let playId, recoveryPlayId;
    function report(event, eventTime) {
      const id = node.dataset.mediaId;
      if (!id || view.audio !== node || !node.isConnected || (event !== "started" && reported.has(id + event))) return;
      const payload = {chat_id:turn.chat_id,turn_id:turn.id,media_id:id,event};
      if (event === "started") {
        payload.play_id = playId = crypto.randomUUID();
        const micros = Math.round((performance.timeOrigin + eventTime) * 1000);
        lastStartedAtUs = Math.max(micros, lastStartedAtUs + 1);
        payload.started_at_us = lastStartedAtUs;
      }
      reported.add(id + event);
      fetch("/api/media-event",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify(payload)}).catch(() => {});
    }
    let playing = false, pausedBeforeError = false;
    node.addEventListener("playing", event => {
      if (node.paused || view.audio !== node || !node.isConnected) return;
      lastStartedAudio = {node, id:node.dataset.mediaId, chat:turn.chat_id};
      if (!playing) {
        report("started", event.timeStamp);
        window.dispatchEvent(new CustomEvent("moss-media-start", {detail:{turnId:turn.id,title:view.projected?.selected?.title}}));
      }
      playing = true;
    });
    node.addEventListener("moss-playback-rejected", e => report(e.detail === "NotAllowedError" ? "blocked" : "failed"));
    node.addEventListener("error", () => {
      const current = latest.turns.find(item => item.id === turn.id);
      if (latest.media_resume === true && !pausedBeforeError && node.error?.code === 2 && playId && playId !== recoveryPlayId &&
          node.played.length && lastStartedAudio?.node === node && projection?.recoverInterrupted?.(view, current, node, playId)) {
        recoveryPlayId = playId;
        return;
      }
      if (node.error?.code !== 1) report("failed");
    });
    node.addEventListener("pause", () => {
      if (view.audio !== node || !node.isConnected) return;
      playing = false;
      if (!node.error) pausedBeforeError = true;
      window.dispatchEvent(new CustomEvent("moss-media-pause", {detail:{turnId:turn.id}}));
    });
    node.addEventListener("play", () => {
      if (view.audio !== node || !node.isConnected) return;
      pausedBeforeError = false;
      delete node.dataset.mossStopped;
      for (const other of document.querySelectorAll(".media-audio")) if (other !== node && !other.paused) other.pause();
      if (view.bounds && (node.currentTime < view.bounds.start || node.currentTime >= view.bounds.end))
        node.currentTime = view.bounds.start;
    });
    node.addEventListener("timeupdate", () => {
      if (view.audio !== node || !node.isConnected) return;
      if (view.bounds && !node.paused && node.currentTime >= view.bounds.end) node.pause();
    });
    view.answers.append(node);
    view.audio = node;
  }
  const audio = view.audio;
  const start = Number(clip?.clip_start_s || 0);
  const end = start + Number(clip?.clip_s || 0);
  view.bounds = Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start ? {start, end} : null;
  const url = "/media/" + selected.media_id + (view.bounds ? "#t=" + start + "," + end : "");
  audio.dataset.mediaId = selected.media_id;
  if (audio.getAttribute("src") !== url && audio.dataset.parkedSrc !== url) {
    audio.pause();
    delete audio.dataset.parkedSrc;
    delete audio.dataset.parkedTime;
    audio.src = url;
  }
  audio.setAttribute("aria-label", (selected.kind === "video" ? "观看 " : "试听 ") + selected.title);
  projection?.prepare(view,turn,items,selected,mayPlay);
  const cancelled = turn.status === "cancelled" || turn.status === "error";
  if (cancelled && !view.audioCancelled) { audio.pause(); projection?.cancel(view); }
  view.audioCancelled = cancelled;
  if ((!clip || !view.bounds) && !fullTrack) return;
  if (media?.recovered_from) return;
  const key = JSON.stringify([turn.chat_id, turn.id, clip?.command_id, url]);
  if (consumedPlayback.has(key)) return;
  consumedPlayback.add(key);
  if (mayPlay && !cancelled) {
    const fromGesture = projection ? projection.claim(turn) :
      submittedMedia?.chat === turn.chat_id && submittedMedia?.text === turn.user && !submittedMedia.previous.has(turn.id);
    if (!fromGesture) { projection?.promptPlay(view); return; }
    submittedMedia = null;
    if (projection) projection.play(view,turn,selected,selected.kind === "audio");
    else audio.play().catch(() => {});
  }
}

window.mossRecoverTrack = async (turn, failedId, resume, signal) => {
  if (latest.chat_id !== turn.chat_id) return null;
  const original = lastStartedAudio;
  if (resume && (original?.id !== failedId || original?.chat !== turn.chat_id)) return null;
  try {
    const response = await fetch("/api/media-event", {method:"POST", headers:{"Content-Type":"application/json"}, signal,
      body:JSON.stringify({chat_id:turn.chat_id, turn_id:turn.id, media_id:failedId, event:"failed", recover:true, ...resume})});
    if (!response.ok) return null;
    const state = await response.json();
    if (!state.turns || signal?.aborted || (resume && lastStartedAudio !== original)) return null;
    accept(state);
    const revised = resume ? latest.turns.find(item => item.id === turn.id) : latest.turns.at(-1);
    return !posting && latest.chat_id === turn.chat_id && revised?.id === turn.id &&
      revised.media?.recovered_from === failedId ? revised : null;
  } catch { return null; }
};

const showCloudRequests = document.querySelector("#show-cloud-requests");
const cloudRequestsNote = document.querySelector("#cloud-requests-note");
const cloudProviders = {deepseek:"DeepSeek", aliyun:"百炼", kimi:"Kimi"};
if (showCloudRequests) {
  showCloudRequests.checked = localStorage.getItem("moss.cloud.requests") === "on";
  cloudRequestsNote.hidden = !showCloudRequests.checked;
  showCloudRequests.addEventListener("change", () => {
    localStorage.setItem("moss.cloud.requests", showCloudRequests.checked ? "on" : "off");
    cloudRequestsNote.hidden = !showCloudRequests.checked;
    for (const turn of latest.turns) {
      const view = views.get(turn.id);
      if (view) renderCloudRequests(view, turn.cloud_requests);
    }
  });
}

function renderCloudRequests(view, usage) {
  if (!showCloudRequests?.checked) {
    if (view.cloudRequests) view.cloudRequests.hidden = true;
    return;
  }
  if (!view.cloudRequests) {
    view.cloudRequests = element("small", "turn-timing cloud-requests");
    view.cloudRequests.dataset.testid = "cloud-requests";
    view.assistantBody.append(view.cloudRequests);
  }
  view.cloudRequests.hidden = false;
  if (!usage) {
    setText(view.cloudRequests, "云模型请求未记录");
    return;
  }
  const providers = Object.entries(usage.by_provider || {});
  const breakdown = providers.map(([provider, count]) =>
    (cloudProviders[provider] || provider) + (providers.length > 1 ? " " + count + " 次" : ""));
  const label = (usage.complete === false ? "启用计数后 · " : "") + "云模型请求 " + usage.total + " 次";
  setText(view.cloudRequests, [label, ...breakdown].join(" · "));
}

function renderTurn(view, turn, mayPlay) {
  view.article.dataset.status = turn.status;
  const replyGoal = turn.reply_context?.goal;
  setText(view.user, replyGoal ? turn.user + "\n回复要求 · " + replyGoal : turn.user);
  const date = new Date(turn.created_at);
  if (turn.created_at && !Number.isNaN(date.getTime())) {
    view.date.dateTime = date.toISOString();
    view.date.title = date.toLocaleString("zh-CN");
    setText(view.date, date.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"}));
  }
  setText(view.status, statusText(turn));
  view.status.dataset.active = String(activeStates.has(turn.status));

  if (turn.status === "pending") {
    if (!view.preview) {
      view.preview = makeCard("preview", turn.desktop_confirmation?.title || "草稿预览");
      view.preview.box.dataset.testid = "preview";
      view.cards.append(view.preview.box);
    }
    setText(view.preview.body, turn.desktop_confirmation?.detail || turn.preview || "");
  } else if (view.preview) {
    view.preview.box.remove();
    view.preview = null;
  }

  if (turn.receipt && !view.receipt) {
    view.receipt = makeCard("receipt", "草稿已写入并核实");
    view.receipt.box.dataset.testid = "receipt";
    view.receipt.box.setAttribute("aria-live", "polite");
    const link = element("a", "", "打开草稿");
    link.href = "/drafts/" + encodeURIComponent(turn.receipt.filename);
    link.target = "_blank";
    link.rel = "noopener";
    link.setAttribute("aria-label", "在新标签页打开已保存的草稿");
    view.receipt.head.append(link);
    setText(view.receipt.body, turn.receipt.text);
    view.cards.append(view.receipt.box);
  }
  const nativeReceipts = Array.isArray(turn.native_receipts) ? turn.native_receipts : [];
  view.nativeCards ??= [];
  for (let i = view.nativeCards.length; i < nativeReceipts.length; i++) {
    const {tool, result} = nativeReceipts[i];
    if (!result) continue;
    const label = {play_media:"播放器回执", search_media:"媒体检索回执", inspect_media:"媒体检查回执"}[tool] || "工具回执";
    const card = makeCard("receipt", label);
    card.box.dataset.testid = "native-receipt";
    card.box.setAttribute("aria-live", "polite");
    if (tool === "play_media" && result.status === "played") {
      setText(card.body, "静音测试播放器已处理《" + (result.title || "音频") + "》" + (result.clip_s || 0) + " 秒。");
    } else if (tool === "search_media") {
      setText(card.body, "找到 " + (result.matches?.length || 0) + " 项。");
    } else {
      setText(card.body, [result.title, result.status, Number.isFinite(result.bytes) ? result.bytes + " 字节" : null].filter(Boolean).join(" · ") || tool);
    }
    view.cards.append(card.box);
    view.nativeCards.push(card);
  }
  const partial = turn.status === "thinking" && turn.partial_reply;
  if (turn.reply || partial) {
    if (!view.final) {
      view.final = element("p", "final message-text");
      view.answers.append(view.final);
    }
    view.final.dataset.testid = partial ? "partial-reply" : "final";
    view.final.setAttribute("aria-live", partial ? "off" : "polite");
    setText(view.final, turn.reply || partial);
  } else if (view.final) {
    view.final.remove(); view.final = null;
  }
  if (turn.status === "replied" && turn.local_overview?.length && !view.openLocalStatus) {
    const label = hasMcpReading(turn.local_overview) ? "查看查询结果" : "查看本机状态";
    const button = view.openLocalStatus = element("button", "projection-link", label);
    button.type = "button";
    button.dataset.testid = "open-local-status";
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-controls", "local-status-panel");
    button.addEventListener("click", () => openLocalStatus(turn, button));
    view.answers.append(button);
  }
  if (turn.status === "replied" && turn.weather?.source === "Open-Meteo" && !view.weatherSource) {
    view.weatherSource = element("small", "turn-timing");
    if (Number.isFinite(turn.weather.latitude) && Number.isFinite(turn.weather.longitude)) {
      const locationLabel = {district:"区县代表点", coordinates:"查询坐标", city:"城市坐标"}[turn.weather.location_kind] || "查询坐标";
      view.weatherSource.append(`${locationLabel} · 纬度 ${turn.weather.latitude.toFixed(4)}°，经度 ${turn.weather.longitude.toFixed(4)}° · `);
    }
    const source = element("a", "", "天气数据由 Open-Meteo 提供");
    source.href = "https://open-meteo.com/";
    source.target = "_blank";
    source.rel = "noopener";
    view.weatherSource.append(source);
    view.answers.append(view.weatherSource);
  }
  if (weatherPanel && hasWeatherReadings(turn) && !view.openWeather) {
    const button = view.openWeather = element("button", "projection-link", "查看天气");
    button.type = "button";
    button.dataset.testid = "open-weather";
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-controls", "weather-panel");
    button.addEventListener("click", () => openWeather(turn, button));
    view.answers.append(button);
  }
  if (turn.context_action && !view.contextWidget) {
    view.contextWidget = element("div", "context-widget");
    view.answers.append(view.contextWidget);
    import("/static/context-tools.js?v=clipboard-reply-voice").then(({mountContextTool}) => {
      function sendContext(payload, action = "message") {
        if (turn.chat_id !== latest.chat_id ||
            latest.turns.some(t => activeStates.has(t.status) && t.id !== payload.reply_context_request_id)) {
          showError("请在原对话当前请求结束后再提交。");
          return false;
        }
        return post(action, {chat_id:turn.chat_id, ...payload});
      }
      if (view.contextWidget.isConnected) mountContextTool(view.contextWidget, turn,
        (incoming, goal, requestId) => sendContext({
          text:"帮我回复：“" + incoming + "”", reply_context_turn:turn.id,
          reply_context_goal:goal, reply_context_request_id:requestId
        }), {
        model:() => ({id:latest.model_id, name:latest.model}),
        send:(question, excerpt, id) => sendContext({text:question, window_context:{turn_id:turn.id, model_id:id, excerpt}})
      }, {
        model:() => latest.vision_model,
        send:(text, image, model_id) => sendContext({text,image,model_id,turn_id:turn.id}, "context/image-question")
      });
    }).catch(() => setText(view.contextWidget, "本地工具暂未载入，请刷新重试。"));
  }
  if (turn.window_context && !view.windowSource) {
    view.windowSource = element("small", "turn-timing", "依据窗口选文 · " + turn.window_context.characters + " 字");
    view.answers.append(view.windowSource);
  }
  if (turn.image_context && !view.windowSource) {
    view.windowSource = element("small", "turn-timing", `依据窗口画面 · 百炼 ${turn.model}`);
    view.answers.append(view.windowSource);
  }
  if (turn.status === "replied" && turn.jev?.candidate_count > 0 && turn.reply && !view.copyReply) {
    if (turn.reply_context) view.answers.append(element("small", "turn-timing",
      turn.reply_context.action === "screen_text" ? "来信来源 · 窗口识别" : "来信来源 · 剪贴板"));
    const copy = element("button", "projection-link", "复制回复");
    copy.type = "button";
    copy.dataset.testid = "copy-reply";
    const note = element("small", "copy-note");
    note.setAttribute("role", "status");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(turn.reply);
        setText(note, "已复制");
      } catch {
        const range = document.createRange();
        range.selectNodeContents(view.final);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        setText(note, "文字已选中，可使用系统复制。");
      }
    });
    view.answers.append(copy);
    if (latest.jev_handoff && window.mossJevHandoff) {
      const fill = element("button", "projection-link", "填入本机草稿窗");
      fill.type = "button";
      fill.dataset.testid = "jev-handoff-open";
      fill.setAttribute("aria-haspopup", "dialog");
      fill.setAttribute("aria-controls", "jev-handoff-panel");
      fill.addEventListener("click", () => window.mossJevHandoff.open({
        chatId:turn.chat_id, turnId:turn.id, text:turn.reply, trigger:fill
      }));
      view.answers.append(fill);
    }
    view.answers.append(note);
    view.copyReply = copy;
  }
  renderAudio(view, turn, mayPlay);
  if (turn.receipt && turn.reply_source !== "receipt" && Number.isFinite(turn.receipt_ms) && Number.isFinite(turn.reply_ms) && !view.timing) {
    const seconds = (turn.reply_ms - turn.receipt_ms) / 1000;
    view.timing = element("p", "turn-timing", "后端回执比模型回复早 " + seconds.toFixed(2) + " 秒");
    view.answers.append(view.timing);
  }
  if (turn.error) {
    if (!view.error) {
      view.error = element("p", "turn-error");
      view.error.setAttribute("role", "alert");
      view.answers.append(view.error);
    }
    const archivedToolError = turn.source === "native" || turn.source === "local-fallback";
    setText(view.error, archivedToolError ? "当时未能完成这次请求。" : turn.error);
    if (archivedToolError && !view.retry && !turn.receipt && !nativeReceipts.length &&
        /(?:想听|要听|来一[首手]|播放|放一首).*(?:歌|曲)/u.test(turn.user)) {
      const retry = element("button", "projection-link", "重新点歌");
      retry.type = "button";
      retry.dataset.testid = "retry-music";
      retry.addEventListener("click", () => {
        if (turn.chat_id !== latest.chat_id || !connected || posting || latest.turns.some(t => activeStates.has(t.status))) return;
        window.mossProjection?.arm(turn.user, latest.chat_id, latest.turns);
        post("message", {text:turn.user, chat_id:latest.chat_id});
      });
      view.answers.append(retry);
      view.retry = retry;
    }
  } else if (view.error) {
    view.error.remove();
    view.error = null;
  }
  renderActions(view, turn);
  renderCloudRequests(view, turn.cloud_requests);
}

window.addEventListener("moss-projection-ready", () => {
  for (const turn of latest.turns) {
    const view = views.get(turn.id);
    if (view) renderAudio(view,turn,false);
  }
});

function renderPanels(state) {
  const backend = document.querySelector("#memory-backend");
  backend.hidden = state.memory_backend !== "mem0";
  if (!backend.hidden) setText(backend, state.memory_index_error || "检索服务：本机 Mem0");
  if (state.memory_limits) memoryInput.maxLength = state.memory_limits.max_item_chars;
  const chats = state.chats || [];
  const signature = JSON.stringify([state.chat_id, chats]);
  if (signature !== historyKey) {
    historyKey = signature;
    const history = document.querySelector("#history-list");
    history.replaceChildren();
    if (!chats.length) history.append(element("li", "panel-empty", "还没有对话记录。"));
    for (const chat of chats) {
      const item = element("li");
      const button = element("button", "history-entry");
      button.type = "button";
      button.dataset.testid = "chat-item";
      button.dataset.chatId = chat.id;
      button.append(element("span", "", chat.title || "新对话"));
      if (chat.id === state.chat_id) {
        button.setAttribute("aria-current", "true");
        button.append(element("span", "current-chat", "当前"));
      }
      button.addEventListener("click", async () => {
        if (chat.id === latest.chat_id || await post("switch-chat", {id: chat.id})) {
          document.querySelector("#history-panel").close();
          composer.focus();
        }
      });
      item.append(button);
      history.append(item);
    }
  }
  const now = Date.now();
  const memories = (state.memories || []).filter(item => !item.expires_at || Date.parse(item.expires_at) > now);
  clearTimeout(memoryExpiryTimer);
  const nextExpiry = memories.reduce((next, item) => item.expires_at ? Math.min(next, Date.parse(item.expires_at)) : next, Infinity);
  if (Number.isFinite(nextExpiry)) {
    memoryExpiryTimer = setTimeout(() => renderPanels(latest), Math.min(nextExpiry - now, 2147483647));
  }
  const entries = JSON.stringify(memories);
  if (entries === memoryKey) return;
  memoryKey = entries;
  const memoryList = document.querySelector("#memory-list");
  memoryList.replaceChildren();
  if (!memories.length) memoryList.append(element("li", "panel-empty", "还没有保存的记忆。"));
  for (const memory of memories) {
    const item = element("li", "memory-entry");
    item.dataset.testid = "memory-item";
    item.dataset.memoryId = memory.id;
    const remove = element("button", "memory-remove", "删除");
    remove.type = "button";
    remove.dataset.testid = "memory-remove";
    remove.setAttribute("aria-label", "删除记忆：" + memory.text);
    remove.addEventListener("click", () => post("forget", {id: memory.id}));
    const origin = {chat:"来自对话", panel:"来自记忆面板", legacy:"原有记忆"}[memory.source] || "原有记忆";
    const updated = new Date(memory.updated_at || memory.created_at).toLocaleDateString("zh-CN");
    let detail = origin + " · " + updated;
    if (memory.expires_at) {
      const cutoff = new Date(memory.expires_at);
      const expiry = cutoff.getHours() || cutoff.getMinutes() || cutoff.getSeconds()
        ? cutoff.toLocaleString("zh-CN") : new Date(cutoff.getTime() - 1).toLocaleDateString("zh-CN");
      detail += " · 有效至" + expiry;
    }
    const description = element("div");
    description.append(element("p", "", memory.text), element("small", "", detail));
    item.append(description, remove);
    memoryList.append(item);
  }
}

function accept(state, streamSnapshot = false) {
  if (!Number.isFinite(state.revision) || !Array.isArray(state.turns)) {
    throw new Error("未收到有效的会话状态。");
  }
  const silentSnapshot = awaitingPlaybackSnapshot;
  if (streamSnapshot) awaitingPlaybackSnapshot = false;
  if (state.revision <= latest.revision) return;
  const follow = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 90;
  const initial = latest.revision < 0;
  const switched = latest.chat_id && state.chat_id !== latest.chat_id;
  if (switched) {
    window.mossJevHandoff?.reset();
    localStatusPanel?.close();
    localStatusRequest = null;
    weatherPanel?.close();
    weatherRequest = null;
    lastStartedAudio = null;
    pauseMedia();
    window.mossProjection?.reset();
    drafts.set(latest.chat_id, composer.value);
    composer.value = drafts.get(state.chat_id) || "";
    resizeComposer();
  }
  latest = state;
  const kernel = document.querySelector("#kernel-label");
  kernel.hidden = state.agent_mode !== "holojarvis";
  setText(kernel, kernel.hidden ? "" : "· HoloJarvis");
  list.dataset.chat = state.chat_id || "";
  window.dispatchEvent(new CustomEvent("moss-agent-state", {detail: state}));
  if (state.model) document.querySelector(".connection").title = "当前模型：" + state.model;
  welcome.hidden = state.turns.length > 0;
  const ids = new Set(state.turns.map(turn => turn.id));
  for (const [id, view] of views) {
    if (!ids.has(id)) {
      view.audio?.pause();
      view.disposePlayer?.();
      view.article.remove();
      views.delete(id);
    }
  }
  for (const turn of state.turns) {
    let view = views.get(turn.id);
    if (!view) {
      view = createTurn(turn);
      views.set(turn.id, view);
      list.append(view.article);
    }
    // 旧轮次也可能补到播放回执，按整轮内容判断是否需要刷新。
    const signature = JSON.stringify(turn);
    if (signature !== view.turnSignature) {
      renderTurn(view, turn, connected && !initial && !switched && !silentSnapshot);
      view.turnSignature = signature;
    }
  }
  syncLocalStatus(state, connected && !initial && !switched && !silentSnapshot);
  syncWeather(state, connected && !initial && !switched && !silentSnapshot);
  renderPanels(state);
  controls();
  if (initial || follow || switched) conversation.scrollTop = conversation.scrollHeight;
}

async function readResponse(response) {
  const data = await response.json();
  if (!response.ok) {
    const reason = typeof data.detail === "string" ? data.detail : data.error;
    throw new Error(reason || "服务未能处理本次请求（" + response.status + "）。");
  }
  return data;
}

async function post(action, payload, selectedText, {turnOnly = false, statusTrigger = null, spoken = false, playback = false} = {}) {
  if (posting || !connected) return false;
  const scopedCancel = action === "cancel" && payload.conversation_only === true;
  const keepSpeech = playback && (window.mossSpeech?.active() || latest.turns.some(turn => activeStates.has(turn.status)));
  const panel = spoken && action === "message" && !playback ? document.querySelector("dialog[open]") : null;
  const view = panel && informationView(panel);
  const panelOrigin = panel ? {panel, view} : null;
  const ownStatusRequest = action === "message" && !playback ? {
    chat:latest.chat_id, text:payload.text, previous:latest.turns.at(-1)?.id,
    trigger:(view?.automatic && view.trigger) || statusTrigger, panelOrigin,
    composerRevision:composerEditRevision, deferPanel:spoken && Boolean(composer.value)
  } : null;
  if (ownStatusRequest) localStatusRequest = ownStatusRequest;
  if (ownStatusRequest && weatherPanel) weatherRequest = ownStatusRequest;
  const queued = action === "message" ? window.mossProjection?.holdPending?.(latest.chat_id) : null;
  if (!window.mossProjection && action === "message")
    submittedMedia = {chat:latest.chat_id,text:payload.text,previous:new Set(latest.turns.map(t=>t.id))};
  if (action === "message") payload = {...payload, player:pagePlayer(),queued_player:queued?.target,transport_request_id:crypto.randomUUID()};
  const prepareOutput = action === "message" && !keepSpeech ? window.mossSpeech?.prepareOutput : null;
  if (action === "media-select") window.mossSpeech?.userAction("message",{text:selectedText},latest);
  else if (action === "context/image-question") window.mossSpeech?.userAction("message",{text:payload.text},latest);
  else if (!keepSpeech && !scopedCancel) window.mossSpeech?.userAction(action, payload, latest, {deferOutput:Boolean(prepareOutput)});
  const cancellingAction = action === "cancel" && latest.turns.some(turn =>
    turn.id === payload.turn_id && (turn.status === "pending" || turn.desktop_confirmation ||
      (turnOnly && turn.source !== "media" && !turn.media_in_flight)));
  if (!scopedCancel && !cancellingAction && ["cancel", "new-chat", "switch-chat"].includes(action)) pauseMedia();
  posting = true;
  controls();
  showError("");
  try {
    const canLocate = ownStatusRequest && !payload.window_context && !payload.reply_context_turn;
    const locateDevice = canLocate && asksDeviceLocation(payload.text) && !pendingWeatherUsesBrowser(payload.text);
    const wantsLocation = canLocate && (locateDevice || usesBrowserWeatherLocation(payload.text));
    const city = wantsLocation && (await cityReady)?.selected();
    if (city) payload = {...payload, preferred_city_id:city.id};
    const locationLookup = wantsLocation && !city && browserWeatherLocation(payload.text, locateDevice, prepareOutput);
    if (locationLookup) {
      const position = await locationLookup;
      if (position) {
        const browserLocation = locateDevice ? Object.fromEntries(Object.entries(position).map(([axis, degree]) =>
          [axis, Math.round(degree * 10) / 10])) : position;
        if (locateDevice) ownStatusRequest.devicePosition = position;
        payload = {...payload, browser_location:browserLocation};
      }
    }
    const request = fetch("/api/" + action, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    // Let the browser dispatch the request before opening the audio device.
    if (!locationLookup) prepareOutput?.({deferred:true});
    const response = await request;
    let state = await readResponse(response);
    if (scopedCancel && state.chat_id === payload.chat_id && latest.chat_id === payload.chat_id &&
        state.turns.some(turn => turn.id === payload.turn_id && turn.status === "cancelled")) {
      window.mossSpeech?.userAction(action, payload, latest);
    }
    if (ownStatusRequest && (localStatusRequest === ownStatusRequest || weatherRequest === ownStatusRequest)) {
      const submitted = state.turns.at(-1);
      if (state.chat_id === ownStatusRequest.chat && submitted?.id && submitted.id !== ownStatusRequest.previous &&
          submitted.user === ownStatusRequest.text) ownStatusRequest.id = submitted.id;
      else {
        if (localStatusRequest === ownStatusRequest) localStatusRequest = null;
        if (weatherRequest === ownStatusRequest) weatherRequest = null;
      }
    }
    if (action === "message" && window.mossTransport)
      state = await window.mossTransport.apply(state,payload.transport_request_id,() => ({snapshot:pagePlayer(),node:lastStartedAudio?.node}),queued);
    const alreadySeen = state.revision <= latest.revision;
    accept(state);
    if (alreadySeen && ownStatusRequest?.id) {
      const canOpen = connected && !awaitingPlaybackSnapshot;
      syncLocalStatus(latest, canOpen);
      syncWeather(latest, canOpen);
    }
    return true;
  } catch (error) {
    if (localStatusRequest === ownStatusRequest) localStatusRequest = null;
    if (weatherRequest === ownStatusRequest) weatherRequest = null;
    showError(error instanceof TypeError ? "连接中断，请等待重新连接后查看本轮状态。" : error.message);
    return false;
  } finally {
    queued?.release();
    posting = false;
    controls();
  }
}

quickLocalStatus?.addEventListener("click", async () => {
  const command = composeAction("现在几点、电脑还有多少电、音量多大？");
  if (command?.action !== "message") return;
  const ok = await post(command.action, command.body, undefined, {statusTrigger:quickLocalStatus});
  const drawer = document.querySelector("#dialogue");
  if (!ok && drawer?.hidden) document.querySelector("#history-toggle")?.click();
});

function resizeComposer() {
  composer.style.height = "auto";
  composer.style.height = Math.min(composer.scrollHeight, 144) + "px";
}

async function submitComposition(command, spoken = false) {
  if (command.action === "stop-speech") {
    const speech = window.mossSpeech;
    if (!speech) return false;
    const speaking = speech.active();
    speech.interruptTurn();
    const statusNode = document.getElementById("status");
    if (statusNode) setText(statusNode, speaking ? "语音已打断。" : "当前没有正在播报的回复。");
    return true;
  }
  if (command.action === "replace-reply") {
    const {chat_id, turn_id} = command.body;
    if (!await post("cancel", command.body, undefined, {turnOnly:true, spoken})) return false;
    if (latest.chat_id !== chat_id || !latest.turns.some(turn => turn.id === turn_id && turn.status === "cancelled") ||
        latest.turns.some(turn => activeStates.has(turn.status))) {
      showError("对话状态已变化，请重新提交新问题。");
      return false;
    }
    window.mossProjection?.arm(command.text, chat_id, latest.turns);
    return post("message", {text:command.text, chat_id}, undefined, {spoken});
  }
  return post(command.action, command.body, undefined, {turnOnly:command.turnOnly, playback:command.playback, spoken});
}

function confirmationScope(turn) {
  if (calendarApproval(turn)) return "pending_calendar_only";
  if (turn?.call_id && !turn.desktop_confirmation) return "pending_draft_only";
  return null;
}

function scopedDecision(command, scope) {
  return submitComposition({...command, body:{...command.body, [scope]:true}}, true);
}

window.mossPendingCalendar = () => latest.turns.find(turn =>
  turn.status === "pending" && confirmationScope(turn) === "pending_calendar_only")?.id ?? null;

window.mossReplaceableReply = () => latest.turns.find(turn =>
  turn.status === "thinking" && turn.conversation_replaceable === true)?.id ?? null;

window.mossSubmitSpokenAction = (text, chatId, calendarTurn, replyTurn) => {
  const replacement = replacementQuestion(text);
  if (replacement) {
    if (!connected || posting || latest.chat_id !== chatId) return Promise.resolve(false);
    const target = latest.turns.find(turn => turn.id === replyTurn);
    const command = replacementCommand(replacement, target);
    if (command) return submitComposition(command, true);
    showError("刚才的回复已经结束或正在执行工具，请直接说新问题。");
    return Promise.resolve(false);
  }
  if (!connected || posting || latest.chat_id !== chatId) return null;
  const active = latest.turns.find(turn => activeStates.has(turn.status));
  if (calendarTurn && calendarTurn !== active?.id) {
    const captured = latest.turns.find(turn => turn.id === calendarTurn);
    const decision = calendarApproval(captured) && composeAction(text.trim(), {...captured, status:"pending"});
    if (["approve", "cancel"].includes(decision?.action)) return Promise.resolve(false);
  }
  const command = composeAction(text.trim(), active);
  const scope = confirmationScope(active);
  if (scope === "pending_calendar_only" && command?.body?.turn_id === active.id) {
    if (active.status !== "pending" || calendarTurn !== active.id ||
        (command.action === "approve" && approvalAnswer(text) !== calendarApproval(active)))
      return Promise.resolve(false);
    if (["approve", "cancel"].includes(command.action)) return scopedDecision(command, scope);
  }
  if ([...calendarApprovalWords.values()].includes(approvalAnswer(text))) return Promise.resolve(false);
  return command && (command.playback || command.draftApproval || ["cancel", "stop-speech"].includes(command.action))
    ? submitComposition(command, true) : null;
};

window.mossSubmitOverlappingDecision = ({original, filtered, chatId, turnId, heardInPlayback, calendarTurn}) => {
  const turn = latest.turns.find(turn => turn.id === turnId && confirmationScope(turn)) ||
    latest.turns.find(turn => turn.status === "pending" && confirmationScope(turn));
  if (!turn) return null;
  const scope = confirmationScope(turn);
  const proposal = {...turn, status:"pending"};
  const raw = composeAction(original.trim(), proposal);
  const cleaned = composeAction(filtered.trim(), proposal);
  const isDecision = command => command?.body?.turn_id === turn.id && ["approve", "cancel"].includes(command.action);
  const shortDecline = !filtered && /^(?:取消|算了)[。！!]*$/u.test(original.trim()) && raw?.action === "cancel";
  if (!isDecision(cleaned) && !shortDecline) return null;
  if (!connected || posting || chatId !== latest.chat_id || turnId !== turn.id || turn.status !== "pending" || heardInPlayback ||
      !isDecision(raw) || (!shortDecline && raw.action !== cleaned.action) ||
      (scope === "pending_calendar_only" && (calendarTurn !== turn.id ||
        (raw.action === "approve" && approvalAnswer(original) !== calendarApproval(turn)))))
    return Promise.resolve(false);
  return scopedDecision(raw, scope);
};

window.mossSubmitSpokenMessage = async (text, chatId, submitContext) => {
  const blocked = () => {
    if (!connected) return "连接中断，语音未提交。";
    if (!chatId || latest.chat_id !== chatId) return "对话已切换，语音未提交。";
    if (posting || latest.turns.some(turn => activeStates.has(turn.status))) return "请等本轮结束后再说一次。";
    return "";
  };
  const reason = blocked();
  if (reason) return reason;
  const contextStatus = await submitContext(text, latest);
  if (contextStatus) return contextStatus;
  const freshReason = blocked();
  if (freshReason) return freshReason;
  const command = composeAction(text.trim());
  if (command?.action !== "message" || command.playback) return "语音未提交，请再说一次。";
  window.mossProjection?.fromVoice(text, chatId, latest.turns);
  window.mossProjection?.arm(command.body.text, chatId, latest.turns);
  conversation.scrollTop = conversation.scrollHeight;
  await submitComposition(command, true);
  return "";
};

const composerForm = document.querySelector("#compose, #composer");
composerForm.addEventListener("submit", async event => {
  event.preventDefault();
  const original = composer.value;
  const editRevision = composerEditRevision;
  const chatId = latest.chat_id;
  const text = original.trim();
  const command = composeAction(text);
  if (send.disabled || !command) return;
  if (command.action === "message" && !command.playback) window.mossProjection?.arm(text,latest.chat_id, latest.turns);
  conversation.scrollTop = conversation.scrollHeight;
  if (await submitComposition(command) && latest.chat_id === chatId && composerEditRevision === editRevision &&
      composer.value === original && !composerHasSelection()) {
    composer.value = "";
    resizeComposer();
    controls();
    composer.focus();
  }
});
composer.addEventListener("input", () => { composerEditRevision++; resizeComposer(); controls(); });
composer.addEventListener("compositionstart", () => { composerComposing = true; composerEditRevision++; });
composer.addEventListener("compositionend", () => { composerComposing = false; });
composer.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !composerComposing && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!send.disabled) document.querySelector("#compose, #composer").requestSubmit();
  }
});
for (const button of document.querySelectorAll("[data-example]")) {
  button.addEventListener("click", () => {
    button.closest("dialog")?.close();
    composer.value = button.dataset.example;
    resizeComposer();
    controls();
    composer.focus();
  });
}

for (const button of document.querySelectorAll("[data-panel]")) {
  button.addEventListener("click", () => {
    showError("");
    document.getElementById(button.dataset.panel).showModal();
  });
}
newChat.addEventListener("click", async () => {
  if (await post("new-chat", {})) composer.focus();
});
memoryInput.addEventListener("input", controls);
document.querySelector("#memory-form").addEventListener("submit", async event => {
  event.preventDefault();
  const original = memoryInput.value;
  if (!memoryAdd.disabled && await post("remember", {text: original.trim()}) && memoryInput.value === original) {
    memoryInput.value = "";
    controls();
    memoryInput.focus();
  }
});

const stream = new EventSource(document.getElementById("dialogue") ? "/api/events?wake=1" : "/api/events");
stream.addEventListener("open", () => { awaitingPlaybackSnapshot = true; setConnection(true, "本地服务已连接"); showError(""); });
stream.addEventListener("error", () => { awaitingPlaybackSnapshot = true; setConnection(false, "正在重连"); });
stream.addEventListener("wake-offer", event => window.dispatchEvent(new CustomEvent("moss-wake-offer", {detail:event.data})));
stream.addEventListener("state", event => {
  try { accept(JSON.parse(event.data), true); }
  catch (error) { showError(error.message); }
});
fetch("/api/state", {cache: "no-store"})
  .then(readResponse)
  .then(accept)
  .catch(() => { if (latest.revision < 0) showError("暂时无法读取会话，正在等待本地服务。"); });
window.addEventListener("pagehide", pauseMedia);
