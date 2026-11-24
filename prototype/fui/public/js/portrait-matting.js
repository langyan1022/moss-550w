/**
 * Micro-Model Portrait Cutout Engine (1-Inch FUI Biometric Cutout).
 *
 * Primary Engine: Local MediaPipe Vision WASM runtime (`/conversation-assets/hand-gesture/`)
 *   + MediaPipe `ImageSegmenter` running the 249 KB `selfie_segmenter.tflite` neural micro-model.
 *   Model bytes are loaded from:
 *     1) Server local cache `/fui/model/selfie_segmenter.tflite`
 *     2) Browser CacheStorage (`moss-fui-models-v1`)
 *     3) Official MediaPipe float16 model URL (cached automatically for offline reuse)
 *     4) Optional user-imported local `.tflite` file (saved to CacheStorage)
 *
 * Offline Fallback: Border-connected perceptual flood-fill + 2px anti-aliased edge feathering
 *   & color-spill suppression so ID photos still cut out cleanly even in air-gapped environments.
 */

const WASM_ROOT = '/conversation-assets/hand-gesture/wasm';
const VISION_BUNDLE_URL = '/conversation-assets/hand-gesture/vision_bundle.mjs';
const SERVER_MODEL_URL = '/fui/model/selfie_segmenter.tflite';
const CDN_MODEL_URL = 'https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite';
const CACHE_NAME = 'moss-fui-models-v1';
const CACHE_KEY = '/__local_cache__/selfie_segmenter.tflite';

let segmenterPromise = null;
let segmenterInstance = null;
let engineStatus = {
  engine: 'idle', // 'idle' | 'loading' | 'mediapipe' | 'fallback'
  detail: '',
};

export function getMattingStatus() {
  return { ...engineStatus };
}

async function readModelFromCache() {
  if (typeof caches === 'undefined') return null;
  try {
    const cache = await caches.open(CACHE_NAME);
    const resp = await cache.match(CACHE_KEY);
    if (resp && resp.ok) return new Uint8Array(await resp.arrayBuffer());
  } catch { /* ignore */ }
  return null;
}

async function writeModelToCache(bytes) {
  if (typeof caches === 'undefined' || !bytes?.byteLength) return;
  try {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(CACHE_KEY, new Response(bytes, {
      headers: { 'Content-Type': 'application/octet-stream' },
    }));
  } catch { /* ignore */ }
}

/** Allow operator to load a local .tflite micro-model file into browser CacheStorage (for air-gapped networks). */
export async function importLocalSegmenterModel(file) {
  const buf = new Uint8Array(await file.arrayBuffer());
  if (buf.byteLength < 1024) throw new Error('Invalid TFLite model file');
  await writeModelToCache(buf);
  // Reset cached segmenter so next cutout initializes immediately with imported model
  if (segmenterInstance) {
    try { segmenterInstance.close?.(); } catch { /* ignore */ }
    segmenterInstance = null;
  }
  segmenterPromise = null;
  await ensureNeuralSegmenter();
  return getMattingStatus();
}

async function fetchModelBytes() {
  // 1. Server-hosted local model (instant, offline-safe if downloaded)
  try {
    const r = await fetch(SERVER_MODEL_URL, { cache: 'force-cache' });
    if (r.ok) {
      const buf = new Uint8Array(await r.arrayBuffer());
      if (buf.byteLength > 10000) {
        await writeModelToCache(buf);
        return buf;
      }
    }
  } catch { /* try next */ }

  // 2. Browser CacheStorage
  const cached = await readModelFromCache();
  if (cached && cached.byteLength > 10000) return cached;

  // 3. Direct browser download of official 249 KB MediaPipe SelfieSegmenter float16 micro-model
  const ctrl = new AbortController();
  const tm = setTimeout(() => ctrl.abort(), 9000);
  try {
    const r = await fetch(CDN_MODEL_URL, { mode: 'cors', signal: ctrl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const buf = new Uint8Array(await r.arrayBuffer());
    if (buf.byteLength > 10000) {
      await writeModelToCache(buf);
      return buf;
    }
  } finally {
    clearTimeout(tm);
  }
  throw new Error('Segmenter model unavailable');
}

export async function ensureNeuralSegmenter() {
  if (segmenterInstance) return segmenterInstance;
  if (segmenterPromise) return segmenterPromise;

  engineStatus = { engine: 'loading', detail: 'MediaPipe SelfieSegmenter (WASM)' };
  segmenterPromise = (async () => {
    try {
      const [visionMod, modelBytes] = await Promise.all([
        import(VISION_BUNDLE_URL),
        fetchModelBytes(),
      ]);
      const { FilesetResolver, ImageSegmenter } = visionMod;
      if (!FilesetResolver || !ImageSegmenter) throw new Error('ImageSegmenter export missing');

      const fileset = await FilesetResolver.forVisionTasks(WASM_ROOT);
      const seg = await ImageSegmenter.createFromOptions(fileset, {
        baseOptions: {
          modelAssetBuffer: modelBytes,
          delegate: 'CPU',
        },
        runningMode: 'IMAGE',
        outputCategoryMask: false,
        outputConfidenceMasks: true,
      });
      segmenterInstance = seg;
      engineStatus = { engine: 'mediapipe', detail: 'MediaPipe SelfieSegmenter · 249KB WASM' };
      return seg;
    } catch (err) {
      segmenterPromise = null;
      engineStatus = { engine: 'fallback', detail: err?.message || 'offline' };
      return null;
    }
  })();
  return segmenterPromise;
}

/** Load an image (File or DataURL) and crop cleanly into standard 1-inch portrait (240×336, exact 25:35). */
export function cropOneInchCanvas(source, width = 240, height = 336) {
  return new Promise((resolve, reject) => {
    const isFile = typeof File !== 'undefined' && source instanceof File;
    const url = isFile ? URL.createObjectURL(source) : String(source || '');
    if (!url) {
      reject(new Error('Empty image source'));
      return;
    }
    const img = new Image();
    img.onload = () => {
      try {
        const targetRatio = width / height;
        const srcW = img.naturalWidth || img.width;
        const srcH = img.naturalHeight || img.height;
        const srcRatio = srcW / srcH;

        let sx = 0; let sy = 0; let sw = srcW; let sh = srcH;
        if (srcRatio > targetRatio) {
          sw = srcH * targetRatio;
          sx = (srcW - sw) * 0.5;
        } else {
          sh = srcW / targetRatio;
          sy = (srcH - sh) * 0.20; // Bias upward so head/hair stays framed cleanly
        }

        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.clearRect(0, 0, width, height);
        ctx.drawImage(img, sx, sy, sw, sh, 0, 0, width, height);
        resolve(canvas);
      } catch (err) {
        reject(err);
      } finally {
        if (isFile) URL.revokeObjectURL(url);
      }
    };
    img.onerror = () => {
      if (isFile) URL.revokeObjectURL(url);
      reject(new Error('Failed to decode image'));
    };
    img.src = url;
  });
}

function hasExistingTransparency(imgData) {
  const d = imgData.data;
  let transparent = 0;
  const total = d.length / 4;
  for (let i = 3; i < d.length; i += 16) {
    if (d[i] < 220) transparent++;
  }
  return transparent / (total / 4) > 0.06;
}

/** Neural MediaPipe Confidence Mask -> Smooth Anti-Aliased Transparent PNG + Edge Spill Suppression. */
function applyNeuralConfidenceMask(canvas, maskFloat, maskW, maskH, sensitivity = 50) {
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const w = canvas.width;
  const h = canvas.height;
  const imgData = ctx.getImageData(0, 0, w, h);
  const d = imgData.data;

  // Map sensitivity (15..85) to foreground midpoint threshold (0.28..0.72)
  const mid = 0.20 + (Math.min(85, Math.max(15, sensitivity)) / 100) * 0.62;
  const band = 0.14;

  // Estimate dominant outer border color for subtle color-spill suppression on semi-transparent hair edges
  let bgR = 0; let bgG = 0; let bgB = 0; let bgCount = 0;
  for (let x = 0; x < w; x += 2) {
    const idx = x * 4;
    bgR += d[idx]; bgG += d[idx + 1]; bgB += d[idx + 2]; bgCount++;
  }
  bgR /= Math.max(1, bgCount);
  bgG /= Math.max(1, bgCount);
  bgB /= Math.max(1, bgCount);

  for (let y = 0; y < h; y++) {
    const my = Math.min(maskH - 1, (y / h) * maskH);
    const y0 = Math.floor(my);
    const y1 = Math.min(maskH - 1, y0 + 1);
    const fy = my - y0;

    for (let x = 0; x < w; x++) {
      const mx = Math.min(maskW - 1, (x / w) * maskW);
      const x0 = Math.floor(mx);
      const x1 = Math.min(maskW - 1, x0 + 1);
      const fx = mx - x0;

      // Bilinear interpolation of neural confidence mask for razor-smooth curves
      const m00 = maskFloat[y0 * maskW + x0];
      const m10 = maskFloat[y0 * maskW + x1];
      const m01 = maskFloat[y1 * maskW + x0];
      const m11 = maskFloat[y1 * maskW + x1];
      const prob = (m00 * (1 - fx) + m10 * fx) * (1 - fy) + (m01 * (1 - fx) + m11 * fx) * fy;

      // Smoothstep transition around threshold
      const t = Math.min(1, Math.max(0, (prob - (mid - band)) / (band * 2)));
      const alpha = t * t * (3 - 2 * t);
      const p = (y * w + x) * 4;

      if (alpha < 0.02) {
        d[p + 3] = 0;
      } else {
        // Gentle edge de-fringing for semi-transparent rim pixels
        if (alpha < 0.92) {
          const inv = 1 - alpha;
          d[p]     = Math.max(0, Math.min(255, Math.round((d[p]     - bgR * inv * 0.65) / Math.max(0.35, alpha))));
          d[p + 1] = Math.max(0, Math.min(255, Math.round((d[p + 1] - bgG * inv * 0.65) / Math.max(0.35, alpha))));
          d[p + 2] = Math.max(0, Math.min(255, Math.round((d[p + 2] - bgB * inv * 0.65) / Math.max(0.35, alpha))));
        }
        d[p + 3] = Math.round(alpha * 255);
      }
    }
  }
  ctx.putImageData(imgData, 0, 0);
}

/** Offline Fallback: Border-Connected Perceptual Flood-Fill + 2px Soft Edge Feathering. */
function applyBorderFloodCutout(canvas, sensitivity = 50) {
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const w = canvas.width;
  const h = canvas.height;
  const imgData = ctx.getImageData(0, 0, w, h);
  const d = imgData.data;

  // Sample median-like average from top & upper-left/right borders (where studio backdrop sits)
  let rSum = 0; let gSum = 0; let bSum = 0; let cnt = 0;
  const addSample = (x, y) => {
    const i = (y * w + x) * 4;
    rSum += d[i]; gSum += d[i + 1]; bSum += d[i + 2]; cnt++;
  };
  for (let x = 0; x < w; x += 2) { addSample(x, 1); addSample(x, 3); }
  for (let y = 2; y < Math.floor(h * 0.68); y += 2) { addSample(1, y); addSample(w - 2, y); }
  const bgR = rSum / cnt;
  const bgG = gSum / cnt;
  const bgB = bSum / cnt;

  const dist = (i) => {
    const dr = d[i] - bgR;
    const dg = d[i + 1] - bgG;
    const db = d[i + 2] - bgB;
    return Math.sqrt(2 * dr * dr + 4 * dg * dg + 3 * db * db) / 3;
  };

  const thresh = 16 + (Math.min(90, Math.max(15, sensitivity)) / 100) * 68;
  const softMax = thresh * 1.48;
  const bgMask = new Uint8Array(w * h); // 1 = background
  const queue = new Int32Array(w * h);
  let qHead = 0; let qTail = 0;

  const enqueue = (x, y) => {
    const p = y * w + x;
    if (bgMask[p]) return;
    if (dist(p * 4) <= thresh * 1.15) {
      bgMask[p] = 1;
      queue[qTail++] = p;
    }
  };

  for (let x = 0; x < w; x++) enqueue(x, 0);
  for (let y = 0; y < Math.floor(h * 0.85); y++) { enqueue(0, y); enqueue(w - 1, y); }

  while (qHead < qTail) {
    const p = queue[qHead++];
    const x = p % w;
    const y = (p - x) / w;
    if (x > 0) {
      const np = p - 1;
      if (!bgMask[np] && dist(np * 4) <= thresh) { bgMask[np] = 1; queue[qTail++] = np; }
    }
    if (x < w - 1) {
      const np = p + 1;
      if (!bgMask[np] && dist(np * 4) <= thresh) { bgMask[np] = 1; queue[qTail++] = np; }
    }
    if (y > 0) {
      const np = p - w;
      if (!bgMask[np] && dist(np * 4) <= thresh) { bgMask[np] = 1; queue[qTail++] = np; }
    }
    if (y < h - 1) {
      const np = p + w;
      if (!bgMask[np] && dist(np * 4) <= thresh) { bgMask[np] = 1; queue[qTail++] = np; }
    }
  }

  // Soft edge feathering for pixels adjacent to flood-filled background
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const p = y * w + x;
      const i = p * 4;
      if (bgMask[p]) {
        d[i + 3] = 0;
        continue;
      }
      let nearBg = 0;
      for (let dy = -2; dy <= 2; dy++) {
        const ny = y + dy;
        if (ny < 0 || ny >= h) continue;
        for (let dx = -2; dx <= 2; dx++) {
          const nx = x + dx;
          if (nx >= 0 && nx < w && bgMask[ny * w + nx]) nearBg++;
        }
      }
      if (nearBg > 0) {
        const cd = dist(i);
        if (cd < softMax) {
          const a = Math.min(1, Math.max(0.08, (cd - thresh * 0.65) / (softMax - thresh * 0.65)));
          d[i + 3] = Math.round(a * 255);
        }
      }
    }
  }
  ctx.putImageData(imgData, 0, 0);
}

/**
 * Full 1-Inch Portrait Cutout Pipeline.
 * Returns { rawDataUrl, cutoutDataUrl, engine, detail }.
 */
export async function runPortraitCutout(source, { enableCutout = true, sensitivity = 50 } = {}) {
  const canvas = await cropOneInchCanvas(source, 240, 336);
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const rawImg = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const alreadyCutout = hasExistingTransparency(rawImg);
  const rawDataUrl = alreadyCutout ? canvas.toDataURL('image/png') : canvas.toDataURL('image/jpeg', 0.88);

  if (!enableCutout || alreadyCutout) {
    return {
      rawDataUrl,
      cutoutDataUrl: alreadyCutout ? rawDataUrl : canvas.toDataURL('image/png'),
      engine: alreadyCutout ? 'alpha' : 'raw',
      detail: alreadyCutout ? 'PNG Alpha Preserved' : 'Original Photo',
    };
  }

  // Tier 1: Real MediaPipe Neural SelfieSegmenter Micro-Model (WASM)
  const seg = await ensureNeuralSegmenter();
  if (seg) {
    try {
      const result = seg.segment(canvas);
      const masks = result?.confidenceMasks || [];
      if (masks.length > 0) {
        // SelfieSegmenter returns 1 foreground mask (or [bg, fg] if 2-class)
        const fgMask = masks.length >= 2 ? masks[1] : masks[0];
        const arr = fgMask.getAsFloat32Array();
        const mw = fgMask.width;
        const mh = fgMask.height;
        // Detect if single mask is inverted (higher mean on top corners than center torso)
        const cornerAvg = (arr[0] + arr[mw - 1] + arr[mw * 2]) / 3;
        const centerAvg = arr[Math.floor(mh * 0.55) * mw + Math.floor(mw * 0.5)];
        const oriented = cornerAvg > centerAvg + 0.15
          ? Float32Array.from(arr, (v) => 1 - v)
          : arr;
        applyNeuralConfidenceMask(canvas, oriented, mw, mh, sensitivity);
        result.close?.();
        return {
          rawDataUrl,
          cutoutDataUrl: canvas.toDataURL('image/png'),
          engine: 'mediapipe',
          detail: engineStatus.detail,
        };
      }
      result?.close?.();
    } catch { /* fall through to offline edge matting */ }
  }

  // Tier 2: Seamless Offline Edge-Connected Matting Fallback
  applyBorderFloodCutout(canvas, sensitivity);
  return {
    rawDataUrl,
    cutoutDataUrl: canvas.toDataURL('image/png'),
    engine: 'fallback',
    detail: 'Local Edge Matting (Offline)',
  };
}
