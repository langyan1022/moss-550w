/**
 * Off-main-thread Web Worker running MediaPipe HandLandmarker inference on ImageBitmaps.
 */
let tracker = null;

self.addEventListener('message', async (ev) => {
  const msg = ev.data || {};
  if (msg.type === 'init') {
    try {
      const assets = msg.assets || '/conversation-assets/hand-gesture';
      const { FilesetResolver, HandLandmarker } = await import(`${assets}/vision_bundle.mjs`);
      const files = await FilesetResolver.forVisionTasks(`${assets}/wasm`);
      tracker = await HandLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: `${assets}/hand_landmarker.task`, delegate: 'CPU' },
        runningMode: 'VIDEO',
        numHands: 1,
      });
      self.postMessage({ type: 'ready' });
    } catch (err) {
      self.postMessage({ type: 'error', message: err?.message || 'worker_init_failed' });
    }
    return;
  }

  if (msg.type === 'frame') {
    const bitmap = msg.bitmap;
    if (!tracker || !bitmap) {
      try { bitmap?.close?.(); } catch { /* ignore */ }
      self.postMessage({ type: 'landmarks', hand: null });
      return;
    }
    try {
      const detected = tracker.detectForVideo(bitmap, msg.ts || performance.now());
      const hand = detected?.landmarks?.[0] || null;
      self.postMessage({ type: 'landmarks', hand });
    } catch (err) {
      self.postMessage({ type: 'fatal', message: err?.message || 'worker_detect_failed' });
    } finally {
      try { bitmap.close(); } catch { /* ignore */ }
    }
    return;
  }

  if (msg.type === 'close') {
    try { tracker?.close?.(); } catch { /* ignore */ }
    tracker = null;
  }
});
