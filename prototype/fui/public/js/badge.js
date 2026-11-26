/**
 * BSC · IQC Personnel Access Badge — Direct 95%+ Replica from F:\moss
 * (`/conversation-assets/staff-badge-print.svg` + reconstructed badge print fonts)
 * + Real ISO/IEC 15417 Code-128B Vector Barcode
 * + Neural Micro-Model 1-Inch Portrait Cutout (MediaPipe SelfieSegmenter WASM + Offline Fallback)
 * + Post-Opening Auth Gate + Live Main-Console Front-Badge & Cutout Dock + MOSS Voice Greeting.
 */
import { escapeHtml } from './hud/utils.js';
import { getLocale, t } from './i18n.js';
import { hasChinese, toPinyinCode } from './pinyin.js';
import { getMattingStatus, importLocalSegmenterModel, runPortraitCutout } from './portrait-matting.js';

const STORAGE_KEY = 'moss-fui.badge.v1';
let scopedStorageKey = STORAGE_KEY;
const PRINT_ASSET = '/conversation-assets/staff-badge-print.svg?v=staff-cs-barcode';

const REFERENCE = {
  name: '马兆',
  latin: 'MAZHAO',
  idCode: 'IQC-001',
  serial: '222179',
  clearance: 'CS',
  role: '实验室主任',
  serialLine: 'BSC–IQC001 222179 MAZHAO',
  barcodeValue: 'BSC-IQC001 222179 MAZHAO',
};

export const DEFAULT_BADGE = {
  name: REFERENCE.name,
  latin: REFERENCE.latin,
  idCode: REFERENCE.idCode,
  serial: REFERENCE.serial,
  clearance: REFERENCE.clearance,
  dept: '智能量子计算机重点实验室',
  deptEn: 'KEY LABORATORY OF INTELLIGENT QUANTUM COMPUTER',
  role: REFERENCE.role,
  customBarcode: '',
  customGreeting: '',
  photoDataUrl: '',
  photoRawDataUrl: '',
  photoCutout: true,
  photoSensitivity: 50,
  photoArchiveTone: false,
  showAfterOpening: true,
  speakGreeting: true,
};

/* ── ISO/IEC 15417 Code-128B Pattern Table (matching F:\moss staff-barcode.js) ── */
const CODE128_PATTERNS = [
  '212222','222122','222221','121223','121322','131222','122213','122312','132212','221213',
  '221312','231212','112232','122132','122231','113222','123122','123221','223211','221132',
  '221231','213212','223112','312131','311222','321122','321221','312212','322112','322211',
  '212123','212321','232121','111323','131123','131321','112313','132113','132311','211313',
  '231113','231311','112133','112331','132131','113123','113321','133121','313121','211331',
  '231131','213113','213311','213131','311123','311321','331121','312113','312311','332111',
  '314111','221411','431111','111224','111422','121124','121421','141122','141221','112214',
  '112412','122114','122411','142112','142211','241211','221114','413111','241112','134111',
  '111242','121142','121241','114212','124112','124211','411212','421112','421211','212141',
  '214121','412121','111143','111341','131141','114113','114311','411113','411311','113141',
  '114131','311141','411131','211412','211214','211232','2331112',
];

function buildBarcodeSvg(rawText, ariaLabel) {
  const clean = String(rawText || REFERENCE.barcodeValue)
    .replace(/[^\x20-\x7E]/g, '')
    .trim() || REFERENCE.barcodeValue;
  const codes = [104];
  let checksum = 104;
  for (let i = 0; i < clean.length; i++) {
    const symbol = clean.charCodeAt(i) - 32;
    codes.push(symbol);
    checksum = (checksum + symbol * (i + 1)) % 103;
  }
  codes.push(checksum, 106);

  const QUIET = 10;
  const BAR_H = 32;
  const bars = [];
  let x = QUIET;
  for (const code of codes) {
    const widths = CODE128_PATTERNS[code] || CODE128_PATTERNS[0];
    for (let i = 0; i < widths.length; i++) {
      const w = Number(widths[i]);
      if (i % 2 === 0) bars.push(`M${x} 0h${w}v${BAR_H}h-${w}Z`);
      x += w;
    }
  }
  return `<svg class="credential-card__barcode" viewBox="0 0 ${x + QUIET} ${BAR_H}" preserveAspectRatio="none" shape-rendering="crispEdges" role="img" aria-label="${escapeHtml(ariaLabel)}"><path fill="currentColor" d="${bars.join('')}"/></svg>`;
}

export function computeBarcodeString(cfg) {
  const custom = (cfg.customBarcode || '').trim();
  if (custom) return custom;
  const idClean = (cfg.idCode || REFERENCE.idCode).replace(/[^A-Za-z0-9]/g, '').toUpperCase() || 'IQC001';
  const serial = (cfg.serial || '').trim();
  const latin = (cfg.latin || '').trim().toUpperCase();
  return [`BSC-${idClean}`, serial, latin].filter(Boolean).join(' ');
}

function defaultGreetingTemplate() {
  return getLocale().startsWith('zh')
    ? '{role}，{name}。身份核验通过。[pause 220ms] 编号 {id}，{clearance} 级操作权限已确认。[pause 240ms] 550W 在线，MOSS 欢迎您接入。'
    : '{role} {name}. Identity verified. [pause 220ms] ID {id}, clearance {clearance} confirmed. [pause 240ms] 550W online. MOSS welcomes your access.';
}

export function buildGreetingSpeech(cfg) {
  const template = (cfg.customGreeting || '').trim() || defaultGreetingTemplate();
  const vars = {
    name: (cfg.name || DEFAULT_BADGE.name).trim(),
    role: (cfg.role || DEFAULT_BADGE.role).trim(),
    id: (cfg.idCode || DEFAULT_BADGE.idCode).trim(),
    clearance: (cfg.clearance || DEFAULT_BADGE.clearance).trim().toUpperCase(),
    dept: (cfg.dept || DEFAULT_BADGE.dept).trim(),
  };
  return template.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
}

function loadConfig() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...DEFAULT_BADGE, ...JSON.parse(raw) };
    const previous = JSON.parse(localStorage.getItem('moss.staff.profile') || 'null');
    if (typeof previous?.name === 'string' && /^[A-Za-z0-9._/-]{1,24}$/.test(previous.number)
        && typeof previous.role === 'string') {
      return { ...DEFAULT_BADGE, name:previous.name, role:previous.role, idCode:previous.number,
        latin:previous.name === REFERENCE.name ? REFERENCE.latin : toPinyinCode(previous.name),
        showAfterOpening:previous.autoWelcome !== false, speakGreeting:previous.autoWelcome !== false };
    }
  } catch { /* ignore */ }
  return { ...DEFAULT_BADGE };
}

function saveConfig(cfg) {
  try { localStorage.setItem(scopedStorageKey, JSON.stringify(cfg)); } catch { /* ignore */ }
}

/**
 * Render the BSC · IQC Badge Face matching F:\moss `credential-card.js`
 * (`/conversation-assets/staff-badge-print.svg` + `ZiHun ChuangCuHei` & `Taobao Maicai`).
 * Note: In staff-badge-print.svg:
 *   - symbol `#back`  = Ma Zhao / CS / BSC Identity Face (rendered as `data-side="front"`, our primary "正面")
 *   - symbol `#front` = IQC / BEIJING SPACE CENTER Lab Face (rendered as `data-side="back"`, our "背面")
 * Name & role ALWAYS render via live `ZiHun ChuangCuHei` / `Taobao Maicai` fonts so custom edits keep identical typography.
 */
export function renderCredentialCardHtml(cfg, barcodeStr, face = 'identity') {
  const isIdentityFace = face === 'identity';
  const artworkSide = isIdentityFace ? 'back' : 'front';
  const name = (cfg.name || DEFAULT_BADGE.name).trim();
  const idCode = (cfg.idCode || DEFAULT_BADGE.idCode).trim();
  const role = (cfg.role || DEFAULT_BADGE.role).trim();

  const isRefId = idCode === REFERENCE.idCode;
  const isRefSerial = barcodeStr === REFERENCE.barcodeValue;

  const layers = [artworkSide];
  if (isRefId) layers.push(`${artworkSide}-reference-id`);
  if (isIdentityFace && isRefSerial) {
    layers.push('reference-serial');
  }

  const serialDisplay = isRefSerial ? REFERENCE.serialLine : barcodeStr;
  const printSvg = `<svg class="credential-card__print" viewBox="0 0 450 282" role="img" aria-label="${escapeHtml(isIdentityFace ? t('badge.front_label') : t('badge.back_label'))}">${layers.map((id) => `<use href="${PRINT_ASSET}#${id}"></use>`).join('')}</svg>`;
  const barcodeSvg = buildBarcodeSvg(barcodeStr, `CODE-128B ${barcodeStr}`);

  const idGlyphCount = [...idCode].length;
  if (isIdentityFace) {
    const nameGlyphCount = [...name].length;
    const nameHasHan = /\p{Script=Han}/u.test(name);
    const nameWrap = nameGlyphCount > 6;
    const isRefPair = name === REFERENCE.name;
    const nameInner = isRefPair ? '马<span>兆</span>' : escapeHtml(name);

    const roleGlyphCount = [...role].length;
    const roleHasHan = /\p{Script=Han}/u.test(role);
    const idWrap = idGlyphCount > 12;

    return `
      <article class="credential-card" data-side="front" aria-label="${escapeHtml(t('badge.front_label'))}">
        ${printSvg}
        <div class="credential-card__identity credential-card__number" lang="en" title="${escapeHtml(idCode)}" style="--glyph-count:${idGlyphCount}"${idWrap ? ' data-wrap=""' : ''}${isRefId ? ' data-reference=""' : ''}>${escapeHtml(idCode)}</div>
        <div class="credential-card__name" title="${escapeHtml(name)}" data-glyph-count="${nameGlyphCount}" style="--glyph-count:${nameGlyphCount}"${nameHasHan ? ' data-han=""' : ''}${nameWrap ? ' data-wrap=""' : ''}${isRefPair ? ' data-reference-pair=""' : ''}>${nameInner}</div>
        <div class="credential-card__role" title="${escapeHtml(role)}" style="--glyph-count:${roleGlyphCount}"${roleHasHan ? ' data-han=""' : ''}>${escapeHtml(role)}</div>
        <div class="credential-card__serial" title="${escapeHtml(serialDisplay)}" style="--glyph-count:${[...serialDisplay].length}"${isRefSerial ? ' data-reference=""' : ''}>${escapeHtml(serialDisplay)}</div>
        ${barcodeSvg}
      </article>`;
  }

  return `
    <article class="credential-card" data-side="back" aria-label="${escapeHtml(t('badge.back_label'))}">
      ${printSvg}
      <div class="credential-card__stripe" lang="en" title="${escapeHtml(idCode)}" style="--glyph-count:${idGlyphCount + 5}"${isRefId ? ' data-reference=""' : ''}><span>IDL. <span class="credential-card__number">${escapeHtml(idCode)}</span></span></div>
      ${barcodeSvg}
    </article>`;
}

function safePortraitUrl(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  if (/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/i.test(s)) return s;
  if (s.startsWith('/') && !s.startsWith('//')) return s;
  return /^(?:https?:|blob:)/i.test(s) ? s : '';
}

/** Standard 1-Inch FUI Cutout Portrait (25×35mm) with Synchronized Top-to-Bottom Scan-to-Cutout Wipe. */
export function renderPortraitHtml(cfg, { compact = false, processing = false, pendingRawUrl = '', scanReveal = false } = {}) {
  const toneClass = cfg.photoArchiveTone ? ' moss-id-photo__img--archive' : '';
  const isCutout = cfg.photoCutout !== false;
  const cutoutClass = isCutout ? ' moss-id-photo__img--cutout' : '';
  const idCode = escapeHtml(cfg.idCode || DEFAULT_BADGE.idCode);
  const nameRaw = escapeHtml(cfg.name || DEFAULT_BADGE.name);

  // While MediaPipe micro-model is computing, display the original raw photo immediately (never a premature cutout!)
  if (processing) {
    const previewSrc = safePortraitUrl(pendingRawUrl || cfg.photoRawDataUrl || '');
    return `
      <div class="moss-id-photo moss-id-photo--busy${compact ? ' moss-id-photo--compact' : ''}">
        <div class="moss-id-photo__backdrop" aria-hidden="true"></div>
        ${previewSrc ? `<img class="moss-id-photo__img${toneClass}" src="${escapeHtml(previewSrc)}" alt="" aria-hidden="true">` : ''}
        <div class="moss-id-photo__busy-box">
          <span class="moss-id-photo__busy-text fui-num">${escapeHtml(t('badge.photo_cutting'))}</span>
          <div class="fui-progress" role="progressbar" aria-busy="true"></div>
        </div>
      </div>`;
  }

  const safePhoto = safePortraitUrl(cfg.photoDataUrl);
  if (safePhoto) {
    // Always clip-reveal top-to-bottom during scanReveal so cutout is NEVER shown before the laser beam passes!
    // When photoRawDataUrl is present, the raw original sits below the beam and is wiped away top-to-bottom.
    const safeRaw = safePortraitUrl(cfg.photoRawDataUrl);
    const hasRaw = isCutout && Boolean(safeRaw);
    return `
      <div class="moss-id-photo${compact ? ' moss-id-photo--compact' : ''}${scanReveal ? ' moss-id-photo--scanning' : ''}">
        <div class="moss-id-photo__backdrop" aria-hidden="true"></div>
        ${scanReveal && hasRaw ? `<img class="moss-id-photo__raw${toneClass}" src="${escapeHtml(safeRaw)}" alt="" aria-hidden="true">` : ''}
        <img class="moss-id-photo__img${cutoutClass}${toneClass}${scanReveal ? ' moss-id-photo__img--reveal' : ''}" src="${escapeHtml(safePhoto)}" alt="${nameRaw} · ${escapeHtml(t('badge.photo_label'))}">
        ${scanReveal ? '<span class="moss-id-photo__scanbeam" aria-hidden="true"></span>' : ''}
        <span class="moss-id-photo__reticle" aria-hidden="true"></span>
        <span class="moss-id-photo__stamp fui-num" aria-hidden="true">${compact ? (isCutout ? 'MATTE' : '1-IN') : `25×35MM · ${idCode}`}</span>
      </div>`;
  }

  return `
    <div class="moss-id-photo moss-id-photo--empty${compact ? ' moss-id-photo--compact' : ''}${scanReveal ? ' moss-id-photo--scanning' : ''}">
      <svg class="moss-id-photo__placeholder" viewBox="0 0 250 350" role="img" aria-label="${escapeHtml(t('badge.photo_empty'))}">
        <rect x="0" y="0" width="250" height="350" fill="#071a26"/>
        <g stroke="#1b3c4f" stroke-width="1" fill="none">
          <line x1="0" y1="70" x2="250" y2="70"/>
          <line x1="0" y1="140" x2="250" y2="140"/>
          <line x1="0" y1="210" x2="250" y2="210"/>
          <line x1="0" y1="280" x2="250" y2="280"/>
          <line x1="62" y1="0" x2="62" y2="350"/>
          <line x1="125" y1="0" x2="125" y2="350"/>
          <line x1="188" y1="0" x2="188" y2="350"/>
        </g>
        <g stroke="#4fd0e6" stroke-width="2" fill="none">
          <path d="M18 42 V18 H42"/>
          <path d="M208 18 H232 V42"/>
          <path d="M18 308 V332 H42"/>
          <path d="M208 332 H232 V308"/>
        </g>
        <circle cx="125" cy="126" r="46" fill="#18384a" stroke="#4fd0e6" stroke-width="1.5"/>
        <path d="M38 320 C42 238 78 200 125 200 C172 200 208 238 212 320 Z" fill="#122c3c" stroke="#4fd0e6" stroke-width="1.5"/>
        <text x="125" y="42" text-anchor="middle" fill="#8ea6b1" font-family="'Rajdhani', sans-serif" font-size="16" font-weight="600" letter-spacing="2">25×35MM</text>
        <text x="125" y="314" text-anchor="middle" fill="#dfeaf0" font-family="'Source Han Sans SC', sans-serif" font-size="17" font-weight="600">${escapeHtml(t('badge.photo_empty'))}</text>
      </svg>
      ${scanReveal ? '<span class="moss-id-photo__scanbeam" aria-hidden="true"></span>' : ''}
    </div>`;
}

export function createBadgeController({ onVerified, onSpeakGreeting, onOpenSettings, onChange, deferLocalCache = false } = {}) {
  const dlg = document.getElementById('dlg-badge');
  const stageEl = document.getElementById('badge-stage');
  const flipBtn = document.getElementById('badge-flip');
  const dualBtn = document.getElementById('badge-dual');
  const previewVoiceBtn = document.getElementById('badge-speak-test');
  const editBtn = document.getElementById('badge-edit');
  const verifyBtn = document.getElementById('badge-verify');
  const metaReadoutEl = document.getElementById('badge-meta-readout');
  const greetingLineEl = document.getElementById('badge-greeting-line');

  // Main-UI Right-Rail Live Dock + Settings Preview
  const railCardBtn = document.getElementById('rail-badge-card');
  const railPhotoBtn = document.getElementById('rail-badge-photo');
  const railSpeakBtn = document.getElementById('rail-badge-speak');
  const railEditBtn = document.getElementById('rail-badge-edit');
  const railMetaEl = document.getElementById('rail-badge-meta');
  const miniPreviewEl = document.getElementById('set-badge-preview');
  const miniPhotoPreviewBtn = document.getElementById('set-badge-photo-preview');
  const photoFileInput = document.getElementById('set-badge-photo-file');
  const photoUploadBtn = document.getElementById('set-badge-photo-upload');
  const photoClearBtn = document.getElementById('set-badge-photo-clear');
  const modelFileInput = document.getElementById('set-badge-model-file');
  const modelImportBtn = document.getElementById('set-badge-model-btn');
  const mattingStatusEl = document.getElementById('set-badge-matting-status');
  const sensValEl = document.getElementById('set-badge-sens-val');

  // Settings form fields
  const fields = {
    name: document.getElementById('set-badge-name'),
    latin: document.getElementById('set-badge-latin'),
    idCode: document.getElementById('set-badge-id'),
    serial: document.getElementById('set-badge-serial'),
    clearance: document.getElementById('set-badge-clearance'),
    dept: document.getElementById('set-badge-dept'),
    role: document.getElementById('set-badge-role'),
    customBarcode: document.getElementById('set-badge-barcode'),
    customGreeting: document.getElementById('set-badge-greeting'),
    photoCutout: document.getElementById('set-badge-photo-cutout'),
    photoSensitivity: document.getElementById('set-badge-photo-sens'),
    photoArchiveTone: document.getElementById('set-badge-photo-tone'),
    showAfterOpening: document.getElementById('set-badge-auto'),
    speakGreeting: document.getElementById('set-badge-speak'),
  };
  const resetBtn = document.getElementById('set-badge-reset');
  const openFromSettingsBtn = document.getElementById('set-badge-open');
  const speakFromSettingsBtn = document.getElementById('set-badge-speak-btn');

  let cfg = deferLocalCache ? { ...DEFAULT_BADGE } : loadConfig();
  let viewMode = 'dual'; // 'dual' | 'front' | 'back'
  let photoProcessing = false;
  let pendingRawPreviewUrl = '';
  let pendingScanReveal = true; // Always play top-to-bottom scan-to-cutout reveal on initial page load / refresh!
  let scanActiveUntil = 0;
  let latinAutoSynced = !cfg.latin || cfg.latin.toUpperCase() === toPinyinCode(cfg.name);
  let lastMattingEngine = getMattingStatus().engine;
  let recutTimer = 0;

  function syncMattingStatusText() {
    if (!mattingStatusEl) return;
    if (photoProcessing) {
      mattingStatusEl.dataset.state = 'warn';
      mattingStatusEl.textContent = t('badge.matting_running');
      return;
    }
    if (lastMattingEngine === 'mediapipe') {
      mattingStatusEl.dataset.state = 'ok';
      mattingStatusEl.textContent = t('badge.matting_neural');
    } else if (lastMattingEngine === 'alpha') {
      mattingStatusEl.dataset.state = 'ok';
      mattingStatusEl.textContent = t('badge.matting_alpha');
    } else if (lastMattingEngine === 'fallback') {
      mattingStatusEl.dataset.state = 'warn';
      mattingStatusEl.textContent = t('badge.matting_fallback');
    } else {
      mattingStatusEl.dataset.state = 'data';
      mattingStatusEl.textContent = t('badge.matting_ready');
    }
  }

  async function processPhotoSource(source) {
    if (!source) return;
    const isFile = typeof File !== 'undefined' && source instanceof File;
    const tempUrl = isFile ? URL.createObjectURL(source) : '';
    pendingRawPreviewUrl = tempUrl || (typeof source === 'string' ? source : '');
    photoProcessing = true;
    syncMattingStatusText();
    renderCards();
    try {
      const res = await runPortraitCutout(source, {
        enableCutout: cfg.photoCutout !== false,
        sensitivity: Number(cfg.photoSensitivity) || 50,
      });
      lastMattingEngine = res.engine;
      cfg = {
        ...cfg,
        photoRawDataUrl: res.rawDataUrl,
        photoDataUrl: cfg.photoCutout !== false ? res.cutoutDataUrl : res.rawDataUrl,
      };
      saveConfig(cfg);
    } catch { /* ignore invalid image */ }
    finally {
      if (tempUrl) URL.revokeObjectURL(tempUrl);
      pendingRawPreviewUrl = '';
      photoProcessing = false;
      pendingScanReveal = true;
      syncMattingStatusText();
      renderCards({ forceScan: true });
    }
  }

  function scheduleRecut() {
    if (!cfg.photoRawDataUrl) return;
    clearTimeout(recutTimer);
    recutTimer = setTimeout(() => processPhotoSource(cfg.photoRawDataUrl), 160);
  }

  function triggerPhotoUpload() {
    if (!photoFileInput) return;
    photoFileInput.value = '';
    photoFileInput.click();
  }

  function renderCards({ forceScan = false, preserveRunningScan = false, silent = false } = {}) {
    const runScan = Boolean(forceScan || pendingScanReveal) && !photoProcessing;
    if (runScan) {
      pendingScanReveal = false;
      scanActiveUntil = performance.now() + 1700;
    }
    const skipPhotoReplace = preserveRunningScan && !runScan && !photoProcessing && performance.now() < scanActiveUntil;

    const code = computeBarcodeString(cfg);
    const identityCardHtml = renderCredentialCardHtml(cfg, code, 'identity');
    const labCardHtml = renderCredentialCardHtml(cfg, code, 'lab');
    const photoFull = renderPortraitHtml(cfg, { compact: false, processing: photoProcessing, pendingRawUrl: pendingRawPreviewUrl, scanReveal: runScan });
    const photoCompact = renderPortraitHtml(cfg, { compact: true, processing: photoProcessing, pendingRawUrl: pendingRawPreviewUrl, scanReveal: runScan });

    if (stageEl && !skipPhotoReplace) {
      stageEl.dataset.view = viewMode;
      stageEl.innerHTML = `
        <div class="moss-id-slot moss-id-slot--photo">
          <span class="moss-id-slot__tag fui-num">PHOTO // 一寸抠图证件照</span>
          <button class="moss-id-photo-frame" type="button" data-badge-photo-trigger title="${escapeHtml(t('set.badge_photo_upload'))}">${photoFull}</button>
        </div>
        <div class="moss-id-slot moss-id-slot--front">
          <span class="moss-id-slot__tag fui-num">FRONT // 身份正面</span>
          <div class="moss-id-frame">${identityCardHtml}</div>
        </div>
        <div class="moss-id-slot moss-id-slot--back">
          <span class="moss-id-slot__tag fui-num">BACK // 实验室背面</span>
          <div class="moss-id-frame">${labCardHtml}</div>
        </div>`;
      stageEl.querySelector('[data-badge-photo-trigger]')?.addEventListener('click', triggerPhotoUpload);
    }
    if (railCardBtn) railCardBtn.innerHTML = identityCardHtml;
    if (railPhotoBtn && !skipPhotoReplace) railPhotoBtn.innerHTML = photoCompact;
    if (railMetaEl) railMetaEl.textContent = cfg.idCode;
    if (metaReadoutEl) metaReadoutEl.textContent = `CODE-128B · ${code} · IDL.${cfg.idCode} · ${cfg.name}`;
    if (greetingLineEl) {
      greetingLineEl.textContent = buildGreetingSpeech(cfg).replace(/\[pause[^\]]*\]/gi, ' ').replace(/\s+/g, ' ').trim();
    }
    if (fields.customGreeting) fields.customGreeting.placeholder = defaultGreetingTemplate();
    if (miniPreviewEl) miniPreviewEl.innerHTML = `<div class="moss-id-frame">${identityCardHtml}</div>`;
    if (miniPhotoPreviewBtn && !skipPhotoReplace) miniPhotoPreviewBtn.innerHTML = photoCompact;
    if (photoClearBtn) photoClearBtn.disabled = !cfg.photoDataUrl;
    if (sensValEl) sensValEl.textContent = String(cfg.photoSensitivity ?? 50);
    if (flipBtn) flipBtn.textContent = t(viewMode === 'back' ? 'badge.show_front' : 'badge.show_back');
    if (dualBtn) {
      dualBtn.setAttribute('aria-pressed', String(viewMode === 'dual'));
      dualBtn.textContent = t(viewMode === 'dual' ? 'badge.single_view' : 'badge.dual_view');
    }
    syncMattingStatusText();
    if (!silent) onChange?.({ ...cfg });
  }

  function syncSettingsInputs() {
    if (fields.name) fields.name.value = cfg.name;
    if (fields.latin) fields.latin.value = cfg.latin;
    if (fields.idCode) fields.idCode.value = cfg.idCode;
    if (fields.serial) fields.serial.value = cfg.serial;
    if (fields.clearance) fields.clearance.value = cfg.clearance;
    if (fields.dept) fields.dept.value = cfg.dept;
    if (fields.role) fields.role.value = cfg.role;
    if (fields.customBarcode) fields.customBarcode.value = cfg.customBarcode;
    if (fields.customGreeting) fields.customGreeting.value = cfg.customGreeting;
    if (fields.photoCutout) fields.photoCutout.checked = cfg.photoCutout !== false;
    if (fields.photoSensitivity) fields.photoSensitivity.value = String(cfg.photoSensitivity ?? 50);
    if (fields.photoArchiveTone) fields.photoArchiveTone.checked = Boolean(cfg.photoArchiveTone);
    if (fields.showAfterOpening) fields.showAfterOpening.checked = Boolean(cfg.showAfterOpening);
    if (fields.speakGreeting) fields.speakGreeting.checked = cfg.speakGreeting !== false;
  }

  function readSettingsInputs(ev) {
    const prevCutout = cfg.photoCutout;
    const prevSens = cfg.photoSensitivity;
    const rawName = fields.name?.value.trim() || DEFAULT_BADGE.name;
    let rawLatin = fields.latin?.value.trim() || '';

    // Auto-convert Chinese name -> uppercase Pinyin, and also auto-convert if Chinese is typed directly in Latin/Pinyin field
    if (ev?.target === fields.name) {
      if (latinAutoSynced || !rawLatin || hasChinese(rawLatin)) {
        const autoPy = toPinyinCode(rawName, { isPersonName: true });
        if (autoPy) {
          rawLatin = autoPy;
          if (fields.latin) fields.latin.value = autoPy;
          latinAutoSynced = true;
        }
      }
    } else if (ev?.target === fields.latin) {
      if (hasChinese(rawLatin)) {
        const converted = toPinyinCode(rawLatin, { isPersonName: true });
        if (converted) {
          rawLatin = converted;
          fields.latin.value = converted;
        }
      }
      latinAutoSynced = !rawLatin || rawLatin.toUpperCase() === toPinyinCode(rawName, { isPersonName: true });
    } else if (hasChinese(rawLatin)) {
      rawLatin = toPinyinCode(rawLatin, { isPersonName: true });
      if (fields.latin) fields.latin.value = rawLatin;
    }

    cfg = {
      ...cfg,
      name: rawName,
      latin: rawLatin || toPinyinCode(rawName, { isPersonName: true }) || DEFAULT_BADGE.latin,
      idCode: fields.idCode?.value.trim() || DEFAULT_BADGE.idCode,
      serial: fields.serial?.value.trim() || DEFAULT_BADGE.serial,
      clearance: fields.clearance?.value.trim() || DEFAULT_BADGE.clearance,
      dept: fields.dept?.value.trim() || DEFAULT_BADGE.dept,
      role: fields.role?.value.trim() || DEFAULT_BADGE.role,
      customBarcode: fields.customBarcode?.value.trim() || '',
      customGreeting: fields.customGreeting?.value.trim() || '',
      photoCutout: fields.photoCutout ? fields.photoCutout.checked : cfg.photoCutout,
      photoSensitivity: fields.photoSensitivity ? Number(fields.photoSensitivity.value) : cfg.photoSensitivity,
      photoArchiveTone: fields.photoArchiveTone ? fields.photoArchiveTone.checked : Boolean(cfg.photoArchiveTone),
      showAfterOpening: fields.showAfterOpening ? fields.showAfterOpening.checked : cfg.showAfterOpening,
      speakGreeting: fields.speakGreeting ? fields.speakGreeting.checked : cfg.speakGreeting,
    };
    saveConfig(cfg);
    renderCards();
    if ((cfg.photoCutout !== prevCutout || cfg.photoSensitivity !== prevSens) && cfg.photoRawDataUrl) {
      if (ev?.target === fields.photoCutout || ev?.target === fields.photoSensitivity) {
        scheduleRecut();
      }
    }
  }

  for (const el of Object.values(fields)) {
    if (!el) continue;
    const ev = el.type === 'checkbox' ? 'change' : 'input';
    el.addEventListener(ev, readSettingsInputs);
  }

  photoFileInput?.addEventListener('change', () => {
    const file = photoFileInput.files?.[0];
    if (file) processPhotoSource(file);
  });
  photoUploadBtn?.addEventListener('click', triggerPhotoUpload);
  miniPhotoPreviewBtn?.addEventListener('click', triggerPhotoUpload);
  miniPhotoPreviewBtn?.addEventListener('dragover', (e) => { e.preventDefault(); });
  miniPhotoPreviewBtn?.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) processPhotoSource(file);
  });
  photoClearBtn?.addEventListener('click', () => {
    cfg = { ...cfg, photoDataUrl: '', photoRawDataUrl: '' };
    saveConfig(cfg);
    renderCards();
  });

  modelImportBtn?.addEventListener('click', () => {
    if (!modelFileInput) return;
    modelFileInput.value = '';
    modelFileInput.click();
  });
  modelFileInput?.addEventListener('change', async () => {
    const file = modelFileInput.files?.[0];
    if (!file) return;
    photoProcessing = true;
    syncMattingStatusText();
    try {
      const st = await importLocalSegmenterModel(file);
      lastMattingEngine = st.engine;
      if (cfg.photoRawDataUrl && cfg.photoCutout !== false) {
        await processPhotoSource(cfg.photoRawDataUrl);
      }
    } catch { /* ignore */ }
    finally {
      photoProcessing = false;
      syncMattingStatusText();
    }
  });

  resetBtn?.addEventListener('click', () => {
    cfg = { ...DEFAULT_BADGE };
    latinAutoSynced = true;
    saveConfig(cfg);
    syncSettingsInputs();
    renderCards();
  });

  openFromSettingsBtn?.addEventListener('click', () => {
    const settingsDlg = document.getElementById('dlg-settings');
    if (settingsDlg?.open) settingsDlg.close();
    open();
  });
  speakFromSettingsBtn?.addEventListener('click', () => {
    onSpeakGreeting?.(buildGreetingSpeech(cfg), cfg);
  });

  railCardBtn?.addEventListener('click', () => open({ mode: 'dual' }));
  railPhotoBtn?.addEventListener('click', () => {
    if (cfg.photoDataUrl) open({ mode: 'dual' });
    else triggerPhotoUpload();
  });
  railSpeakBtn?.addEventListener('click', () => onSpeakGreeting?.(buildGreetingSpeech(cfg), cfg));
  railEditBtn?.addEventListener('click', () => {
    onOpenSettings?.();
    requestAnimationFrame(() => fields.name?.focus()); // fui-lint-ignore: R15 one-shot focus handoff to badge editor
  });

  flipBtn?.addEventListener('click', () => {
    viewMode = viewMode === 'back' ? 'front' : 'back';
    renderCards();
  });
  dualBtn?.addEventListener('click', () => {
    viewMode = viewMode === 'dual' ? 'front' : 'dual';
    renderCards();
  });
  previewVoiceBtn?.addEventListener('click', () => {
    onSpeakGreeting?.(buildGreetingSpeech(cfg), cfg);
  });
  editBtn?.addEventListener('click', () => {
    if (dlg?.open) dlg.close();
    onOpenSettings?.();
    requestAnimationFrame(() => fields.name?.focus()); // fui-lint-ignore: R15 one-shot focus handoff after dialog switch
  });
  verifyBtn?.addEventListener('click', () => {
    if (dlg?.open) dlg.close();
    const speech = buildGreetingSpeech(cfg);
    if (cfg.speakGreeting !== false) onSpeakGreeting?.(speech, cfg);
    onVerified?.(cfg, computeBarcodeString(cfg), speech);
  });

  function open({ mode = 'dual' } = {}) {
    if (!dlg) return;
    viewMode = mode;
    renderCards({ forceScan: true });
    if (!dlg.open) dlg.showModal();
  }

  syncSettingsInputs();
  renderCards();

  return {
    open,
    getConfig: () => ({ ...cfg }),
    applyExternalConfig(nextCfg, { forcedClearance = '', silent = true, storageScope = '' } = {}) {
      if (storageScope) {
        const cleanScope = String(storageScope).trim().toUpperCase();
        scopedStorageKey = !cleanScope || cleanScope === 'MOSS' || cleanScope === 'CS'
          ? STORAGE_KEY
          : `${STORAGE_KEY}.${cleanScope}`;
      }
      if (nextCfg && typeof nextCfg === 'object') {
        cfg = { ...DEFAULT_BADGE, ...nextCfg };
      } else if (storageScope) {
        let localSaved = null;
        try {
          const raw = localStorage.getItem(scopedStorageKey);
          if (raw) localSaved = JSON.parse(raw);
        } catch { /* ignore */ }
        if (localSaved && typeof localSaved === 'object') {
          cfg = { ...DEFAULT_BADGE, ...localSaved };
        } else {
          const cleanScope = String(storageScope).trim().toUpperCase();
          cfg = (!cleanScope || cleanScope === 'MOSS' || cleanScope === 'CS')
            ? { ...DEFAULT_BADGE }
            : {
                ...DEFAULT_BADGE,
                name: cleanScope,
                latin: cleanScope,
                idCode: `OP-${cleanScope}`,
                serial: cleanScope,
                role: 'OPERATOR',
                clearance: forcedClearance || 'C',
                photoDataUrl: '',
                photoRawDataUrl: '',
              };
        }
      }
      if (forcedClearance) cfg.clearance = forcedClearance;
      saveConfig(cfg);
      syncSettingsInputs();
      renderCards({ preserveRunningScan: true, silent });
    },
    isShowAfterOpening: () => Boolean(cfg.showAfterOpening),
    refreshI18n: () => renderCards({ preserveRunningScan: true }),
  };
}
