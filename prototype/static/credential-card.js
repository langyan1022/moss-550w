import {drawStaffBarcode} from "./staff-barcode.js?v=staff-card";

const printAsset = "/conversation-assets/staff-badge-print.svg?v=staff-cs-barcode";
const reference = {number:"IQC-001", name:"马兆", role:"实验室主任", serial:"BSC–IQC001 222179 MAZHAO"};
// The source badge's Code 128 uses an ASCII hyphen in its printed serial.
const referenceBarcode = reference.serial.replace("–", "-");
const barcode = '<svg class="credential-card__barcode" role="img"></svg>';
const faces = {
  back: `
    <div class="credential-card__stripe" lang="en"><span>IDL. <span class="credential-card__number"></span></span></div>
    ${barcode}`,
  front: `
    <div class="credential-card__identity credential-card__number" lang="en"></div>
    <div class="credential-card__name"></div>
    <div class="credential-card__role"></div>
    <div class="credential-card__serial"></div>
    ${barcode}`,
};

function setBarcode(code, profile, useReferenceSerial) {
  const barcodeValue = useReferenceSerial ? referenceBarcode : profile.number;
  code.setAttribute("aria-label", `工牌${useReferenceSerial ? "流水号" : "编号"} ${barcodeValue} 的条形码`);
  code.dataset.badgeNumber = profile.number;
  code.dataset.value = barcodeValue;
  drawStaffBarcode(code, barcodeValue);
}

export function renderStaffScan(container, profile) {
  const code = container.ownerDocument.createElementNS("http://www.w3.org/2000/svg", "svg");
  code.setAttribute("role", "img");
  setBarcode(code, profile, profile.number === reference.number && profile.name === reference.name);
  code.style.setProperty("--barcode-width", `${code.viewBox.baseVal.width}px`);
  container.replaceChildren(code);
}

function fitSerial(label) {
  const document = label.ownerDocument;
  const style = document.defaultView.getComputedStyle(label);
  const optical = name => parseFloat(style.getPropertyValue(`--serial-${name}`));
  const width = optical("width");
  const context = document.createElement("canvas").getContext("2d");
  if (!context || !("letterSpacing" in context) || !("wordSpacing" in context) || !width) return;
  const font = `${style.fontWeight} ${optical("size")}px ${style.fontFamily}`;
  const text = label.textContent;
  function applyFit() {
    if (!label.isConnected) return;
    // Measure in badge artwork units, also when the parent dialog is hidden.
    context.font = font;
    context.letterSpacing = `${optical("tracking")}px`;
    context.wordSpacing = `${optical("word-spacing")}px`;
    const textWidth = context.measureText(text).width;
    label.style.setProperty("--serial-scale", Math.min(1, (width - 4) / Math.max(1, textWidth)));
  }
  if (document.fonts.check(font, text)) applyFit();
  else void document.fonts.load(font, text).then(applyFit, applyFit);
}

export function renderCredentialCard(container, profile, side = "front") {
  const front = side !== "back";
  const card = container.ownerDocument.createElement("article");
  card.className = "credential-card";
  card.dataset.side = front ? "front" : "back";
  card.setAttribute("aria-label", front ? "工牌正面" : "工牌背面");
  card.innerHTML = faces[card.dataset.side];

  // The shipped print SVG uses the previous face IDs.
  const artworkSide = front ? "back" : "front";
  const layers = [artworkSide];
  function setLabel(selector, value, referenceLayer, exact, hiddenWrapper = selector) {
    const node = card.querySelector(selector);
    node.textContent = value;
    node.title = value;
    const glyphCount = [...value].length;
    const wrapper = card.querySelector(hiddenWrapper);
    wrapper.style.setProperty("--glyph-count", glyphCount + (wrapper === node ? 0 : 5));
    if (node.matches(".credential-card__name, .credential-card__role") && /\p{Script=Han}/u.test(value)) {
      node.dataset.han = "";
    }
    if (selector === ".credential-card__name") {
      node.dataset.glyphCount = glyphCount;
      if (glyphCount > 6) node.dataset.wrap = "";
      if (value === reference.name) {
        const raised = card.ownerDocument.createElement("span");
        raised.textContent = "兆";
        node.replaceChildren("马", raised);
        node.dataset.referencePair = "";
      }
    }
    if (selector === ".credential-card__number" && front && glyphCount > 12) node.dataset.wrap = "";
    if (exact) {
      wrapper.dataset.reference = "";
      layers.push(referenceLayer);
    }
  }

  const referenceNumber = profile.number === reference.number;
  const referenceName = profile.name === reference.name;
  const referenceSerial = referenceNumber && referenceName;
  setLabel(".credential-card__number", profile.number, `${artworkSide}-reference-id`, referenceNumber,
    front ? ".credential-card__identity" : ".credential-card__stripe");
  if (front) {
    setLabel(".credential-card__name", profile.name, null, false);
    setLabel(".credential-card__role", profile.role, null, false);
    setLabel(".credential-card__serial",
      referenceSerial ? reference.serial : `BSC-${profile.number} ${profile.name}`, "reference-serial", referenceSerial);
  }

  const print = `<svg class="credential-card__print" viewBox="0 0 450 282" role="img"
    aria-label="${front ? "北京航天中心，CS，智能量子计算机重点实验室，IDL." : "北京航天中心，IQC 智能量子计算机重点实验室"}">
    ${layers.map(layer => `<use href="${printAsset}#${layer}"></use>`).join("")}</svg>`;
  card.insertAdjacentHTML("afterbegin", print);

  setBarcode(card.querySelector(".credential-card__barcode"), profile, referenceSerial);
  container.replaceChildren(card);
  if (front && !referenceSerial) fitSerial(card.querySelector(".credential-card__serial"));
  return card;
}
