import {projectionPlayer} from "/conversation-assets/projection-player.js";
import {attachMusicSpectrum, configureMusicAnalysis} from "/conversation-assets/music-spectrum.js?v=fui-signal";

const labels = {full: "完整版", preview: "试听", unknown: "完整性未确认"};

function availability(item, mode, clipSeconds) {
  const label = document.createElement("span");
  label.className = "music-availability";
  const version = mode === "excerpt" || item?.preview === true ? "preview" : item?.availability;
  label.dataset.version = Object.hasOwn(labels, version) ? version : "unknown";
  label.textContent = labels[label.dataset.version];
  if (label.dataset.version === "preview" && Number.isFinite(clipSeconds) && clipSeconds > 0)
    label.textContent += " · " + Number(clipSeconds.toFixed(1)) + " 秒";
  return label;
}

function mount(audio, {items, selected, mode, chooseTrack, clipStart, clipSeconds}) {
  const listeners = new AbortController();
  const heading = document.createElement("div");
  heading.className = "player-heading";
  const title = document.createElement("strong");
  title.textContent = selected.title;
  heading.append(title, availability(selected, mode, clipSeconds));
  if (selected.artist) {
    const artist = document.createElement("span");
    artist.className = "player-artist";
    artist.textContent = selected.artist;
    heading.append(artist);
  }
  const src = audio.getAttribute("src");
  const tracks = items.flatMap((item, index) => {
    if (item.kind !== "audio" || !/^[a-f0-9]{32}$/.test(item.media_id)) return [];
    return [{item, number: index + 1, url: item.media_id === selected.media_id ? src : "/media/" + item.media_id}];
  });
  const controls = projectionPlayer(audio, {
    tracks,
    switchTrack: track => chooseTrack(track.item, track.number),
    signal: listeners.signal,
    clipStart: mode === "excerpt" && Number.isFinite(clipStart) ? clipStart : 0,
    clipSeconds: mode === "excerpt" && Number.isFinite(clipSeconds) && clipSeconds > 0 ? clipSeconds : undefined,
  });
  let disposeSpectrum;
  if (src && configureMusicAnalysis(audio, src)) {
    const canvas = document.createElement("canvas");
    canvas.className = "player-spectrum";
    canvas.setAttribute("aria-hidden", "true");
    controls.prepend(canvas);
    disposeSpectrum = attachMusicSpectrum(audio, canvas);
  }
  return {heading, controls, dispose() { listeners.abort(); disposeSpectrum?.(); }};
}

export const mossLegacyMusic = {availability, mount};
window.mossLegacyMusic = mossLegacyMusic;
