/**
 * Shared FUI HUD utilities (weather SVG glyphs, localization, XSS-safe escaping, URL sanitization).
 */
import { getLocale } from '../i18n.js';

export const GIB = 1024 ** 3;

export function weatherGlyphSvg(code, condition = '') {
  const txt = `${condition || ''}`;
  const isStorm = code >= 95 || /雷/.test(txt);
  const isRain = (code >= 51 && code <= 67) || (code >= 80 && code <= 82) || /雨/.test(txt);
  const isSnow = (code >= 71 && code <= 77) || (code >= 85 && code <= 86) || /雪/.test(txt);
  const isClear = code === 0 || code === 1 || /晴/.test(txt);
  if (isStorm) {
    return `<svg class="moss-hud-glyph" viewBox="0 0 40 40" aria-hidden="true"><path d="M10 22a6 6 0 0 1 1-11.9A8.5 8.5 0 0 1 27.5 11a5.5 5.5 0 0 1 1.5 10.8H10Z"/><path d="M21 22l-4 7h4l-2 7 7-9h-4l2-5" data-accent="warn"/></svg>`;
  }
  if (isRain) {
    return `<svg class="moss-hud-glyph" viewBox="0 0 40 40" aria-hidden="true"><path d="M10 22a6 6 0 0 1 1-11.9A8.5 8.5 0 0 1 27.5 11a5.5 5.5 0 0 1 1.5 10.8H10Z"/><path d="M14 26l-2 6M21 26l-2 6M28 26l-2 6" data-accent="data"/></svg>`;
  }
  if (isSnow) {
    return `<svg class="moss-hud-glyph" viewBox="0 0 40 40" aria-hidden="true"><path d="M10 21a6 6 0 0 1 1-11.9A8.5 8.5 0 0 1 27.5 10a5.5 5.5 0 0 1 1.5 10.8H10Z"/><circle cx="14" cy="28" r="1.5"/><circle cx="21" cy="30" r="1.5"/><circle cx="28" cy="28" r="1.5"/></svg>`;
  }
  if (isClear) {
    return `<svg class="moss-hud-glyph" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="7" data-accent="data"/><path d="M20 5v4M20 31v4M5 20h4M31 20h4M9.4 9.4l2.8 2.8M27.8 27.8l2.8 2.8M30.6 9.4l-2.8 2.8M12.2 27.8l-2.8 2.8"/></svg>`;
  }
  return `<svg class="moss-hud-glyph" viewBox="0 0 40 40" aria-hidden="true"><path d="M10 26a6 6 0 0 1 1-11.9A8.5 8.5 0 0 1 27.5 15a5.5 5.5 0 0 1 1.5 10.8H10Z"/><path d="M12 10h8M8 31h24" opacity="0.45"/></svg>`;
}

export function localizeCity(name) {
  if (!name) return '';
  if (getLocale().startsWith('zh')) return name;
  return { '上海': 'Shanghai', '北京': 'Beijing', '深圳': 'Shenzhen' }[name] || name;
}

export function safePopupUrl(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  if (s.startsWith('/') && !s.startsWith('//')) return s;
  return /^(?:https?:|blob:)/i.test(s) ? s : '';
}

export function escapeHtml(str) {
  return String(str ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
