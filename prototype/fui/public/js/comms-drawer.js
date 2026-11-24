/**
 * Slide-Out Tactical Comms Drawer & Detachable Floating Window Controller (MOSS v5.5).
 *
 * Modes:
 *   - drawer (default): smooth right-edge slide-out drawer with pull-tab (#comms-drawer-tab)
 *   - float: detached freely draggable & resizable floating tactical window over the console
 *   - popout button: opens standalone browser window (/mobile) for dual-monitor setups
 */
import { t } from './i18n.js';

const STORAGE_KEY = 'moss-fui.comms-drawer.v1';

export function createCommsDrawerController({ onModeChange } = {}) {
  const drawerEl = document.getElementById('comms-drawer');
  const tabEl = document.getElementById('comms-drawer-tab');
  const detachBtn = document.getElementById('comms-detach');
  const popoutBtn = document.getElementById('comms-popout');
  const collapseBtn = document.getElementById('comms-collapse');
  const dragHead = document.getElementById('comms-drag-head');
  const resizeHandle = document.getElementById('comms-resize-handle');
  const unreadDot = document.getElementById('comms-drawer-unread');

  const state = {
    mode: 'drawer', // 'drawer' | 'float'
    open: true,
    x: null,
    y: null,
    w: 440,
    h: 640,
  };

  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      if (saved.mode === 'float' || saved.mode === 'drawer') state.mode = saved.mode;
      if (typeof saved.open === 'boolean') state.open = saved.open;
      if (Number.isFinite(saved.x)) state.x = saved.x;
      if (Number.isFinite(saved.y)) state.y = saved.y;
      if (Number.isFinite(saved.w)) state.w = Math.max(340, Math.min(820, saved.w));
      if (Number.isFinite(saved.h)) state.h = Math.max(400, Math.min(880, saved.h));
    }
  } catch { /* ignore */ }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch { /* ignore */ }
  }

  function clampFloatBox() {
    const vw = window.innerWidth || 1440;
    const vh = window.innerHeight || 900;
    const w = Math.min(state.w, vw - 24);
    const h = Math.min(state.h, vh - 72);
    const maxX = Math.max(12, vw - w - 12);
    const maxY = Math.max(52, vh - h - 16);
    if (state.x === null) state.x = Math.max(12, vw - w - 32);
    if (state.y === null) state.y = Math.max(56, Math.round((vh - h) / 2));
    state.x = Math.max(12, Math.min(maxX, state.x));
    state.y = Math.max(52, Math.min(maxY, state.y));
    state.w = w;
    state.h = h;
  }

  function apply() {
    if (!drawerEl) return;
    drawerEl.dataset.mode = state.mode;
    drawerEl.dataset.open = String(state.open);
    if (state.mode === 'float') {
      clampFloatBox();
      drawerEl.style.left = `${state.x}px`;
      drawerEl.style.top = `${state.y}px`;
      drawerEl.style.width = `${state.w}px`;
      drawerEl.style.height = `${state.h}px`;
    } else {
      drawerEl.style.left = '';
      drawerEl.style.top = '';
      drawerEl.style.width = '';
      drawerEl.style.height = '';
    }
    if (tabEl) {
      tabEl.setAttribute('aria-expanded', String(state.open));
      tabEl.dataset.drawerOpen = String(state.open);
    }
    if (detachBtn) {
      const isFloat = state.mode === 'float';
      detachBtn.setAttribute('aria-pressed', String(isFloat));
      detachBtn.textContent = t(isFloat ? 'comms.dock' : 'comms.detach');
    }
    if (state.open && unreadDot) unreadDot.hidden = true;
    document.body.dataset.commsOpen = String(state.open);
    document.body.dataset.commsMode = state.mode;
    onModeChange?.({ ...state });
  }

  function open() {
    state.open = true;
    apply();
    save();
  }

  function close() {
    state.open = false;
    apply();
    save();
  }

  function toggle() {
    state.open = !state.open;
    apply();
    save();
  }

  function setMode(mode) {
    state.mode = mode === 'float' ? 'float' : 'drawer';
    state.open = true;
    apply();
    save();
  }

  function toggleDetach() {
    setMode(state.mode === 'float' ? 'drawer' : 'float');
  }

  function markUnread() {
    if (!state.open && unreadDot) unreadDot.hidden = false;
  }

  function resetLayout() {
    state.mode = 'drawer';
    state.open = true;
    state.x = null;
    state.y = null;
    state.w = 440;
    state.h = 640;
    apply();
    save();
  }

  tabEl?.addEventListener('click', toggle);
  collapseBtn?.addEventListener('click', close);
  detachBtn?.addEventListener('click', toggleDetach);
  popoutBtn?.addEventListener('click', () => {
    const url = `${location.origin}/mobile`;
    window.open(url, 'moss_comms_popout', 'width=440,height=780,resizable=yes,scrollbars=yes');
  });

  // Dragging detached floating comms window via header
  let dragCtx = null;
  dragHead?.addEventListener('pointerdown', (ev) => {
    if (state.mode !== 'float') return;
    if (ev.target?.closest?.('button, a, input, select')) return;
    dragCtx = {
      id: ev.pointerId,
      startX: ev.clientX,
      startY: ev.clientY,
      origX: state.x ?? 40,
      origY: state.y ?? 64,
    };
    dragHead.setPointerCapture?.(ev.pointerId);
    drawerEl.dataset.dragging = 'true';
  });
  dragHead?.addEventListener('pointermove', (ev) => {
    if (!dragCtx || ev.pointerId !== dragCtx.id) return;
    state.x = dragCtx.origX + (ev.clientX - dragCtx.startX);
    state.y = dragCtx.origY + (ev.clientY - dragCtx.startY);
    clampFloatBox();
    drawerEl.style.left = `${state.x}px`;
    drawerEl.style.top = `${state.y}px`;
  });
  const endDrag = () => {
    if (!dragCtx) return;
    dragCtx = null;
    if (drawerEl) delete drawerEl.dataset.dragging;
    save();
  };
  dragHead?.addEventListener('pointerup', endDrag);
  dragHead?.addEventListener('pointercancel', endDrag);

  // Resizing detached floating comms window via corner grip
  let resizeCtx = null;
  resizeHandle?.addEventListener('pointerdown', (ev) => {
    if (state.mode !== 'float') return;
    ev.preventDefault();
    resizeCtx = {
      id: ev.pointerId,
      startX: ev.clientX,
      startY: ev.clientY,
      origW: state.w,
      origH: state.h,
    };
    resizeHandle.setPointerCapture?.(ev.pointerId);
  });
  resizeHandle?.addEventListener('pointermove', (ev) => {
    if (!resizeCtx || ev.pointerId !== resizeCtx.id) return;
    state.w = Math.max(340, Math.min(820, resizeCtx.origW + (ev.clientX - resizeCtx.startX)));
    state.h = Math.max(400, Math.min(880, resizeCtx.origH + (ev.clientY - resizeCtx.startY)));
    clampFloatBox();
    drawerEl.style.width = `${state.w}px`;
    drawerEl.style.height = `${state.h}px`;
  });
  const endResize = () => {
    if (!resizeCtx) return;
    resizeCtx = null;
    save();
  };
  resizeHandle?.addEventListener('pointerup', endResize);
  resizeHandle?.addEventListener('pointercancel', endResize);

  window.addEventListener('resize', () => {
    if (state.mode === 'float') apply();
  });

  apply();

  return {
    open,
    close,
    toggle,
    setMode,
    toggleDetach,
    markUnread,
    resetLayout,
    refreshLocale: apply,
    isOpen: () => state.open,
    getMode: () => state.mode,
  };
}
