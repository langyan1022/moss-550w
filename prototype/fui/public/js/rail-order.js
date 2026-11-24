/**
 * Bidirectional Cross-Rail Drag & Drop + Vertical Reordering for MOSS 550W Station Cards (v5.5).
 *
 * - Drag any card header (`[data-rail-card] > .moss-rail-card__head`) vertically within its rail
 *   OR across between the Left Rail (#left-rail) and Right Rail (#right-rail-cards).
 * - Click the compact `⇄` swap button on any card header (or press Alt+ArrowLeft / Alt+ArrowRight)
 *   to instantaneously move a panel to the opposite rail while keeping live canvases/streams intact.
 * - Persists `{ left: [...], right: [...] }` in localStorage cleanly.
 */
import { t } from './i18n.js';

const STORAGE_KEY = 'moss-fui.dual-rail-layout.v2';
const LEGACY_KEY = 'moss-fui.left-rail-order';

export function initLeftRailOrder(leftRailEl, rightZoneEl = document.getElementById('right-rail-cards')) {
  if (!leftRailEl) return { reset() {}, getOrder: () => [], getLayout: () => ({ left: [], right: [] }), moveCard() {} };

  const zones = [leftRailEl, ...(rightZoneEl ? [rightZoneEl] : [])];
  const getZoneCards = (zone) => [...zone.querySelectorAll(':scope > .moss-rail-card[data-rail-card]')];
  const getAllCards = () => zones.flatMap(getZoneCards);
  const defaultLeft = getZoneCards(leftRailEl).map((el) => el.dataset.railCard);
  const defaultRight = rightZoneEl ? getZoneCards(rightZoneEl).map((el) => el.dataset.railCard) : [];

  function syncEmptyHints() {
    if (!rightZoneEl) return;
    const count = getZoneCards(rightZoneEl).length;
    rightZoneEl.dataset.empty = String(count === 0);
  }

  function ensureSwapButtons() {
    for (const card of getAllCards()) {
      const head = card.querySelector(':scope > .moss-rail-card__head');
      if (!head || head.querySelector('.moss-rail-swap-btn')) continue;
      if (!rightZoneEl) continue;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-rail-swap-btn';
      btn.textContent = '⇄';
      btn.title = t('rail.swap_side');
      btn.setAttribute('aria-label', t('rail.swap_side'));
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const curZone = card.parentElement === rightZoneEl ? 'right' : 'left';
        moveCard(card.dataset.railCard, curZone === 'left' ? 'right' : 'left');
      });
      head.append(btn);
    }
  }

  function saveLayout() {
    const layout = {
      left: getZoneCards(leftRailEl).map((el) => el.dataset.railCard),
      right: rightZoneEl ? getZoneCards(rightZoneEl).map((el) => el.dataset.railCard) : [],
    };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
    } catch { /* ignore */ }
    syncEmptyHints();
    window.dispatchEvent(new CustomEvent('moss-rail-layout-change', { detail: layout }));
  }

  function applyLayout(layout) {
    const byId = new Map(getAllCards().map((el) => [el.dataset.railCard, el]));
    const placed = new Set();
    const leftList = Array.isArray(layout?.left) ? layout.left : defaultLeft;
    const rightList = Array.isArray(layout?.right) ? layout.right : defaultRight;

    for (const id of leftList) {
      const node = byId.get(id);
      if (node && !placed.has(id)) {
        leftRailEl.append(node);
        placed.add(id);
      }
    }
    if (rightZoneEl) {
      for (const id of rightList) {
        const node = byId.get(id);
        if (node && !placed.has(id)) {
          rightZoneEl.append(node);
          placed.add(id);
        }
      }
    }
    for (const id of [...defaultLeft, ...defaultRight]) {
      const node = byId.get(id);
      if (node && !placed.has(id)) {
        leftRailEl.append(node);
        placed.add(id);
      }
    }
    syncEmptyHints();
  }

  function moveCard(cardId, targetSide) {
    const card = getAllCards().find((el) => el.dataset.railCard === cardId);
    const targetZone = targetSide === 'right' && rightZoneEl ? rightZoneEl : leftRailEl;
    if (!card || !targetZone) return false;
    targetZone.append(card);
    saveLayout();
    return true;
  }

  // Restore saved dual-rail layout (or migrate legacy single-rail order)
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (saved && Array.isArray(saved.left)) {
      applyLayout(saved);
    } else {
      const legacy = JSON.parse(localStorage.getItem(LEGACY_KEY) || 'null');
      if (Array.isArray(legacy)) applyLayout({ left: legacy, right: [] });
    }
  } catch { /* ignore */ }

  ensureSwapButtons();
  syncEmptyHints();

  let draggedCard = null;
  let dropTarget = null;
  let dropZone = null;
  let dropPos = 'before';

  function clearDropHints() {
    for (const card of getAllCards()) {
      delete card.dataset.drop;
      delete card.dataset.dragging;
    }
    for (const z of zones) delete z.dataset.dropActive;
    document.body.dataset.railDragging = 'false';
    dropTarget = null;
    dropZone = null;
  }

  for (const zone of zones) {
    zone.addEventListener('dragstart', (ev) => {
      const head = ev.target?.closest?.('.moss-rail-card__head');
      if (!head || ev.target?.closest?.('button, select, input')) {
        ev.preventDefault();
        return;
      }
      const card = head.closest('.moss-rail-card[data-rail-card]');
      if (!card) return;
      draggedCard = card;
      card.dataset.dragging = 'true';
      document.body.dataset.railDragging = 'true';
      ev.dataTransfer.effectAllowed = 'move';
      try {
        ev.dataTransfer.setData('text/plain', card.dataset.railCard || '');
      } catch { /* ignore */ }
    });

    zone.addEventListener('dragover', (ev) => {
      if (!draggedCard) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      for (const z of zones) z.dataset.dropActive = String(z === zone);
      dropZone = zone;
      const card = ev.target?.closest?.('.moss-rail-card[data-rail-card]');
      if (!card || card === draggedCard || card.parentElement !== zone) {
        for (const item of getAllCards()) delete item.dataset.drop;
        dropTarget = null;
        return;
      }
      const rect = card.getBoundingClientRect();
      const pos = ev.clientY < rect.top + rect.height * 0.5 ? 'before' : 'after';
      if (dropTarget !== card || dropPos !== pos) {
        for (const item of getAllCards()) delete item.dataset.drop;
        dropTarget = card;
        dropPos = pos;
        card.dataset.drop = pos;
      }
    });

    zone.addEventListener('drop', (ev) => {
      if (!draggedCard) {
        clearDropHints();
        return;
      }
      ev.preventDefault();
      const targetZone = dropZone || zone;
      if (dropTarget && dropTarget !== draggedCard && dropTarget.parentElement === targetZone) {
        if (dropPos === 'before') targetZone.insertBefore(draggedCard, dropTarget);
        else targetZone.insertBefore(draggedCard, dropTarget.nextSibling);
      } else {
        targetZone.append(draggedCard);
      }
      clearDropHints();
      draggedCard = null;
      saveLayout();
    });

    zone.addEventListener('dragend', () => {
      clearDropHints();
      draggedCard = null;
    });

    // Keyboard: Alt+ArrowUp/Down reorders; Alt+ArrowLeft/Right moves across rails
    zone.addEventListener('keydown', (ev) => {
      if (!ev.altKey) return;
      const card = ev.target?.closest?.('.moss-rail-card[data-rail-card]');
      if (!card) return;
      if (ev.key === 'ArrowUp' && card.previousElementSibling) {
        ev.preventDefault();
        zone.insertBefore(card, card.previousElementSibling);
        saveLayout();
      } else if (ev.key === 'ArrowDown' && card.nextElementSibling) {
        ev.preventDefault();
        zone.insertBefore(card, card.nextElementSibling.nextSibling);
        saveLayout();
      } else if (ev.key === 'ArrowRight' && rightZoneEl && zone === leftRailEl) {
        ev.preventDefault();
        moveCard(card.dataset.railCard, 'right');
      } else if (ev.key === 'ArrowLeft' && rightZoneEl && zone === rightZoneEl) {
        ev.preventDefault();
        moveCard(card.dataset.railCard, 'left');
      }
    });
  }

  return {
    reset() {
      applyLayout({ left: defaultLeft, right: defaultRight });
      saveLayout();
    },
    moveCard,
    getOrder: () => getZoneCards(leftRailEl).map((el) => el.dataset.railCard),
    getLayout: () => ({
      left: getZoneCards(leftRailEl).map((el) => el.dataset.railCard),
      right: rightZoneEl ? getZoneCards(rightZoneEl).map((el) => el.dataset.railCard) : [],
    }),
  };
}
