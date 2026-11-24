// Build only public, single-video player URLs from the provider's own watch page.
export function videoEmbed(value) {
  let page;
  try { page = new URL(value); } catch { return null; }
  if (!['http:', 'https:'].includes(page.protocol) || page.username || page.password || page.port) return null;
  const host = page.hostname.replace(/^www\./, '');
  const path = page.pathname;

  if (host === 'bilibili.com' || host === 'm.bilibili.com') {
    const video = /^\/video\/(BV[0-9A-Za-z]{10}|av[1-9]\d*)\/?$/.exec(path);
    const episode = /^\/bangumi\/play\/ep([1-9]\d*)\/?$/.exec(path);
    if (!video && !episode) return null;
    const player = new URL('https://player.bilibili.com/player.html');
    if (episode) player.searchParams.set('episodeId', episode[1]);
    else {
      const id = video[1];
      if (id.startsWith('BV')) player.searchParams.set('bvid', id);
      else player.searchParams.set('aid', id.slice(2));
      const part = page.searchParams.get('p');
      if (/^[1-9]\d{0,4}$/.test(part ?? '')) player.searchParams.set('p', part);
    }
    player.searchParams.set('autoplay', '1');
    return {url: player.href, provider: '哔哩哔哩'};
  }
  if (host === 'douyin.com') {
    const video = /^\/video\/([1-9]\d{15,19})\/?$/.exec(path);
    if (!video) return null;
    return {url: 'https://open.douyin.com/player/video?vid=' + video[1] + '&autoplay=1', provider: '抖音'};
  }
  if (host === 'v.qq.com') {
    const video = /^\/x\/(?:cover\/([A-Za-z0-9]+)\/|page\/)([A-Za-z0-9]{11})\.html$/.exec(path);
    if (!video) return null;
    const player = new URL('https://v.qq.com/txp/iframe/player.html');
    player.searchParams.set('vid', video[2]);
    if (video[1]) player.searchParams.set('cid', video[1]);
    player.searchParams.set('autoplay', 'true');
    return {url: player.href, provider: '腾讯视频'};
  }
  if (host === 'youtube.com' || host === 'm.youtube.com' || host === 'youtu.be') {
    let id;
    if (host === 'youtu.be') id = /^\/([\w-]{11})\/?$/.exec(path)?.[1];
    else if (path === '/watch') id = page.searchParams.get('v');
    else id = /^\/(?:shorts|live)\/([\w-]{11})\/?$/.exec(path)?.[1];
    if (!id || !/^[\w-]{11}$/.test(id)) return null;
    return {url: 'https://www.youtube.com/embed/' + id + '?autoplay=1&playsinline=1', provider: 'YouTube'};
  }
  if (host === 'vimeo.com') {
    const video = /^\/([1-9]\d{0,11})\/?$/.exec(path);
    if (!video) return null;
    return {url: 'https://player.vimeo.com/video/' + video[1] + '?autoplay=1', provider: 'Vimeo'};
  }
  return null;
}
