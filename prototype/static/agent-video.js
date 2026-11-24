import {projectionPlayer} from "/conversation-assets/projection-player.js";
import {videoEmbed} from "/conversation-assets/video-embed.js";

function create(item, media = document.createElement("video")) {
  if (media.localName !== "video" || !/^[a-f0-9]{32}$/.test(item.media_id))
    throw new TypeError("视频文件或播放器无效。");
  const url = "/media/" + item.media_id;
  media.playsInline = true;
  media.setAttribute("aria-label", item.title || "影像资料");
  if ((media.getAttribute("src") || "").split("#", 1)[0] !== url) {
    media.preload = "metadata";
    media.src = url;
  }
  const root = projectionPlayer(media);
  root.prepend(media);
  return {root, media};
}

function createExternal(item) {
  const root = document.createElement("div");
  const heading = document.createElement("div");
  heading.className = "player-heading";
  const title = document.createElement("strong");
  title.textContent = item.title || "影像资料";
  heading.append(title);
  root.append(heading);

  let page;
  try { page = new URL(item.url); } catch { /* 缺少可访问的片源。 */ }
  if (page && ["http:", "https:"].includes(page.protocol) && !page.username && !page.password) {
    const link = document.createElement("a");
    link.href = page.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "打开片源 ↗";
    heading.append(link);
  }

  const embedded = videoEmbed(item.url);
  if (embedded) {
    const frame = document.createElement("iframe");
    frame.className = "player-window";
    frame.title = embedded.provider + " · " + title.textContent;
    frame.allow = "autoplay; fullscreen; encrypted-media; picture-in-picture";
    frame.allowFullscreen = true;
    frame.referrerPolicy = "strict-origin-when-cross-origin";
    frame.src = embedded.url;
    root.append(frame);
  } else {
    const note = document.createElement("small");
    note.className = "player-feedback";
    note.textContent = heading.querySelector("a") ? "此片源需在原站观看。" : "片源地址无效。";
    root.append(note);
  }
  return {root, media: null};
}

async function locateClip(media, bounds) {
  if (media.readyState < HTMLMediaElement.HAVE_METADATA) {
    await new Promise((resolve, reject) => {
      const listeners = new AbortController();
      const finish = () => { clearTimeout(timer); listeners.abort(); };
      const failed = () => { finish(); reject(new Error("未取得视频时长")); };
      const timer = setTimeout(failed, 15000);
      media.addEventListener("loadedmetadata", () => { finish(); resolve(); }, {signal:listeners.signal});
      media.addEventListener("error", failed, {signal:listeners.signal});
    });
  }
  if (Number.isFinite(media.duration)) {
    if (bounds.start >= media.duration) throw new RangeError("这句字幕的起点超出了视频时长。");
    bounds.end = Math.min(bounds.end, media.duration);
  }
  media.currentTime = bounds.start;
}

export const mossLegacyVideo = {create, locateClip, canEmbed: url => Boolean(videoEmbed(url)), createExternal};
window.mossLegacyVideo = mossLegacyVideo;
