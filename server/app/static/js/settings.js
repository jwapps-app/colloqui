import { api } from './api.js';
import { createPasskey, signOutLocal } from './auth.js';
import { avatarEl } from './avatars.js';
import { appAlert, appConfirm, appPrompt } from './dialogs.js';
import { $, copyText, titledRow } from './dom.js';
import { relTime } from './format.js';
import { state } from './state.js';

export function applyCompact(on) {
  document.body.classList.toggle('compact', on);
  localStorage.setItem('compact', on ? '1' : '0');
}

// Theme: 'system' (follow OS), 'light', or 'dark'. The effective light/dark is
// set on <html data-theme>; CSS overrides the palette vars for light.
function effectiveTheme(pref) {
  if (pref === 'light' || pref === 'dark') return pref;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
export function applyTheme(pref) {
  pref = pref || 'system';
  localStorage.setItem('theme', pref);
  const eff = effectiveTheme(pref);
  document.documentElement.dataset.theme = eff;
  const tc = document.querySelector('meta[name="theme-color"]');
  if (tc) tc.setAttribute('content', eff === 'dark' ? '#15131c' : '#f4f3fa');
  const seg = document.getElementById('theme-seg');
  if (seg) seg.querySelectorAll('button').forEach(
    b => b.classList.toggle('active', b.dataset.themePref === pref)
  );
}
// Re-resolve when the OS flips light/dark, but only while following the system.
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if ((localStorage.getItem('theme') || 'system') === 'system') applyTheme('system');
});

export async function openAccount() {
  $('account').classList.remove('hidden');
  $('profile-display').value = state.me.display_name;
  $('profile-status').value = state.me.status || '';
  $('badge-channels-toggle').checked = state.me.badge_channel_messages !== false;
  $('compact-toggle').checked = localStorage.getItem('compact') === '1';
  api('/calendar/url').then(r => { $('calendar-url').value = r.url; }).catch(() => {});
  loadPasswordSection();
  loadTotpSection();
  await Promise.all([loadPasskeys(), loadSessions()]);
}

export async function loadTotpSection() {
  $('totp-enroll').classList.add('hidden');
  $('totp-recovery').classList.add('hidden');
  let enabled = false;
  try { enabled = (await api('/auth/totp')).enabled; } catch {}
  $('totp-state').textContent = enabled
    ? 'On. Password sign-in asks for a code from your authenticator app.'
    : 'Off. Adds a second step to password sign-in. Passkey sign-in is already strong and never asks for a code.';
  $('totp-enable').classList.toggle('hidden', enabled);
  $('totp-disable').classList.toggle('hidden', !enabled);
}

export async function startTotpEnroll() {
  try {
    const s = await api('/auth/totp/setup', { method: 'POST' });
    $('totp-qr').innerHTML = s.qr_svg;   // our own server-generated SVG
    $('totp-secret').textContent = s.secret;
    $('totp-code').value = '';
    $('totp-enable').classList.add('hidden');
    $('totp-enroll').classList.remove('hidden');
    $('totp-code').focus();
  } catch (e) { appAlert(e.message); }
}

export async function confirmTotpEnroll() {
  const code = $('totp-code').value.trim();
  if (!code) return;
  try {
    const r = await api('/auth/totp/confirm', { method: 'POST', body: JSON.stringify({ code }) });
    $('totp-enroll').classList.add('hidden');
    const list = $('totp-recovery-list');
    list.innerHTML = '';
    for (const c of r.recovery_codes) {
      const li = document.createElement('li');
      li.textContent = c;
      list.appendChild(li);
    }
    $('totp-recovery').classList.remove('hidden');
  } catch (e) { appAlert(e.message); }
}

export async function disableTotp() {
  const code = await appPrompt('Enter a current code from your app (or a recovery code) to turn off 2FA:');
  if (!code) return;
  try {
    await api('/auth/totp/disable', { method: 'POST', body: JSON.stringify({ code: code.trim() }) });
    await loadTotpSection();
  } catch (e) { appAlert(e.message); }
}

async function loadPasswordSection() {
  $('pw-current').value = ''; $('pw-new').value = '';
  let has = false;
  try { has = (await api('/auth/password')).has_password; } catch {}
  $('pw-state').textContent = has
    ? 'You have a password set. You can change or remove it.'
    : 'No password set — you sign in with a passkey only.';
  $('pw-current').classList.toggle('hidden', !has);
  $('pw-remove').classList.toggle('hidden', !has);
  $('pw-save').textContent = has ? 'Change password' : 'Set password';
}

export async function savePassword() {
  const password = $('pw-new').value;
  if (password.length < 8) { appAlert('Password must be at least 8 characters.'); return; }
  const current = $('pw-current').value;
  try {
    await api('/auth/password', {
      method: 'POST',
      body: JSON.stringify(current ? { password, current_password: current } : { password }),
    });
    appAlert('Password saved.');
    await loadPasswordSection();
  } catch (e) { appAlert(e.message); }
}

export async function removePassword() {
  if (!await appConfirm('Remove your password? You\'ll sign in with passkeys only.',
      { okText: 'Remove', danger: true })) return;
  try {
    await api('/auth/password', { method: 'DELETE' });
    await loadPasswordSection();
  } catch (e) { appAlert(e.message); }
}

export async function copyCalendarUrl() {
  const url = $('calendar-url').value;
  if (!url) return;
  if (await copyText(url)) appAlert('Calendar link copied.');
  else $('calendar-url').select();  // let the user copy by hand
}

export async function regenerateCalendarUrl() {
  if (!await appConfirm('Regenerate the calendar link? The old link stops working and you\'ll need to re-subscribe.',
      { okText: 'Regenerate', danger: true })) return;
  try {
    const r = await api('/calendar/regenerate', { method: 'POST' });
    $('calendar-url').value = r.url;
    appAlert('New calendar link created. Re-subscribe with it in your calendar app.');
  } catch (e) { appAlert(e.message); }
}

export async function saveProfile() {
  const display_name = $('profile-display').value.trim();
  if (!display_name) return;
  const status = $('profile-status').value.trim();  // "" clears it server-side
  try {
    state.me = { ...state.me, ...(await api('/users/me', { method: 'PATCH', body: JSON.stringify({ display_name, status }) })) };
    renderMeHeader();
  } catch (e) { appAlert(e.message); }
}

export async function uploadAvatar() {
  const file = $('avatar-input').files[0];
  $('avatar-input').value = '';
  if (!file) return;
  try {
    const fd = new FormData();
    fd.append('upload', file);
    state.me = { ...state.me, ...(await api('/users/me/avatar', { method: 'POST', body: fd })) };
    renderMeHeader();
    appAlert('Photo updated ✓ (it appears on your new messages right away)');
  } catch (e) { appAlert(e.message); }
}

async function loadPasskeys() {
  const list = $('passkey-list');
  const keys = await api('/auth/passkeys');
  list.innerHTML = '';
  for (const k of keys) {
    const li = titledRow(
      k.label || 'Unnamed passkey',
      `added ${relTime(k.created_at)}`
      + (k.last_used_at ? ` · last used ${relTime(k.last_used_at)}` : ''));
    if (keys.length > 1) {
      const del = document.createElement('button');
      del.textContent = 'Remove';
      del.onclick = async () => {
        if (!await appConfirm('Remove this passkey? Devices using it will no longer sign in.')) return;
        try { await api(`/auth/passkeys/${k.id}`, { method: 'DELETE' }); await loadPasskeys(); }
        catch (e) { appAlert(e.message); }
      };
      li.appendChild(del);
    }
    list.appendChild(li);
  }
}

export async function addPasskey() {
  const label = await appPrompt('Name this passkey (e.g. "MacBook", "iPhone backup"):', { okText: 'Continue' });
  if (label === null) return;
  try {
    const { add_token, options } = await api('/auth/passkeys/options', {
      method: 'POST', body: JSON.stringify({ label: label.trim() || null }),
    });
    const credential = await createPasskey(options);
    await api('/auth/passkeys/verify', {
      method: 'POST', body: JSON.stringify({ token: add_token, credential }),
    });
    await loadPasskeys();
  } catch (e) { appAlert(e.message); }
}

async function loadSessions() {
  const list = $('session-list');
  const sessions = await api('/auth/sessions');
  list.innerHTML = '';
  for (const s of sessions) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    const ua = s.user_agent || 'Unknown device';
    grow.textContent = ua.length > 48 ? ua.slice(0, 48) + '…' : ua;
    grow.title = ua;
    if (s.current) {
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = 'this device';
      grow.appendChild(tag);
    }
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `active ${relTime(s.last_seen_at)} · signed in ${relTime(s.created_at)}`;
    grow.appendChild(sub);
    li.appendChild(grow);
    const btn = document.createElement('button');
    btn.textContent = s.current ? 'Sign out' : 'Revoke';
    btn.onclick = async () => {
      try {
        await api(`/auth/sessions/${s.id}`, { method: 'DELETE' });
        if (s.current) signOutLocal();
        else await loadSessions();
      } catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

export function renderMeHeader() {
  const span = $('me-name');
  span.innerHTML = '';
  span.appendChild(avatarEl(state.me));
  span.appendChild(document.createTextNode(state.me.display_name));
  span.title = state.me.status || 'Your account';
}
