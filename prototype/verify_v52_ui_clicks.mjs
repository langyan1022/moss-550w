/**
 * Interactive end-to-end Edge Playwright QA for MOSS 550W v5.2:
 * - Exercises Brain Hub (all 5 tabs including 生成态势简报 & 执行夜间做梦深度整理 — catching voice.* / handleUserSubmit bugs!)
 * - Exercises Settings 3-card MOSS Personality Picker (partner / rational / lively live backend round-trip)
 * - Exercises Hand-Gesture Deck button (#stage-gesture-btn) & PiP optical viewfinder HUD (#stage-gesture-hud)
 * - Exercises "先来首歌吧" real music playback + HUD Media Player Full <-> Mini collapse toggle (verifying zero <audio> unmount!)
 * - Exercises EN / ZH locale switch + asserts 0 page errors & 0 console errors
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const playwrightPath = process.env.PLAYWRIGHT_PATH || 'F:\\moss\\agent-lab\\prototype\\fui\\node_modules\\playwright';
const { chromium } = require(fs.existsSync(playwrightPath) ? playwrightPath : 'playwright');

const BASE = process.env.MOSS_URL || 'http://127.0.0.1:18221';
const OUT = process.env.OUT_DIR || path.resolve('runs/live-v52-qa');
fs.mkdirSync(OUT, { recursive: true });

function assert(cond, msg) {
  if (!cond) throw new Error(`ASSERTION FAILED: ${msg}`);
}

const pageErrors = [];
const consoleErrors = [];

const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--autoplay-policy=no-user-gesture-required'],
});

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  page.on('pageerror', (err) => pageErrors.push(String(err?.stack || err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });

  // 1. Load live console (skip opening splash via query or click skip)
  await page.goto(`${BASE}/?skipOpening=1`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const skipBtn = page.locator('#opening-skip');
  if (await skipBtn.isVisible({ timeout: 2500 }).catch(() => false)) {
    await skipBtn.click().catch(() => {});
  }
  const badgeDlg = page.locator('#dlg-badge');
  if (await badgeDlg.evaluate((el) => Boolean(el?.open)).catch(() => false)) {
    await page.locator('#badge-verify').click().catch(() => {});
  }
  // Wait for live SSE connection
  await page.waitForFunction(() => document.body.dataset.backend === 'live', { timeout: 20000 });
  await page.waitForTimeout(1200);

  // 2. Test Brain Hub — ALL 5 TABS + 生成态势简报 + 执行夜间做梦深度整理
  console.log('[QA 1/5] Testing Brain Hub (5 tabs + 生成态势简报 + 夜间做梦)...');
  await page.locator('#strip-brain-btn').click();
  await page.waitForSelector('#dlg-brain[open]', { timeout: 5000 });
  await page.waitForTimeout(400);

  // Tab: RSI
  await page.locator('[data-brain-tab="rsi"]').click();
  await page.locator('#brain-rsi-run').click();
  await page.waitForTimeout(500);

  // Tab: SAG
  await page.locator('[data-brain-tab="sag"]').click();
  await page.fill('#brain-sag-search-input', 'MOSS');
  await page.locator('#brain-sag-search-form button[type="submit"]').click();
  await page.waitForTimeout(500);

  // Tab: Pulse & Dream (where voice.isTtsEnabled bug was caught!)
  await page.locator('[data-brain-tab="pulse"]').click();
  await page.locator('#brain-briefing-btn').click();
  await page.waitForFunction(() => {
    const txt = (document.getElementById('brain-briefing-box')?.textContent || '').trim();
    return txt.length > 8;
  }, { timeout: 15000 });
  const briefingTxt = (await page.locator('#brain-briefing-box').textContent() || '').trim();
  console.log('  Briefing box:', briefingTxt.slice(0, 90));
  assert(!/not a function|ReferenceError|TypeError/i.test(briefingTxt), `Briefing threw JS error: ${briefingTxt}`);

  await page.locator('#brain-dream-btn').click();
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(OUT, '01_brain_pulse_briefing.png') });

  // Tab: Jobs & Skills
  await page.locator('[data-brain-tab="jobs"]').click();
  await page.waitForTimeout(250);
  // Tab: IoT & 550W Hardware
  await page.locator('[data-brain-tab="iot"]').click();
  await page.waitForTimeout(250);
  await page.locator('#dlg-brain [data-dialog-close]').first().click();
  await page.waitForTimeout(250);

  // 3. Test Settings Tab 01 — 3-Card Personality Selector
  console.log('[QA 2/5] Testing Settings 3-Card Personality Picker...');
  await page.locator('#strip-settings').click();
  await page.waitForSelector('#dlg-settings[open]', { timeout: 5000 });
  await page.locator('[data-set-tab="model"]').click();
  await page.waitForSelector('.moss-personality-card[data-personality="rational"]', { timeout: 5000 });
  const cardCount = await page.locator('.moss-personality-card').count();
  assert(cardCount === 3, `Expected 3 personality cards, got ${cardCount}`);

  // Switch to rational (550W 绝对理性)
  await page.locator('.moss-personality-card[data-personality="rational"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="rational"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );
  const persTagAfterRational = (await page.locator('#tag-pers-code').textContent() || '').trim();
  console.log('  Switched personality tag:', persTagAfterRational);
  assert(persTagAfterRational.includes('550W') || persTagAfterRational.includes('理性'), `Unexpected pers tag: ${persTagAfterRational}`);
  await page.screenshot({ path: path.join(OUT, '02_settings_personality.png') });

  // Switch to lively then back to partner (default)
  await page.locator('.moss-personality-card[data-personality="lively"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="lively"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );
  await page.locator('.moss-personality-card[data-personality="partner"]').click();
  await page.waitForFunction(
    () => document.querySelector('.moss-personality-card[data-personality="partner"]')?.getAttribute('aria-checked') === 'true',
    { timeout: 6000 },
  );

  // Cycle remaining Settings tabs to verify 0 errors
  for (const tab of ['audio', 'display', 'location', 'badge']) {
    await page.locator(`[data-set-tab="${tab}"]`).click();
    await page.waitForTimeout(180);
  }
  await page.locator('#dlg-settings [data-dialog-close]').first().click();
  await page.waitForTimeout(250);

  // 4. Test Hand-Gesture Deck Button + PiP Viewfinder layout
  console.log('[QA 3/5] Testing Hand-Gesture Deck toggle & PiP HUD...');
  const gestureBtn = page.locator('#stage-gesture-btn');
  assert(await gestureBtn.isVisible(), '#stage-gesture-btn should be visible in left-rail Camera Presets');
  // Preview PiP HUD visually (draw a sample 21-point hand skeleton on #gesture-overlay to inspect styling/geometry!)
  await page.evaluate(() => {
    const hud = document.getElementById('stage-gesture-hud');
    const ov = document.getElementById('gesture-overlay');
    const badge = document.getElementById('stage-gesture-badge');
    const btn = document.getElementById('stage-gesture-btn');
    if (hud) hud.hidden = false;
    if (btn) { btn.setAttribute('aria-pressed', 'true'); btn.dataset.state = 'ok'; }
    if (badge) { badge.textContent = '🖐 旋转机体'; badge.dataset.state = 'ok'; }
    if (ov) {
      const ctx = ov.getContext('2d');
      ctx.clearRect(0, 0, ov.width, ov.height);
      const pts = [
        [120, 152],
        [98, 138], [84, 120], [74, 102], [68, 86],
        [104, 104], [100, 76], [98, 56], [96, 38],
        [120, 98], [120, 68], [120, 46], [120, 28],
        [136, 104], [140, 76], [142, 56], [144, 40],
        [150, 114], [158, 92], [164, 76], [168, 62],
      ];
      const edges = [
        [0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],
        [5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],
        [13,17],[17,18],[18,19],[19,20],[0,17],
      ];
      ctx.strokeStyle = '#36e090';
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (const [a, b] of edges) {
        ctx.moveTo(pts[a][0], pts[a][1]);
        ctx.lineTo(pts[b][0], pts[b][1]);
      }
      ctx.stroke();
      ctx.fillStyle = '#e6f4ff';
      for (const [x, y] of pts) {
        ctx.beginPath();
        ctx.arc(x, y, 2.8, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  });
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(OUT, '03_stage_gesture_deck.png') });
  // Clicking stop button must cleanly hide HUD and reset state
  await page.locator('#stage-gesture-stop').click();
  await page.waitForTimeout(150);
  assert(await page.locator('#stage-gesture-hud').isHidden(), '#stage-gesture-hud should hide after clicking stop');

  // 5. Test "先来首歌吧" real music playback + Media HUD Full <-> Mini Collapse Mode
  console.log('[QA 4/5] Testing "先来首歌吧" & Media HUD Full <-> Mini mode...');
  await page.locator('#comms-new').click();
  await page.waitForTimeout(400);
  await page.fill('#comms-input', '先来首歌吧');
  await page.locator('#comms-send').click();

  await page.waitForSelector('.moss-hud-card[data-hud-type="media"] [data-hud-mini]', { timeout: 25000 });
  await page.waitForTimeout(1000);

  // Ensure full mode first (in case localStorage had mini)
  const isAlreadyMini = await page.locator('.moss-hud-card[data-hud-type="media"]').getAttribute('data-mini');
  if (isAlreadyMini === 'true') {
    await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-mini]').click();
    await page.waitForTimeout(250);
  }
  const fullMetrics = await page.evaluate(() => {
    const card = document.querySelector('.moss-hud-card[data-hud-type="media"]');
    const audio = card?.querySelector('audio, video');
    if (audio) audio.dataset.qaTag = 'same-audio-node';
    const list = card?.querySelector('.moss-hud-tracklist');
    const viz = card?.querySelector('[data-media-viz], [data-sim-viz]');
    return {
      height: Math.round(card?.getBoundingClientRect().height || 0),
      width: Math.round(card?.getBoundingClientRect().width || 0),
      hasAudio: Boolean(audio),
      listVisible: Boolean(list && list.offsetHeight > 0),
      vizVisible: Boolean(viz && viz.offsetHeight > 0),
    };
  });
  console.log('  Media HUD Full:', fullMetrics);
  assert(fullMetrics.vizVisible, 'Spectrum canvas must be visible in full mode');
  await page.screenshot({ path: path.join(OUT, '04_media_hud_full.png') });

  // Click [−] to enter Mini Mode (Track Info + Live Spectrum ONLY!)
  await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-mini]').click();
  await page.waitForTimeout(350);
  const miniMetrics = await page.evaluate(() => {
    const card = document.querySelector('.moss-hud-card[data-hud-type="media"]');
    const audio = card?.querySelector('audio, video');
    const list = card?.querySelector('.moss-hud-tracklist');
    const controls = card?.querySelector('.moss-fui-media-controls');
    const scrub = card?.querySelector('.moss-fui-scrub');
    const miniPlay = card?.querySelector('.moss-hud-media-mini-play');
    const viz = card?.querySelector('[data-media-viz], [data-sim-viz]');
    return {
      dataMini: card?.getAttribute('data-mini'),
      height: Math.round(card?.getBoundingClientRect().height || 0),
      width: Math.round(card?.getBoundingClientRect().width || 0),
      sameAudioNode: !audio || audio.dataset.qaTag === 'same-audio-node',
      listHidden: !list || list.offsetHeight === 0,
      controlsHidden: !controls || controls.offsetHeight === 0,
      scrubHidden: !scrub || scrub.offsetHeight === 0,
      miniPlayVisible: Boolean(miniPlay && miniPlay.offsetHeight > 0),
      vizVisible: Boolean(viz && viz.offsetHeight > 0),
    };
  });
  console.log('  Media HUD Mini:', miniMetrics);
  assert(miniMetrics.dataMini === 'true', 'Card must set data-mini="true"');
  assert(miniMetrics.sameAudioNode, '<audio> node must NOT be recreated when toggling mini mode!');
  assert(miniMetrics.listHidden && miniMetrics.controlsHidden && miniMetrics.scrubHidden, 'Playlist/scrub/controls must be hidden in mini mode');
  assert(miniMetrics.miniPlayVisible && miniMetrics.vizVisible, 'Mini play button and live spectrum must be visible in mini mode');
  assert(miniMetrics.height < fullMetrics.height - 60, `Expected mini height (${miniMetrics.height}) significantly smaller than full (${fullMetrics.height})`);
  await page.screenshot({ path: path.join(OUT, '05_media_hud_mini.png') });

  // Restore back to full mode so localStorage returns to default
  await page.locator('.moss-hud-card[data-hud-type="media"] [data-hud-mini]').click();
  await page.waitForTimeout(250);

  // 6. Test EN / ZH toggle + verify 0 missing i18n keys
  console.log('[QA 5/5] Testing EN / ZH toggle & zero-error check...');
  await page.locator('.fui-lang__opt[lang="en"]').click();
  await page.waitForTimeout(250);
  await page.locator('.fui-lang__opt[lang="zh-CN"]').click();
  await page.waitForTimeout(250);

  console.log('Page errors   :', pageErrors.length);
  console.log('Console errors:', consoleErrors.length);
  if (pageErrors.length) console.error('PAGE_ERRORS:', pageErrors);
  if (consoleErrors.length) console.error('CONSOLE_ERRORS:', consoleErrors);

  const summary = {
    ok: pageErrors.length === 0 && consoleErrors.length === 0,
    briefingTxt,
    persTagAfterRational,
    fullMetrics,
    miniMetrics,
    pageErrors,
    consoleErrors,
  };
  fs.writeFileSync(path.join(OUT, 'qa-result.json'), JSON.stringify(summary, null, 2), 'utf8');
  assert(summary.ok, `Expected 0 page/console errors, got page=${pageErrors.length} console=${consoleErrors.length}`);
  console.log('ALL LIVE UI CLICK & VISUAL QA PASSED!');
} finally {
  await browser.close();
}
