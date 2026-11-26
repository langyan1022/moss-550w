/**
 * Backend-Persisted Per-Identity Preferences + 4-Char CS/C Auth Code Gate + CS User Manager (MOSS v5.5).
 *
 * - All user personalization (badge, location, spectrum/OLED, rail layout, comms drawer,
 *   meeting mute, companion personality) is persisted in backend SQLite per 4-char auth code.
 * - "MOSS" (case-insensitive) => CS admin (full system configuration + user manager).
 * - Any other 4-char alphanumeric code => C operator (persistent personal profile; system config locked).
 */
import { escapeHtml } from './hud/utils.js';
import { t } from './i18n.js';

export function createAuthPrefsController({ onApplyPrefs, onIdentityChange } = {}) {
  const chipBtn = document.getElementById('strip-auth-chip');
  const gateDlg = document.getElementById('dlg-auth-gate');
  const gateForm = document.getElementById('auth-gate-form');
  const gateInput = document.getElementById('auth-gate-input');
  const gateErr = document.getElementById('auth-gate-err');
  const gateSwitchHint = document.getElementById('auth-gate-hint');
  const setRoleNote = document.getElementById('set-role-note');
  const clearanceInput = document.getElementById('set-badge-clearance');

  // CS-Only User Manager elements (Settings Tab 06)
  const authEnabledCheck = document.getElementById('set-auth-enabled');
  const authScopeSel = document.getElementById('set-auth-scope');
  const authGateSaveBtn = document.getElementById('set-auth-save');
  const authGateFeedback = document.getElementById('set-auth-feedback');
  const authUserForm = document.getElementById('set-auth-user-form');
  const authUserCode = document.getElementById('set-auth-user-code');
  const authUserLabel = document.getElementById('set-auth-user-label');
  const authUsersList = document.getElementById('set-auth-users-list');
  const authSwitchBtn = document.getElementById('set-auth-switch');

  const identity = {
    enabled: true,
    scope: 'lan',
    authorized: false,
    statusUnavailable: false,
    code: '',
    role: 'C',
    is_admin: false,
    prefs: {},
  };

  let pendingPatch = {};
  let saveTimer = 0;

  function syncRoleDom() {
    const role = identity.role || 'C';
    const code = identity.code || '';
    document.body.dataset.authRole = role;
    document.body.dataset.authCode = code;
    if (chipBtn) {
      chipBtn.textContent = `${role} · ${code}`;
      chipBtn.dataset.state = role === 'CS' ? 'intel' : 'data';
    }
    if (setRoleNote) {
      setRoleNote.hidden = role === 'CS';
    }
    if (clearanceInput) {
      if (role !== 'CS') {
        clearanceInput.value = 'C';
        clearanceInput.readOnly = true;
      } else {
        clearanceInput.readOnly = false;
      }
    }
    onIdentityChange?.({ ...identity });
  }

  async function fetchStatus() {
    try {
      const resp = await fetch('/api/auth/status', { cache: 'no-store' });
      if (!resp.ok) {
        identity.statusUnavailable = true;
        return identity;
      }
      const data = await resp.json();
      Object.assign(identity, {
        enabled: Boolean(data.enabled),
        scope: data.scope || 'lan',
        authorized: Boolean(data.authorized),
        statusUnavailable: false,
        code: data.code || '',
        role: data.role || '',
        is_admin: Boolean(data.is_admin),
        prefs: data.prefs || {},
      });
      if (identity.authorized) {
        syncRoleDom();
        onApplyPrefs?.(identity.prefs, identity);
      }
      return identity;
    } catch {
      identity.statusUnavailable = true;
      return identity;
    }
  }

  function openGate({ switchMode = false, forceRequired = false } = {}) {
    if (!gateDlg) return;
    if (forceRequired) {
      identity.authorized = false;
      identity.code = '';
      identity.role = '';
      identity.is_admin = false;
    }
    if (gateErr) { gateErr.hidden = true; gateErr.textContent = ''; }
    if (gateInput) gateInput.value = '';
    if (gateSwitchHint) {
      gateSwitchHint.textContent = switchMode
        ? t('auth.gate_switch_hint')
        : t('auth.gate_hint');
    }
    gateDlg.dataset.required = String(forceRequired || (!identity.authorized && !switchMode));
    if (!gateDlg.open) gateDlg.showModal();
    setTimeout(() => gateInput?.focus(), 40);
  }

  // Prevent Esc closing the mandatory auth gate when not authorized
  gateDlg?.addEventListener('cancel', (ev) => {
    if (gateDlg.dataset.required === 'true' && !identity.authorized) {
      ev.preventDefault();
    }
  });

  gateForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!gateInput) return;
    const raw = gateInput.value.trim().toUpperCase();
    if (!/^[A-Z0-9]{4}$/.test(raw)) {
      if (gateErr) {
        gateErr.textContent = t('auth.code_invalid');
        gateErr.hidden = false;
      }
      return;
    }
    try {
      const resp = await fetch('/api/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: raw }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        if (gateErr) {
          gateErr.textContent = data.error || t('auth.code_invalid');
          gateErr.hidden = false;
        }
        return;
      }
      const prevAuthorized = Boolean(identity.authorized);
      const prevCode = identity.code;
      clearTimeout(saveTimer);
      saveTimer = 0;
      pendingPatch = {};
      Object.assign(identity, {
        enabled: Boolean(data.enabled),
        scope: data.scope || 'lan',
        authorized: true,
        code: data.code,
        role: data.role,
        is_admin: Boolean(data.is_admin),
        prefs: data.prefs || {},
      });
      gateDlg.dataset.required = 'false';
      if (gateDlg.open) gateDlg.close();
      syncRoleDom();
      onApplyPrefs?.(identity.prefs, identity);
      if (identity.role === 'CS') void refreshUsersList();
      if ((!prevAuthorized && identity.authorized) || (prevCode && prevCode !== identity.code)) {
        window.dispatchEvent(new CustomEvent('moss-auth-switched', { detail: { ...identity } }));
      }
    } catch {
      if (gateErr) {
        gateErr.textContent = t('auth.net_err');
        gateErr.hidden = false;
      }
    }
  });

  function savePrefs(patch) {
    if (!patch || typeof patch !== 'object' || !identity.authorized) return;
    Object.assign(identity.prefs, patch);
    Object.assign(pendingPatch, patch);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      const batch = { ...pendingPatch };
      pendingPatch = {};
      try {
        await fetch('/api/console-settings/prefs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prefs: batch }),
        });
      } catch { /* ignore offline/simulated */ }
    }, 220);
  }

  // ── CS-Only User & Authorization Code Manager (Settings Tab 06) ──
  function showAdminFeedback(msg, isErr = false) {
    if (!authGateFeedback) return;
    authGateFeedback.hidden = !msg;
    authGateFeedback.textContent = msg || '';
    authGateFeedback.dataset.state = isErr ? 'crit' : 'ok';
  }

  function renderUsers(users) {
    if (!authUsersList) return;
    authUsersList.replaceChildren();
    if (!users?.length) {
      authUsersList.innerHTML = `<li class="moss-modal-row"><span class="moss-modal-row__meta">${t('auth.users_empty')}</span></li>`;
      return;
    }
    for (const u of users) {
      const li = document.createElement('li');
      li.className = 'moss-modal-row moss-auth-user-row';
      const stateBadge = u.disabled
        ? `<span class="fui-badge" data-state="crit">${t('auth.user_disabled')}</span>`
        : `<span class="fui-badge" data-state="${u.role === 'CS' ? 'intel' : 'ok'}">${u.role}</span>`;
      const meta = document.createElement('div');
      meta.className = 'moss-modal-row__main';
      meta.innerHTML = `
        <div class="moss-row moss-row--tight">
          <strong class="fui-num">${u.code}</strong>
          ${stateBadge}
          <span>${escapeHtml(u.label || '')}</span>
        </div>
        <span class="moss-modal-row__meta fui-num">${t('auth.user_meta', { n: u.pref_count || 0, seen: (u.last_seen_at || '').slice(0, 16).replace('T', ' ') })}</span>`;
      li.append(meta);

      if (u.role !== 'CS') {
        const btns = document.createElement('div');
        btns.className = 'moss-row moss-row--tight';
        const toggleBtn = document.createElement('button');
        toggleBtn.type = 'button';
        toggleBtn.className = 'fui-key fui-key--sm';
        toggleBtn.textContent = t(u.disabled ? 'auth.user_enable' : 'auth.user_disable');
        toggleBtn.addEventListener('click', async () => {
          toggleBtn.disabled = true;
          try {
            const resp = await fetch('/api/auth/users', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ code: u.code, disabled: !u.disabled }),
            });
            const d = await resp.json().catch(() => ({}));
            if (resp.ok) {
              renderUsers(d.users);
            } else if (authGateFeedback) {
              authGateFeedback.hidden = false;
              authGateFeedback.textContent = d.error || `HTTP ${resp.status}`;
            }
          } catch (err) {
            if (authGateFeedback) {
              authGateFeedback.hidden = false;
              authGateFeedback.textContent = err.message || 'Network error';
            }
          } finally {
            toggleBtn.disabled = false;
          }
        });
        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'fui-key fui-key--sm';
        delBtn.textContent = t('auth.user_delete');
        delBtn.addEventListener('click', async () => {
          delBtn.disabled = true;
          try {
            const resp = await fetch(`/api/auth/users/${encodeURIComponent(u.code)}`, { method: 'DELETE' });
            const d = await resp.json().catch(() => ({}));
            if (resp.ok) {
              renderUsers(d.users);
            } else if (authGateFeedback) {
              authGateFeedback.hidden = false;
              authGateFeedback.textContent = d.error || `HTTP ${resp.status}`;
            }
          } catch (err) {
            if (authGateFeedback) {
              authGateFeedback.hidden = false;
              authGateFeedback.textContent = err.message || 'Network error';
            }
          } finally {
            delBtn.disabled = false;
          }
        });
        btns.append(toggleBtn, delBtn);
        li.append(btns);
      }
      authUsersList.append(li);
    }
  }

  async function refreshUsersList() {
    if (identity.role !== 'CS') return;
    try {
      const resp = await fetch('/api/auth/users', { cache: 'no-store' });
      if (!resp.ok) return;
      const data = await resp.json();
      if (authEnabledCheck) authEnabledCheck.checked = Boolean(data.gate?.enabled);
      if (authScopeSel && data.gate?.scope) authScopeSel.value = data.gate.scope;
      renderUsers(data.users || []);
    } catch { /* ignore */ }
  }

  authGateSaveBtn?.addEventListener('click', async () => {
    showAdminFeedback('');
    try {
      const resp = await fetch('/api/auth/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: Boolean(authEnabledCheck?.checked),
          scope: authScopeSel?.value || 'lan',
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showAdminFeedback(data.error || 'Save failed', true);
        return;
      }
      identity.enabled = Boolean(data.enabled);
      identity.scope = data.scope || 'lan';
      showAdminFeedback(t('auth.policy_saved'));
    } catch {
      showAdminFeedback(t('auth.net_err'), true);
    }
  });

  authUserForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    showAdminFeedback('');
    const code = (authUserCode?.value || '').trim().toUpperCase();
    const label = (authUserLabel?.value || '').trim();
    if (!/^[A-Z0-9]{4}$/.test(code)) {
      showAdminFeedback(t('auth.code_invalid'), true);
      return;
    }
    try {
      const resp = await fetch('/api/auth/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, label, disabled: false }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showAdminFeedback(data.error || 'Failed', true);
        return;
      }
      if (authUserCode) authUserCode.value = '';
      if (authUserLabel) authUserLabel.value = '';
      renderUsers(data.users || []);
      showAdminFeedback(t('auth.user_saved', { code }));
    } catch {
      showAdminFeedback(t('auth.net_err'), true);
    }
  });

  chipBtn?.addEventListener('click', () => openGate({ switchMode: true }));
  authSwitchBtn?.addEventListener('click', () => openGate({ switchMode: true }));

  return {
    init: fetchStatus,
    openGate,
    savePrefs,
    refreshUsersList,
    getIdentity: () => ({ ...identity }),
    isAdmin: () => identity.role === 'CS',
    maybePromptAfterOpening() {
      if (identity.statusUnavailable) return false;
      if (identity.enabled && !identity.authorized) {
        openGate({ switchMode: false });
        return true;
      }
      return false;
    },
  };
}
