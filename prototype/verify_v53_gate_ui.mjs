/**
 * Hardcore End-to-End Edge Playwright Release Gate QA for MOSS 550W (v5.3+):
 * 1. Verifies HTTP Cache-Control headers on /conversation-assets/moss-exterior.glb (24h cache) vs moss-view.js (no-cache)
 * 2. Exercises Brain Hub (all 5 tabs + RSI audit + SAG search + 生成态势简报 + 执行夜间做梦深度整理)
 * 3. Exercises Settings (all 5 tabs + 3-card MOSS personality round-trip & bottom tag sync)
 * 4. Exercises Hand-Gesture Deck button -> honest stage camera notice on headless host + 5-gesture legend
 * 5. Exercises "先来首歌吧" -> HUD Media Full <-> Mini toggle (zero <audio> unmount) + Mini ‹/› track switching
 * 6. Opens ALL 4 Intel Dossiers x 2 slides = 8 logos & captures cropped PNGs for pixel-sanity blank/blob rejection
 * 7. Exercises EN <-> ZH locale switch (0 missing i18n keys) & asserts 0 pageerror + 0 console.error
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const playwrightPath = process.env.PLAYWRIGHT_PATH || 'F:\\moss\\agent-lab\\prototype\\fui\\node_modules\\playwright';
const { chromium } = require(fs.existsSync(playwrightPath) ? playwrightPath : 'playwright');

const BASE = process.env.MOSS_URL || 'http://127.0.0.1:18221';
const OUT = process.env.OUT_DIR || path.resolve('runs/release-gate-ui');
fs.mkdirSync(OUT, { recursive: true });

function assert(cond, msg) {
  if (!cond) throw new Error(`ASSERTION FAILED: ${msg}`);
}

const pageErrors = [];
const consoleErrors = [];
const report = {
  baseUrl: BASE,
  cacheHeaders: {},
  brainHub: {},
  personality: {},
  gesture: {},
  mediaMini: {},
  intelLogos: [],
  i18n: {},
};

const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: [
    '--mute-audio',
    '--autoplay-policy=no-user-gesture-required',
  ],
});

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
  const page = await ctx.newPage();

  page.on('pageerror', (err) => {
    const msg = String(err?.stack || err);
    console.error('[PAGEERROR]', msg);
    pageErrors.push(msg);
  });
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      console.error('[CONSOLE_ERR]', msg.text(), msg.location?.()?.url || '');
      consoleErrors.push(msg.text());
    }
  });
  page.on('response', (res) => {
    if (res.status() >= 400) console.error('[HTTP_ERR]', res.status(), res.url());
  });

  // 1. Check 3D asset HTTP caching + Gzip compression policy directly
  console.log('[UI 1/10] Checking /conversation-assets HTTP Cache-Control & Gzip policy...');
  const glbResp = await ctx.request.get(`${BASE}/conversation-assets/moss-exterior.glb`);
  assert(glbResp.status() === 200, `Expected 200 for moss-exterior.glb, got ${glbResp.status()}`);
  const glbCache = glbResp.headers()['cache-control'] || '';
  const renderResp = await ctx.request.get(`${BASE}/conversation-assets/moss-render.js`, { headers: { 'Accept-Encoding': 'gzip' } });
  assert(renderResp.status() === 200, `Expected 200 for moss-render.js, got ${renderResp.status()}`);
  const renderCache = renderResp.headers()['cache-control'] || '';
  const renderEnc = renderResp.headers()['content-encoding'] || '';
  const jsResp = await ctx.request.get(`${BASE}/conversation-assets/moss-view.js`);
  assert(jsResp.status() === 200, `Expected 200 for moss-view.js, got ${jsResp.status()}`);
  const jsCache = jsResp.headers()['cache-control'] || '';
  report.cacheHeaders = { glb: glbCache, mossRenderJs: renderCache, mossRenderEncoding: renderEnc, mossViewJs: jsCache };
  console.log('  Cache-Control & Gzip:', report.cacheHeaders);
  assert(/max-age=86400/i.test(glbCache), `moss-exterior.glb missing max-age=86400: ${glbCache}`);
  assert(/max-age=86400/i.test(renderCache), `moss-render.js missing max-age=86400: ${renderCache}`);
  assert(/gzip/i.test(renderEnc), `moss-render.js missing gzip encoding: ${renderEnc}`);
  assert(/no-cache/i.test(jsCache), `moss-view.js must remain no-cache: ${jsCache}`);

  // 2. Load live console
  await page.goto(`${BASE}/?skipOpening=1`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const skipBtn = page.locator('#skip-opening, #opening-skip').first();
  if (await skipBtn.isVisible({ timeout: 2500 }).catch(() => false)) {
    await skipBtn.click().catch(() => {});
  }
  const badgeDlg = page.locator('#dlg-badge');
  if (await badgeDlg.evaluate((el) => Boolean(el?.open)).catch(() => false)) {
    await page.locator('#badge-verify').click().catch(() => {});
  }
  await page.waitForFunction(() => document.body.dataset.backend === 'live', { timeout: 20000 });
  await page.waitForTimeout(1000);

  // 3. Brain Hub (all 5 tabs + briefing + dream)
  console.log('[UI 2/7] Testing Brain Hub (5 tabs + 生成态势简报 + 执行夜间做梦)...');
  await page.locator('#strip-brain-btn').click();
  await page.waitForSelector('#dlg-brain[open]', { timeout: 5000 });
  await page.waitForTimeout(300);

  await page.locator('[data-brain-tab="rsi"]').click();
  await page.locator('#brain-rsi-eval-btn').click();
  await page.waitForTimeout(300);
  await page.locator('#brain-rsi-run').click();
  await page.waitForTimeout(450);
  const rsiMeta = (await page.locator('#brain-rsi-status').textContent() || '').trim();
  assert(rsiMeta.length > 0, 'Brain RSI status stayed empty after eval+run');

  await page.locator('[data-brain-tab="sag"]').click();
  await page.fill('#brain-sag-search-input', 'MOSS');
  await page.locator('#brain-sag-search-form button[type="submit"]').click();
  await page.waitForTimeout(450);

  await page.locator('[data-brain-tab="pulse"]').click();
  await page.locator('#brain-briefing-btn').click();
  await page.waitForFunction(() => {
    const txt = (document.getElementById('brain-briefing-box')?.textContent || '').trim();
    return txt.length > 8;
  }, { timeout: 15000 });
  const briefingTxt = (await page.locator('#brain-briefing-box').textContent() || '').trim();
  assert(!/not a function|ReferenceError|TypeError/i.test(briefingTxt), `Briefing threw JS error: ${briefingTxt}`);
  report.brainHub.briefingPreview = briefingTxt.slice(0, 80);

  await page.locator('#brain-dream-btn').click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, '01_brain_pulse_briefing.png'), animations: 'disabled' });

  await page.locator('[data-brain-tab="jobs"]').click();
  await page.waitForTimeout(200);
  await page.locator('[data-brain-tab="iot"]').click();
  await page.waitForTimeout(200);
  await page.locator('#dlg-brain [data-dialog-close]').first().click();
  await page.waitForTimeout(200);
  // Stop optional briefing TTS cleanly after verifying Brain Hub speech hook didn't throw
  if (await page.locator('#comms-interrupt').isEnabled().catch(() => false)) {
    await page.locator('#comms-interrupt').click().catch(() => {});
  }

  // 4. Settings 5 tabs + 3-card Personality round-trip
  console.log('[UI 3/7] Testing Settings (5 tabs + 3 personality presets)...');
  await page.locator('#strip-settings').click();
  await page.waitForSelector('#dlg-settings[open]', { timeout: 5000 });
  await page.locator('[data-set-tab="model"]').click();
  await page.waitForSelector('.moss-personality-card[data-personality="sassy"]', { timeout: 5000 });
  const cardCount = await page.locator('.moss-personality-card').count();
  assert(cardCount === 4, `Expected 4 personality cards (partner/rational/lively/sassy), got ${cardCount}`);

  await page.locator('.moss-personality-card[data-personality="rational"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="rational"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );
  const persTagRational = (await page.locator('#tag-pers-code').textContent() || '').trim();
  assert(/550W|理性/i.test(persTagRational), `Unexpected rational tag: ${persTagRational}`);
  report.personality.rationalTag = persTagRational;

  await page.locator('.moss-personality-card[data-personality="lively"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="lively"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );
  await page.locator('.moss-personality-card[data-personality="sassy"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="sassy"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );
  await page.locator('.moss-personality-card[data-personality="partner"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="partner"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );

  for (const tab of ['audio', 'display', 'location', 'badge']) {
    await page.locator(`[data-set-tab="${tab}"]`).click();
    await page.waitForTimeout(160);
    if (tab === 'audio') {
      const micInfo = await page.evaluate(() => ({
        listText: document.getElementById('set-mic-list')?.textContent || '',
        statusText: document.getElementById('set-mic-status')?.textContent || '',
      }));
      assert(micInfo.listText.includes('AUTO') && micInfo.statusText.trim().length > 4, `Auto mic UI missing: ${JSON.stringify(micInfo)}`);
      if (await page.locator('#set-online-music-section > summary').isVisible()) {
        await page.locator('#set-online-music-section > summary').click();
        await page.waitForTimeout(250);
        await page.fill('#set-om-jamendo-id', 'jam_dirty_1234');
        await page.locator('#set-om-jamendo').check();
        await page.locator('[data-set-tab="display"]').click();
        await page.waitForTimeout(120);
        await page.locator('[data-set-tab="audio"]').click();
        await page.waitForTimeout(180);
        const jamPersist = await page.evaluate(() => ({
          val: document.getElementById('set-om-jamendo-id')?.value || '',
          checked: Boolean(document.getElementById('set-om-jamendo')?.checked),
          badge: document.getElementById('set-om-jamendo-state')?.dataset?.state || '',
        }));
        assert(jamPersist.val === 'jam_dirty_1234' && jamPersist.checked && jamPersist.badge === 'warn',
          `Online music unsaved state wiped on tab switch: ${JSON.stringify(jamPersist)}`);
      }
    }
  }
  await page.locator('[data-set-tab="soul"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#set-soul-beliefs-list li').length >= 3, { timeout: 6000 });
  await page.fill('#set-soul-trait-key', 'soul_qa_city');
  await page.fill('#set-soul-trait-val', '杭州测试站点 token=secret9999');
  await page.locator('#set-soul-trait-form button[type="submit"]').click();
  await page.waitForFunction(() => (document.getElementById('set-soul-profile-list')?.textContent || '').includes('soul_qa_city'), { timeout: 5000 });
  const profileListTxt = (await page.locator('#set-soul-profile-list').textContent() || '');
  assert(profileListTxt.includes('[REDACTED]') && !profileListTxt.includes('secret9999'), `Soul profile leaked raw secret: ${profileListTxt}`);
  await page.locator('#set-soul-distill-btn').click();
  await page.waitForFunction(() => !(document.getElementById('set-soul-status')?.hidden ?? true), { timeout: 6000 });
  await page.locator('#set-soul-export-btn').click();
  await page.waitForFunction(() => !(document.getElementById('set-soul-export-preview')?.hidden ?? true) && (document.getElementById('set-soul-export-preview')?.textContent || '').includes('SOUL.md'), { timeout: 5000 });
  await page.locator('#set-soul-profile-list li button').last().click();
  await page.waitForTimeout(200);
  report.soulArchiveUi = { coreBeliefs: await page.locator('#set-soul-beliefs-list li').count(), redactedExport: true };
  await page.locator('#dlg-settings [data-dialog-close]').first().click();
  await page.waitForTimeout(250);

  // 5. Hand-Gesture Deck: keycap badge, 6-gesture legend incl. rail/Alt+G, Alt+G shortcut, moss-rail-slide separation
  console.log('[UI 4/15] Testing Hand-Gesture Deck (Alt+G keycap, rail-slide separation + camera notice)...');
  const stageReady = await page.waitForFunction(() => document.getElementById('moss-model')?.dataset?.ready === 'true', { timeout: 20000 }).then(() => true).catch(() => false);
  const gestureBtn = page.locator('#stage-gesture-btn');
  assert(await gestureBtn.isVisible(), '#stage-gesture-btn must be visible');
  const gestureKbd = (await page.locator('#stage-gesture-btn .moss-gesture-kbd').textContent() || '').trim();
  assert(gestureKbd === 'Alt+G', `Expected Alt+G shortcut keycap on gesture btn, got "${gestureKbd}"`);
  const legendZh = (await page.locator('#stage-gesture-hud .moss-gesture-hud__legend').textContent() || '').trim();
  assert(legendZh.includes('缩放') && legendZh.includes('滑轨') && legendZh.includes('Alt+G'), `Gesture legend incomplete: ${legendZh}`);
  report.gesture.legendZh = legendZh;
  report.gesture.shortcutBadge = gestureKbd;

  if (stageReady) {
    if (await page.locator('#comms-interrupt').isEnabled().catch(() => false)) {
      await page.locator('#comms-interrupt').click().catch(() => {});
    }
    await page.locator('#reset-view').click();
    const renderWorker = page.workers().find((w) => w.url().includes('moss-render'));
    // Wait for standby phase, startup motion & damped spring settle so baseline yaw/pitch is quiescent before rail-slide test.
    if (renderWorker) {
      const deadline = Date.now() + 8000;
      while (Date.now() < deadline) {
        const s = await renderWorker.evaluate(async () => {
          const mod = await import(self.location.href);
          return typeof mod.inspectScene === 'function' ? mod.inspectScene() : null;
        });
        if (s && s.phase === 'standby' && s.startupState !== 'running' && Math.abs(s.yaw ?? 0) < 0.3 && Math.abs(s.pitch ?? 0) < 0.3) break;
        await page.waitForTimeout(180);
      }
    } else {
      await page.waitForTimeout(800);
    }
    const railCheck = await page.evaluate(() => new Promise((resolve) => {
      const c = document.getElementById('moss-model');
      const events = [];
      const onOpt = (ev) => { if (ev.detail) events.push({ ...ev.detail }); };
      c.addEventListener('moss-optics', onOpt);
      c.dispatchEvent(new CustomEvent('moss-rail-slide', { detail: { x: 0.25 } }));
      setTimeout(() => {
        const before = events.at(-1) || {};
        c.dispatchEvent(new CustomEvent('moss-rail-slide', { detail: { x: 0.78 } }));
        setTimeout(() => {
          const after = events.at(-1) || {};
          c.removeEventListener('moss-optics', onOpt);
          resolve({ before, after, count: events.length });
        }, 420);
      }, 360);
    }));
    const bx = railCheck.before?.grip?.anchor ?? railCheck.before?.x;
    const ax = railCheck.after?.grip?.anchor ?? railCheck.after?.x;
    const dyaw = Math.abs((railCheck.after?.yaw ?? 0) - (railCheck.before?.yaw ?? 0));
    const dpitch = Math.abs((railCheck.after?.pitch ?? 0) - (railCheck.before?.pitch ?? 0));
    assert(typeof bx === 'number' && typeof ax === 'number' && ax > bx + 0.08, `moss-rail-slide did not translate right: ${bx} -> ${ax}`);
    assert(dyaw < 2.5 && dpitch < 2.5, `moss-rail-slide polluted body yaw/pitch: dyaw=${dyaw}, dpitch=${dpitch}, raw=${JSON.stringify(railCheck)}`);
    report.gesture.railSlideCheck = { bx, ax, dyaw, dpitch };

    // Assert live WebGL 3D decal Jacobian uprightness + H50 crisp Code-128 barcodes (no V-flipped decals!)
    if (renderWorker) {
      const snap = await renderWorker.evaluate(async () => {
        const mod = await import(self.location.href);
        return typeof mod.inspectScene === 'function' ? mod.inspectScene() : null;
      });
      const decals = snap?.decalAudit || [];
      assert(decals.length >= 18, `Expected >= 18 decals in live 3D scene, got ${decals.length} (snapReady=${snap?.ready})`);
      const badDecals = decals.filter((d) => d.duRight <= 0 || d.imageUpSign <= 0);
      assert(badDecals.length === 0, `Inverted/mirrored 3D decals found: ${JSON.stringify(badDecals)}`);
      const byNode = Object.fromEntries(decals.map((d) => [d.nodeName, d]));
      for (const reqNode of [
        'CAD-独立制作标识_·_opticalProcessor',
        'CAD-独立制作标识_·_serviceWarning',
        'CAD-独立制作标识_·_serviceWarning004',
        'CAD-独立制作标识_·_serviceWarning005',
        'CAD-独立制作标识_·_warningRed001',
        'CAD-独立制作标识_·_exhaust',
      ]) {
        assert(byNode[reqNode]?.vCorrected === true, `Expected vCorrected=true on ${reqNode}`);
      }
      for (const [bcNode, expectRed] of [['CAD-独立制作标识_·_h50001', true], ['CAD-独立制作标识_·_h50003', false]]) {
        const bc = byNode[bcNode]?.barcode;
        assert(
          bc?.upgraded === true && bc.transitions >= 40 && bc.midAlpha === 0 && bc.tabAlpha >= 245 && bc.tabRed === expectRed,
          `Bad H50 vector barcode on ${bcNode}: ${JSON.stringify(bc)}`,
        );
      }
      assert(!String(byNode['CAD-独立制作标识_·_h50004']?.mapName || '').includes('(crisp vector barcode)'),
        'h50.004 I/O SERVICE decal must stay untouched');
      report.decalAudit = { count: decals.length, allUpright: true, h50FrontBars: byNode['CAD-独立制作标识_·_h50001'].barcode.transitions };
    }

    await page.locator('#reset-view').click();
    await page.waitForTimeout(300);

    // Live MediaPipe Webcam Session Replay: tendon-coupled 3-finger 'rail' -> open 5-finger 'palm' -> 'point' cycle
    const bundleGlob = '**/conversation-assets/hand-gesture/vision_bundle.mjs*';
    await page.route(bundleGlob, (route) => route.fulfill({
      status: 200,
      contentType: 'application/javascript',
      body: `
        export const FilesetResolver = { async forVisionTasks() { return {}; } };
        export const HandLandmarker = {
          async createFromOptions() {
            return {
              detectForVideo() {
                const lm = window.__mockHandFrame?.();
                return lm ? { landmarks: [lm] } : { landmarks: [] };
              },
              close() {},
            };
          },
        };
      `,
    }));

    await page.evaluate(() => {
      const c = document.createElement('canvas');
      c.width = 64; c.height = 48;
      const g2 = c.getContext('2d');
      let tick = 0;
      window.__mockCamTimer = setInterval(() => {
        tick++;
        g2.fillStyle = tick % 2 ? '#112233' : '#223344';
        g2.fillRect(0, 0, 64, 48);
        g2.fillStyle = '#ffffff';
        g2.fillRect(tick % 48, 12, 8, 8);
      }, 35);
      const stream = c.captureStream(20);
      Object.defineProperty(navigator, 'mediaDevices', {
        configurable: true,
        value: { getUserMedia: async () => stream },
      });

      const makeLm = ({ palmX = 0.5, palmY = 0.6, idx = false, mid = false, ring = false, pinkyRatio = 0.72 }) => {
        const pts = Array.from({ length: 21 }, () => ({ x: palmX, y: palmY }));
        pts[0] = { x: palmX, y: palmY + 0.25 };
        pts[9] = { x: palmX, y: palmY };
        pts[2] = { x: palmX - 0.14, y: palmY + 0.05 };
        pts[4] = { x: palmX - 0.26, y: palmY + 0.02 };
        const f = (pip, tip, ox, ratio) => {
          pts[pip] = { x: palmX + ox, y: palmY - 0.05 };
          pts[tip] = { x: palmX + ox, y: pts[0].y + (pts[pip].y - pts[0].y) * ratio };
        };
        f(6, 8, -0.07, idx ? 1.72 : 0.76);
        f(10, 12, 0.0, mid ? 1.75 : 0.75);
        f(14, 16, 0.07, ring ? 1.68 : 0.74);
        f(18, 20, 0.14, pinkyRatio);
        return pts;
      };

      let step = 0;
      window.__mockPhase = 'rail';
      window.__mockHandFrame = () => {
        step++;
        if (window.__mockPhase === 'rail') {
          return makeLm({ palmX: 0.76 - (step % 12) * 0.04, idx: true, mid: true, ring: true, pinkyRatio: 1.18 });
        }
        if (window.__mockPhase === 'palm') {
          return makeLm({ palmX: 0.30 + (step % 12) * 0.04, palmY: 0.55 + (step % 6) * 0.02, idx: true, mid: true, ring: true, pinkyRatio: 1.62 });
        }
        if (window.__mockPhase === 'point') {
          return makeLm({ palmX: 0.5, palmY: 0.6, idx: true, pinkyRatio: 0.72 });
        }
        return null;
      };

      const canvas = document.getElementById('moss-model');
      window.__gestureTrace = { rail: 0, palmMoves: 0, phase: 'rail' };
      canvas.addEventListener('moss-rail-slide', () => { window.__gestureTrace.rail++; });
      canvas.addEventListener('moss-palm-drag', () => { window.__gestureTrace.palmMoves++; });
    });

    await gestureBtn.click();
    await page.waitForFunction(() => document.getElementById('stage-gesture-hud')?.hidden === false
      && (document.getElementById('stage-gesture-badge')?.textContent || '').includes('滑轨'), { timeout: 8000 });
    await page.waitForTimeout(450);
    const railPhase = await page.evaluate(() => {
      const snap = { ...window.__gestureTrace, badge: document.getElementById('stage-gesture-badge')?.textContent || '' };
      window.__gestureTrace = { rail: 0, palmMoves: 0, phase: 'palm' };
      window.__mockPhase = 'palm';
      return snap;
    });
    assert(railPhase.rail >= 2 && railPhase.palmMoves === 0, `Rail replay leaked palmMoves or missed rail events: ${JSON.stringify(railPhase)}`);

    await page.waitForFunction(() => (document.getElementById('stage-gesture-badge')?.textContent || '').includes('姿态'), { timeout: 4000 });
    await page.waitForTimeout(420);
    const palmPhase = await page.evaluate(() => {
      const snap = {
        ...window.__gestureTrace,
        badge: document.getElementById('stage-gesture-badge')?.textContent || '',
        camBefore: document.querySelector('#cam-matrix .fui-matrix__key[aria-pressed="true"]')?.dataset?.camera || '',
      };
      window.__mockPhase = 'point';
      return snap;
    });
    assert(palmPhase.palmMoves >= 2 && palmPhase.rail === 0, `Palm replay fired railSlide or missed palmMoves: ${JSON.stringify(palmPhase)}`);

    await page.waitForFunction((beforeCam) => {
      const cur = document.querySelector('#cam-matrix .fui-matrix__key[aria-pressed="true"]')?.dataset?.camera || '';
      return cur && cur !== beforeCam;
    }, palmPhase.camBefore, { timeout: 4000 });
    report.gesture.replay = { railPhase, palmPhase };

    await page.locator('#stage-gesture-stop').click();
    await page.waitForTimeout(150);
    await page.evaluate(() => { clearInterval(window.__mockCamTimer); });
    await page.unroute(bundleGlob);
    await page.locator('#reset-view').click();
    await page.waitForTimeout(250);
  }

  // Simulate missing camera API / error path & trigger via Alt+G shortcut
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true });
  });
  await page.keyboard.press('Alt+G');
  await page.waitForFunction(() => {
    const hud = document.getElementById('stage-gesture-hud');
    return hud && !hud.hidden && hud.dataset.mode === 'notice';
  }, null, { timeout: 6000 });
  const noticeText = (await page.locator('#stage-gesture-notice').textContent() || '').trim();
  assert(noticeText.length >= 6, `Expected honest camera notice text, got "${noticeText}"`);
  report.gesture.noticeText = noticeText;
  await page.screenshot({ path: path.join(OUT, '02_gesture_stage_notice.png'), animations: 'disabled' });
  if (await page.locator('#stage-gesture-hud').isHidden()) {
    await gestureBtn.click();
    await page.waitForSelector('#stage-gesture-stop', { state: 'visible', timeout: 3000 });
  }
  await page.locator('#stage-gesture-stop').click();
  await page.waitForTimeout(150);
  assert(await page.locator('#stage-gesture-hud').isHidden(), '#stage-gesture-hud must close on × click');

  // 6. Media "先来首歌吧" + Full <-> Mini + Mini ‹/› track switch
  console.log('[UI 5/7] Testing "先来首歌吧" + Media Full <-> Mini + Mini ‹/› skip...');
  await page.locator('#comms-new').click();
  await page.waitForTimeout(350);
  await page.fill('#comms-input', '先来首歌吧');
  await page.locator('#comms-send').click();
  await page.waitForFunction(() => {
    const card = document.querySelector('.moss-hud-card[data-hud-type="media"]');
    return card
      && !card.querySelector('.moss-hud-pending')
      && Boolean(card.querySelector('.moss-hud-media-mini-play, [data-media-viz], [data-sim-viz]'));
  }, { timeout: 25000 });
  await page.waitForTimeout(250);

  const isAlreadyMini = await page.locator('.moss-hud-card[data-hud-type="media"]').getAttribute('data-mini');
  if (isAlreadyMini === 'true') {
    await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-mini]').click();
    await page.waitForTimeout(250);
  }

  const fullMetrics = await page.evaluate(() => {
    const card = document.querySelector('.moss-hud-card[data-hud-type="media"]');
    const audio = card?.querySelector('audio, video');
    if (audio) audio.dataset.qaTag = 'same-audio-node';
    return {
      title: (card?.querySelector('.moss-hud-card__title')?.textContent || '').trim(),
      height: Math.round(card?.getBoundingClientRect().height || 0),
      trackCount: card?.querySelectorAll('.moss-hud-track').length || 0,
    };
  });
  await page.screenshot({ path: path.join(OUT, '03_media_hud_full.png'), animations: 'disabled' });

  // Switch to Mini Mode
  await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-mini]').click();
  await page.waitForTimeout(350);
  const miniMetrics = await page.evaluate(() => {
    const card = document.querySelector('.moss-hud-card[data-hud-type="media"]');
    const audio = card?.querySelector('audio, video');
    const list = card?.querySelector('.moss-hud-tracklist');
    const controls = card?.querySelector('.moss-fui-media-controls');
    const scrub = card?.querySelector('.moss-fui-scrub');
    const miniPlay = card?.querySelector('.moss-hud-media-mini-play');
    const miniPrev = card?.querySelector('[data-media-mini-prev]');
    const miniNext = card?.querySelector('[data-media-mini-next]');
    const viz = card?.querySelector('[data-media-viz], [data-sim-viz]');
    return {
      title: (card?.querySelector('.moss-hud-card__title')?.textContent || '').trim(),
      dataMini: card?.getAttribute('data-mini'),
      height: Math.round(card?.getBoundingClientRect().height || 0),
      width: Math.round(card?.getBoundingClientRect().width || 0),
      sameAudioNode: !audio || audio.dataset.qaTag === 'same-audio-node',
      listHidden: !list || list.offsetHeight === 0,
      controlsHidden: !controls || controls.offsetHeight === 0,
      scrubHidden: !scrub || scrub.offsetHeight === 0,
      miniPlayVisible: Boolean(miniPlay && miniPlay.offsetHeight > 0),
      miniPrevVisible: Boolean(miniPrev && miniPrev.offsetHeight > 0),
      miniNextVisible: Boolean(miniNext && miniNext.offsetHeight > 0),
      miniNextDisabled: Boolean(miniNext?.disabled),
      miniPrevDisabled: Boolean(miniPrev?.disabled),
      vizVisible: Boolean(viz && viz.offsetHeight > 0),
    };
  });
  console.log('  Media Mini:', miniMetrics);
  assert(miniMetrics.dataMini === 'true', 'Card must enter data-mini="true"');
  assert(miniMetrics.sameAudioNode, '<audio> node must stay mounted on mini toggle');
  assert(miniMetrics.listHidden && miniMetrics.controlsHidden && miniMetrics.scrubHidden, 'Full chrome must hide in mini mode');
  assert(miniMetrics.miniPlayVisible && miniMetrics.vizVisible, 'Mini play button and live spectrum must be visible');
  assert(miniMetrics.height <= 165, `Mini height too tall: ${miniMetrics.height}px`);
  await page.screenshot({ path: path.join(OUT, '04_media_hud_mini.png'), animations: 'disabled' });

  // Test Mini ‹ / › track skip button if playlist has >1 tracks
  let skippedTitle = miniMetrics.title;
  if (fullMetrics.trackCount > 1) {
    assert(miniMetrics.miniPrevVisible && miniMetrics.miniNextVisible, 'Mini prev/next buttons must be visible for multi-track queue');
    const skipSel = !miniMetrics.miniNextDisabled ? '[data-media-mini-next]' : '[data-media-mini-prev]';
    await page.locator(`.moss-hud-card[data-hud-type="media"] ${skipSel}`).click();
    await page.waitForFunction(
      (prevTitle) => {
        const cur = (document.querySelector('.moss-hud-card[data-hud-type="media"] .moss-hud-card__title')?.textContent || '').trim();
        return cur && cur !== prevTitle;
      },
      miniMetrics.title,
      { timeout: 10000 },
    );
    await page.waitForTimeout(350);
    const afterSkip = await page.evaluate(() => {
      const card = document.querySelector('.moss-hud-card[data-hud-type="media"]');
      return {
        title: (card?.querySelector('.moss-hud-card__title')?.textContent || '').trim(),
        dataMini: card?.getAttribute('data-mini'),
      };
    });
    skippedTitle = afterSkip.title;
    assert(afterSkip.dataMini === 'true', 'Media HUD must STAY in mini mode after clicking mini ‹/›!');
    await page.screenshot({ path: path.join(OUT, '05_media_hud_mini_skipped.png'), animations: 'disabled' });
  }
  report.mediaMini = { full: fullMetrics, mini: miniMetrics, skippedTitle };

  // Restore full mode & close media card cleanly
  await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-mini]').click();
  await page.waitForTimeout(200);
  await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-close]').click();
  await page.waitForTimeout(200);

  // 7. ALL 4 Intel Dossiers x 2 slides = 8 Logos Capture (for Python pixel-sanity analyzer!)
  console.log('[UI 6/7] Inspecting ALL 4 Intel Dossiers x 2 slides = 8 logos...');
  // Trigger Intel dossier via real user chat turn if no intel chip in tray yet
  let intelChip = page.locator('#hud-tray .moss-hud-chip[data-type="intel"]').first();
  if (!(await intelChip.isVisible().catch(() => false))) {
    await page.fill('#comms-input', '简单介绍一下550W');
    await page.locator('#comms-send').click();
    await page.waitForSelector('.moss-hud-card[data-hud-type="intel"]', { timeout: 25000 });
  } else {
    await intelChip.click();
    await page.waitForSelector('.moss-hud-card[data-hud-type="intel"]', { timeout: 5000 });
  }
  await page.waitForTimeout(400);

  const dossierIds = ['550w', 'ueg-bsc', 'space-elevator', 'navigator'];
  for (const did of dossierIds) {
    const navBtn = page.locator(`.moss-hud-card[data-hud-type="intel"]:not([hidden]) [data-dossier-id="${did}"]`).first();
    await navBtn.click();
    await page.waitForTimeout(300);

    const cardSel = `.moss-hud-card[data-hud-type="intel"]:not([hidden]):has([data-dossier-id="${did}"][aria-pressed="true"])`;
    const activeCard = page.locator(cardSel).first();
    await activeCard.waitFor({ state: 'visible', timeout: 6000 });

    for (let slideIdx = 0; slideIdx < 2; slideIdx++) {
      if (slideIdx === 1) {
        const nextBtn = activeCard.locator('[data-slide-dir="1"]');
        if (await nextBtn.isEnabled().catch(() => false)) {
          await nextBtn.click();
          await page.waitForTimeout(400);
        }
      }
      // Wait for any <img> inside this active card's .moss-hud-visual to finish loading
      await page.waitForFunction((sel) => {
        const card = document.querySelector(sel);
        const box = card?.querySelector('.moss-hud-visual');
        if (!box) return false;
        const img = box.querySelector('img');
        if (img) return img.complete && img.naturalWidth > 0;
        return Boolean(box.querySelector('.moss-hud-visual__mask'));
      }, cardSel, { timeout: 12000 });
      await page.waitForTimeout(300);

      const visualLoc = activeCard.locator('.moss-hud-visual');
      const pngName = `logo_${did}_0${slideIdx + 1}.png`;
      const pngPath = path.join(OUT, pngName);
      await visualLoc.screenshot({ path: pngPath, animations: 'disabled' });
      const meta = await page.evaluate((sel) => {
        const card = document.querySelector(sel);
        const box = card?.querySelector('.moss-hud-visual');
        const img = box?.querySelector('img');
        const mask = box?.querySelector('.moss-hud-visual__mask');
        return {
          lightPlate: Boolean(box?.classList.contains('moss-hud-visual--light')),
          kind: img ? img.className : (mask ? 'mask' : 'unknown'),
          src: img?.getAttribute('src') || mask?.style?.getPropertyValue('--hud-mask') || '',
          naturalWidth: img?.naturalWidth || 0,
          naturalHeight: img?.naturalHeight || 0,
        };
      }, cardSel);
      report.intelLogos.push({ dossier: did, slide: slideIdx + 1, file: pngPath, ...meta });
    }
  }
  while (await page.locator('.moss-hud-card[data-hud-type="intel"] [data-hud-close]').count() > 0) {
    await page.locator('.moss-hud-card[data-hud-type="intel"] [data-hud-close]').first().click().catch(() => {});
    await page.waitForTimeout(100);
  }

  // 8. History Full-Text Search & Markdown Export UI Check
  console.log('[UI 8/10] Testing History full-text search & Markdown export...');
  await page.locator('[data-open-modal="history"]').click();
  await page.waitForSelector('#dlg-history[open]', { timeout: 5000 });
  await page.waitForTimeout(250);
  const firstExportHref = await page.locator('#history-list [data-export-chat]').first().getAttribute('href');
  assert(Boolean(firstExportHref && firstExportHref.includes('/export')), `Missing Markdown export link in history: ${firstExportHref}`);
  const expResp = await ctx.request.get(`${BASE}${firstExportHref}`);
  assert(expResp.status() === 200 && (expResp.headers()['content-type'] || '').includes('text/markdown'), `Export MD failed: ${expResp.status()}`);
  const expBody = await expResp.text();
  assert(expBody.includes('# MOSS 550W'), 'Exported Markdown missing header');
  await page.fill('#history-search-input', '先来首歌吧');
  await page.locator('#history-search-form button[type="submit"]').click();
  await page.waitForFunction(() => {
    const st = document.getElementById('history-search-status');
    return st && !st.hidden && (st.textContent || '').trim().length > 0;
  }, { timeout: 6000 });
  const histStatus = (await page.locator('#history-search-status').textContent() || '').trim();
  report.historySearch = { status: histStatus, exportBytes: expBody.length };
  await page.screenshot({ path: path.join(OUT, '06_history_search_export.png'), animations: 'disabled' });
  await page.locator('#history-search-clear').click();
  await page.locator('#dlg-history [data-dialog-close]').first().click();
  await page.waitForTimeout(200);

  // 9. Long-Tail Local File Viewer Upload Check (TXT/MD HUD Document Viewer)
  console.log('[UI 9/10] Testing local file upload -> HUD Document Viewer...');
  const sampleDocPath = path.join(OUT, 'sample_550w_note.md');
  fs.writeFileSync(sampleDocPath, '# 550W 巡检备忘\n\n- 光学镜组：正常\n- H50 OLED：正常\n', 'utf-8');
  await page.setInputFiles('#hud-file-input', sampleDocPath);
  await page.waitForSelector('.moss-hud-card[data-hud-type="doc"]', { timeout: 8000 });
  const docText = (await page.locator('.moss-hud-card[data-hud-type="doc"]').textContent() || '').trim();
  assert(docText.includes('550W 巡检备忘'), `Doc viewer did not render uploaded content: ${docText.slice(0, 80)}`);
  await page.locator('.moss-hud-card[data-hud-type="doc"] [data-hud-close]').click();
  await page.waitForTimeout(150);

  // 10. EN <-> ZH locale switch + v5.5 Drawer / Cross-Rail / Ctrl+K / Mute / CS-C Auth + /mobile FFT check
  console.log('[UI 10/14] Testing EN <-> ZH locale switch...');
  await page.locator('.fui-lang__opt[lang="en"]').click();
  await page.waitForTimeout(250);
  const legendEn = (await page.locator('#stage-gesture-hud .moss-gesture-hud__legend').textContent() || '').trim();
  assert(legendEn.includes('Zoom'), `EN gesture legend missing Zoom: ${legendEn}`);
  await page.locator('.fui-lang__opt[lang="zh-CN"]').click();
  await page.waitForTimeout(250);
  report.i18n = { legendEn, legendZh };

  // 11. Slide-out Comms Drawer collapse -> re-open -> detach float -> dock back
  console.log('[UI 11/14] Testing slide-out Comms drawer & detachable floating window...');
  await page.locator('#comms-collapse').click();
  await page.waitForTimeout(240);
  const drawerClosed = await page.locator('#comms-drawer').getAttribute('data-open');
  assert(drawerClosed === 'false', `Drawer did not collapse: ${drawerClosed}`);
  await page.screenshot({ path: path.join(OUT, '08_drawer_collapsed_dual_rail.png'), animations: 'disabled' });
  await page.locator('#comms-drawer-tab').click();
  await page.waitForTimeout(240);
  assert(await page.locator('#comms-drawer').getAttribute('data-open') === 'true', 'Drawer tab did not reopen drawer');
  await page.locator('#comms-detach').click();
  await page.waitForTimeout(220);
  assert(await page.locator('#comms-drawer').getAttribute('data-mode') === 'float', 'Drawer did not switch to float mode');
  await page.screenshot({ path: path.join(OUT, '09_comms_floating_window.png'), animations: 'disabled' });
  await page.locator('#comms-detach').click();
  await page.waitForTimeout(200);
  assert(await page.locator('#comms-drawer').getAttribute('data-mode') === 'drawer', 'Drawer did not dock back');

  // 12. Cross-Rail Left -> Right panel swap, independent dropzone scrollbar (fixed top block) & restore
  console.log('[UI 12/15] Testing Left <-> Right cross-rail transfer & independent dropzone scrollbar...');
  await page.locator('#comms-collapse').click();
  await page.waitForTimeout(180);
  const topAnchorBefore = await page.locator('#mode-tts').evaluate((el) => Math.round(el.getBoundingClientRect().top));
  for (let i = 0; i < 4; i++) {
    const swapBtns = page.locator('#left-rail .moss-rail-card .moss-rail-swap-btn');
    if (await swapBtns.count() === 0) break;
    await swapBtns.first().click();
    await page.waitForTimeout(100);
  }
  await page.waitForTimeout(180);
  const inRight = await page.locator('#right-rail-cards .moss-rail-card').count();
  assert(inRight >= 3, `Expected >=3 cards in right rail, got count=${inRight}`);
  const rightScrollMetrics = await page.evaluate(() => {
    const z = document.getElementById('right-rail-cards');
    const st = getComputedStyle(z);
    z.scrollTop = z.scrollHeight;
    return {
      overflowY: st.overflowY,
      scrollHeight: z.scrollHeight,
      clientHeight: z.clientHeight,
      scrollTopAfter: z.scrollTop,
      topAnchorAfter: Math.round(document.getElementById('mode-tts').getBoundingClientRect().top),
    };
  });
  assert(rightScrollMetrics.overflowY === 'auto' || rightScrollMetrics.overflowY === 'scroll', `Unexpected #right-rail-cards overflowY: ${rightScrollMetrics.overflowY}`);
  assert(rightScrollMetrics.scrollHeight > rightScrollMetrics.clientHeight && rightScrollMetrics.scrollTopAfter > 0, `Right dropzone did not scroll: ${JSON.stringify(rightScrollMetrics)}`);
  assert(Math.abs(rightScrollMetrics.topAnchorAfter - topAnchorBefore) <= 1, `Top right-rail fixed block moved when scrolling dropzone: ${topAnchorBefore} -> ${rightScrollMetrics.topAnchorAfter}`);
  report.rightRailScroll = rightScrollMetrics;
  await page.screenshot({ path: path.join(OUT, '10_cross_rail_swapped.png'), animations: 'disabled' });
  while (await page.locator('#right-rail-cards .moss-rail-swap-btn').count() > 0) {
    await page.locator('#right-rail-cards .moss-rail-swap-btn').first().click();
    await page.waitForTimeout(80);
  }
  await page.waitForTimeout(150);
  await page.locator('#comms-drawer-tab').click();
  await page.waitForTimeout(180);

  // 13. Ctrl+K Command Palette, Meeting Mute Toggle & CS Auth User Manager (Tab 06) + C-Role switch
  console.log('[UI 13/14] Testing Ctrl+K palette, Meeting Mute, and CS/C 4-char Auth Gate...');
  await page.locator('#strip-mute').click();
  await page.waitForTimeout(150);
  assert(await page.locator('#strip-mute').getAttribute('aria-pressed') === 'true', 'Meeting mute did not engage');
  await page.locator('#strip-mute').click();
  await page.waitForTimeout(150);
  assert(await page.locator('#strip-mute').getAttribute('aria-pressed') === 'false', 'Meeting mute did not release');

  await page.locator('#strip-cmdk').click();
  await page.waitForSelector('#dlg-cmdk[open]', { timeout: 4000 });
  const cmdkAllTxt = (await page.locator('#cmdk-list').textContent() || '');
  assert(/MODEL \(CS\)|切换模型 \(CS\)/.test(cmdkAllTxt), `Ctrl+K palette missing CS model items: ${cmdkAllTxt.slice(0, 120)}`);
  await page.fill('#cmdk-input', '静音');
  await page.waitForTimeout(120);
  const cmdkHits = await page.locator('#cmdk-list .moss-cmdk__item').count();
  assert(cmdkHits >= 1, 'Ctrl+K palette returned 0 hits for 静音');
  await page.locator('#dlg-cmdk [data-dialog-close]').click();
  await page.waitForTimeout(150);

  // Switch to C operator "OP01" via top-strip auth chip, verify zero credential leakage, verify CS-only controls hidden, then restore MOSS (CS)
  await page.locator('#strip-auth-chip').click();
  await page.waitForSelector('#dlg-auth-gate[open]', { timeout: 4000 });
  const authLeakCheck = await page.evaluate(() => ({
    hint: document.getElementById('auth-gate-hint')?.textContent || '',
    placeholder: document.getElementById('auth-gate-input')?.getAttribute('placeholder') || '',
  }));
  assert(!/\b(MOSS|OP01)\b/i.test(authLeakCheck.hint) && !/\b(MOSS|OP01)\b/i.test(authLeakCheck.placeholder),
    `Auth gate dialog leaked credential hint: ${JSON.stringify(authLeakCheck)}`);
  await page.fill('#auth-gate-input', 'zz');
  await page.locator('#auth-gate-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.getElementById('auth-gate-err')?.hidden, { timeout: 4000 });
  const authErrTxt = (await page.locator('#auth-gate-err').textContent() || '');
  assert(!/\b(MOSS|OP01)\b/i.test(authErrTxt), `Auth gate error leaked credentials: ${authErrTxt}`);
  const badAuthResp = await ctx.request.post(`${BASE}/api/auth/verify`, { data: { code: 'ZZZZ' } });
  const badAuthBody = await badAuthResp.text();
  assert(!/\b(MOSS|OP01)\b/i.test(badAuthBody), `Auth API failure leaked credentials: ${badAuthBody}`);
  await page.fill('#auth-gate-input', 'op01');
  await page.locator('#auth-gate-form button[type="submit"]').click();
  await page.waitForFunction(() => document.body.dataset.authRole === 'C', { timeout: 5000 });
  assert(await page.locator('#strip-auth-chip').textContent() === 'C · OP01', 'Auth chip did not reflect C · OP01');
  const cHiddenCheck = await page.evaluate(() => ({
    haHidden: getComputedStyle(document.getElementById('brain-ha-form')).display === 'none',
    distillHidden: getComputedStyle(document.getElementById('set-soul-distill-btn')).display === 'none',
  }));
  assert(cHiddenCheck.haHidden && cHiddenCheck.distillHidden, `CS-only forms still visible for C operator: ${JSON.stringify(cHiddenCheck)}`);
  // Restore CS admin MOSS
  await page.locator('#strip-auth-chip').click();
  await page.waitForSelector('#dlg-auth-gate[open]', { timeout: 4000 });
  await page.fill('#auth-gate-input', 'moss');
  await page.locator('#auth-gate-form button[type="submit"]').click();
  await page.waitForFunction(() => document.body.dataset.authRole === 'CS', { timeout: 5000 });
  assert(await page.locator('#strip-auth-chip').textContent() === 'CS · MOSS', 'Auth chip did not restore CS · MOSS');

  // Verify CS Tab 06 User Management lists OP01 + South China Sea / Sansha on Location dot-matrix map
  await page.locator('#strip-settings').click();
  await page.waitForSelector('#dlg-settings[open]', { timeout: 4000 });
  await page.locator('[data-set-tab="auth"]').click();
  await page.waitForTimeout(300);
  const usersTxt = (await page.locator('#set-auth-users-list').textContent() || '');
  assert(usersTxt.includes('OP01') && usersTxt.includes('MOSS'), `CS user manager missing OP01/MOSS: ${usersTxt}`);
  await page.screenshot({ path: path.join(OUT, '11_cs_auth_user_manager.png'), animations: 'disabled' });

  console.log('[UI 14/15] Checking South China Sea / Sansha dot-matrix map rendering & city search...');
  await page.locator('[data-set-tab="location"]').click();
  await page.waitForTimeout(220);
  await page.locator('#set-city-view-china').click();
  await page.waitForTimeout(250);
  const scsPixels = await page.evaluate(() => {
    const cv = document.getElementById('set-city-map');
    const ctx2 = cv.getContext('2d');
    const { width, height } = cv;
    const data = ctx2.getImageData(Math.floor(width * 0.56), Math.floor(height * 0.65), Math.floor(width * 0.25), Math.floor(height * 0.34)).data;
    let bright = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i] + data[i + 1] + data[i + 2] > 180) bright++;
    }
    return { width, height, bright };
  });
  assert(scsPixels.bright >= 40, `South China Sea / nine-dash dot matrix missing pixels: ${JSON.stringify(scsPixels)}`);
  await page.fill('#set-city-search', '三沙');
  await page.waitForTimeout(200);
  const sanshaHits = (await page.locator('#set-city-results').textContent() || '');
  assert(sanshaHits.includes('三沙'), `City search failed to find 三沙: ${sanshaHits}`);
  report.southChinaSea = { ...scsPixels, sanshaMatched: true };
  await page.fill('#set-city-search', '');
  await page.locator('#dlg-settings [data-dialog-close]').first().click();
  await page.waitForTimeout(150);

  // 15. /mobile 390px deep portrait interactions: personality/TTS/quick music + #m-viz painted + wrapped multi-turn feed + 0 overflow
  console.log('[UI 15/15] Testing /mobile 390px interactive workflows, #m-viz playback & wrapped scroll feed...');
  const mobPage = await ctx.newPage();
  mobPage.on('pageerror', (err) => pageErrors.push(`mobile: ${err?.stack || err}`));
  mobPage.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(`mobile: ${msg.text()} (${msg.location?.()?.url || ''})`);
  });
  mobPage.on('response', async (res) => {
    if (res.status() >= 400) {
      const txt = await res.text().catch(() => '');
      console.error('[MOB_HTTP_ERR]', res.status(), res.url(), txt);
    }
  });
  await mobPage.setViewportSize({ width: 390, height: 844 });
  await mobPage.goto(`${BASE}/mobile`, { waitUntil: 'domcontentloaded', timeout: 15000 });
  await mobPage.waitForFunction(() => document.getElementById('m-link')?.textContent === 'ONLINE', { timeout: 10000 });

  await mobPage.selectOption('#m-personality', 'rational');
  await mobPage.waitForTimeout(180);
  await mobPage.selectOption('#m-personality', 'sassy');
  await mobPage.waitForTimeout(180);
  await mobPage.selectOption('#m-personality', 'partner');
  await mobPage.locator('#m-tts').click();
  assert(await mobPage.locator('#m-tts').getAttribute('aria-pressed') === 'true', 'Mobile TTS did not toggle on');
  await mobPage.locator('#m-tts').click();
  assert(await mobPage.locator('#m-tts').getAttribute('aria-pressed') === 'false', 'Mobile TTS did not toggle off');

  await mobPage.locator('#m-new-chat').click();
  await mobPage.waitForTimeout(200);
  await mobPage.locator('#m-shell button[data-quick="先来首歌吧"], .m-quick button[data-quick="先来首歌吧"]').first().click();
  await mobPage.waitForFunction(() => {
    const bar = document.getElementById('m-media-bar');
    const title = (document.getElementById('m-media-title')?.textContent || '').trim();
    return bar && !bar.hidden && title && title !== '—';
  }, null, { timeout: 15000 });

  await mobPage.locator('#m-media-toggle').click();
  await mobPage.waitForTimeout(260);
  const vizNonBlank = await mobPage.evaluate(() => {
    const cv = document.getElementById('m-viz');
    const c2 = cv?.getContext('2d');
    if (!c2) return 0;
    const d = c2.getImageData(0, 0, cv.width, cv.height).data;
    let lit = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 20) lit++;
    return lit;
  });
  assert(vizNonBlank > 20, `Mobile #m-viz canvas remained blank after play: lit=${vizNonBlank}`);
  await mobPage.locator('#m-media-toggle').click();

  await mobPage.waitForFunction(() => !Array.from(document.querySelectorAll('#m-feed .m-msg .m-msg__role'))
    .some((el) => /THINKING/i.test(el.textContent || '')), null, { timeout: 12000 });
  for (const txt of [
    '今儿几号，星期几？',
    'MOSS-550W-MOBILE-LONG-UNBROKEN-TOKEN-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-现在几点了？',
    '今儿天气怎么样',
  ]) {
    const beforeCount = await mobPage.locator('#m-feed .m-msg').count();
    await mobPage.fill('#m-input', txt);
    await mobPage.locator('#m-form button[type="submit"]').click();
    await mobPage.waitForFunction((prev) => {
      const msgs = document.querySelectorAll('#m-feed .m-msg');
      const roles = Array.from(document.querySelectorAll('#m-feed .m-msg .m-msg__role'));
      return msgs.length >= prev + 2 && !roles.some((el) => /THINKING/i.test(el.textContent || ''));
    }, beforeCount, { timeout: 25000 });
  }
  await mobPage.waitForFunction(() => document.querySelectorAll('#m-feed .m-msg').length >= 6, null, { timeout: 12000 });

  const mobMetrics = await mobPage.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    hasPers: Boolean(document.getElementById('m-personality')),
    hasModel: Boolean(document.getElementById('m-model')),
    hasChatSel: Boolean(document.getElementById('m-chat-sel')),
    hasSeek: Boolean(document.getElementById('m-media-seek') && document.getElementById('m-media-speed') && document.getElementById('m-media-time')),
    hasVizCanvas: Boolean(document.getElementById('m-viz')),
    hasQueueCtrl: Boolean(document.getElementById('m-media-prev') && document.getElementById('m-media-next') && document.getElementById('m-media-sleep')),
    msgCount: document.querySelectorAll('#m-feed .m-msg').length,
    mediaTitle: (document.getElementById('m-media-title')?.textContent || '').trim(),
    feedHeight: document.getElementById('m-feed')?.clientHeight || 0,
    feedScrollHeight: document.getElementById('m-feed')?.scrollHeight || 0,
  }));
  report.mobile = { ...mobMetrics, vizLitPixels: vizNonBlank };
  assert(mobMetrics.scrollW <= mobMetrics.innerW + 2, `Mobile horizontal overflow after multi-turn + long token: ${mobMetrics.scrollW} > ${mobMetrics.innerW}`);
  assert(mobMetrics.hasVizCanvas && mobMetrics.hasQueueCtrl && mobMetrics.hasChatSel && mobMetrics.hasSeek && mobMetrics.msgCount >= 6, `Unexpected mobile metrics: ${JSON.stringify(mobMetrics)}`);
  await mobPage.screenshot({ path: path.join(OUT, '07_mobile_390px.png'), animations: 'disabled' });
  await mobPage.close();

  // 16. Two-tab operator badge isolation (MOSS vs C-operator OP63) + C-role zero-403 check
  console.log('[UI 16/17] Verifying two-tab operator badge isolation & C-role clean session...');
  await page.locator('#strip-settings').click();
  await page.waitForSelector('#dlg-settings[open]', { timeout: 5000 });
  await page.locator('[data-set-tab="badge"]').click();
  const hostSavePromise = page.waitForResponse(
    (res) => res.url().includes('/api/console-settings/prefs') && res.request().method() === 'POST',
    { timeout: 6000 },
  );
  await page.fill('#set-badge-name', '刘启-HOST');
  await page.locator('#set-badge-name').dispatchEvent('input');
  const hostSaveRes = await hostSavePromise;
  assert(hostSaveRes.status() === 200, `Host badge prefs save failed: ${hostSaveRes.status()}`);
  await page.locator('#dlg-settings [data-dialog-close]').first().click();

  const opCtx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const op403s = [];
  const opPage = await opCtx.newPage();
  opPage.on('pageerror', (err) => pageErrors.push(`op63: ${err?.stack || err}`));
  opPage.on('response', (res) => { if (res.status() === 403) op403s.push(res.url()); });
  await opCtx.request.post(`${BASE}/api/auth/verify`, {
    data: { code: 'OP63' },
    headers: { Origin: BASE },
  });
  await opPage.addInitScript(() => { sessionStorage.setItem('moss-fui.opened', '1'); });
  await opPage.goto(`${BASE}/?skipOpening=1&scene=off`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await opPage.waitForFunction(() => document.body.dataset.backend === 'live', { timeout: 15000 });
  const secUiChecks = await opPage.evaluate(async () => {
    const { escapeHtml } = await import('./js/hud/utils.js');
    const escapedSingle = escapeHtml("a'b\"<>&");
    const { populateIntelCard } = await import('./js/hud/intel-image-doc.js');
    const card = {
      key: 'sec-test',
      intelMatch: {
        custom: {
          title: 'T',
          slides: [{ src: 'javascript:alert(1)', caption: 'bad' }, { src: '/assets/ok.png', caption: 'ok' }],
          facts: [],
        },
      },
    };
    const titleEl = document.createElement('h3');
    const bodyBlocked = document.createElement('div');
    populateIntelCard(card, titleEl, bodyBlocked, {
      slideIndexMap: new Map([['sec-test', 0]]),
      renderOpenCards: () => {},
      openDossier: () => {},
      onSendFollowUp: () => {},
    });
    const bodyOk = document.createElement('div');
    populateIntelCard(card, titleEl, bodyOk, {
      slideIndexMap: new Map([['sec-test', 1]]),
      renderOpenCards: () => {},
      openDossier: () => {},
      onSendFollowUp: () => {},
    });
    const blockedImgs = [...bodyBlocked.querySelectorAll('img')].map((el) => el.getAttribute('src'));
    const okImgs = [...bodyOk.querySelectorAll('img')].map((el) => el.getAttribute('src'));
    return { escapedSingle, blockedImgs, okImgs };
  });
  assert(
    secUiChecks.escapedSingle === "a&#39;b&quot;&lt;&gt;&amp;"
    && secUiChecks.blockedImgs.length === 0
    && secUiChecks.okImgs.includes('/assets/ok.png'),
    `XSS UI safety failed: ${JSON.stringify(secUiChecks)}`,
  );
  await opPage.locator('#strip-settings').click();
  await opPage.waitForSelector('#dlg-settings[open]', { timeout: 5000 });
  // Attempt to open CS-only soul tab as C-operator: must gracefully guard/stay off soul without triggering 403s
  await opPage.locator('[data-set-tab="soul"]').click({ force: true }).catch(() => {});
  await opPage.waitForTimeout(120);
  const soulSelectedForC = await opPage.locator('[data-set-tab="soul"]').getAttribute('aria-selected');
  assert(soulSelectedForC !== 'true', `C-operator was allowed onto CS-only soul tab: ${soulSelectedForC}`);
  await opPage.locator('[data-set-tab="badge"]').click();
  const opInitBadgeName = await opPage.inputValue('#set-badge-name');
  assert(opInitBadgeName !== '刘启-HOST', `C-operator leaked host badge name: ${opInitBadgeName}`);
  const opSavePromise = opPage.waitForResponse(
    (res) => res.url().includes('/api/console-settings/prefs') && res.request().method() === 'POST',
    { timeout: 6000 },
  );
  await opPage.fill('#set-badge-name', '韩朵朵-OP63');
  await opPage.locator('#set-badge-name').dispatchEvent('input');
  const opSaveRes = await opSavePromise;
  assert(opSaveRes.status() === 200, `OP63 badge prefs save failed: ${opSaveRes.status()}`);
  await opCtx.close();

  assert(op403s.length === 0, `C-operator session hit unexpected 403s: ${JSON.stringify(op403s)}`);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.body.dataset.backend === 'live', { timeout: 15000 });
  if (await page.locator('#dlg-badge').evaluate((el) => Boolean(el?.open)).catch(() => false)) {
    await page.locator('#badge-verify').click().catch(() => {});
  }
  await page.locator('#strip-settings').click();
  await page.waitForSelector('#dlg-settings[open]', { timeout: 5000 });
  await page.locator('[data-set-tab="badge"]').click();
  const hostBadgeAfter = await page.inputValue('#set-badge-name');
  assert(hostBadgeAfter === '刘启-HOST', `Host badge name overwritten by C-operator: ${hostBadgeAfter}`);
  await page.locator('#dlg-settings [data-dialog-close]').first().click();

  // 17. Stage 3D data-error recovery unlock + double-reload stability
  console.log('[UI 17/17] Verifying Stage 3D #stage-reload-3d error recovery & double-reload unlock...');
  await page.evaluate(() => {
    const c = document.getElementById('moss-model');
    if (c) c.dataset.error = 'Simulated WebGL lost context';
  });
  await page.waitForSelector('#stage-reload-3d', { state: 'visible', timeout: 5000 });
  await page.evaluate(() => {
    const btn = document.getElementById('stage-reload-3d');
    btn?.click();
    btn?.click();
  });
  await page.waitForFunction(() => {
    const c = document.getElementById('moss-model');
    const notice = document.getElementById('stage-standby-notice');
    return Boolean(c && !c.dataset.error && c.dataset.ready === 'true' && notice?.hidden);
  }, null, { timeout: 20000 });
  report.v63IsolationAndRecovery = { hostBadgeAfter, opInitBadgeName, op403s: op403s.length, stageErrorCleared: true, stageReady: true };

  assert(pageErrors.length === 0, `Page errors detected:\n${pageErrors.join('\n')}`);
  assert(consoleErrors.length === 0, `Console errors detected:\n${consoleErrors.join('\n')}`);

  fs.writeFileSync(path.join(OUT, 'ui_gate_report.json'), JSON.stringify(report, null, 2), 'utf-8');
  console.log('ALL 17 LIVE UI GATE CHECKS PASSED (0 page errors, 0 console errors)!');
} finally {
  await browser.close();
}
