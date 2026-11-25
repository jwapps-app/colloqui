import { api } from './api.js';
import { avatarCache } from './avatars.js';
import { clearChannelView } from './channels.js';
import { resolveDialog } from './dialogs.js';
import { $ } from './dom.js';
import { imageUrls } from './files.js';
import { b64u } from './format.js';
import { clearInfoPane } from './infopane.js';
import { resetPushSetup } from './notifications.js';
import { disconnectWs, typers } from './realtime.js';
import { closeWhen } from './reminders.js';
import { showApp } from './shell.js';
import { presence, state } from './state.js';

export async function createPasskey(options) {
  const pk = options;
  pk.challenge = b64u.dec(pk.challenge);
  pk.user.id = b64u.dec(pk.user.id);
  (pk.excludeCredentials || []).forEach(c => { c.id = b64u.dec(c.id); });
  const cred = await navigator.credentials.create({ publicKey: pk });
  return {
    id: cred.id,
    rawId: b64u.enc(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: b64u.enc(cred.response.clientDataJSON),
      attestationObject: b64u.enc(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : [],
    },
  };
}

async function getPasskey(options) {
  const pk = options;
  pk.challenge = b64u.dec(pk.challenge);
  (pk.allowCredentials || []).forEach(c => { c.id = b64u.dec(c.id); });
  const cred = await navigator.credentials.get({ publicKey: pk });
  return {
    id: cred.id,
    rawId: b64u.enc(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: b64u.enc(cred.response.clientDataJSON),
      authenticatorData: b64u.enc(cred.response.authenticatorData),
      signature: b64u.enc(cred.response.signature),
      userHandle: cred.response.userHandle ? b64u.enc(cred.response.userHandle) : null,
    },
  };
}

export function authError(msg) { $('auth-error').textContent = msg || ''; }

export async function register() {
  authError('');
  try {
    const { reg_token, options } = await api('/auth/register/options', {
      method: 'POST',
      body: JSON.stringify({
        username: $('reg-username').value.trim(),
        display_name: $('reg-display').value.trim() || $('reg-username').value.trim(),
        invite_code: $('reg-invite').value.trim() || null,
      }),
    });
    const credential = await createPasskey(options);
    const result = await api('/auth/register/verify', {
      method: 'POST',
      body: JSON.stringify({ token: reg_token, credential }),
    });
    onSignedIn(result);
  } catch (e) { authError(e.message); }
}

export async function login() {
  authError('');
  try {
    const username = $('login-username').value.trim();
    const { login_token, options } = await api('/auth/login/options', {
      method: 'POST',
      body: JSON.stringify({ username: username || null }),
    });
    const credential = await getPasskey(options);
    const result = await api('/auth/login/verify', {
      method: 'POST',
      body: JSON.stringify({ token: login_token, credential }),
    });
    onSignedIn(result);
  } catch (e) { authError(e.message); }
}

export async function loginWithPassword() {
  authError('');
  const username = $('login-username').value.trim();
  const password = $('login-password').value;
  if (!username || !password) { authError('Enter your username and password.'); return; }
  const code = $('login-code').value.trim() || undefined;  // only after 2FA prompt
  try {
    const result = await api('/auth/login/password', {
      method: 'POST', body: JSON.stringify({ username, password, code }),
    });
    onSignedIn(result);
  } catch (e) {
    if (e.message === 'mfa_required') {
      $('login-code').classList.remove('hidden');
      $('login-code').focus();
      authError('Enter the 6-digit code from your authenticator app (or a recovery code).');
    } else {
      authError(e.message);
    }
  }
}

export async function registerWithPassword() {
  authError('');
  const password = $('reg-password').value;
  if (password.length < 8) { authError('Password must be at least 8 characters.'); return; }
  try {
    const result = await api('/auth/register/password', {
      method: 'POST',
      body: JSON.stringify({
        username: $('reg-username').value.trim(),
        display_name: $('reg-display').value.trim() || $('reg-username').value.trim(),
        invite_code: $('reg-invite').value.trim() || null,
        password,
      }),
    });
    onSignedIn(result);
  } catch (e) { authError(e.message); }
}

function onSignedIn(result) {
  state.token = result.token;
  localStorage.setItem('token', state.token);
  state.me = result.user;
  showApp();
}

export function signOutLocal() {
  // Single cleanup path for BOTH explicit logout and a forced sign-out (401 /
  // revocation): every overlay, every cached blob, every account-scoped bit of
  // state goes, so the next account on this device can't see the last one's.
  state.token = null; state.me = null; state.currentChannel = null; state.spaces = []; state.channels = [];
  localStorage.removeItem('token');
  presence.clear(); typers.clear();
  state.notifUnread = 0;
  resetPushSetup();   // the next login must re-establish push ownership
  disconnectWs();
  resolveDialog(false);
  for (const id of ['account', 'settings', 'space', 'members', 'notifs', 'tasks',
                    'pins', 'threads-inbox']) {
    const el = $(id); if (el) el.classList.add('hidden');
  }
  closeWhen(null);
  // Resets the header, composer and panes too: the next account must not see
  // the last one's open channel name or its action buttons.
  clearChannelView();
  // Drop rendered conversation + list DOM and every cached file/avatar blob.
  // Only ever empty a list that a script fills: a container that holds page
  // structure cannot be rebuilt (tests/test_static.py enforces this).
  for (const id of ['messages', 'spaces-container', 'dm-list', 'pins-list',
                    'threads-list']) {
    const el = $(id); if (el) el.innerHTML = '';
  }
  clearInfoPane();
  for (const entry of imageUrls.values()) { try { URL.revokeObjectURL(entry.url); } catch {} }
  imageUrls.clear();
  for (const url of avatarCache.values()) { try { URL.revokeObjectURL(url); } catch {} }
  avatarCache.clear();
  $('app').classList.add('hidden');
  $('auth').classList.remove('hidden');
}

export async function signOut() {
  // Drop this device's push subscription first (needs the session token), so a
  // signed-out device stops receiving notifications.
  try {
    const reg = await navigator.serviceWorker?.ready;
    const sub = await reg?.pushManager.getSubscription();
    if (sub) {
      const j = sub.toJSON();
      await api('/push/subscribe', {
        method: 'DELETE',
        body: JSON.stringify({ endpoint: j.endpoint, keys: j.keys }),
      });
      await sub.unsubscribe();
    }
  } catch {}
  resetPushSetup();
  try { await api('/auth/logout', { method: 'POST' }); } catch {}
  signOutLocal();
}
