let foreground, covered;

export function bindProjection(panel, {onHidden} = {}) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let timer = 0;
  let exitTimer = 0;
  let hovering = false;
  let timed = true;
  let pinned = false;
  panel.classList.add('media-projection');

  function reading() {
    if (pinned || hovering || panel.matches(':focus-within')) return true;
    if (panel.hasAttribute('data-media-pending') || panel.querySelector('.player-window')) return true;
    return [...panel.querySelectorAll('audio,video')].some(media => media.hasAttribute('data-awaiting-narration') || (!media.paused && !media.ended));
  }

  function renew() {
    clearTimeout(timer);
    if (!timed || panel.hidden || document.hidden || panel.hasAttribute('data-leaving') || reading()) return;
    timer = setTimeout(() => { if (!reading()) hide(); }, 8000);
  }

  function hold(value) {
    pinned = value;
    renew();
  }

  function cover() {
    clearTimeout(timer);
    clearTimeout(exitTimer);
    hovering = false;
    panel.hidden = true;
    panel.removeAttribute('data-leaving');
  }

  function uncover() {
    panel.hidden = false;
    renew();
  }

  const projection = {reading, cover, uncover, hide};

  function show(options = {}) {
    if (foreground !== projection) {
      const previous = foreground;
      if (previous?.reading() && !options.manual) return false;
      covered = null;
      foreground = projection;
      if (previous?.reading()) {
        previous.cover();
        covered = previous;
      } else {
        previous?.hide({immediate:true});
      }
    }
    const wasOpen = !panel.hidden;
    clearTimeout(exitTimer);
    panel.removeAttribute('data-leaving');
    panel.hidden = false;
    timed = options.timed ?? true;
    hold(options.pinned ?? pinned);
    if (wasOpen && !reduced.matches) {
      for (const effect of panel.getAnimations({subtree:true})) {
        if (['moss-projection-enter','moss-edge-lock','moss-signal-slice'].includes(effect.animationName)) {
          effect.currentTime = 0;
          effect.play();
        }
      }
    }
    return true;
  }

  function hide({immediate = false, afterHidden} = {}) {
    clearTimeout(timer);
    clearTimeout(exitTimer);
    if (panel.hidden) return;
    panel.setAttribute('data-leaving','');
    for (const media of panel.querySelectorAll('audio,video')) media.pause();
    function finish() {
      panel.hidden = true;
      panel.removeAttribute('data-leaving');
      hovering = false;
      pinned = false;
      onHidden?.();
      if (foreground === projection) {
        foreground = covered;
        covered = null;
        foreground?.uncover();
      }
      afterHidden?.();
    }
    if (immediate || reduced.matches) finish();
    else exitTimer = setTimeout(finish,300);
  }

  panel.addEventListener('pointerenter', event => {
    hovering = event.pointerType === 'mouse' || event.pointerType === 'pen';
    renew();
  });
  panel.addEventListener('pointerleave',()=>{ hovering = false; renew(); });
  panel.addEventListener('focusin',renew);
  panel.addEventListener('focusout',()=>queueMicrotask(renew));
  panel.addEventListener('scroll',renew,{passive:true,capture:true});
  for (const type of ['play','pause','ended']) panel.addEventListener(type,renew,true);
  document.addEventListener('visibilitychange',renew);
  window.addEventListener('pagehide',()=>hide({immediate:true}));
  return {show,hide,hold,renew,reading,covered:()=>covered === projection};
}
