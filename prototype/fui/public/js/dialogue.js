/**
 * Always-On Right Rail Tactical MOSS Comms Log (.fui-chat).
 *
 * Strictly follows references/components/chat.md + game-FUI tactical command discipline:
 *  - Left-aligned sequence-indexed attributed statements (NO bubbles, NO zig-zag, NO avatars)
 *  - Bright leading bar for OP-01, line-3 leading bar for MOSS
 *  - Structured inline cards + 1-click contextual HUD popup triggers for every turn
 *  - Compact Quick-Directive bar right above the IME-safe composer
 */
import { matchIntelDossier } from './hud-popups.js';
import { formatClock, t } from './i18n.js';
import { safeCopyText } from './safe-web.js';
import { renderGenerativeWidget } from './brain-deck.js';
import { submitWindowReplyByVoice } from '/static/context-tools.js';

const ACTIVE_STATES = new Set(['thinking', 'pending', 'saving']);
const MEDIA_INTENT_RE = /(?:来|放|播|点|听|选|唱).*?歌(?:儿)?|(?<!谷)歌(?:曲|单|词|谱)|播放|音乐|视频|曲库|媒体|mv|music|play\s|曲儿/i;
const PLAYBACK_TARGET = '(?:(?:当前(?:的)?|这(?:首|段|个))?(?:音乐|歌曲|歌|音频|视频|影片|短片|电影|播放器)|这(?:一)?首)';
const PLAYBACK_CONTROL = '(?:暂停|停一下|停一停|停下|停止|关掉|关闭|继续|恢复|接着)';
const PLAYBACK_VERB = '(?:播放|放|播)';
const PLAYBACK_MODIFIER = String.raw`\s*(?:一下|下)?\s*`;
const EXPLICIT_PLAYBACK_CONTROL = new RegExp(
  String.raw`^(?:(?:MOSS|莫斯|摩斯)\s*[,，、]\s*)?(?:(?:请你?|麻烦你?|帮我|给我|先|那就)\s*)*(?:` +
  String.raw`${PLAYBACK_CONTROL}(?:${PLAYBACK_VERB}(?:${PLAYBACK_MODIFIER}${PLAYBACK_TARGET})?|${PLAYBACK_MODIFIER}${PLAYBACK_TARGET})|` +
  String.raw`(?:把|将|让)?${PLAYBACK_TARGET}\s*(?:先)?${PLAYBACK_CONTROL}${PLAYBACK_VERB}?|` +
  String.raw`(?:别|不要|不用)(?:再|继续)?${PLAYBACK_VERB}\s*(?:这(?:一)?(?:首(?:歌曲|歌|曲子)?|段)了?|${PLAYBACK_TARGET}了|了)` +
  String.raw`)(?:一下|一会儿|一会)?(?:吧|啦)?\s*[。！!]?\s*$`, 'iu',
);
const REPLY_CORRECTION = /^(?:(?:MOSS|莫斯|摩斯)[\s,，、]+)?(?:(?:请你?|麻烦你?)\s*)?(?:先)?(?:停止(?:继续)?(?:当前的?|这轮的?|本轮的?)?(?:回答|回复)|别(?:继续)?(?:回答|回复)(?:了)?|取消(?:这轮|本轮|当前)(?:回答|回复)|算了)[\s,，、。;；!！]*(?:改问|我改问|改成问|换(?:一)?个问题)[\s,，、。:：;；!！]*(\S[\s\S]*\S)$/iu;

const CALENDAR_DECISIONS = new Map([
  ['calendar_create', ['确认创建', '取消日程', '不创建了', '不用安排了']],
  ['calendar_reschedule', ['确认改期', '取消改期', '不改了']],
  ['calendar_cancel', ['确认取消', '保留这项日程', '保留日程', '不取消了']],
]);

export function isExplicitPlaybackControl(text) {
  return EXPLICIT_PLAYBACK_CONTROL.test((text || '').trim());
}

function replacementQuestion(text) {
  const next = REPLY_CORRECTION.exec((text || '').trim())?.[1];
  return next && next.length <= 1200 && new TextEncoder().encode(next).length <= 2700 ? next : null;
}

const calendarDecision = (turn) => CALENDAR_DECISIONS.get(turn?.desktop_confirmation?.action);
const decisionWords = (text) => text.normalize('NFKC').trim().replace(/[。!]+$/u, '').trimEnd();
const commandAnswer = (text) => text.normalize('NFKC').replace(/^(?:MOSS|莫斯)[\s,、]+/iu, '')
  .replace(/[。!]+$/u, '').trim().replace(/^(?:请你?|麻烦你?)\s*/u, '').replace(/(?:吧|呀|啊|呗)$/u, '');

function readDecision(text, turn, spoken) {
  const answer = decisionWords(text);
  const cmd = commandAnswer(text);
  const replyStop = /^(?:停止(?:回复|播报)|(?:先)?别(?:继续)?(?:说|播报)了|不用(?:继续)?说了|不要继续说了)$/u.test(cmd);
  if (replyStop && (cmd.includes('播报') ||
      (turn?.status !== 'thinking' && !(turn?.status === 'saving' && turn?.receipt)))) {
    return 'stop-speech';
  }
  if (!turn) return null;
  if (isExplicitPlaybackControl(text) && turn.source !== 'media' && !turn.media_in_flight) return null;
  const calendar = calendarDecision(turn);
  if (turn.status === 'pending') {
    const confirm = calendar?.[0] || (turn.desktop_confirmation ? '确认执行' : '确认保存');
    const allowed = spoken ? (calendar ? [confirm] : turn.desktop_confirmation ? [] : [confirm, '确认'])
      : [confirm, '确认', ...(calendar ? ['确认执行'] : [])];
    if (allowed.includes(answer)) return 'approve';
    if (calendar?.slice(1).includes(answer) || (!calendar && ['算了', '取消保存', '不保存了', '不用保存了', '别保存了'].includes(cmd))) {
      return 'cancel';
    }
  }
  if (turn.status === 'saving' && turn.desktop_confirmation && turn.desktop_confirmation.action !== 'powershell') {
    return null;
  }
  const wait = turn.status === 'thinking' && /^(?:先)?(?:停一下|等一下|等等|稍等一下)$/u.test(cmd);
  const stopTurn = /^(?:取消(?:这轮|本轮|当前请求|当前任务|操作|草稿)?|停止(?:这轮|本轮|当前请求|当前任务|回复)?)$/u.test(cmd);
  return (replyStop || wait || stopTurn) ? 'cancel' : null;
}

function turnTagMeta(turn) {
  if (turn.context_action) return { code: 'CTX', state: 'data' };
  if (turn.status === 'pending') return { code: 'AUTH', state: 'warn' };
  if (turn.receipt) return { code: 'RCPT', state: 'ok' };
  if (turn.document || turn.file) return { code: 'DOC', state: 'intel' };
  if (turn.images?.length) return { code: 'IMG', state: 'data' };
  if (turn.media?.items?.length || turn.media?.selected || turn.media_in_flight) return { code: 'MEDIA', state: 'ok' };
  if (turn.weather) return { code: 'METEO', state: 'data' };
  if (turn.local_overview?.length || turn.local_overview_partial?.length) return { code: 'HOST', state: 'warn' };
  if (matchIntelDossier(turn)) return { code: 'INTEL', state: 'intel' };
  if (turn.status === 'error') return { code: 'ERR', state: 'crit' };
  return null;
}

export function createDialogueController({
  onSendMessage,
  onMediaIntent,
  onApprove,
  onCancel,
  onReplaceReply,
  onStopSpeech,
  onToggleMic,
  onInterrupt,
  onOpenHud,
  onOpenJevHandoff,
  onNewChat,
  onOpenModal,
  onSpeakTurn,
  onRetryTurn,
  onSaveNoteTurn,
  onAttachSubmit,
  onSlashCommand,
}) {
  const panelEl = document.getElementById('comms-panel');
  const logEl = document.getElementById('comms-log');
  const emptyEl = document.getElementById('comms-empty');
  const typingEl = document.getElementById('comms-typing');
  const formEl = document.getElementById('comms-composer');
  const inputEl = document.getElementById('comms-input');
  const sendBtn = document.getElementById('comms-send');
  const hintEl = document.getElementById('comms-hint');
  const countEl = document.getElementById('comms-count');
  const micBtn = document.getElementById('comms-mic');
  const interruptBtn = document.getElementById('comms-interrupt');
  const voiceStatusEl = document.getElementById('comms-voice-status');
  const errorEl = document.getElementById('comms-error');
  const newChatBtn = document.getElementById('comms-new');
  const attachBtn = document.getElementById('comms-attach-btn');
  const attachInput = document.getElementById('comms-attach-input');
  const attachPill = document.getElementById('comms-attach-pill');
  const attachThumb = document.getElementById('comms-attach-thumb');
  const attachNameEl = document.getElementById('comms-attach-name');
  const attachMetaEl = document.getElementById('comms-attach-meta');
  const attachClearBtn = document.getElementById('comms-attach-clear');
  const slashMenuEl = document.getElementById('comms-slash-menu');

  let latestState = { turns: [] };
  let posting = false;
  let pendingAttachment = null;
  let attachmentSeq = 0;
  let editRevision = 0;
  let composing = false;
  const drafts = new Map();
  const renderedTurns = new Map();
  let renderedChatId = null;
  let renderEpoch = 0;
  let lastRenderEpoch = 0;
  let showCloudCounts = false;
  try {
    showCloudCounts = (localStorage.getItem('moss-fui.cloud-counts') ?? localStorage.getItem('moss.cloud.requests')) === 'on';
  } catch { /* ignore */ }

  const SLASH_COMMANDS = [
    { cmd: '/brief', label: '态势简报', prompt: '请给我一份当前时刻的态势简报' },
    { cmd: '/music', label: '先来首歌', prompt: '先来首歌吧' },
    { cmd: '/dream', label: '夜间深度整理', prompt: '帮我做一次深度整理并汇报世界模型' },
    { cmd: '/sleep30', label: '30 分钟后停歌', prompt: '30分钟后停歌' },
    { cmd: '/routines', label: '查看周期任务', prompt: '列出我所有的周期例行任务' },
    { cmd: '/favs', label: '查看收藏歌单', prompt: '列出我收藏的歌曲' },
    { cmd: '/gesture', label: '开启/关闭手势交互 (Alt+G)', action: 'toggle-gesture' },
    { cmd: '/mute', label: '切换会议静默', action: 'toggle-mute' },
    { cmd: '/float', label: '切换独立浮窗', action: 'toggle-float' },
    { cmd: '/cmd', label: '打开 ⌘K 命令面板', action: 'open-cmdk' },
    { cmd: '/history', label: '打开历史搜索', action: 'open-history' },
  ];

  function clearAttachment() {
    ++attachmentSeq;
    pendingAttachment = null;
    if (attachPill) attachPill.hidden = true;
    if (attachThumb) { attachThumb.hidden = true; attachThumb.removeAttribute('src'); }
    if (attachInput) attachInput.value = '';
    updateComposerState();
  }

  function queueFileAttachment(file) {
    if (!file) return;
    // Account for ~34% Data URL base64 overhead against the 16 MiB server JSON limit.
    if (file.size > 11.5 * 1024 * 1024) {
      showError('附件超出 11.5 MB 上限。');
      return;
    }
    const seq = ++attachmentSeq;
    const reader = new FileReader();
    reader.onerror = () => {
      if (seq !== attachmentSeq) return;
      showError('读取附件失败，请重试。');
    };
    reader.onload = () => {
      if (seq !== attachmentSeq) return;
      const dataUrl = String(reader.result || '');
      const isImg = (file.type || '').startsWith('image/');
      pendingAttachment = {
        name: file.name || (isImg ? `screenshot-${Date.now()}.png` : 'document.txt'),
        mime: file.type || (isImg ? 'image/png' : 'application/octet-stream'),
        size: file.size,
        data_b64: dataUrl,
        isImage: isImg,
      };
      if (attachNameEl) attachNameEl.textContent = pendingAttachment.name;
      if (attachMetaEl) attachMetaEl.textContent = file.size >= 1024 * 1024
        ? `${(file.size / (1024 * 1024)).toFixed(1)} MB`
        : `${Math.max(1, Math.round(file.size / 1024))} KB`;
      if (attachThumb) {
        if (isImg) { attachThumb.src = dataUrl; attachThumb.hidden = false; }
        else { attachThumb.hidden = true; attachThumb.removeAttribute('src'); }
      }
      if (attachPill) attachPill.hidden = false;
      showError('');
      updateComposerState();
      inputEl?.focus();
    };
    reader.readAsDataURL(file);
  }

  function renderSlashMenu() {
    if (!slashMenuEl || !inputEl) return;
    const raw = inputEl.value;
    if (!raw.startsWith('/') || raw.includes(' ') || raw.includes('\n')) {
      slashMenuEl.hidden = true;
      return;
    }
    const q = raw.toLowerCase();
    const matches = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q) || c.label.toLowerCase().includes(q.slice(1)));
    if (!matches.length) {
      slashMenuEl.hidden = true;
      return;
    }
    slashMenuEl.replaceChildren();
    for (const item of matches.slice(0, 7)) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'moss-slash-item';
      btn.innerHTML = `<span class="fui-num moss-slash-item__cmd">${escapeHtml(item.cmd)}</span><span class="moss-slash-item__lbl">${escapeHtml(item.label)}</span>`;
      btn.addEventListener('click', () => {
        slashMenuEl.hidden = true;
        inputEl.value = '';
        resizeInput();
        updateComposerState();
        if (item.action) onSlashCommand?.(item.action);
        else if (item.prompt) void submitText(item.prompt);
      });
      slashMenuEl.append(btn);
    }
    slashMenuEl.hidden = false;
  }

  function atBottom() {
    if (!logEl) return true;
    return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 32;
  }

  function resizeInput() {
    if (!inputEl) return;
    inputEl.style.height = 'auto';
    inputEl.style.height = `${Math.min(120, inputEl.scrollHeight)}px`;
    if (countEl) countEl.textContent = `${inputEl.value.length} / 1200`;
  }

  function updateComposerState() {
    const active = (latestState.turns || []).find((tr) => ACTIVE_STATES.has(tr.status));
    const hasText = Boolean(inputEl?.value.trim()) || Boolean(pendingAttachment);
    if (sendBtn) sendBtn.disabled = posting || !hasText;
    if (typingEl) typingEl.hidden = !(active && (active.status === 'thinking' || active.status === 'saving'));
    if (hintEl) {
      if (pendingAttachment) hintEl.textContent = t('comms.attach_ready');
      else if (active?.status === 'pending') hintEl.textContent = t('comms.hint_pending');
      else if (active) hintEl.textContent = t('comms.hint_working');
      else hintEl.textContent = t('comms.hint');
    }
    if (newChatBtn) newChatBtn.disabled = posting || Boolean(active && active.status !== 'pending');
  }

  function makeHudBtn(label, onClick, kind = 'data') {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'fui-btn moss-hud-trigger';
    btn.dataset.hudKind = kind;
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function renderTurnItems(turn, index) {
    const frag = document.createDocumentFragment();
    const seq = String(index + 1).padStart(2, '0');
    const stamp = turn.created_at ? formatClock(turn.created_at) : '--:--:--';
    const tag = turnTagMeta(turn);

    // 1. Operator directive
    const userLi = document.createElement('li');
    userLi.className = 'fui-chat__item';
    userLi.dataset.from = 'self';
    userLi.dataset.turnId = turn.id;
    userLi.innerHTML = `
      <div class="fui-chat__head">
        <span class="moss-turn-seq fui-num">${seq}</span>
        <span class="fui-chat__from">OP-01</span>
        <span class="fui-chat__role">${escapeHtml(t('comms.you'))}</span>
        <time class="fui-chat__time" datetime="${escapeHtml(turn.created_at || '')}">${stamp}</time>
      </div>
      <div class="fui-chat__body"><p></p></div>`;
    const userText = turn.reply_context?.goal ? `${turn.user}\n${turn.reply_context.goal}` : turn.user;
    userLi.querySelector('.fui-chat__body p').textContent = userText || '';
    frag.append(userLi);

    // 2. MOSS tactical response
    const aiLi = document.createElement('li');
    aiLi.className = 'fui-chat__item';
    aiLi.dataset.from = 'assistant';
    aiLi.dataset.turnId = turn.id;
    const isStreaming = turn.status === 'thinking' || turn.status === 'saving';
    if (isStreaming) {
      aiLi.dataset.streaming = 'true';
      aiLi.setAttribute('aria-busy', 'true');
    }
    if (turn.status === 'error') aiLi.dataset.status = 'failed';

    const statusWord = isStreaming
      ? t('turn.receiving')
      : turn.status === 'error'
        ? t('turn.failed')
        : '';
    const replyTime = turn.replied_at ? formatClock(turn.replied_at) : stamp;

    const head = document.createElement('div');
    head.className = 'fui-chat__head';
    head.innerHTML = `
      <span class="fui-chat__from">MOSS</span>
      ${tag ? `<span class="fui-badge" data-state="${tag.state}">${tag.code}</span>` : `<span class="fui-chat__role">${escapeHtml(t('comms.role_ai'))}</span>`}
      ${turn.failover_provider?.to ? `<span class="fui-badge" data-state="warn">${escapeHtml(t('turn.failover_tag', { to: String(turn.failover_provider.to).toUpperCase() }))}</span>` : ''}
      ${statusWord ? `<span class="fui-chat__status">${escapeHtml(statusWord)}</span>` : ''}
      <time class="fui-chat__time">${replyTime}</time>`;
    aiLi.append(head);

    const body = document.createElement('div');
    body.className = 'fui-chat__body';

    // Collapsible 1-line tool execution micro-rail (v5.5)
    if (Array.isArray(turn.tool_trace) && turn.tool_trace.length) {
      const traceBox = document.createElement('details');
      traceBox.className = 'moss-tool-trace';
      const totalMs = turn.tool_trace.reduce((acc, item) => acc + (Number(item.ms) || 0), 0);
      const anyPar = turn.tool_trace.some((item) => item.parallel);
      const sum = document.createElement('summary');
      sum.className = 'moss-tool-trace__sum fui-num';
      sum.textContent = `${t('turn.trace_label', { n: turn.tool_trace.length, ms: totalMs })}${anyPar ? ` · ${t('turn.trace_parallel')}` : ''}`;
      traceBox.append(sum);
      const ul = document.createElement('ul');
      ul.className = 'moss-tool-trace__list';
      for (const tr of turn.tool_trace) {
        const li = document.createElement('li');
        li.className = 'moss-tool-trace__item fui-num';
        li.dataset.state = tr.status === 'error' ? 'crit' : tr.status === 'running' ? 'warn' : 'ok';
        li.textContent = `${tr.name} · ${tr.status === 'running' ? '…' : `${tr.ms ?? 0}ms`}`;
        ul.append(li);
      }
      traceBox.append(ul);
      body.append(traceBox);
    }

    const stText = turnStatusLine(turn);
    if (stText) {
      const p = document.createElement('p');
      p.className = 'moss-card__meta';
      p.textContent = stText;
      body.append(p);
    }

    // Pending draft or desktop confirmation card
    if (turn.status === 'pending') {
      const card = document.createElement('div');
      card.className = 'moss-card';
      card.dataset.state = 'warn';
      const title = turn.desktop_confirmation?.title || t('turn.draft_preview');
      const detail = turn.desktop_confirmation?.detail || turn.preview || '';
      card.innerHTML = `<div class="moss-card__head"><span class="moss-card__title">${escapeHtml(title)}</span><span class="fui-badge" data-state="warn">${escapeHtml(t('level.warn'))}</span></div><p class="moss-card__body"></p>`;
      card.querySelector('.moss-card__body').textContent = detail;
      body.append(card);
    }

    // Verified draft receipt card
    if (turn.receipt) {
      const card = document.createElement('div');
      card.className = 'moss-card';
      card.dataset.state = 'ok';
      const linkHtml = turn.receipt.filename
        ? `<a class="fui-chat__attach" href="/drafts/${encodeURIComponent(turn.receipt.filename)}" target="_blank" rel="noopener">${escapeHtml(t('turn.open_draft'))}</a>`
        : '';
      card.innerHTML = `<div class="moss-card__head"><span class="moss-card__title">${escapeHtml(t('turn.draft_receipt'))}</span><span class="fui-badge" data-state="ok">${escapeHtml(t('level.ok'))}</span></div><p class="moss-card__body"></p>${linkHtml}`;
      card.querySelector('.moss-card__body').textContent = turn.receipt.text || '';
      body.append(card);
    }

    // Answer or partial streaming text (Safe DOM Markdown renderer — v5.5)
    const answerText = turn.reply || (turn.status === 'thinking' ? turn.partial_reply : '');
    if (answerText) {
      body.append(renderSafeMarkdown(answerText));
    }

    // Generative FUI Declarative Widget (sliders, comparison table, checklist, KPI metrics)
    if (turn.fui_widget) {
      const widgetNode = renderGenerativeWidget(turn.fui_widget);
      if (widgetNode) body.append(widgetNode);
    }

    // Error detail
    if (turn.error) {
      const errP = document.createElement('p');
      errP.className = 'moss-chat-error';
      errP.setAttribute('role', 'alert');
      errP.textContent = turn.error;
      body.append(errP);
    }

    if (showCloudCounts && turn.cloud_requests) {
      const meta = document.createElement('span');
      meta.className = 'moss-card__meta fui-num';
      meta.textContent = t('turn.cloud_req', { total: turn.cloud_requests.total ?? 0 });
      body.append(meta);
    }

    aiLi.append(body);

    // Inline Action & Contextual HUD Popup Triggers
    const actions = document.createElement('div');
    actions.className = 'fui-chat__actions';

    if (turn.status === 'pending') {
      const okBtn = document.createElement('button');
      okBtn.type = 'button';
      okBtn.className = 'fui-btn fui-btn--primary';
      okBtn.textContent = turn.desktop_confirmation
        ? t('turn.approve_desktop')
        : turn.verify_only
          ? t('turn.approve_verify')
          : t('turn.approve_draft');
      okBtn.addEventListener('click', () => onApprove?.(turn.id));

      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'fui-btn';
      cancelBtn.textContent = turn.desktop_confirmation ? t('turn.cancel_action') : t('turn.cancel_draft');
      cancelBtn.addEventListener('click', () => onCancel?.(turn.id));

      actions.append(okBtn, cancelBtn, makeHudBtn(t('turn.hud_action'), () => onOpenHud?.(turn, 'action'), 'warn'));
    } else if (isStreaming) {
      const stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.className = 'fui-btn';
      stopBtn.textContent = turn.desktop_confirmation?.action === 'powershell' ? t('turn.stop_ps') : t('turn.stop_reply');
      stopBtn.addEventListener('click', () => onCancel?.(turn.id));
      actions.append(stopBtn);
    }

    if (turn.document || turn.file) {
      actions.append(makeHudBtn(t('turn.hud_doc'), () => onOpenHud?.(turn, 'doc'), 'intel'));
    }
    if (turn.images?.length) {
      actions.append(makeHudBtn(t('turn.hud_image'), () => onOpenHud?.(turn, 'image'), 'data'));
    }
    if (turn.local_overview?.length || turn.local_overview_partial?.length) {
      actions.append(makeHudBtn(t('turn.hud_host'), () => onOpenHud?.(turn, 'host'), 'warn'));
    }
    if (turn.weather) {
      actions.append(makeHudBtn(t('turn.hud_weather'), () => onOpenHud?.(turn, 'weather'), 'data'));
    }
    if (turn.media?.items?.length || turn.media?.selected) {
      actions.append(makeHudBtn(t('turn.hud_media'), () => onOpenHud?.(turn, 'media'), 'ok'));
    }
    if (turn.context_action) {
      actions.append(makeHudBtn(t('turn.hud_context'), () => onOpenHud?.(turn, 'context'), 'data'));
    }
    if (matchIntelDossier(turn)) {
      actions.append(makeHudBtn(t('turn.hud_intel'), () => onOpenHud?.(turn, 'intel'), 'intel'));
    }

    if (turn.status === 'replied' && turn.jev?.candidate_count > 0 && turn.reply) {
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'fui-btn';
      copyBtn.textContent = t('turn.copy_reply');
      copyBtn.addEventListener('click', async () => {
        try {
          const ok = await safeCopyText(turn.reply);
          if (ok) copyBtn.textContent = t('turn.copied');
        } catch { /* ignore */ }
      });
      actions.append(copyBtn);
      if (latestState.jev_handoff) {
        const fillBtn = document.createElement('button');
        fillBtn.type = 'button';
        fillBtn.className = 'fui-btn';
        fillBtn.textContent = t('turn.jev_fill');
        fillBtn.addEventListener('click', () => onOpenJevHandoff?.({ chatId: turn.chat_id, turnId: turn.id, text: turn.reply }));
        actions.append(fillBtn);
      }
    }

    // Restrained per-turn utility action bar for completed or failed MOSS turns (v5.5)
    if (turn.status === 'replied' && turn.reply) {
      const utilRow = document.createElement('div');
      utilRow.className = 'moss-turn-tools';

      const cpBtn = document.createElement('button');
      cpBtn.type = 'button';
      cpBtn.className = 'moss-turn-tool-btn';
      cpBtn.textContent = t('turn.copy');
      cpBtn.addEventListener('click', async () => {
        if (!(await safeCopyText(turn.reply))) return;
        cpBtn.textContent = t('turn.copied');
        setTimeout(() => { cpBtn.textContent = t('turn.copy'); }, 1400);
      });

      const spkBtn = document.createElement('button');
      spkBtn.type = 'button';
      spkBtn.className = 'moss-turn-tool-btn';
      spkBtn.textContent = t('turn.speak');
      spkBtn.addEventListener('click', () => onSpeakTurn?.(turn));

      const rtyBtn = document.createElement('button');
      rtyBtn.type = 'button';
      rtyBtn.className = 'moss-turn-tool-btn';
      rtyBtn.textContent = t('turn.retry');
      rtyBtn.addEventListener('click', () => onRetryTurn?.(turn));

      const noteBtn = document.createElement('button');
      noteBtn.type = 'button';
      noteBtn.className = 'moss-turn-tool-btn';
      noteBtn.textContent = t('turn.save_note');
      noteBtn.addEventListener('click', async () => {
        const ok = await onSaveNoteTurn?.(turn);
        if (ok !== false) {
          noteBtn.textContent = t('turn.saved_note');
          setTimeout(() => { noteBtn.textContent = t('turn.save_note'); }, 1500);
        }
      });

      utilRow.append(cpBtn, spkBtn, rtyBtn, noteBtn);
      actions.append(utilRow);
    } else if (turn.status === 'error' && turn.user) {
      const utilRow = document.createElement('div');
      utilRow.className = 'moss-turn-tools';
      const rtyBtn = document.createElement('button');
      rtyBtn.type = 'button';
      rtyBtn.className = 'moss-turn-tool-btn';
      rtyBtn.textContent = t('turn.retry');
      rtyBtn.addEventListener('click', () => onRetryTurn?.(turn));
      utilRow.append(rtyBtn);
      actions.append(utilRow);
    }

    if (actions.children.length) aiLi.append(actions);
    frag.append(aiLi);
    return frag;
  }

  function turnStatusLine(turn) {
    switch (turn.status) {
      case 'thinking': return t('turn.thinking');
      case 'pending': return turn.desktop_confirmation ? t('turn.pending_desktop') : t('turn.pending_draft');
      case 'saving': return t('turn.saving');
      case 'saved': return turn.reply ? '' : t('turn.saved');
      case 'cancelled': return t('turn.cancelled');
      case 'error': return turn.error ? '' : t('turn.error');
      default: return '';
    }
  }

  function turnSignature(turn, index) {
    return JSON.stringify([
      index,
      turn.id,
      turn.status,
      turn.user,
      turn.reply,
      turn.partial_reply,
      turn.error,
      turn.preview,
      turn.verify_only,
      turn.created_at,
      turn.replied_at,
      turn.receipt,
      turn.desktop_confirmation,
      turn.media,
      turn.media_in_flight,
      turn.weather,
      turn.local_overview,
      turn.local_overview_partial,
      turn.document,
      turn.file,
      turn.images,
      turn.context_action,
      turn.jev,
      turn.reply_context,
      turn.tool_trace,
      turn.failover_provider,
      turn.fui_widget,
      showCloudCounts ? turn.cloud_requests : null,
      Boolean(latestState.jev_handoff),
    ]);
  }

  function renderState(state) {
    const shouldFollow = atBottom();
    if (inputEl && latestState.chat_id && latestState.chat_id !== state?.chat_id) {
      drafts.set(latestState.chat_id, inputEl.value);
      inputEl.value = drafts.get(state?.chat_id) || '';
      editRevision++;
      resizeInput();
    }
    latestState = state || { turns: [] };
    const turns = latestState.turns || [];
    if (emptyEl) emptyEl.hidden = turns.length > 0;
    if (logEl) {
      const fullReset = renderedChatId !== (latestState.chat_id || null) ||
        renderEpoch !== lastRenderEpoch || turns.length === 0;
      if (fullReset) {
        for (const child of [...logEl.children]) {
          if (child !== emptyEl) child.remove();
        }
        renderedTurns.clear();
        renderedChatId = latestState.chat_id || null;
        lastRenderEpoch = renderEpoch;
      } else {
        const activeIds = new Set(turns.map((tr) => tr.id));
        for (const [id, entry] of renderedTurns) {
          if (!activeIds.has(id)) {
            entry.userLi.remove();
            entry.aiLi.remove();
            renderedTurns.delete(id);
          }
        }
      }
      let cursor = emptyEl ? emptyEl.nextElementSibling : logEl.firstElementChild;
      turns.forEach((turn, idx) => {
        const sig = turnSignature(turn, idx);
        const existing = renderedTurns.get(turn.id);
        if (existing && existing.sig === sig && existing.userLi.isConnected && existing.aiLi.isConnected) {
          if (cursor !== existing.userLi) {
            logEl.insertBefore(existing.userLi, cursor);
            logEl.insertBefore(existing.aiLi, cursor);
          } else {
            cursor = existing.aiLi.nextElementSibling;
          }
          return;
        }
        const frag = renderTurnItems(turn, idx);
        const [userLi, aiLi] = [...frag.children];
        if (existing) {
          existing.userLi.replaceWith(userLi);
          existing.aiLi.replaceWith(aiLi);
          cursor = aiLi.nextElementSibling;
        } else {
          logEl.insertBefore(frag, cursor);
        }
        renderedTurns.set(turn.id, { sig, userLi, aiLi });
      });
      if (shouldFollow) logEl.scrollTop = logEl.scrollHeight;
    }
    updateComposerState();
  }

  async function submitText(raw, { spoken = false, calendarTurn = null, overlapTurn = null, replyTurn = null } = {}) {
    const typed = raw === undefined;
    const original = inputEl?.value ?? '';
    const revision = editRevision;
    const chatId = latestState.chat_id;
    const initialFocus = document.activeElement;
    const text = (typed ? original : raw).trim();
    if (!text || posting || (typed && composing)) return false;

    const clearTypedDraft = () => {
      const selected = inputEl && document.activeElement === inputEl && inputEl.selectionStart !== inputEl.selectionEnd;
      if (typed && inputEl && chatId === latestState.chat_id && editRevision === revision && inputEl.value === original && !selected) {
        inputEl.value = '';
        resizeInput();
        if (initialFocus === sendBtn && document.activeElement === document.body && !document.querySelector('dialog[open]')) {
          inputEl.focus({ preventScroll: true });
        }
      }
    };

    // Mid-generation correction: "先停止回答，改问..."
    const replacement = replacementQuestion(text);
    if (replacement) {
      const active = (latestState.turns || []).find((turn) => ACTIVE_STATES.has(turn.status));
      const target = spoken ? (latestState.turns || []).find((turn) => turn.id === replyTurn) : active;
      if (target?.status === 'thinking' && target.conversation_replaceable === true) {
        posting = true;
        showError('');
        updateComposerState();
        try {
          const result = await onReplaceReply?.({ turnId: target.id, chatId, text: replacement, spoken });
          if (result === false) return false;
          clearTypedDraft();
          return true;
        } catch (err) {
          showError(err.message || 'Request failed');
          return false;
        } finally {
          posting = false;
          updateComposerState();
        }
      }
      if (spoken || active) {
        showError('刚才的回复已经结束或正在执行工具，请直接说新问题。');
        return false;
      }
    }

    // Voice/explicit submission of an open clipboard/OCR reply draft editor
    if (spoken) {
      const contextNotice = await submitWindowReplyByVoice(text, latestState);
      if (contextNotice !== null) {
        if (!contextNotice.startsWith('已提交')) showError(contextNotice);
        else showError('');
        return contextNotice.startsWith('已提交');
      }
    }

    const active = (latestState.turns || []).find((turn) => ACTIVE_STATES.has(turn.status));
    const decision = readDecision(text, active, spoken);
    if (decision === 'stop-speech') {
      onStopSpeech?.();
      clearTypedDraft();
      return true;
    }

    const calendar = calendarDecision(active);
    const exactCalendarPhrase = [...CALENDAR_DECISIONS.values()].some((words) => words[0] === decisionWords(text));
    if (spoken && (exactCalendarPhrase || calendarTurn)) {
      const captured = latestState.turns.find((turn) => turn.id === calendarTurn);
      const staleDecision = captured && calendarDecision(captured) && readDecision(text, { ...captured, status: 'pending' }, true);
      if ((exactCalendarPhrase || staleDecision) && (!calendar || active?.status !== 'pending' || active.id !== calendarTurn || !decision)) {
        showError('刚才的日程已不在等待这项确认。');
        return false;
      }
    }
    if (spoken && calendar && decision && calendarTurn !== active.id) return false;
    if (overlapTurn && (!decision || active?.status !== 'pending' || active.id !== overlapTurn ||
        (!calendar && (!active.call_id || active.desktop_confirmation)))) return false;
    const scope = spoken && calendar ? { pending_calendar_only: true } : overlapTurn ? { pending_draft_only: true } : {};
    const playback = !decision && isExplicitPlaybackControl(text);
    posting = true;
    showError('');
    if (!decision && !playback && MEDIA_INTENT_RE.test(text)) onMediaIntent?.(text);
    updateComposerState();
    try {
      const result = decision === 'approve' ? await onApprove?.(active.id, scope) : decision === 'cancel' ?
        await onCancel?.(active.id, scope) : await onSendMessage?.(text, { playback });
      if (result === false) return false;
      clearTypedDraft();
      return true;
    } catch (err) {
      showError(err.message || 'Request failed');
      return false;
    } finally {
      posting = false;
      updateComposerState();
    }
  }

  function showError(msg) {
    if (!errorEl) return;
    errorEl.hidden = !msg;
    errorEl.textContent = msg || '';
  }

  function setVoiceUi({ micActive = false, canInterrupt = false, statusKey = 'voice.ready', extra = '' } = {}) {
    if (micBtn) {
      micBtn.setAttribute('aria-pressed', String(micActive));
      if (micActive) micBtn.dataset.state = 'crit';
      else micBtn.removeAttribute('data-state');
      micBtn.textContent = t(micActive ? 'comms.mic_stop' : 'comms.mic_start');
    }
    if (interruptBtn) interruptBtn.disabled = !canInterrupt;
    if (voiceStatusEl) {
      const base = t(statusKey);
      voiceStatusEl.textContent = extra ? `${base} · ${extra}` : base;
    }
  }

  function fillComposer(text, { submit = false } = {}) {
    if (submit) return submitText(text);
    if (!inputEl) return;
    inputEl.value = text;
    editRevision++;
    resizeInput();
    updateComposerState();
    inputEl.focus();
  }

  formEl?.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (slashMenuEl) slashMenuEl.hidden = true;
    if (pendingAttachment && onAttachSubmit) {
      const att = pendingAttachment;
      const prompt = (inputEl?.value || '').trim();
      posting = true;
      updateComposerState();
      showError('');
      try {
        const ok = await onAttachSubmit({ ...att, prompt });
        if (ok !== false) {
          clearAttachment();
          if (inputEl) { inputEl.value = ''; resizeInput(); }
        }
      } catch (err) {
        showError(err?.message || 'Attachment upload failed');
      } finally {
        posting = false;
        updateComposerState();
      }
      return;
    }
    submitText();
  });
  inputEl?.addEventListener('input', () => {
    editRevision++;
    resizeInput();
    updateComposerState();
    renderSlashMenu();
  });
  inputEl?.addEventListener('paste', (ev) => {
    const items = ev.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) {
          ev.preventDefault();
          queueFileAttachment(file);
          return;
        }
      }
    }
  });
  attachBtn?.addEventListener('click', () => attachInput?.click());
  attachInput?.addEventListener('change', () => {
    const file = attachInput.files?.[0];
    if (file) queueFileAttachment(file);
  });
  attachClearBtn?.addEventListener('click', clearAttachment);
  panelEl?.addEventListener('dragover', (ev) => {
    if (ev.dataTransfer?.types?.includes('Files')) {
      ev.preventDefault();
      panelEl.dataset.fileDrag = 'true';
    }
  });
  panelEl?.addEventListener('dragleave', (ev) => {
    if (!panelEl.contains(ev.relatedTarget)) delete panelEl.dataset.fileDrag;
  });
  panelEl?.addEventListener('drop', (ev) => {
    delete panelEl.dataset.fileDrag;
    const file = ev.dataTransfer?.files?.[0];
    if (file) {
      ev.preventDefault();
      queueFileAttachment(file);
    }
  });
  inputEl?.addEventListener('compositionstart', () => { composing = true; editRevision++; });
  inputEl?.addEventListener('compositionend', () => { composing = false; });
  inputEl?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !composing && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      if (!sendBtn?.disabled) formEl?.requestSubmit();
    }
  });
  micBtn?.addEventListener('click', () => onToggleMic?.());
  interruptBtn?.addEventListener('click', () => onInterrupt?.());
  newChatBtn?.addEventListener('click', () => onNewChat?.());

  for (const btn of document.querySelectorAll('[data-open-modal]')) {
    btn.addEventListener('click', () => onOpenModal?.(btn.dataset.openModal));
  }
  for (const btn of document.querySelectorAll('[data-starter-key]')) {
    btn.addEventListener('click', () => fillComposer(t(btn.dataset.starterKey), { submit: true }));
  }
  for (const btn of document.querySelectorAll('[data-quick-key]')) {
    btn.addEventListener('click', () => fillComposer(t(btn.dataset.quickKey), { submit: true }));
  }

  resizeInput();
  updateComposerState();

  return {
    renderState,
    showError,
    setVoiceUi,
    fillComposer,
    submitText,
    focusInput: () => inputEl?.focus(),
    scrollToTurn(turnId) {
      const target = logEl?.querySelector(`[data-turn-id="${CSS.escape(turnId)}"]`);
      target?.scrollIntoView({ block: 'center' });
    },
    setShowCloudCounts(on) {
      showCloudCounts = Boolean(on);
      try { localStorage.setItem('moss-fui.cloud-counts', showCloudCounts ? 'on' : 'off'); } catch { /* ignore */ }
      renderEpoch++;
      renderState(latestState);
    },
    getShowCloudCounts: () => showCloudCounts,
    refreshI18n() {
      renderEpoch++;
      renderState(latestState);
    },
  };
}

function appendInlineMarkdown(parent, text) {
  const tokenRe = /(\*\*[^*\n]+?\*\*|`[^`\n]+?`|\[[^\]]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s<>()]+)/g;
  let last = 0;
  let m;
  while ((m = tokenRe.exec(text)) !== null) {
    if (m.index > last) parent.append(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith('**') && tok.endsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = tok.slice(2, -2);
      parent.append(strong);
    } else if (tok.startsWith('`') && tok.endsWith('`')) {
      const code = document.createElement('code');
      code.className = 'moss-md-inline-code';
      code.textContent = tok.slice(1, -1);
      parent.append(code);
    } else if (tok.startsWith('[')) {
      const lm = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(tok);
      if (lm) {
        const a = document.createElement('a');
        a.className = 'moss-md-link';
        a.href = lm[2];
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = lm[1];
        parent.append(a);
      } else {
        parent.append(document.createTextNode(tok));
      }
    } else {
      const a = document.createElement('a');
      a.className = 'moss-md-link';
      a.href = tok;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = tok;
      parent.append(a);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
}

export function renderSafeMarkdown(rawText) {
  const wrap = document.createElement('div');
  wrap.className = 'moss-md';
  const lines = String(rawText ?? '').replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // 1. Fenced code block ```lang (must match paragraph termination condition `startsWith('```')`)
    const fence = /^```([^\n]*)$/.exec(line.trim());
    if (fence) {
      const lang = (fence[1].trim().split(/\s+/)[0] || 'code').slice(0, 24);
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        buf.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      const codeText = buf.join('\n');
      const block = document.createElement('div');
      block.className = 'moss-md-code';
      const head = document.createElement('div');
      head.className = 'moss-md-code__head';
      const langSpan = document.createElement('span');
      langSpan.className = 'fui-num moss-md-code__lang';
      langSpan.textContent = lang.toUpperCase();
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'moss-turn-tool-btn';
      copyBtn.textContent = t('turn.copy');
      copyBtn.addEventListener('click', async () => {
        if (!(await safeCopyText(codeText))) return;
        copyBtn.textContent = t('turn.copied');
        setTimeout(() => { copyBtn.textContent = t('turn.copy'); }, 1400);
      });
      head.append(langSpan, copyBtn);
      const pre = document.createElement('pre');
      const codeEl = document.createElement('code');
      codeEl.textContent = codeText;
      pre.append(codeEl);
      block.append(head, pre);
      wrap.append(block);
      continue;
    }

    // 2. Compact Markdown Table
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
      const parseRow = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const headers = parseRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(parseRow(lines[i]));
        i++;
      }
      const tblWrap = document.createElement('div');
      tblWrap.className = 'moss-md-table-wrap';
      const tbl = document.createElement('table');
      tbl.className = 'moss-md-table';
      const thead = document.createElement('thead');
      const htr = document.createElement('tr');
      for (const h of headers) {
        const th = document.createElement('th');
        appendInlineMarkdown(th, h);
        htr.append(th);
      }
      thead.append(htr);
      const tbody = document.createElement('tbody');
      for (const r of rows) {
        const tr = document.createElement('tr');
        for (let ci = 0; ci < headers.length; ci++) {
          const td = document.createElement('td');
          appendInlineMarkdown(td, r[ci] || '');
          tr.append(td);
        }
        tbody.append(tr);
      }
      tbl.append(thead, tbody);
      tblWrap.append(tbl);
      wrap.append(tblWrap);
      continue;
    }

    // 3. Heading
    const hm = /^(#{1,3})\s+(.+)$/.exec(line.trim());
    if (hm) {
      const h = document.createElement('p');
      h.className = 'moss-md-heading';
      appendInlineMarkdown(h, hm[2]);
      wrap.append(h);
      i++;
      continue;
    }

    // 4. Blockquote
    if (line.trim().startsWith('> ')) {
      const bq = document.createElement('blockquote');
      bq.className = 'moss-md-quote';
      const qLines = [];
      while (i < lines.length && lines[i].trim().startsWith('> ')) {
        qLines.push(lines[i].trim().slice(2));
        i++;
      }
      appendInlineMarkdown(bq, qLines.join('\n'));
      wrap.append(bq);
      continue;
    }

    // 5. Unordered list
    if (/^\s*[-*•]\s+/.test(line)) {
      const ul = document.createElement('ul');
      ul.className = 'moss-md-list';
      while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) {
        const li = document.createElement('li');
        appendInlineMarkdown(li, lines[i].replace(/^\s*[-*•]\s+/, ''));
        ul.append(li);
        i++;
      }
      wrap.append(ul);
      continue;
    }

    // 6. Blank line
    if (!line.trim()) {
      i++;
      continue;
    }

    // 7. Regular paragraph (merge consecutive plain lines cleanly)
    const pLines = [];
    const startI = i;
    while (
      i < lines.length && lines[i].trim()
      && !lines[i].trim().startsWith('```')
      && !lines[i].trim().startsWith('> ')
      && !/^(#{1,3})\s+/.test(lines[i].trim())
      && !/^\s*[-*•]\s+/.test(lines[i])
      && !(lines[i].includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1]))
    ) {
      pLines.push(lines[i]);
      i++;
    }
    if (i === startI) {
      pLines.push(lines[i]);
      i++;
    }
    const p = document.createElement('p');
    pLines.forEach((pl, idx) => {
      if (idx > 0) p.append(document.createElement('br'));
      appendInlineMarkdown(p, pl);
    });
    wrap.append(p);
  }
  return wrap;
}

function escapeHtml(str) {
  return String(str ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}
