const storageKey = "moss.speech.space.percent";
const defaultAmount = 100;
const previewText = "这里是 MOSS。语音通道已就绪。";

const markup = `
  <header class="speech-space-header">
    <h2 id="speech-space-title">语音空间感</h2>
    <form method="dialog"><button type="submit">关闭</button></form>
  </header>
  <div class="speech-space-body">
    <div class="speech-space-reading">
      <label for="speech-space-amount">强度</label>
      <output id="speech-space-value" for="speech-space-amount" aria-hidden="true"></output>
    </div>
    <input id="speech-space-amount" type="range" min="0" max="150" step="5" value="100" autofocus>
    <div class="speech-space-presets" role="group" aria-label="空间感预设">
      <button type="button" data-amount="0" aria-pressed="false"><span>原声</span><small>0%</small></button>
      <button id="speech-space-reset" type="button" data-amount="100" aria-pressed="true" title="恢复原版强度"><span>原版空间</span><small>100%</small></button>
      <button type="button" data-amount="150" aria-pressed="false"><span>更强</span><small>150%</small></button>
    </div>
    <div class="speech-space-actions">
      <button id="speech-space-preview" type="button">试听</button>
    </div>
    <p id="speech-space-status" role="status"></p>
  </div>`;

function validAmount(value) {
  return Number.isInteger(value) && value >= 0 && value <= 150 && value % 5 === 0;
}

export function speechRoomAmount() {
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null && /^(?:0|[1-9]\d{0,2})$/u.test(saved) && validAmount(Number(saved))) return Number(saved);
  } catch { /* 使用默认的原版空间感。 */ }
  return defaultAmount;
}

export function mountSpeechSpace({playPreview}) {
  const dialog = document.getElementById("speech-space-settings");
  const trigger = document.getElementById("speech-space-configure");
  dialog.innerHTML = markup;
  dialog.setAttribute("aria-labelledby", "speech-space-title");
  const amount = dialog.querySelector("#speech-space-amount");
  const value = dialog.querySelector("#speech-space-value");
  const presets = dialog.querySelectorAll(".speech-space-presets button");
  const preview = dialog.querySelector("#speech-space-preview");
  const status = dialog.querySelector("#speech-space-status");
  const error = {busy:"MOSS 正在播报，请稍后试听。", disabled:"先开启语音播报，再试听。"};
  let activePreview;

  function showAmount(percent) {
    amount.value = String(percent);
    value.value = `${percent}%`;
    const preset = [...presets].find(button => Number(button.dataset.amount) === percent);
    amount.setAttribute("aria-valuetext", preset ? preset.textContent : `百分之${percent}`);
    for (const button of presets) button.setAttribute("aria-pressed", String(button === preset));
  }

  function stopPreview(message = "") {
    if (!activePreview) return;
    activePreview.abort();
    activePreview = undefined;
    preview.textContent = "试听";
    status.textContent = message;
  }

  function setAmount(percent) {
    if (!validAmount(percent)) return;
    if (percent !== speechRoomAmount()) stopPreview();
    try {
      localStorage.setItem(storageKey, String(percent));
      showAmount(percent);
      if (!activePreview) status.textContent = "";
    } catch {
      showAmount(speechRoomAmount());
      status.textContent = "当前浏览器无法保存空间感设定。";
    }
  }

  async function previewCurrentAmount() {
    if (activePreview) {
      stopPreview("试听已停止。");
      return;
    }
    const controller = new AbortController();
    activePreview = controller;
    const percent = speechRoomAmount();
    preview.textContent = "停止试听";
    status.textContent = `正在准备 ${percent}% 的试听…`;
    let result;
    try {
      result = await playPreview(previewText, percent, {signal:controller.signal});
    } catch { result = false; }
    if (activePreview !== controller) return;
    activePreview = undefined;
    preview.textContent = "试听";
    status.textContent = result === true ? "试听完成。" : error[result] || "试听暂不可用，请重试。";
  }

  trigger.addEventListener("click", () => {
    if (dialog.open) return;
    const menu = trigger.closest("details");
    if (menu) menu.open = false;
    showAmount(speechRoomAmount());
    status.textContent = "";
    dialog.showModal();
  });
  dialog.addEventListener("close", () => {
    stopPreview();
    const menu = trigger.closest("details");
    if (menu && !menu.open && document.activeElement === document.body) menu.querySelector("summary")?.focus();
  });
  amount.addEventListener("input", () => setAmount(amount.valueAsNumber));
  for (const button of presets) button.addEventListener("click", () => setAmount(Number(button.dataset.amount)));
  preview.addEventListener("click", previewCurrentAmount);
  window.addEventListener("storage", event => {
    if (event.key === storageKey || event.key === null) {
      stopPreview();
      showAmount(speechRoomAmount());
    }
  });
}
