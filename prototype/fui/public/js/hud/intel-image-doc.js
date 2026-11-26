/**
 * HUD card view renderers for Intel Dossier, Image Inspector, Doc Viewer,
 * Action Approval Gate, and Context Capture tools.
 */
import { getLocale, t } from '../i18n.js';
import { INTEL_DOSSIERS } from '../intel-dossiers.js';
import { escapeHtml, safePopupUrl } from './utils.js';

export function populateIntelCard(card, titleEl, body, { slideIndexMap, renderOpenCards, openDossier, onSendFollowUp }) {
  const isZh = getLocale().startsWith('zh');
  const src = card.intelMatch?.dossier
    ? card.intelMatch.dossier[isZh ? 'zh' : 'en']
    : card.intelMatch?.custom || {};
  const slides = src.slides || [];
  const curIdx = Math.min(Math.max(0, slideIndexMap.get(card.key) || 0), Math.max(0, slides.length - 1));
  const slide = slides[curIdx];
  titleEl.textContent = src.title || t('hud.intel_default');

  let visualHtml = '';
  const slideSrc = safePopupUrl(slide?.src);
  if (slide && slideSrc) {
    if (slide.kind === 'plate-light') {
      visualHtml = `<div class="moss-hud-visual moss-hud-visual--light moss-slide-anim"><img class="moss-hud-visual__img" src="${escapeHtml(slideSrc)}" alt="${escapeHtml(slide.caption || src.title)}"></div>`;
    } else if (slide.kind === 'plate-dark') {
      visualHtml = `<div class="moss-hud-visual moss-hud-visual--seal moss-slide-anim"><img class="moss-hud-visual__seal" src="${escapeHtml(slideSrc)}" alt="${escapeHtml(slide.caption || src.title)}"></div>`;
    } else if (slide.kind === 'plate-mask') {
      visualHtml = `<div class="moss-hud-visual moss-hud-visual--seal moss-slide-anim"><span class="moss-hud-visual__mask" style="--hud-mask: url('${escapeHtml(slideSrc)}')" role="img" aria-label="${escapeHtml(slide.caption || src.title)}"></span></div>`;
    } else {
      visualHtml = `<div class="moss-hud-visual moss-slide-anim"><img class="moss-hud-visual__photo" src="${escapeHtml(slideSrc)}" alt="${escapeHtml(slide.caption || src.title)}"></div>`;
    }
  }

  const pagerHtml = slides.length > 1
    ? `<div class="moss-hud-pager">
        <span class="moss-hud-caption">${escapeHtml(slide?.caption || '')}</span>
        <div class="moss-hud-pager__btns">
          <button class="moss-hud-ctrl" type="button" data-slide-dir="-1" ${curIdx <= 0 ? 'disabled' : ''}>‹</button>
          <span class="fui-num">${String(curIdx + 1).padStart(2, '0')} / ${String(slides.length).padStart(2, '0')}</span>
          <button class="moss-hud-ctrl" type="button" data-slide-dir="1" ${curIdx >= slides.length - 1 ? 'disabled' : ''}>›</button>
        </div>
      </div>`
    : (slide?.caption ? `<p class="moss-hud-caption">${escapeHtml(slide.caption)}</p>` : '');

  const specsHtml = (src.specs || []).length
    ? `<dl class="moss-hud-specs">${src.specs.map(([k, v]) => `<div><dt class="fui-label">${escapeHtml(k)}</dt><dd class="fui-num">${escapeHtml(v)}</dd></div>`).join('')}</dl>`
    : '';

  body.innerHTML = `
    ${src.kicker ? `<span class="fui-label moss-hud-kicker">${escapeHtml(src.kicker)}</span>` : ''}
    ${visualHtml}
    ${pagerHtml}
    ${src.summary ? `<p class="moss-hud-summary">${escapeHtml(src.summary)}</p>` : ''}
    ${specsHtml}
    <div class="moss-hud-foot">
      <div class="moss-hud-dossier-nav">
        ${INTEL_DOSSIERS.map((d) => `<button class="moss-hud-ctrl" type="button" data-dossier-id="${d.id}" aria-pressed="${String(`intel:${d.id}` === card.key)}">${escapeHtml(d.code.replace('INTEL-', ''))}</button>`).join('')}
      </div>
      ${src.followUp ? `<button class="fui-btn" type="button" data-follow-up>${escapeHtml(t('hud.ask_related'))}</button>` : ''}
    </div>`;

  for (const btn of body.querySelectorAll('[data-slide-dir]')) {
    btn.addEventListener('click', () => {
      const next = curIdx + Number(btn.dataset.slideDir);
      slideIndexMap.set(card.key, Math.min(slides.length - 1, Math.max(0, next)));
      renderOpenCards({ refreshKey: card.key });
    });
  }
  for (const btn of body.querySelectorAll('[data-dossier-id]')) {
    btn.addEventListener('click', () => openDossier(btn.dataset.dossierId));
  }
  body.querySelector('[data-follow-up]')?.addEventListener('click', () => onSendFollowUp?.(src.followUp));
}

export function populateImageCard(card, titleEl, body, { imageViewStates, renderOpenCards }) {
  let imageViewState = imageViewStates.get(card.key);
  if (!imageViewState) {
    imageViewState = { zoom: 1, rotate: 0, idx: 0 };
    imageViewStates.set(card.key, imageViewState);
  }
  const imgs = card.images || [];
  const idx = Math.min(Math.max(0, imageViewState.idx), Math.max(0, imgs.length - 1));
  const cur = imgs[idx] || {};
  titleEl.textContent = cur.title || t('hud.image_title');

  const pct = Math.round(imageViewState.zoom * 100);
  body.innerHTML = `
    <div class="moss-img-toolbar">
      <button class="moss-hud-ctrl" type="button" data-img-zoom="-0.25">−</button>
      <span class="fui-num">${pct}%</span>
      <button class="moss-hud-ctrl" type="button" data-img-zoom="0.25">+</button>
      <button class="moss-hud-ctrl" type="button" data-img-rot>↻ 90°</button>
      <button class="moss-hud-ctrl" type="button" data-img-fit>${escapeHtml(t('hud.fit'))}</button>
      ${imgs.length > 1 ? `<span class="moss-img-toolbar__sep"></span><button class="moss-hud-ctrl" type="button" data-img-nav="-1" ${idx <= 0 ? 'disabled' : ''}>‹</button><span class="fui-num">${String(idx + 1).padStart(2, '0')}/${String(imgs.length).padStart(2, '0')}</span><button class="moss-hud-ctrl" type="button" data-img-nav="1" ${idx >= imgs.length - 1 ? 'disabled' : ''}>›</button>` : ''}
    </div>
    <div class="moss-img-stage">
      <img class="moss-img-stage__img" src="${escapeHtml(safePopupUrl(cur.src))}" alt="${escapeHtml(cur.title || 'Image')}"
           style="transform: scale(${imageViewState.zoom}) rotate(${imageViewState.rotate}deg)">
    </div>
    ${cur.caption ? `<p class="moss-hud-caption">${escapeHtml(cur.caption)}</p>` : ''}
    <div class="moss-hud-foot">
      <span class="fui-num moss-card__meta">IMG VIEWER · ${pct}% · ${imageViewState.rotate}°</span>
      ${safePopupUrl(cur.src) ? `<a class="fui-btn" href="${escapeHtml(safePopupUrl(cur.src))}" target="_blank" rel="noopener">${escapeHtml(t('hud.open_raw'))}</a>` : ''}
    </div>`;

  for (const b of body.querySelectorAll('[data-img-zoom]')) {
    b.addEventListener('click', () => {
      imageViewState.zoom = Math.min(2.5, Math.max(0.5, +(imageViewState.zoom + Number(b.dataset.imgZoom)).toFixed(2)));
      renderOpenCards({ refreshKey: card.key });
    });
  }
  body.querySelector('[data-img-rot]')?.addEventListener('click', () => {
    imageViewState.rotate = (imageViewState.rotate + 90) % 360;
    renderOpenCards({ refreshKey: card.key });
  });
  body.querySelector('[data-img-fit]')?.addEventListener('click', () => {
    imageViewState.zoom = 1;
    imageViewState.rotate = 0;
    renderOpenCards({ refreshKey: card.key });
  });
  for (const b of body.querySelectorAll('[data-img-nav]')) {
    b.addEventListener('click', () => {
      imageViewState.idx = Math.min(imgs.length - 1, Math.max(0, idx + Number(b.dataset.imgNav)));
      imageViewState.zoom = 1;
      imageViewState.rotate = 0;
      renderOpenCards({ refreshKey: card.key });
    });
  }
}

export function populateDocCard(card, titleEl, body, { fileInputEl }) {
  const doc = card.doc || {};
  const fmt = (doc.format || 'text').toLowerCase();
  titleEl.textContent = doc.title || doc.filename || t('hud.doc_title');

  const safeDocUrl = safePopupUrl(doc.url);
  let contentHtml = '';
  if (fmt === 'pdf' && safeDocUrl) {
    contentHtml = `<iframe class="moss-doc-pdf" src="${escapeHtml(safeDocUrl)}" title="${escapeHtml(titleEl.textContent)}"></iframe>`;
  } else if (doc.blocks?.length) {
    contentHtml = `<div class="moss-doc-sheet">${doc.blocks.map((b) => {
      if (b.kind === 'heading') return `<h3 class="moss-doc-h">${escapeHtml(b.text)}</h3>`;
      if (b.kind === 'table') {
        return `<div class="fui-table-wrap"><table class="fui-table fui-table--compact"><tbody>${b.rows.map((r) => `<tr>${r.map((c) => `<td>${escapeHtml(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      }
      return `<p class="moss-doc-p">${escapeHtml(b.text)}</p>`;
    }).join('')}</div>`;
  } else {
    contentHtml = `<pre class="moss-pre moss-doc-pre">${escapeHtml(doc.text || t('hud.doc_empty'))}</pre>`;
  }

  body.innerHTML = `
    <div class="moss-doc-meta">
      <span class="fui-badge" data-state="warn">${escapeHtml(fmt.toUpperCase())}</span>
      <span class="fui-num moss-card__meta">${escapeHtml(doc.size || doc.pages || 'READ-ONLY VIEW')}</span>
      <button class="moss-hud-ctrl" type="button" data-doc-load>${escapeHtml(t('hud.open_local_doc'))}</button>
    </div>
    ${contentHtml}
    <div class="moss-hud-foot">
      <span class="fui-num moss-card__meta">${escapeHtml(t('hud.doc_drop_hint'))}</span>
      ${safeDocUrl ? `<a class="fui-btn" href="${escapeHtml(safeDocUrl)}" target="_blank" rel="noopener">${escapeHtml(t('hud.open_raw'))}</a>` : ''}
    </div>`;

  body.querySelector('[data-doc-load]')?.addEventListener('click', () => fileInputEl?.click());
}

export function populateActionCard(card, titleEl, body, cardEl, { onApprove, onCancel }) {
  const turn = card.turn || {};
  const isPending = turn.status === 'pending';
  if (isPending) {
    cardEl.classList.add('moss-hud-card--warn');
    titleEl.textContent = turn.desktop_confirmation?.title || t('hud.action_pending_title');
    const preview = turn.desktop_confirmation?.detail || turn.preview || '';
    const approveLabel = turn.desktop_confirmation
      ? t('turn.approve_desktop')
      : turn.verify_only
        ? t('turn.approve_verify')
        : t('turn.approve_draft');
    const cancelLabel = turn.desktop_confirmation ? t('turn.cancel_action') : t('turn.cancel_draft');

    const isC = document.body.dataset.authRole === 'C';
    body.innerHTML = `
      <div class="fui-hazard moss-hud-hazard" aria-hidden="true"></div>
      <p class="moss-hud-caption">${escapeHtml(turn.desktop_confirmation ? t('turn.pending_desktop') : t('turn.pending_draft'))}</p>
      <pre class="moss-pre moss-hud-pre"></pre>
      <div class="moss-hud-foot">
        <button class="fui-btn" type="button" data-hud-cancel>${escapeHtml(cancelLabel)}</button>
        ${isC ? '' : `<button class="fui-btn fui-btn--primary" type="button" data-hud-approve>${escapeHtml(approveLabel)}</button>`}
      </div>`;
    body.querySelector('.moss-hud-pre').textContent = preview;
    body.querySelector('[data-hud-approve]')?.addEventListener('click', () => onApprove?.(turn.id));
    body.querySelector('[data-hud-cancel]')?.addEventListener('click', () => onCancel?.(turn.id));
  } else if (turn.receipt) {
    titleEl.textContent = t('turn.draft_receipt');
    body.innerHTML = `
      <pre class="moss-pre moss-hud-pre"></pre>
      <div class="moss-hud-foot">
        <span class="fui-badge" data-state="ok">${escapeHtml(t('turn.saved'))}</span>
        ${turn.receipt.filename ? `<a class="fui-btn" href="/drafts/${encodeURIComponent(turn.receipt.filename)}" target="_blank" rel="noopener">${escapeHtml(t('turn.open_draft'))}</a>` : ''}
      </div>`;
    body.querySelector('.moss-hud-pre').textContent = turn.receipt.text || '';
  }
}

export function populateContextCard(card, titleEl, body, { getLatestState, onContextSubmit }) {
  const turn = card.turn || {};
  const action = typeof turn.context_action === 'string' ? turn.context_action : (turn.context_action?.action || 'clipboard');
  const labelKey = {
    clipboard: 'hud.ctx_clipboard',
    screen_text: 'hud.ctx_ocr',
    screenshot: 'hud.ctx_screenshot',
    screen_image: 'hud.ctx_image',
  }[action] || 'hud.ctx_clipboard';
  titleEl.textContent = t(labelKey);
  const widget = document.createElement('div');
  widget.className = 'context-widget moss-stack';
  body.append(widget);
  import('/static/context-tools.js').then(({ mountContextTool }) => {
    if (!widget.isConnected) return;
    const contextTurn = { ...turn, context_action: action };
    mountContextTool(widget, contextTurn,
      (incoming, goal, requestId) => onContextSubmit?.({ turn: contextTurn, kind: 'reply', incoming, goal, requestId }),
      {
        model: () => {
          const st = getLatestState();
          return { id: st?.model_id, name: st?.model };
        },
        send: (question, excerpt, modelId) => onContextSubmit?.({ turn: contextTurn, kind: 'question', question, excerpt, modelId }),
      }, {
        model: () => getLatestState()?.vision_model,
        send: (question, image, modelId) => onContextSubmit?.({ turn: contextTurn, kind: 'image', question, image, modelId }),
      });
    widget.querySelector('button')?.classList.add('fui-btn', 'fui-btn--primary');
  }).catch(() => {
    if (widget.isConnected) widget.textContent = t('hud.ctx_failed');
  });
}
