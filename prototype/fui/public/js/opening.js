/**
 * Preserved 550W Opening Animation Controller.
 * Replicates the two-stage opening sequence:
 *   1. 550W SVG stroke-trace + fill reveal with bilingual caption
 *   2. moss-opening.mp4 zoom-out film with bottom progress bar
 *
 * Skips automatically under prefers-reduced-motion, ?skipOpening=1, ?beat=,
 * ?paused=1, or repeat loads within the same session — always replayable via
 * the top-left 550W brand button or Settings -> Replay opening.
 */

let cachedPaths = null;

async function loadMarkPaths() {
  const urls = ['/conversation-assets/550w.svg', '/vendor/logos/550w.svg'];
  for (const url of urls) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) continue;
      const doc = new DOMParser().parseFromString(await resp.text(), 'image/svg+xml');
      const paths = [...doc.documentElement.querySelectorAll('path')];
      if (paths.length) return paths;
    } catch { /* try fallback */ }
  }
  return [];
}

export function initOpening({ onFinish } = {}) {
  const overlay = document.getElementById('opening-overlay');
  const identity = document.getElementById('opening-identity');
  const mark = document.getElementById('opening-mark');
  const film = document.getElementById('opening-film');
  const progress = document.getElementById('opening-position');
  const waiting = document.getElementById('opening-wait-model');
  const skipBtn = document.getElementById('skip-opening');
  const stageMain = document.querySelector('main');
  const model = document.getElementById('moss-model');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');

  let timer = 0;
  let stallTimer = 0;
  let raf = 0;
  let revision = 0;

  function finish(notify = true) {
    const wasVisible = Boolean(overlay && !overlay.hidden);
    revision += 1;
    clearTimeout(timer);
    clearTimeout(stallTimer);
    cancelAnimationFrame(raf);
    if (stageMain) delete stageMain.dataset.opening;
    if (film) {
      film.pause();
      film.hidden = true;
    }
    if (identity) identity.hidden = true;
    if (waiting) waiting.hidden = true;
    if (overlay) {
      overlay.hidden = true;
      delete overlay.dataset.leaving;
    }
    if (notify && wasVisible) onFinish?.({ wasVisible: true });
  }

  function updateCrop() {
    if (!stageMain || stageMain.dataset.opening !== 'film') return;
    const t = reduced.matches ? 1 : Math.max(0, Math.min(1, (film.currentTime - 0.13) / 1.27));
    const rem = 1 - t * t * (3 - 2 * t);
    film.style.setProperty('--opening-zoom', (1 + 0.62 * rem).toFixed(4));
    if (t < 1 && !film.paused && !film.ended) {
      raf = requestAnimationFrame(updateCrop); // fui-lint-ignore: R15 bounded to 1.27s opening film zoom-out
    }
  }

  function prepareFilm() {
    if (!film?.dataset.src) return;
    film.preload = 'auto';
    if (film.getAttribute('src') !== film.dataset.src || film.error) film.src = film.dataset.src;
    else if (film.currentTime) film.currentTime = 0;
  }

  function modelStillLoading() {
    return model && !model.hidden && model.dataset.ready !== 'true' && !model.dataset.error;
  }

  function holdLastFrame() {
    clearTimeout(stallTimer);
    cancelAnimationFrame(raf);
    if (stageMain) stageMain.dataset.opening = 'handoff';
    const began = performance.now();
    const minimum = reduced.matches ? 100 : 420;
    const deadline = began + 2500;
    const settle = () => {
      if (modelStillLoading() && performance.now() < deadline) {
        if (waiting) waiting.hidden = false;
        timer = setTimeout(settle, 60);
        return;
      }
      if (waiting) waiting.hidden = true;
      if (overlay) overlay.dataset.leaving = 'true';
      timer = setTimeout(() => finish(), reduced.matches ? 0 : 240);
    };
    timer = setTimeout(settle, minimum);
  }

  function failedPlayback(own) {
    if (own !== revision || overlay?.hidden) return;
    if (stageMain?.dataset.opening === 'handoff') return;
    film?.pause();
    holdLastFrame();
  }

  function watchPlayback() {
    clearTimeout(stallTimer);
    const own = revision;
    stallTimer = setTimeout(() => failedPlayback(own), 10000);
  }

  async function playFilm(own) {
    if (own !== revision || !film) return;
    if (stageMain) stageMain.dataset.opening = 'buffering';
    watchPlayback();
    try {
      await film.play();
    } catch {
      failedPlayback(own);
      return;
    }
    if (own !== revision || stageMain?.dataset.opening !== 'buffering') return;
    if (stageMain) stageMain.dataset.opening = 'film';
    if (identity) identity.hidden = true;
    film.hidden = false;
    updateCrop();
  }

  async function start(explicit = false) {
    finish(false);
    const qs = new URLSearchParams(location.search);
    const autoSkipped =
      reduced.matches ||
      qs.get('skipOpening') === '1' ||
      qs.has('beat') ||
      qs.get('paused') === '1' ||
      qs.get('chat') === '1' ||
      sessionStorage.getItem('moss-fui.opened') === '1';

    if (!explicit && autoSkipped) return;
    try { sessionStorage.setItem('moss-fui.opened', '1'); } catch { /* ignore */ }

    const own = revision;
    if (progress) progress.value = 0;
    if (stageMain) stageMain.dataset.opening = 'identity';
    overlay.hidden = false;
    identity.hidden = false;
    film.hidden = true;
    // Start buffering while the identity animation and model loading are both running.
    prepareFilm();

    try {
      cachedPaths ??= await loadMarkPaths();
      if (own !== revision) return;
      if (cachedPaths.length && mark) {
        mark.replaceChildren(...cachedPaths.map((p) => document.importNode(p, true)));
      }
    } catch { /* The film can still play when the mark fails to load. */ }
    if (own !== revision) return;

    timer = setTimeout(() => playFilm(own), reduced.matches ? 550 : 2100);
  }

  film?.addEventListener('ended', () => {
    if (stageMain?.dataset.opening === 'film' && film.ended && film.currentTime >= film.duration - .08) holdLastFrame();
  });
  film?.addEventListener('error', () => {
    if (film.error && ['buffering', 'film'].includes(stageMain?.dataset.opening)) failedPlayback(revision);
  });
  film?.addEventListener('timeupdate', () => {
    if (stageMain?.dataset.opening !== 'film') return;
    if (progress && film.duration) progress.value = film.currentTime / film.duration;
    if (!film.ended) watchPlayback();
  });
  skipBtn?.addEventListener('click', finish);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay && !overlay.hidden) {
      e.preventDefault();
      finish();
    }
  });

  return {
    start,
    finish,
    isActive: () => Boolean(overlay && !overlay.hidden),
  };
}
