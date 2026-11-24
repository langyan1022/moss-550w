function button(label, className) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = className;
  el.title = label;
  el.setAttribute('aria-label', label);
  const icon = document.createElement('i');
  icon.setAttribute('aria-hidden', 'true');
  el.append(icon);
  return el;
}

function clock(seconds) {
  if (!Number.isFinite(seconds)) return '–:––';
  const whole = Math.floor(seconds);
  return Math.floor(whole / 60) + ':' + String(whole % 60).padStart(2, '0');
}

export function projectionPlayer(media, {tracks, switchTrack, signal, clipStart = 0, clipSeconds} = {}) {
  const root = document.createElement('div');
  root.className = 'projection-player';
  root.dataset.kind = media.localName;
  media.controls = false;
  const bar = document.createElement('div');
  bar.className = 'player-controls';
  const toggle = button('播放', 'player-toggle');
  const seek = document.createElement('input');
  seek.type = 'range';
  seek.className = 'player-seek';
  seek.setAttribute('aria-label', '播放进度');
  seek.min = '0';
  seek.step = '0.1';
  const time = document.createElement('span');
  time.className = 'player-time';
  const silence = button('静音', 'player-mute');
  const volume = document.createElement('input');
  volume.type = 'range';
  volume.className = 'player-volume';
  volume.setAttribute('aria-label', '音量');
  volume.min = '0'; volume.max = '1'; volume.step = '0.05';
  const feedback = document.createElement('small');
  feedback.className = 'player-feedback';
  feedback.setAttribute('role', 'status');
  function sync() {
    const duration = clipSeconds || media.duration;
    const elapsed = Math.max(0, Math.min(media.currentTime - clipStart, duration || Infinity));
    const playing = !media.paused && !media.ended;
    root.toggleAttribute('data-playing', playing);
    toggle.title = toggle.ariaLabel = media.ended ? '重播' : playing ? '暂停' : '播放';
    const muted = media.muted || media.volume === 0;
    silence.title = silence.ariaLabel = muted ? '取消静音' : '静音';
    silence.setAttribute('aria-pressed', String(muted));
    volume.value = muted ? '0' : String(media.volume);
    volume.style.setProperty('--played', (Number(volume.value) * 100) + '%');
    seek.disabled = !(media.readyState >= 1 && Number.isFinite(duration) && duration > 0);
    seek.max = seek.disabled ? '1' : String(duration);
    seek.value = String(elapsed);
    seek.style.setProperty('--played', (seek.disabled ? 0 : 100 * elapsed / duration) + '%');
    time.textContent = clock(elapsed) + ' / ' + clock(duration);
    seek.setAttribute('aria-valuetext', time.textContent);
  }
  function playOrPause() {
    if (!media.paused && !media.ended) { media.pause(); return; }
    window.mossProjection.manual();
    media.play().catch(error => {
      if (error.name !== 'AbortError') {
        media.dispatchEvent(new CustomEvent('moss-playback-rejected',{detail:error.name}));
        feedback.textContent = media.error ? '媒体暂时无法载入。' : '请再次点击播放。';
      }
    });
  }
  toggle.addEventListener('click', playOrPause);
  seek.addEventListener('input', () => { if (!seek.disabled) media.currentTime = clipStart + Number(seek.value); });
  silence.addEventListener('click', () => {
    if (media.volume === 0) { media.volume = 0.5; media.muted = false; }
    else media.muted = !media.muted;
  });
  volume.addEventListener('input', () => { media.muted = false; media.volume = Number(volume.value); });
  if (tracks?.length > 1) {
    const index = tracks.findIndex(track => track.url === media.getAttribute('src'));
    for (const [step, label, name] of [[-1,'上一首','player-prev'],[1,'下一首','player-next']]) {
      const skip = button(label, name);
      const target = tracks[index + step];
      skip.disabled = !target;
      skip.addEventListener('click', () => switchTrack(target));
      bar.append(skip);
      if (step < 0) bar.append(toggle);
    }
  } else bar.append(toggle);
  bar.append(seek, time, silence, volume);
  if (media.localName === 'video') {
    media.playsInline = true;
    media.addEventListener('click', playOrPause, {signal});
    if (root.requestFullscreen || media.webkitEnterFullscreen) {
      const full = button('全屏播放', 'player-fullscreen');
      full.addEventListener('click', async () => {
        try {
          if (document.fullscreenElement === root) await document.exitFullscreen();
          else if (root.requestFullscreen) await root.requestFullscreen();
          else media.webkitEnterFullscreen();
        } catch { feedback.textContent = '当前窗口无法全屏。'; }
      });
      root.addEventListener('fullscreenchange', () => {
        full.title = full.ariaLabel = document.fullscreenElement === root ? '退出全屏' : '全屏播放';
      });
      bar.append(full);
    }
  }
  for (const event of ['timeupdate','durationchange','loadedmetadata','play','pause','ended','volumechange','emptied'])
    media.addEventListener(event, sync, {signal});
  media.addEventListener('playing', () => { feedback.textContent = ''; }, {signal});
  media.addEventListener('error', () => { feedback.textContent = '媒体暂时无法载入。'; }, {signal});
  root.append(bar, feedback);
  sync();
  return root;
}
