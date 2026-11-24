import {renderCredentialCard, renderStaffScan} from "./credential-card.js?v=staff-editable-english";

const storageKey = "moss.staff.profile";
const defaults = {name:"马兆", number:"IQC-001", role:"实验室主任", autoWelcome:true};
const validNumber = /^[A-Za-z0-9._/-]{1,24}$/u;
const voiceFeedback = {
  disabled:"请先在设置中开启 MOSS 语音播报。",
  busy:"MOSS 正在处理其他操作，稍后可以重播。",
  activation:"浏览器需要点击后才能播放声音。",
  unavailable:"当前无法启用音频输出。"
};

function storedProfile(raw) {
  try {
    const profile = JSON.parse(raw);
    if (typeof profile.number === "string" && validNumber.test(profile.number) && typeof profile.name === "string" &&
        profile.name.trim() && profile.name.length <= 12 && typeof profile.role === "string" &&
        profile.role.trim() && profile.role.length <= 16) {
      return {...defaults, name:profile.name.trim(), number:profile.number, role:profile.role.trim(),
        autoWelcome:profile.autoWelcome !== false};
    }
  } catch { /* An empty or invalid browser setting falls back to the example badge. */ }
  return {...defaults};
}

export function mountStaffConsole({playGreeting}) {
  const byId = id => document.getElementById(id);
  const instrument = byId("staff-instrument"), mainCard = byId("staff-preview");
  const view = byId("staff-view"), largeCard = byId("staff-large-preview"), settings = byId("staff-settings");
  const scan = byId("staff-scan-preview"), scanToggle = byId("staff-view-scan");
  const form = byId("staff-profile-form"), saveFeedback = byId("staff-save-feedback");
  const welcomeFeedback = byId("staff-voice-feedback"), landingWelcome = byId("staff-landing-welcome");
  const name = byId("staff-name"), number = byId("staff-number"), role = byId("staff-role");
  const autoWelcome = byId("staff-auto-welcome"), faces = view.querySelectorAll("[data-staff-face]");
  let profile = {...defaults};
  let face = "front", greetingPending = false;
  try { profile = storedProfile(localStorage.getItem(storageKey)); } catch { /* Storage can be disabled per site. */ }

  function showFace(side) {
    face = side;
    showScan(false);
    for (const button of faces) button.setAttribute("aria-pressed", String(button.dataset.staffFace === side));
    renderCredentialCard(largeCard, profile, face);
  }
  function showScan(open) {
    largeCard.hidden = open;
    scan.hidden = !open;
    scanToggle.setAttribute("aria-pressed", String(open));
    if (open) renderStaffScan(scan, profile);
  }
  function render() {
    renderCredentialCard(mainCard, profile, "front");
    if (view.open) showFace(face);
  }
  function configure() {
    name.value = profile.name;
    number.value = profile.number;
    role.value = profile.role;
    autoWelcome.checked = profile.autoWelcome;
    saveFeedback.hidden = true;
    for (const input of [name, number, role]) input.setCustomValidity("");
    settings.showModal();
  }
  async function welcome(automatic = false) {
    if (greetingPending || (automatic && !profile.autoWelcome)) return;
    greetingPending = true;
    let result;
    try {
      result = await playGreeting(`${profile.name}${profile.role}，操作权限已确认。欢迎接入 MOSS。`);
    } catch {
      result = "unavailable";
    } finally {
      greetingPending = false;
    }
    landingWelcome.hidden = automatic ? !["activation", "busy", "unavailable"].includes(result) : result === "started";
    if (!automatic && result !== "started" && !view.open) {
      showFace("front");
      view.showModal();
    }
    welcomeFeedback.textContent = voiceFeedback[result] || "";
    welcomeFeedback.hidden = !voiceFeedback[result];
  }

  byId("staff-open").addEventListener("click", () => {
    showFace("front");
    view.showModal();
  });
  for (const button of faces) button.addEventListener("click", () => showFace(button.dataset.staffFace));
  scanToggle.addEventListener("click", () => showScan(scan.hidden));
  byId("staff-configure").addEventListener("click", event => {
    event.currentTarget.closest("details").open = false;
    configure();
  });
  byId("staff-view-configure").addEventListener("click", configure);
  byId("staff-view-welcome").addEventListener("click", () => void welcome());
  landingWelcome.addEventListener("click", () => void welcome());
  for (const input of [name, number, role]) input.addEventListener("input", () => input.setCustomValidity(""));
  form.addEventListener("submit", event => {
    event.preventDefault();
    const next = {name:name.value.trim(), number:number.value.trim(), role:role.value.trim(),
      autoWelcome:autoWelcome.checked};
    name.setCustomValidity(next.name ? "" : "请输入姓名。");
    number.setCustomValidity(validNumber.test(next.number) ? "" : "请用字母、数字或 . _ / - 填写编号。");
    role.setCustomValidity(next.role ? "" : "请输入职务。");
    if (!form.reportValidity()) return;
    try { localStorage.setItem(storageKey, JSON.stringify(next)); }
    catch {
      saveFeedback.textContent = "当前浏览器禁止保存工牌设置。";
      saveFeedback.hidden = false;
      return;
    }
    profile = next;
    landingWelcome.hidden = welcomeFeedback.hidden = true;
    render();
    settings.close();
  });
  window.addEventListener("storage", event => {
    if (event.key !== storageKey && event.key !== null) return;
    profile = storedProfile(event.newValue);
    landingWelcome.hidden = true;
    render();
  });
  render();
  instrument.hidden = false;
  return {welcomeAfterOpening:() => void welcome(true)};
}
