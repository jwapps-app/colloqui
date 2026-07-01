'use strict';

// Register the service worker so the installed app always loads the latest
// version (defeats iOS standalone's stale page cache).
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
  // A tapped notification (app already open) asks us to jump to its channel.
  navigator.serviceWorker.addEventListener('message', e => {
    if (e.data && e.data.type === 'open-channel') {
      openChannelFromLink(e.data.channelId, e.data.rootId);
    }
  });
}

// Auto-update: iOS standalone caches the start page at the OS level, so a cold
// launch can run stale code. On load (and when the app is foregrounded) we
// fetch the live index.html, and if it references a newer build than the one
// running, reload — which goes through the service worker and pulls the fresh
// version. A per-session cap prevents reload loops.
const APP_VERSION = '107';
async function checkForUpdate() {
  try {
    const html = await (await fetch('/?_=' + Date.now(), { cache: 'no-store' })).text();
    const m = html.match(/app\.js\?v=(\d+)/);
    if (!m) return;
    if (m[1] === APP_VERSION) { sessionStorage.removeItem('updTries'); return; }
    const tries = +(sessionStorage.getItem('updTries') || 0);
    if (tries < 2) { sessionStorage.setItem('updTries', tries + 1); location.reload(); }
  } catch {}
}
checkForUpdate();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkForUpdate();
});

// Show the real release version (the git tag baked into the image) in the
// footer, instead of the internal cache-bust counter. Falls back silently.
fetch('/healthz').then(r => r.json()).then(d => {
  const el = document.getElementById('ver');
  if (el && d && d.version) el.textContent = d.version === 'dev' ? 'dev' : d.version;
}).catch(() => {});

// Pin the layout height (--vh) to what the web view can actually paint. The
// `apple-mobile-web-app-status-bar-style: black` meta seats the iOS standalone
// canvas below the status bar, so the full screen is the paintable height.
//
// Robustness: iOS standalone sometimes reports a STALE, too-small innerHeight
// (or visualViewport.height) after a keyboard/background transition, which used
// to latch a giant gap at the bottom until a force-close. So we take the LARGER
// of the two readings as the full height, and only shrink below it when the
// keyboard is genuinely open — which we gate on an input actually being focused
// (a real keyboard) so a stale small reading alone can't shrink the layout.
function setViewportHeight() {
  const vv = window.visualViewport;
  const inner = window.innerHeight;
  const full = vv ? Math.max(inner, vv.height) : inner;
  let h = full;
  const ae = document.activeElement;
  const typing = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable);
  if (typing && vv) {
    const kb = Math.max(0, full - vv.height - vv.offsetTop);
    if (kb > 120) h = vv.height;  // sit above the on-screen keyboard
  }
  document.documentElement.style.setProperty('--vh', Math.round(h) + 'px');
}
// Whether the message list is pinned to the latest message. Updated on scroll
// (see `messages` onscroll); used to keep it pinned across keyboard reflows.
let stickBottom = true;
let _lastVH = 0;
function keepMessagesPinned() {
  const app = document.getElementById('app');
  if (!app || !app.classList.contains('chat-open')) return;  // no channel open
  const box = document.getElementById('messages');
  if (box && stickBottom) box.scrollTop = box.scrollHeight;
}
// On every viewport change: re-measure, then undo iOS's scroll-into-view. When
// an input is focused, iOS scrolls the layout viewport to reveal it — but the
// body is position:fixed, so that scroll drags the whole UI (and the keyboard)
// up into a stuck state. We already shrink #app above the keyboard, so reset it.
function pinViewport() {
  setViewportHeight();
  if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
  const se = document.scrollingElement;
  if (se && se.scrollTop) se.scrollTop = 0;
  // When the keyboard opens/closes, --vh changes and the list reflows. If the
  // user was reading the latest messages, keep them there instead of stranding
  // them at the oldest message up top.
  const vh = parseInt(document.documentElement.style.getPropertyValue('--vh'), 10) || 0;
  if (vh !== _lastVH) {
    _lastVH = vh;
    keepMessagesPinned();
  }
}
// Re-measure repeatedly for ~2s. iOS standalone reports its true height late at
// startup, and — crucially — reports a STALE, keyboard-sized height for a beat
// after the app returns to the foreground. Re-arming this poll on every
// foreground lets the layout self-heal instead of latching a giant bottom gap
// until a force-close.
let _vhWarmup = null;
function rearmViewportWarmup() {
  let ticks = 0;
  clearInterval(_vhWarmup);
  _vhWarmup = setInterval(() => {
    pinViewport();
    if (++ticks > 14) { clearInterval(_vhWarmup); _vhWarmup = null; }
  }, 150);
}
pinViewport();
rearmViewportWarmup();
['resize', 'orientationchange', 'pageshow', 'focus'].forEach(
  e => window.addEventListener(e, pinViewport)
);
window.addEventListener('pageshow', rearmViewportWarmup);
if (window.visualViewport) {
  // The reliable signal for keyboard open/close on iOS.
  window.visualViewport.addEventListener('resize', pinViewport);
  window.visualViewport.addEventListener('scroll', pinViewport);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Drop keyboard focus on the way out: otherwise a stale "an input is still
    // focused" state on return tricks setViewportHeight into shrinking for a
    // keyboard that isn't actually on screen — the giant-gap latch.
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) ae.blur();
  } else {
    pinViewport();
    rearmViewportWarmup();
  }
});
// iOS does its scroll-into-view shortly after focus and again as the keyboard
// animates in, so snap back across that window too.
document.addEventListener('focusin', () => {
  pinViewport();
  setTimeout(pinViewport, 50);
  setTimeout(pinViewport, 300);
});

// ---------- helpers ----------

const $ = id => document.getElementById(id);

const b64u = {
  enc: buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''),
  dec: s => Uint8Array.from(
    atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')),
    c => c.charCodeAt(0)
  ).buffer,
};

function relTime(iso) {
  const d = new Date(iso), now = new Date(), diff = (now - d) / 1000;
  if (diff < 60) return 'now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
}

function shortDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

let token = localStorage.getItem('token');
let me = null;
let spaces = [];
let channels = [];
let currentChannel = null;
let manageSpaceId = null;
let editingMessageId = null;
let replyingTo = null;  // { id, sender_name, snippet }
let threadRootId = null;  // id of the thread currently open in the thread pane
let sock = null;
const presence = new Map();  // user_id -> 'online' | 'away' | 'offline'
function presenceOf(userId) { return presence.get(userId) || 'offline'; }
function lastSeenText(user) {
  return user && user.last_seen_at ? 'last seen ' + relTime(user.last_seen_at) : 'offline';
}
const imageUrls = new Map();
const typers = new Map();   // user_id -> {name, timer}
let lastTypingSent = 0;
let notifUnread = 0;
let oldestMessageId = null;
let allHistoryLoaded = false;
let loadingOlder = false;

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch('/api/v1' + path, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    // Only an expired/revoked *session* means we're signed out — a 401 from
    // a login attempt should surface its real error message.
    if (res.status === 401 && token) signOutLocal();
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// ---------- in-app dialogs (replace native alert/confirm/prompt) ----------

let dialogResolve = null;

function openDialog(opts) {
  return new Promise(resolve => {
    dialogResolve = resolve;
    const title = $('dialog-title');
    title.textContent = opts.title || '';
    title.classList.toggle('hidden', !opts.title);
    const msg = $('dialog-message');
    msg.textContent = opts.message || '';
    msg.classList.toggle('hidden', !opts.message);
    const input = $('dialog-input');
    const ta = $('dialog-textarea');
    const sel = $('dialog-select');
    input.classList.add('hidden');
    ta.classList.add('hidden');
    sel.classList.add('hidden');
    let field = null;
    if (opts.input === 'select') {
      field = sel;
      sel.innerHTML = '';
      for (const o of (opts.options || [])) {
        const opt = document.createElement('option');
        opt.value = o.value;
        opt.textContent = o.label;
        if (o.value === (opts.value || '')) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.classList.remove('hidden');
    } else {
      field = opts.input === 'textarea' ? ta : opts.input === 'text' ? input : null;
      if (field) {
        field.classList.remove('hidden');
        field.value = opts.value || '';
        field.placeholder = opts.placeholder || '';
      }
    }
    const ok = $('dialog-ok');
    const cancel = $('dialog-cancel');
    ok.textContent = opts.okText || 'OK';
    ok.classList.toggle('danger-btn', !!opts.danger);
    cancel.classList.toggle('hidden', opts.noCancel === true);
    $('dialog').dataset.mode = opts.input ? 'input' : 'confirm';
    $('dialog').classList.remove('hidden');
    if (field) setTimeout(() => { field.focus(); if (field.select) field.select(); }, 0);
    else setTimeout(() => ok.focus(), 0);
  });
}

function resolveDialog(confirmed) {
  const d = $('dialog');
  if (d.classList.contains('hidden')) return;
  d.classList.add('hidden');
  const resolve = dialogResolve;
  dialogResolve = null;
  if (!resolve) return;
  if (d.dataset.mode === 'input') {
    if (!confirmed) return resolve(null);
    const sel = $('dialog-select');
    if (!sel.classList.contains('hidden')) return resolve(sel.value);
    const ta = $('dialog-textarea');
    resolve(ta.classList.contains('hidden') ? $('dialog-input').value : ta.value);
  } else {
    resolve(confirmed);
  }
}

function appAlert(message, title) {
  return openDialog({ title, message, okText: 'OK', noCancel: true });
}
function appConfirm(message, opts = {}) {
  return openDialog({ title: opts.title, message, okText: opts.okText || 'Confirm', danger: opts.danger });
}
function appPrompt(message, opts = {}) {
  return openDialog({
    title: opts.title, message, input: opts.multiline ? 'textarea' : 'text',
    value: opts.value, placeholder: opts.placeholder, okText: opts.okText || 'Save',
  });
}
// options: [{ value, label }]. Resolves to the chosen value, or null if cancelled.
function appChoose(message, options, opts = {}) {
  return openDialog({
    title: opts.title, message, input: 'select', options,
    value: opts.value, okText: opts.okText || 'Save',
  });
}

// ---------- WebAuthn ceremonies ----------

async function createPasskey(options) {
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

// ---------- auth ----------

function authError(msg) { $('auth-error').textContent = msg || ''; }

async function register() {
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

async function login() {
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

async function loginWithPassword() {
  authError('');
  const username = $('login-username').value.trim();
  const password = $('login-password').value;
  if (!username || !password) { authError('Enter your username and password.'); return; }
  try {
    const result = await api('/auth/login/password', {
      method: 'POST', body: JSON.stringify({ username, password }),
    });
    onSignedIn(result);
  } catch (e) { authError(e.message); }
}

async function registerWithPassword() {
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
  token = result.token;
  localStorage.setItem('token', token);
  me = result.user;
  showApp();
}

function signOutLocal() {
  token = null; me = null; currentChannel = null; spaces = [];
  localStorage.removeItem('token');
  presence.clear(); typers.clear();
  notifUnread = 0;
  if (sock) { sock.close(); sock = null; }
  resolveDialog(false);
  $('account').classList.add('hidden');
  $('settings').classList.add('hidden');
  $('space').classList.add('hidden');
  $('members').classList.add('hidden');
  $('notifs').classList.add('hidden');
  $('tasks').classList.add('hidden');
  closeWhen(null);
  closePreview();
  closeInfoPane();
  closeThread();
  cancelReply();
  $('app').classList.add('hidden');
  $('auth').classList.remove('hidden');
}

async function signOut() {
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
  _pushSetupDone = false;
  try { await api('/auth/logout', { method: 'POST' }); } catch {}
  signOutLocal();
}

// ---------- composer (visual editor) ----------
// The contenteditable box shows live bold/italic/code; on send it serializes
// to the same markdown-ish plain text the renderer understands, so storage,
// search, and the future iOS app all keep working on plain text.

function setComposerEnabled(on) {
  $('send-input').setAttribute('contenteditable', on ? 'true' : 'false');
  $('send-btn').disabled = !on;
  $('attach-btn').disabled = !on;
  document.querySelectorAll('#toolbar button').forEach(b => { b.disabled = !on; });
}

function serializeNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent.replace(/​/g, '');
  if (node.nodeType !== Node.ELEMENT_NODE) return '';
  const inner = [...node.childNodes].map(serializeNode).join('');
  switch (node.tagName) {
    case 'BR': return '\n';
    case 'B': case 'STRONG': return inner.trim() ? `**${inner}**` : inner;
    case 'I': case 'EM': return inner.trim() ? `*${inner}*` : inner;
    case 'S': case 'STRIKE': case 'DEL': return inner.trim() ? `~~${inner}~~` : inner;
    case 'CODE': return inner.trim() ? `\`${inner}\`` : inner;
    case 'DIV': case 'P': return '\n' + inner;
    default: return inner;
  }
}

function serializeEditor() {
  return [...$('send-input').childNodes].map(serializeNode).join('')
    .replace(/\u00a0/g, ' ')
    .trim();
}

function clearEditor() { $('send-input').innerHTML = ''; }

// Per-channel unsent drafts, kept in localStorage.
function saveDraft() {
  if (!currentChannel || editingMessageId) return;
  const content = serializeEditor();
  if (content) localStorage.setItem('draft:' + currentChannel.id, content);
  else localStorage.removeItem('draft:' + currentChannel.id);
}
function restoreDraft(channelId) {
  clearEditor();
  const d = localStorage.getItem('draft:' + channelId);
  if (d) fillEditor(d);
}
function clearDraft(channelId) { localStorage.removeItem('draft:' + channelId); }

function updateToolbar() {
  for (const cmd of ['bold', 'italic', 'strikeThrough']) {
    const btn = document.querySelector(`#toolbar button[data-cmd="${cmd}"]`);
    let on = false;
    try { on = document.queryCommandState(cmd); } catch {}
    if (btn) btn.classList.toggle('active', on);
  }
}

function toggleCode() {
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  const box = $('send-input');
  if (!box.contains(range.commonAncestorContainer)) return;
  let n = range.commonAncestorContainer;
  while (n && n !== box) {
    if (n.nodeType === 1 && n.tagName === 'CODE') {
      const parent = n.parentNode;
      while (n.firstChild) parent.insertBefore(n.firstChild, n);
      parent.removeChild(n);
      return;
    }
    n = n.parentNode;
  }
  if (range.collapsed) {
    // No selection: drop in an empty code span and put the caret inside it
    // (zero-width char gives the inline element something to hold the caret).
    const code = document.createElement('code');
    code.appendChild(document.createTextNode('​'));
    range.insertNode(code);
    const r = document.createRange();
    r.setStart(code.firstChild, 1);
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
    return;
  }
  const code = document.createElement('code');
  try {
    range.surroundContents(code);
  } catch {
    code.textContent = range.toString();
    range.deleteContents();
    range.insertNode(code);
  }
}

function execFormat(cmd) {
  $('send-input').focus();
  if (cmd === 'code') toggleCode();
  else document.execCommand(cmd);
  updateToolbar();
}

// ---------- @mention autocomplete ----------

const memberCache = new Map();
let mentionState = null;   // { node, atOffset, endOffset }
let mentionItems = [];
let mentionIndex = 0;

async function membersFor(channelId) {
  if (memberCache.has(channelId)) return memberCache.get(channelId);
  let members = [];
  try { members = await api(`/channels/${channelId}/members`); } catch {}
  memberCache.set(channelId, members);
  return members;
}

function closeMention() {
  mentionState = null;
  $('mention-box').classList.add('hidden');
}

async function updateMention() {
  const sel = window.getSelection();
  if (!currentChannel || !sel.rangeCount || !sel.isCollapsed) return closeMention();
  const node = sel.anchorNode;
  const offset = sel.anchorOffset;
  if (!node || node.nodeType !== Node.TEXT_NODE) return closeMention();
  const before = node.textContent.slice(0, offset);
  const m = before.match(/(^|\s)@([a-z0-9_]*)$/i);
  if (!m) return closeMention();
  const query = m[2].toLowerCase();
  const atOffset = offset - m[2].length - 1;  // position of '@'
  const members = await membersFor(currentChannel.id);
  mentionItems = members.filter(u =>
    u.username.toLowerCase().startsWith(query)
    || u.display_name.toLowerCase().includes(query)).slice(0, 6);
  if (!mentionItems.length) return closeMention();
  mentionIndex = 0;
  mentionState = { node, atOffset, endOffset: offset };
  renderMentionBox();
}

function renderMentionBox() {
  const box = $('mention-box');
  box.innerHTML = '';
  mentionItems.forEach((u, i) => {
    const item = document.createElement('div');
    item.className = 'mention-item' + (i === mentionIndex ? ' active' : '');
    item.appendChild(document.createTextNode(u.display_name));
    const uname = document.createElement('span');
    uname.className = 'uname';
    uname.textContent = '@' + u.username;
    item.appendChild(uname);
    item.onmousedown = e => { e.preventDefault(); chooseMention(i); };
    box.appendChild(item);
  });
  const sel = window.getSelection();
  if (sel.rangeCount) {
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    box.style.left = Math.min(rect.left, window.innerWidth - 290) + 'px';
    box.style.bottom = (window.innerHeight - rect.top + 6) + 'px';  // above the caret
  }
  box.classList.remove('hidden');
}

function chooseMention(i) {
  if (!mentionState) return;
  const u = mentionItems[i];
  const { node, atOffset, endOffset } = mentionState;
  const range = document.createRange();
  range.setStart(node, atOffset);
  range.setEnd(node, endOffset);
  range.deleteContents();
  const text = document.createTextNode('@' + u.username + ' ');
  range.insertNode(text);
  const sel = window.getSelection();
  sel.removeAllRanges();
  const after = document.createRange();
  after.setStartAfter(text);
  after.collapse(true);
  sel.addRange(after);
  closeMention();
  $('send-input').focus();
}

function currentLineText() {
  const s = window.getSelection();
  if (!s.rangeCount || !s.anchorNode || s.anchorNode.nodeType !== Node.TEXT_NODE) return '';
  const upto = s.anchorNode.textContent.slice(0, s.anchorOffset);
  return upto.slice(upto.lastIndexOf('\n') + 1);
}

function insertPrefix(prefix) {
  const box = $('send-input');
  box.focus();
  const s = window.getSelection();
  let before = '';
  if (s.rangeCount && s.anchorNode && s.anchorNode.nodeType === Node.TEXT_NODE) {
    before = s.anchorNode.textContent.slice(0, s.anchorOffset);
  }
  const atLineStart = before === '' || before.endsWith('\n');
  document.execCommand('insertText', false, (atLineStart ? '' : '\n') + prefix);
}

// ---------- channels ----------

function channelLabel(ch) {
  if (!ch.is_dm) return '# ' + ch.name;
  if (ch.dm_user) return ch.dm_user.display_name;
  if (ch.dm_members && ch.dm_members.length) {
    return ch.dm_members.map(u => u.display_name).join(', ');
  }
  return 'DM';
}

function canManage(ch) {
  return !ch.is_dm && (ch.my_role === 'owner' || (me && me.is_admin));
}

function channelLi(ch) {
  const li = document.createElement('li');
  li.dataset.id = ch.id;
  const muted = ch.notify_level === 'muted';
  if (muted) li.classList.add('muted');
  if (ch.is_dm && ch.dm_user) {
    const dot = document.createElement('span');
    const st = presenceOf(ch.dm_user.id);
    dot.className = 'dot ' + st;
    dot.title = ch.dm_user.status || (st === 'offline' ? lastSeenText(ch.dm_user) : st);
    li.appendChild(dot);
  }
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = channelLabel(ch);
  li.appendChild(label);
  if (ch.open_task_count > 0) {
    const tasks = document.createElement('span');
    tasks.className = 'task-chip';
    tasks.textContent = '☑ ' + ch.open_task_count;
    tasks.title = `${ch.open_task_count} open task(s)`;
    li.appendChild(tasks);
  }
  if (ch.recent_count) {
    const recent = document.createElement('span');
    recent.className = 'msg-count';
    recent.textContent = ch.recent_count;
    recent.title = `${ch.recent_count} message(s) in the last 7 days`;
    li.appendChild(recent);
  }
  if (muted) {
    const bell = document.createElement('span');
    bell.className = 'mute-tag';
    bell.textContent = '🔕';
    bell.title = 'Muted';
    li.appendChild(bell);
  } else if (ch.unread_count > 0) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = ch.unread_count > 99 ? '99+' : ch.unread_count;
    badge.title = `${ch.unread_count} unread`;
    li.appendChild(badge);
  }
  if (currentChannel && currentChannel.id === ch.id) li.classList.add('active');
  li.onclick = () => selectChannel(ch);
  return li;
}

function channelById(id) {
  return channels.find(c => c.id === id);
}

function markRead(channelId) {
  api(`/channels/${channelId}/read`, { method: 'POST' }).catch(() => {});
}

// Opening a channel clears its 🔔 notifications and drops the unread/app badge
// by however many were cleared (so tapping a push, or just reading the channel,
// brings the count down).
async function clearChannelNotifs(channelId) {
  try {
    const r = await api(`/notifications/read-channel/${channelId}`, { method: 'POST' });
    if (r && r.cleared > 0) {
      notifUnread = Math.max(0, notifUnread - r.cleared);
      updateNotifBadge();
      if (!$('notifs').classList.contains('hidden')) loadNotifications();
    }
  } catch {}
}

// Jump to a channel from a notification (service-worker message or ?channel=).
function openChannelFromLink(channelId, rootId) {
  const ch = channelById(channelId);
  if (!ch) return;
  $('notifs').classList.add('hidden');
  selectChannel(ch).then(() => { if (rootId) openThread({ id: rootId }); });
}

function renderChannels() {
  const container = $('spaces-container');
  const dmList = $('dm-list');
  container.innerHTML = ''; dmList.innerHTML = '';

  const bySpace = new Map();
  for (const ch of channels) {
    if (ch.is_dm) { dmList.appendChild(channelLi(ch)); continue; }
    if (!bySpace.has(ch.space_id)) bySpace.set(ch.space_id, []);
    bySpace.get(ch.space_id).push(ch);
  }

  for (const sp of spaces) {
    const section = document.createElement('div');
    section.className = 'section';
    const h3 = document.createElement('h3');
    const name = document.createElement('span');
    name.className = 'space-name';
    name.textContent = sp.name;
    h3.appendChild(name);
    const btns = document.createElement('span');
    // Space management (rename / add members) lives in Settings → Spaces now,
    // so the sidebar header only carries the "add channel" action.
    const add = document.createElement('button');
    add.textContent = '+';
    add.title = 'Create or join a channel in this space';
    add.onclick = () => newChannel(sp.id);
    btns.appendChild(add);
    h3.appendChild(btns);
    section.appendChild(h3);
    const ul = document.createElement('ul');
    for (const ch of (bySpace.get(sp.id) || [])) ul.appendChild(channelLi(ch));
    section.appendChild(ul);
    container.appendChild(section);
  }
  updateActionIcons();
}

// Gray out the pins header icon when nothing is pinned anywhere. (Threads is
// driven by refreshThreadCount to match its inbox; tasks/notifs toggle their
// own icons elsewhere.)
function updateActionIcons() {
  $('pins-btn').classList.toggle('active', channels.some(c => c.pinned_count > 0));
}

async function loadChannels() {
  [spaces, channels] = await Promise.all([api('/spaces'), api('/channels')]);
  if (currentChannel) {
    const fresh = channels.find(c => c.id === currentChannel.id);
    if (fresh) { currentChannel = fresh; updateChannelHeader(); }
  }
  renderChannels();
}

function updateChannelHeader() {
  if (!currentChannel) {
    $('channel-title').textContent = 'Select a channel';
    $('channel-topic').textContent = '';
    $('channel-actions').classList.add('hidden');
    return;
  }
  $('channel-title').textContent = channelLabel(currentChannel);
  $('channel-topic').textContent = currentChannel.topic || '';
  $('channel-actions').classList.remove('hidden');
  $('members-btn').classList.toggle('hidden', currentChannel.is_dm);
  $('edit-channel-btn').classList.toggle('hidden', !canManage(currentChannel));
}

async function selectChannel(ch) {
  exitEditMode();
  cancelReply();
  closeThread();
  closeMention();
  currentChannel = ch;
  if (ch.unread_count) { ch.unread_count = 0; markRead(ch.id); }
  clearChannelNotifs(ch.id);
  typers.clear(); renderTyping();
  closePreview();
  renderChannels();
  updateChannelHeader();
  $('app').classList.add('chat-open');
  setComposerEnabled(true);
  if (!editingMessageId) restoreDraft(ch.id);
  const messages = await api(`/channels/${ch.id}/messages?limit=100`);
  const box = $('messages');
  box.innerHTML = '';
  messages.forEach(renderMessage);
  box.scrollTop = box.scrollHeight;
  stickBottom = true;
  _lastVH = parseInt(document.documentElement.style.getPropertyValue('--vh'), 10) || 0;
  oldestMessageId = messages.length ? messages[0].id : null;
  allHistoryLoaded = messages.length < 100;
  loadingOlder = false;
  $('send-input').focus();
  // On desktop the Channel pane is the default right-hand column — always open.
  // On mobile it's a full-screen overlay, so leave it closed (open it manually).
  if (!isNarrow()) {
    closePreview();
    $('info-pane').classList.remove('hidden');
  } else {
    closeInfoPane();
  }
  loadInfoPane();  // refresh the info pane for the new channel
}

async function maybeLoadOlder(force = false) {
  const box = $('messages');
  if ((!force && box.scrollTop > 80) || loadingOlder || allHistoryLoaded
      || !currentChannel || !oldestMessageId) return;
  loadingOlder = true;
  const channelAtStart = currentChannel.id;
  try {
    const older = await api(
      `/channels/${channelAtStart}/messages?before=${oldestMessageId}&limit=100`);
    if (!currentChannel || currentChannel.id !== channelAtStart) return;
    if (older.length < 100) allHistoryLoaded = true;
    if (older.length === 0) return;
    oldestMessageId = older[0].id;
    const fragment = document.createDocumentFragment();
    older.forEach(m => fragment.appendChild(buildMessageNode(m)));
    const prevHeight = box.scrollHeight;
    box.insertBefore(fragment, box.firstChild);
    box.scrollTop += box.scrollHeight - prevHeight;  // keep view anchored
  } catch {} finally {
    loadingOlder = false;
  }
}

async function jumpToMessage(channelId, messageId) {
  const ch = channels.find(c => c.id === channelId);
  if (!ch) { appAlert('That conversation is no longer available'); return; }
  if (!currentChannel || currentChannel.id !== channelId) await selectChannel(ch);
  // Page back until the target message is in the DOM (or history runs out).
  let tries = 0;
  while (!document.querySelector(`.msg[data-id="${messageId}"]`)
         && !allHistoryLoaded && tries < 40) {
    await maybeLoadOlder(true);
    tries++;
  }
  const el = document.querySelector(`.msg[data-id="${messageId}"]`);
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.classList.remove('flash');
    void el.offsetWidth;  // restart the animation if it's already applied
    el.classList.add('flash');
  }
}

function clearChannelView() {
  exitEditMode();
  cancelReply();
  closeThread();
  currentChannel = null;
  $('messages').innerHTML = '';
  closePreview();
  closeInfoPane();
  setComposerEnabled(false);
  $('app').classList.remove('chat-open');
  updateChannelHeader();
  renderChannels();
}

async function editChannel() {
  if (!currentChannel || !canManage(currentChannel)) return;
  const name = await appPrompt('Channel name:', { value: currentChannel.name || '' });
  if (name === null) return;
  const topic = await appPrompt('Topic (empty to clear):', { value: currentChannel.topic || '' });
  if (topic === null) return;
  // Optional: move to another space. Only offer spaces other than the current one.
  let moveTo = null;
  const others = spaces.filter(s => s.id !== currentChannel.space_id);
  if (others.length) {
    const choice = await appChoose('Space:', [
      { value: '', label: 'Keep in current space' },
      ...others.map(s => ({ value: s.id, label: 'Move to ' + s.name })),
    ], { value: '' });
    if (choice === null) return;
    if (choice) moveTo = choice;
  }
  try {
    await api(`/channels/${currentChannel.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: name.trim() || null, topic }),
    });
    if (moveTo) {
      await api(`/channels/${currentChannel.id}/space`, {
        method: 'PUT',
        body: JSON.stringify({ space_id: moveTo }),
      });
    }
    await loadChannels();
  } catch (e) { appAlert(e.message); }
}

async function loadAdminChannels() {
  const list = $('admin-channel-list');
  const chans = await api('/admin/channels');
  list.innerHTML = '';
  if (chans.length === 0) {
    const li = document.createElement('li');
    li.className = 'sub';
    li.textContent = 'No channels.';
    list.appendChild(li);
  }
  for (const c of chans) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = '# ' + c.name;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `${c.member_count} member(s)` + (c.is_private ? ' · private' : '');
    grow.appendChild(sub);
    li.appendChild(grow);
    const btn = document.createElement('button');
    btn.textContent = 'Delete';
    btn.onclick = async () => {
      const typed = await appPrompt(
        `This permanently deletes "# ${c.name}" with all its messages and files.\n\nType the channel name to confirm:`,
        { okText: 'Delete', danger: true, placeholder: c.name });
      if (typed === null) return;
      if (typed.trim() !== c.name) { appAlert('Name did not match — nothing was deleted.'); return; }
      try {
        await api(`/channels/${c.id}`, { method: 'DELETE' });
        await Promise.all([loadAdminChannels(), loadChannels()]);
      } catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

// ---------- members ----------

async function openMembers() {
  if (!currentChannel || currentChannel.is_dm) return;
  $('members-title').textContent = `Members — ${channelLabel(currentChannel)}`;
  $('member-add-row').classList.toggle('hidden', !canManage(currentChannel));
  $('members').classList.remove('hidden');
  await loadMembers();
}

async function loadMembers() {
  const list = $('member-list');
  const members = await api(`/channels/${currentChannel.id}/members`);
  list.innerHTML = '';
  for (const m of members) {
    const li = document.createElement('li');
    const dot = document.createElement('span');
    dot.className = 'dot ' + presenceOf(m.id);
    dot.dataset.uid = m.id;
    dot.title = presenceOf(m.id) === 'offline' ? lastSeenText(m) : presenceOf(m.id);
    li.appendChild(dot);
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = m.display_name;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = '@' + m.username + (m.is_admin ? ' · server admin' : '');
    grow.appendChild(sub);
    if (m.status) {
      const st = document.createElement('div');
      st.className = 'sub status-line';
      st.textContent = m.status;
      grow.appendChild(st);
    }
    li.appendChild(grow);
    if (canManage(currentChannel) && m.id !== me.id) {
      const btn = document.createElement('button');
      btn.textContent = 'Remove';
      btn.onclick = async () => {
        if (!await appConfirm(`Remove ${m.display_name} from this channel?`)) return;
        try {
          await api(`/channels/${currentChannel.id}/members/${m.id}`, { method: 'DELETE' });
          await loadMembers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(btn);
    }
    list.appendChild(li);
  }
}

async function addMember() {
  const username = $('member-add-input').value.trim().toLowerCase();
  if (!username || !currentChannel) return;
  try {
    const user = (await api('/users')).find(u => u.username === username);
    if (!user) throw new Error('No such user');
    await api(`/channels/${currentChannel.id}/members`, {
      method: 'POST', body: JSON.stringify({ user_id: user.id }),
    });
    $('member-add-input').value = '';
    await loadMembers();
  } catch (e) { appAlert(e.message); }
}

// ---------- avatars ----------

const avatarCache = new Map();

function colorFor(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 45% 42%)`;
}

async function loadAvatar(user) {
  const key = `${user.id}:${user.avatar_at}`;
  if (avatarCache.has(key)) return avatarCache.get(key);
  const res = await fetch(`/api/v1/users/${user.id}/avatar`, {
    headers: { Authorization: 'Bearer ' + token },
  });
  if (!res.ok) throw new Error('no avatar');
  const url = URL.createObjectURL(await res.blob());
  avatarCache.set(key, url);
  return url;
}

function avatarEl(user) {
  const el = document.createElement('span');
  el.className = 'avatar';
  if (user.avatar_at) {
    const img = document.createElement('img');
    img.alt = user.display_name;
    loadAvatar(user).then(url => { img.src = url; }).catch(() => {
      el.textContent = (user.display_name || '?').trim().charAt(0).toUpperCase();
      el.style.background = colorFor(user.id);
    });
    el.appendChild(img);
  } else {
    el.textContent = (user.display_name || '?').trim().charAt(0).toUpperCase();
    el.style.background = colorFor(user.id);
  }
  return el;
}

// ---------- messages ----------

async function loadAuthedFile(fileId) {
  if (imageUrls.has(fileId)) return imageUrls.get(fileId);
  const res = await fetch(`/api/v1/files/${fileId}`, {
    headers: { Authorization: 'Bearer ' + token },
  });
  if (!res.ok) throw new Error('File unavailable');
  const blob = await res.blob();
  const entry = { blob, url: URL.createObjectURL(blob) };
  imageUrls.set(fileId, entry);
  return entry;
}

async function loadAuthedBlob(fileId) {
  return (await loadAuthedFile(fileId)).url;
}

// Preview only types that are inert when rendered. Never frame anything
// HTML-ish: a blob iframe runs in our origin, so that would be self-XSS.
function previewKind(contentType) {
  if (contentType.startsWith('image/')) return 'image';
  if (contentType.startsWith('video/')) return 'video';
  if (contentType.startsWith('audio/')) return 'audio';
  if (contentType === 'application/pdf') return 'pdf';
  if (contentType === 'text/plain' || contentType === 'text/csv'
      || contentType === 'text/markdown' || contentType === 'application/json') return 'text';
  return null;
}

async function downloadFile(file) {
  try {
    const url = await loadAuthedBlob(file.id);
    const dl = document.createElement('a');
    dl.href = url; dl.download = file.filename;
    dl.click();
  } catch (err) { appAlert(err.message); }
}

function closePreview() {
  $('preview-pane').classList.add('hidden');
  $('viewer-content').innerHTML = '';
}

// ---------- channel info pane (tasks + reminders) ----------

function isNarrow() {
  return window.matchMedia('(max-width: 700px)').matches;
}

function closeInfoPane() {
  $('info-pane').classList.add('hidden');
}

// After closing a thread/preview on desktop, fall back to the Channel pane
// (the default right-hand column). No-op on mobile or with no channel open.
function maybeReopenInfoPane() {
  if (!isNarrow() && currentChannel) {
    $('info-pane').classList.remove('hidden');
    loadInfoPane();
  }
}

// When jumping to a message from the pane, only close the pane on mobile
// (where it's a full-screen overlay covering the chat). On desktop it's a
// side column, so keep it open.
function jumpFromInfoPane(channelId, messageId) {
  if (isNarrow()) closeInfoPane();
  jumpToMessage(channelId, messageId);
}

async function toggleInfoPane() {
  if (!$('info-pane').classList.contains('hidden')) { closeInfoPane(); return; }
  if (!currentChannel) return;
  closePreview();
  closeThread();
  $('info-pane').classList.remove('hidden');
  await loadInfoPane();
}

let infoPaneTimer = null;
function scheduleInfoPane() {
  if ($('info-pane').classList.contains('hidden')) return;
  clearTimeout(infoPaneTimer);
  infoPaneTimer = setTimeout(loadInfoPane, 120);
}

function renderNotifyPref() {
  const box = $('notify-pref');
  if (!box || !currentChannel) return;
  box.innerHTML = '';
  const levels = [
    ['all', 'All', 'Notify on every message'],
    ['mentions', 'Mentions', 'Only @mentions and threads you’re in'],
    ['muted', 'Muted', 'No notifications; hidden from unread'],
  ];
  const current = currentChannel.notify_level || 'all';
  for (const [value, label, hint] of levels) {
    const b = document.createElement('button');
    b.textContent = label;
    b.title = hint;
    if (value === current) b.classList.add('active');
    b.onclick = () => setNotifyLevel(value);
    box.appendChild(b);
  }
}

async function setNotifyLevel(level) {
  if (!currentChannel || currentChannel.notify_level === level) return;
  try {
    const updated = await api(`/channels/${currentChannel.id}/notify`, {
      method: 'PUT', body: JSON.stringify({ level }),
    });
    currentChannel.notify_level = updated.notify_level;
    const ch = channelById(updated.id);
    if (ch) ch.notify_level = updated.notify_level;
    renderNotifyPref();
    renderChannels();
  } catch (e) { appAlert(e.message); }
}

async function renderWebhooks() {
  const section = $('info-webhooks-section');
  if (!currentChannel || !canManage(currentChannel)) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');
  const cid = currentChannel.id;
  const list = $('info-webhooks');
  let hooks;
  try { hooks = await api(`/channels/${cid}/webhooks`); }
  catch { return; }
  if (!currentChannel || currentChannel.id !== cid) return;
  list.innerHTML = '';
  if (!hooks.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No webhooks. Use + to create one.';
    list.appendChild(li);
    return;
  }
  for (const h of hooks) {
    const li = document.createElement('li');
    const grow = document.createElement('div');
    grow.className = 'grow';
    const b = document.createElement('b');
    b.textContent = h.name;
    grow.appendChild(b);
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = h.last_used_at ? 'last used ' + relTime(h.last_used_at) : 'never used';
    grow.appendChild(sub);
    li.appendChild(grow);
    const del = document.createElement('button');
    del.textContent = 'Delete';
    del.onclick = async () => {
      if (!await appConfirm(`Delete webhook “${h.name}”? Its URL will stop working.`)) return;
      try { await api(`/webhooks/${h.id}`, { method: 'DELETE' }); renderWebhooks(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(del);
    list.appendChild(li);
  }
}

async function createWebhook() {
  if (!currentChannel) return;
  const name = await appPrompt('Name this webhook (shown as the sender), e.g. “Backups”:');
  if (!name || !name.trim()) return;
  try {
    const hook = await api(`/channels/${currentChannel.id}/webhooks`, {
      method: 'POST', body: JSON.stringify({ name: name.trim() }),
    });
    let copied = false;
    try { await navigator.clipboard.writeText(hook.url); copied = true; } catch {}
    await appAlert(
      `Webhook “${hook.name}” created.\n\nPOST JSON like {"text":"hello"} to:\n\n${hook.url}\n\n`
      + (copied ? 'The URL has been copied to your clipboard. ' : '')
      + 'This is the only time the URL is shown — save it now.');
    renderWebhooks();
  } catch (e) { appAlert(e.message); }
}

async function loadInfoPane() {
  if (!currentChannel || $('info-pane').classList.contains('hidden')) return;
  renderNotifyPref();
  renderWebhooks();
  const cid = currentChannel.id;
  const taskList = $('info-tasks');
  const remList = $('info-reminders');
  const pinList = $('info-pins');
  const threadList = $('info-threads');
  // Fetch BEFORE clearing so the list never flashes empty mid-refresh.
  let tasks = [], reminders = [], pins = [], threads = [];
  try {
    [tasks, reminders, pins, threads] = await Promise.all([
      api('/tasks'), api('/reminders'), api(`/channels/${cid}/pins`),
      api(`/channels/${cid}/threads`),
    ]);
  } catch (e) { appAlert(e.message); return; }
  if (!currentChannel || currentChannel.id !== cid) return;
  taskList.innerHTML = ''; remList.innerHTML = ''; pinList.innerHTML = '';
  threadList.innerHTML = '';

  if (!threads.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No threads in this channel.';
    threadList.appendChild(li);
  }
  for (const r of threads) {
    const li = document.createElement('li');
    const grow = document.createElement('div');
    grow.className = 'grow';
    const who = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = r.sender.display_name + ': ';
    who.appendChild(b);
    who.appendChild(document.createTextNode(
      r.content ? r.content.slice(0, 80) : (r.file ? r.file.filename : '(message)')));
    grow.appendChild(who);
    const sub = document.createElement('div');
    sub.className = 'sub thread-sub';
    if (r.thread_repliers && r.thread_repliers.length) {
      const faces = document.createElement('span');
      faces.className = 'faces';
      r.thread_repliers.forEach(u => faces.appendChild(avatarEl(u)));
      sub.appendChild(faces);
    }
    const n = r.reply_count;
    const cnt = document.createElement('span');
    cnt.textContent = `${n} ${n === 1 ? 'reply' : 'replies'}`
      + (r.thread_last_at ? ' · ' + relTime(r.thread_last_at) : '');
    sub.appendChild(cnt);
    grow.appendChild(sub);
    grow.onclick = () => openThread(r);
    li.appendChild(grow);
    threadList.appendChild(li);
  }

  if (!pins.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No pinned messages.';
    pinList.appendChild(li);
  }
  for (const pm of pins) {
    const li = document.createElement('li');
    const grow = document.createElement('div');
    grow.className = 'grow';
    const who = document.createElement('div');
    who.innerHTML = '';
    const b = document.createElement('b');
    b.textContent = pm.sender.display_name + ': ';
    who.appendChild(b);
    who.appendChild(document.createTextNode(
      pm.content ? pm.content.slice(0, 100) : (pm.file ? pm.file.filename : '(message)')));
    grow.appendChild(who);
    grow.onclick = () => jumpFromInfoPane(cid, pm.id);
    li.appendChild(grow);
    const unpin = document.createElement('button');
    unpin.textContent = 'Unpin';
    unpin.onclick = async () => {
      try { await api(`/messages/${pm.id}/pin`, { method: 'DELETE' }); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(unpin);
    pinList.appendChild(li);
  }

  const myTasks = tasks.filter(t => t.channel_id === cid);
  if (!myTasks.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No open tasks in this channel.';
    taskList.appendChild(li);
  }
  for (const t of myTasks) {
    const li = document.createElement('li');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.title = 'Mark done';
    cb.onchange = async () => {
      cb.disabled = true;
      try {
        await api(`/messages/${t.message_id}/checkbox`, {
          method: 'POST', body: JSON.stringify({ line: t.line, checked: true }),
        });
        li.remove();
        refreshTaskCount();
      } catch (e) { cb.checked = false; cb.disabled = false; appAlert(e.message); }
    };
    li.appendChild(cb);
    const grow = document.createElement('div');
    grow.className = 'grow';
    renderInline(t.text, grow);
    grow.onclick = () => jumpFromInfoPane(cid, t.message_id);
    li.appendChild(grow);
    taskList.appendChild(li);
  }

  const myRems = reminders
    .filter(r => r.channel_id === cid)
    .sort((a, b) => new Date(a.due_at) - new Date(b.due_at));
  if (!myRems.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No reminders for this channel.';
    remList.appendChild(li);
  }
  for (const r of myRems) {
    const li = document.createElement('li');
    const grow = document.createElement('div');
    grow.className = 'grow';
    grow.textContent = r.text;
    if (r.message_id) grow.onclick = () => jumpFromInfoPane(cid, r.message_id);
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = 'due ' + new Date(r.due_at).toLocaleString();
    grow.appendChild(sub);
    li.appendChild(grow);
    const cancel = document.createElement('button');
    cancel.textContent = 'Cancel';
    cancel.onclick = async () => {
      try { await api(`/reminders/${r.id}`, { method: 'DELETE' }); await loadInfoPane(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(cancel);
    remList.appendChild(li);
  }
}

const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

async function openViewer(file) {
  const kind = previewKind(file.content_type);
  if (!kind) return downloadFile(file);
  closeInfoPane();
  if (kind === 'pdf' && IS_IOS) {
    // iOS WebKit can't render PDFs in embedded frames — hand the blob to
    // the system viewer in a new tab instead. The window must be opened
    // synchronously (before any await) or Safari blocks it as a popup.
    const w = window.open('', '_blank');
    try {
      const entry = await loadAuthedFile(file.id);
      if (w) { w.location = entry.url; return; }
    } catch {
      if (w) w.close();
    }
    return downloadFile(file);
  }
  $('viewer-title').textContent = file.filename;
  $('viewer-download').onclick = () => downloadFile(file);
  const box = $('viewer-content');
  box.innerHTML = '';
  closeInfoPane();
  closeThread();
  $('preview-pane').classList.remove('hidden');
  try {
    const entry = await loadAuthedFile(file.id);
    if (kind === 'text') {
      if (entry.blob.size > 1024 * 1024) throw new Error('Too large to preview — use Download');
      const pre = document.createElement('pre');
      pre.textContent = await entry.blob.text();
      box.appendChild(pre);
      return;
    }
    const el = kind === 'image' ? document.createElement('img')
      : kind === 'video' ? document.createElement('video')
      : kind === 'audio' ? document.createElement('audio')
      : document.createElement('iframe');
    if (kind === 'video' || kind === 'audio') el.controls = true;
    if (kind === 'image') el.alt = file.filename;
    el.src = entry.url;
    box.appendChild(el);
  } catch (err) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = err.message;
    box.appendChild(p);
  }
}

// Lightweight message formatting, all built with DOM nodes (never innerHTML):
// `code`, **bold**, *italic*, ```blocks```, auto-linked URLs, @me highlight.
function linkEl(url, label) {
  const a = document.createElement('a');
  a.href = url;
  a.textContent = label;
  a.target = '_blank';
  a.rel = 'noopener noreferrer nofollow';
  return a;
}

function renderInline(text, container) {
  // Order matters: markdown links before bare URLs.
  const combined = new RegExp(
    '(`[^`\\n]+`)'                                 // 1 inline code
    + '|(\\[[^\\]\\n]+\\]\\(https?:\\/\\/[^\\s)]+\\))'  // 2 [text](url)
    + '|(\\*\\*[^*\\n]+\\*\\*)'                     // 3 bold
    + '|(~~[^~\\n]+~~)'                             // 4 strikethrough
    + '|(\\*[^*\\n]+\\*)'                           // 5 italic
    + '|(https?:\\/\\/[^\\s<>"]+)'                  // 6 bare url
    + (me ? `|(@${me.username}\\b)` : ''),          // 7 @me
    'gi'
  );
  let last = 0, m;
  while ((m = combined.exec(text)) !== null) {
    container.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (m[1]) {
      const el = document.createElement('code');
      el.textContent = tok.slice(1, -1);
      container.appendChild(el);
    } else if (m[2]) {
      const mk = tok.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      container.appendChild(linkEl(mk[2], mk[1]));
    } else if (m[3]) {
      const el = document.createElement('strong');
      el.textContent = tok.slice(2, -2);
      container.appendChild(el);
    } else if (m[4]) {
      const el = document.createElement('del');
      el.textContent = tok.slice(2, -2);
      container.appendChild(el);
    } else if (m[5]) {
      const el = document.createElement('em');
      el.textContent = tok.slice(1, -1);
      container.appendChild(el);
    } else if (m[6]) {
      let url = tok, trail = '';
      while (/[.,;:!?)\]]$/.test(url)) { trail = url.slice(-1) + trail; url = url.slice(0, -1); }
      container.appendChild(linkEl(url, url));
      if (trail) container.appendChild(document.createTextNode(trail));
    } else {
      const el = document.createElement('span');
      el.className = 'mention';
      el.textContent = tok;
      container.appendChild(el);
    }
    last = m.index + tok.length;
  }
  container.appendChild(document.createTextNode(text.slice(last)));
}

function isBlockLine(line) {
  return /^\[( |x)\] /i.test(line) || /^- /.test(line) || /^\d+\. /.test(line)
    || /^> /.test(line) || line.trim().startsWith('```');
}

function renderBody(text, messageId, cleared) {
  cleared = cleared || {};
  const body = document.createElement('div');
  body.className = 'body';
  const lines = text.split('\n');
  let inCode = false;
  let codeBuf = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      if (inCode) {
        const pre = document.createElement('pre');
        pre.className = 'code-block';
        pre.textContent = codeBuf.join('\n');
        body.appendChild(pre);
        codeBuf = [];
        inCode = false;
      } else {
        inCode = true;
      }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }

    let m;
    if ((m = line.match(/^\[( |x)\] (.*)$/i))) {
      const row = document.createElement('div');
      const done = m[1].toLowerCase() === 'x';
      row.className = 'task-line' + (done ? ' done' : '');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = done;
      const lineIndex = i;
      cb.onchange = async () => {
        try {
          await api(`/messages/${messageId}/checkbox`, {
            method: 'POST',
            body: JSON.stringify({ line: lineIndex, checked: cb.checked }),
          });
          refreshTaskCount();
        } catch (err) { cb.checked = !cb.checked; appAlert(err.message); }
      };
      row.appendChild(cb);
      const span = document.createElement('span');
      renderInline(m[2], span);
      row.appendChild(span);
      if (done && cleared[String(i)]) {
        const stamp = document.createElement('span');
        stamp.className = 'task-cleared';
        stamp.textContent = 'cleared ' + shortDate(cleared[String(i)]);
        row.appendChild(stamp);
      }
      body.appendChild(row);
      continue;
    }
    if ((m = line.match(/^- (.*)$/))) {
      const row = document.createElement('div');
      row.className = 'bullet-line';
      const dot = document.createElement('span');
      dot.className = 'bullet';
      dot.textContent = '•';
      row.appendChild(dot);
      const span = document.createElement('span');
      renderInline(m[1], span);
      row.appendChild(span);
      body.appendChild(row);
      continue;
    }
    if ((m = line.match(/^(\d+)\. (.*)$/))) {
      const row = document.createElement('div');
      row.className = 'bullet-line';
      const num = document.createElement('span');
      num.className = 'bullet';
      num.textContent = m[1] + '.';
      row.appendChild(num);
      const span = document.createElement('span');
      renderInline(m[2], span);
      row.appendChild(span);
      body.appendChild(row);
      continue;
    }
    if ((m = line.match(/^> (.*)$/))) {
      const row = document.createElement('div');
      row.className = 'blockquote-line';
      renderInline(m[1], row);
      body.appendChild(row);
      continue;
    }
    renderInline(line, body);
    if (i < lines.length - 1 && !isBlockLine(lines[i + 1])) {
      body.appendChild(document.createTextNode('\n'));
    }
  }
  if (inCode) {
    // unclosed fence — render what we have as text
    renderInline('```', body);
    if (codeBuf.length) body.appendChild(document.createTextNode('\n' + codeBuf.join('\n')));
  }
  return body;
}

let _toastTimer = null;
function toast(msg) {
  let el = document.getElementById('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1600);
}

async function copyMessageText(m) {
  const text = m.content || '';
  if (!text) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback for non-secure contexts (e.g. http LAN testing).
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast('Copied');
  } catch { toast('Could not copy'); }
}

// Mobile message-action popup (the "⋯" menu). Built from the same action
// descriptors the desktop inline icons use.
function closeMsgMenu() {
  const m = document.getElementById('msg-menu');
  if (m) m.remove();
  document.removeEventListener('pointerdown', onMsgMenuDown, true);
  window.removeEventListener('scroll', closeMsgMenu, true);
  window.removeEventListener('resize', closeMsgMenu, true);
}
function onMsgMenuDown(e) {
  const menu = document.getElementById('msg-menu');
  if (menu && !menu.contains(e.target)) closeMsgMenu();
}
function openMsgMenu(anchor, acts) {
  closeMsgMenu();
  const menu = document.createElement('div');
  menu.id = 'msg-menu';
  menu.className = 'msg-menu';
  for (const a of acts) {
    const item = document.createElement('button');
    const ic = document.createElement('span');
    ic.className = 'mm-icon';
    ic.textContent = a.icon;
    item.appendChild(ic);
    item.appendChild(document.createTextNode(a.label));
    item.onclick = () => { closeMsgMenu(); a.fn(); };
    menu.appendChild(item);
  }
  menu.style.visibility = 'hidden';
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  // Always right-aligned to the message (away from the avatar/thumb side).
  let left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8));
  let top = Math.max(8, Math.min(r.top, window.innerHeight - mh - 8));
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  menu.style.visibility = 'visible';
  setTimeout(() => {
    document.addEventListener('pointerdown', onMsgMenuDown, true);
    window.addEventListener('scroll', closeMsgMenu, true);
    window.addEventListener('resize', closeMsgMenu, true);
  }, 0);
}

function buildMessageNode(m, opts) {
  opts = opts || {};
  const div = document.createElement('div');
  div.className = 'msg';
  div.dataset.id = m.id;

  div.appendChild(avatarEl(m.sender));
  const main = document.createElement('div');
  main.className = 'msg-main';
  div.appendChild(main);

  const meta = document.createElement('div');
  meta.className = 'meta';
  const name = document.createElement('b');
  name.textContent = m.sender.display_name;
  meta.appendChild(name);
  if (m.sender.username === 'webhook') {
    const tag = document.createElement('span');
    tag.className = 'bot-tag';
    tag.textContent = 'APP';
    tag.title = 'Posted by an incoming webhook';
    meta.appendChild(tag);
  }
  const when = document.createElement('span');
  when.textContent = relTime(m.created_at) + (m.edited_at ? ' (edited)' : '');
  when.title = new Date(m.created_at).toLocaleString();
  meta.appendChild(when);
  if (m.pinned) {
    const pinTag = document.createElement('span');
    pinTag.className = 'pin-tag';
    pinTag.textContent = '📌';
    pinTag.title = 'Pinned';
    meta.appendChild(pinTag);
  }

  // One source of truth for the per-message actions: rendered as inline hover
  // icons on desktop, and collapsed into a tap-to-open "⋯" menu on mobile (so
  // they never wrap onto a second row on a narrow screen).
  const acts = [
    { icon: '💬', label: opts.inThread ? 'Reply in this thread' : 'Reply in thread', fn: () => openThread(m) },
  ];
  if (m.content) acts.push({ icon: '📋', label: 'Copy text', fn: () => copyMessageText(m) });
  acts.push({ icon: '📌', label: m.pinned ? 'Unpin' : 'Pin', cls: m.pinned ? 'active-pin' : '', fn: () => togglePin(m) });
  acts.push({ icon: '⏰', label: 'Remind me about this', fn: () => remindAboutMessage(m) });
  if (me && m.sender.id === me.id) {
    if (!/^\[( |x)\] /i.test(m.content)) {
      acts.push({ icon: '☑', label: 'Convert to task', fn: () => convertToTask(m) });
    }
    acts.push({ icon: '✎', label: 'Edit', fn: () => editMessage(m) });
    acts.push({ icon: '🗑', label: 'Delete', fn: () => deleteMessage(m) });
  } else if (m.sender.username === 'webhook' && currentChannel && canManage(currentChannel)) {
    acts.push({ icon: '🗑', label: 'Delete', fn: () => deleteMessage(m) });
  }
  const actions = document.createElement('span');
  actions.className = 'actions';
  for (const a of acts) {
    const b = document.createElement('button');
    b.textContent = a.icon;
    b.title = a.label;
    if (a.cls) b.classList.add(a.cls);
    b.onclick = a.fn;
    actions.appendChild(b);
  }
  meta.appendChild(actions);

  // Touch/narrow clients open the action menu by tapping the message (except on
  // interactive bits: links, images, reactions, reply/thread jumps, buttons).
  // Only true wide hover-capable desktops skip it and use the inline hover icons
  // instead; an iPad PWA is wide but has no hover, so it gets the menu too.
  div.addEventListener('click', (e) => {
    if (!isNarrow() && matchMedia('(hover: hover)').matches) return;
    if (e.target.closest('a, button, input, label, img, .reply-preview, .thread-summary')) return;
    openMsgMenu(div, acts);
  });
  main.appendChild(meta);

  if (m.reply_to) {
    const rp = document.createElement('div');
    rp.className = 'reply-preview';
    rp.title = 'Go to replied message';
    const who = document.createElement('b');
    who.textContent = '↳ ' + m.reply_to.sender_name;
    const snip = document.createElement('span');
    snip.className = 'snip';
    snip.textContent = m.reply_to.snippet;
    rp.appendChild(who);
    rp.appendChild(snip);
    rp.onclick = () => jumpToMessage(m.channel_id, m.reply_to.id);
    main.appendChild(rp);
  }

  if (m.content) main.appendChild(renderBody(m.content, m.id, m.task_cleared));

  if (m.file) {
    if (m.file.content_type.startsWith('image/')) {
      const img = document.createElement('img');
      img.className = 'attachment-img';
      img.alt = m.file.filename;
      img.title = 'Click to view full size';
      img.onclick = () => openViewer(m.file);
      loadAuthedBlob(m.file.id).then(url => { img.src = url; }).catch(() => img.remove());
      main.appendChild(img);
    } else {
      const row = document.createElement('div');
      row.className = 'attachment-row';
      const a = document.createElement('a');
      a.href = '#';
      a.className = 'attachment-file';
      const viewable = previewKind(m.file.content_type) !== null;
      a.textContent = `📄 ${m.file.filename} (${fmtSize(m.file.size_bytes)})`;
      a.title = viewable ? 'Click to preview' : 'Click to download';
      a.onclick = e => { e.preventDefault(); openViewer(m.file); };
      row.appendChild(a);
      const dl = document.createElement('button');
      dl.textContent = '⤓';
      dl.title = 'Download';
      dl.onclick = () => downloadFile(m.file);
      row.appendChild(dl);
      main.appendChild(row);
    }
  }

  if (m.link_previews && m.link_previews.length) {
    main.appendChild(linkCardsEl(m.link_previews));
  }

  const rrow = document.createElement('div');
  rrow.className = 'reactions';
  renderReactions(rrow, m.id, m.reactions);
  main.appendChild(rrow);

  if (!opts.inThread && m.reply_count > 0) {
    main.appendChild(threadSummaryEl(m));
  }

  return div;
}

function linkCardsEl(previews) {
  const wrap = document.createElement('div');
  wrap.className = 'link-cards';
  for (const p of previews) {
    const a = document.createElement('a');
    a.className = 'link-card';
    a.href = p.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    if (p.site_name) {
      const site = document.createElement('div');
      site.className = 'lc-site';
      site.textContent = p.site_name;
      a.appendChild(site);
    }
    if (p.title) {
      const t = document.createElement('div');
      t.className = 'lc-title';
      t.textContent = p.title;
      a.appendChild(t);
    }
    if (p.description) {
      const d = document.createElement('div');
      d.className = 'lc-desc';
      d.textContent = p.description;
      a.appendChild(d);
    }
    wrap.appendChild(a);
  }
  return wrap;
}

function renderMessage(m) {
  $('messages').appendChild(buildMessageNode(m));
}

// ---------- threads ----------

function threadSummaryEl(m) {
  const el = document.createElement('div');
  el.className = 'thread-summary';
  if (m.thread_repliers && m.thread_repliers.length) {
    const faces = document.createElement('span');
    faces.className = 'faces';
    m.thread_repliers.forEach(u => faces.appendChild(avatarEl(u)));
    el.appendChild(faces);
  }
  const n = m.reply_count;
  const label = document.createElement('span');
  label.textContent = n + (n === 1 ? ' reply' : ' replies');
  el.appendChild(label);
  if (m.thread_last_at) {
    const last = document.createElement('span');
    last.className = 'last';
    last.textContent = '· ' + relTime(m.thread_last_at);
    el.appendChild(last);
  }
  el.onclick = () => openThread(m);
  return el;
}

// Refresh (or remove) a root message's "N replies" affordance in the timeline.
function updateThreadSummary(rootId, meta) {
  const node = document.querySelector(`#messages .msg[data-id="${rootId}"] .msg-main`);
  if (!node) return;
  const existing = node.querySelector('.thread-summary');
  if (existing) existing.remove();
  if (meta.reply_count > 0) {
    node.appendChild(threadSummaryEl({
      id: rootId,
      channel_id: meta.channel_id,
      reply_count: meta.reply_count,
      thread_last_at: meta.thread_last_at,
      thread_repliers: meta.thread_repliers || [],
    }));
  }
}

async function openThread(m) {
  const rootId = m.thread_root_id || m.id;
  threadRootId = rootId;
  closePreview();
  closeInfoPane();
  $('thread-pane').classList.remove('hidden');
  $('thread-content').innerHTML = '<div class="empty" style="color:var(--muted);padding:12px">Loading…</div>';
  try {
    const msgs = await api(`/messages/${rootId}/thread`);
    if (threadRootId !== rootId) return;  // user moved on while loading
    renderThread(msgs);
  } catch (err) {
    $('thread-content').innerHTML = '';
    appAlert(err.message);
  }
}

function renderThread(msgs) {
  const box = $('thread-content');
  box.innerHTML = '';
  if (!msgs.length) { closeThread(); return; }
  const root = buildMessageNode(msgs[0], { inThread: true });
  root.classList.add('thread-root-msg');
  box.appendChild(root);
  const replies = msgs.slice(1);
  const count = document.createElement('div');
  count.id = 'thread-count';
  count.textContent = replies.length
    ? `${replies.length} ${replies.length === 1 ? 'reply' : 'replies'}`
    : 'No replies yet — start one below';
  box.appendChild(count);
  replies.forEach(r => box.appendChild(buildMessageNode(r, { inThread: true })));
  box.scrollTop = box.scrollHeight;
  $('thread-input').focus();
}

function appendThreadReply(m) {
  const box = $('thread-content');
  box.appendChild(buildMessageNode(m, { inThread: true }));
  const c = $('thread-count');
  if (c) {
    const n = box.querySelectorAll('.msg').length - 1;  // exclude the root
    c.textContent = `${n} ${n === 1 ? 'reply' : 'replies'}`;
  }
  box.scrollTop = box.scrollHeight;
}

function closeThread() {
  threadRootId = null;
  $('thread-pane').classList.add('hidden');
}

function autoGrowThread() {
  const t = $('thread-input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 140) + 'px';
}

async function sendThreadReply(e) {
  e.preventDefault();
  const ta = $('thread-input');
  const content = ta.value.trim();
  if (!content || !threadRootId || !currentChannel) return;
  if (content.length > 4000) { appAlert('Message is too long (4000 characters max)'); return; }
  ta.value = '';
  autoGrowThread();
  try {
    await api(`/channels/${currentChannel.id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content, thread_root_id: threadRootId }),
    });
  } catch (err) { ta.value = content; autoGrowThread(); appAlert(err.message); }
}

async function sendMessage(e) {
  e.preventDefault();
  const content = serializeEditor();
  if (!content || !currentChannel) return;
  if (content.length > 4000) { appAlert('Message is too long (4000 characters max)'); return; }
  if (editingMessageId) {
    const id = editingMessageId;
    exitEditMode();
    try {
      await api(`/messages/${id}`, { method: 'PATCH', body: JSON.stringify({ content }) });
    } catch (err) { appAlert(err.message); }
    return;
  }
  clearEditor();
  clearDraft(currentChannel.id);
  const reply_to_id = replyingTo ? replyingTo.id : null;
  cancelReply();
  try {
    await api(`/channels/${currentChannel.id}/messages`, {
      method: 'POST', body: JSON.stringify({ content, reply_to_id }),
    });
  } catch (err) { $('send-input').textContent = content; appAlert(err.message); }
}

// Edit reuses the main composer (full toolbar) in an "edit mode".
function fillInlineEditor(text, container) {
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(~~[^~\n]+~~)|(\*[^*\n]+\*)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    container.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    let el, inner;
    if (m[1]) { el = document.createElement('code'); inner = tok.slice(1, -1); }
    else if (m[2]) { el = document.createElement('strong'); inner = tok.slice(2, -2); }
    else if (m[3]) { el = document.createElement('del'); inner = tok.slice(2, -2); }
    else { el = document.createElement('em'); inner = tok.slice(1, -1); }
    el.textContent = inner;
    container.appendChild(el);
    last = m.index + tok.length;
  }
  container.appendChild(document.createTextNode(text.slice(last)));
}

function fillEditor(text) {
  const el = $('send-input');
  el.innerHTML = '';
  text.split('\n').forEach((line, i) => {
    if (i) el.appendChild(document.createElement('br'));
    fillInlineEditor(line, el);
  });
}

function editMessage(m) {
  if (!currentChannel) return;
  cancelReply();  // can't reply and edit at once
  editingMessageId = m.id;
  setComposerEnabled(true);
  fillEditor(m.content);
  $('send-input').focus();
  $('edit-banner').classList.remove('hidden');
  $('send-btn').textContent = 'Save';
  $('attach-btn').disabled = true;
}

function exitEditMode() {
  if (!editingMessageId) return;
  editingMessageId = null;
  clearEditor();
  $('edit-banner').classList.add('hidden');
  $('send-btn').textContent = 'Send';
  $('attach-btn').disabled = !currentChannel;
}

function startReply(m) {
  exitEditMode();  // can't edit and reply at once
  replyingTo = { id: m.id, sender_name: m.sender.display_name,
                 snippet: m.content ? m.content.slice(0, 80) : (m.file ? m.file.filename : 'message') };
  const text = $('reply-banner-text');
  text.innerHTML = '';
  const b = document.createElement('b');
  b.textContent = '↩ Replying to ' + replyingTo.sender_name + ': ';
  text.appendChild(b);
  text.appendChild(document.createTextNode(replyingTo.snippet));
  $('reply-banner').classList.remove('hidden');
  $('send-input').focus();
}

function cancelReply() {
  replyingTo = null;
  $('reply-banner').classList.add('hidden');
}

async function togglePin(m) {
  try {
    await api(`/messages/${m.id}/pin`, { method: m.pinned ? 'DELETE' : 'POST' });
  } catch (e) { appAlert(e.message); }
}

async function convertToTask(m) {
  const lines = m.content.split('\n');
  if (/^\[( |x)\] /i.test(lines[0])) return;
  lines[0] = '[ ] ' + lines[0];
  try {
    await api(`/messages/${m.id}`, {
      method: 'PATCH', body: JSON.stringify({ content: lines.join('\n') }),
    });
  } catch (e) { appAlert(e.message); }
}

async function deleteMessage(m) {
  if (!await appConfirm('Delete this message?')) return;
  try { await api(`/messages/${m.id}`, { method: 'DELETE' }); }
  catch (err) { appAlert(err.message); }
}

async function uploadAttachment() {
  const file = $('file-input').files[0];
  $('file-input').value = '';
  if (!file || !currentChannel) return;
  const attach = $('attach-btn');
  attach.disabled = true;
  try {
    const fd = new FormData();
    fd.append('upload', file);
    const meta = await api(`/channels/${currentChannel.id}/files`, { method: 'POST', body: fd });
    const reply_to_id = replyingTo ? replyingTo.id : null;
    cancelReply();
    await api(`/channels/${currentChannel.id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content: serializeEditor(), file_id: meta.id, reply_to_id }),
    });
    clearEditor();
    clearDraft(currentChannel.id);
  } catch (err) { appAlert(err.message); }
  finally { attach.disabled = false; }
}

// ---------- reminders & notifications ----------

// Promise-based "when?" picker: quick options + native datetime control.
let whenResolve = null;

function toLocalInput(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function pickWhen() {
  return new Promise(resolve => {
    whenResolve = resolve;
    const p = n => String(n).padStart(2, '0');
    const suggestion = new Date(Date.now() + 3600000);
    suggestion.setMinutes(Math.ceil(suggestion.getMinutes() / 15) * 15, 0, 0);
    // Keep the default within the 8 AM–6 PM window the dropdown offers.
    if (suggestion.getHours() < 8) {
      suggestion.setHours(8, 0, 0, 0);
    } else if (suggestion.getHours() > 18) {
      suggestion.setDate(suggestion.getDate() + 1);
      suggestion.setHours(8, 0, 0, 0);
    }
    const now = new Date();
    $('when-date').min = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
    $('when-date').value =
      `${suggestion.getFullYear()}-${p(suggestion.getMonth() + 1)}-${p(suggestion.getDate())}`;
    $('when-hour').value = String(suggestion.getHours());
    $('when-min').value = String(suggestion.getMinutes());
    $('when').classList.remove('hidden');
  });
}

function closeWhen(result) {
  $('when').classList.add('hidden');
  if (whenResolve) { whenResolve(result); whenResolve = null; }
}

const EMOJI_SET = [
  '👍', '👎', '❤️', '🔥', '🎉', '😂', '😍', '🤔', '😮', '😢',
  '😡', '🙏', '👏', '✅', '❌', '⭐', '💯', '👀', '🚀', '✨',
  '🙌', '💪', '🤝', '👋', '🤣', '😅', '😊', '😎', '🥳', '😤',
  '😴', '🤯', '🤷', '🍕', '☕', '💔', '⚡', '📌', '➕', '🎯',
];

function renderReactions(container, messageId, reactions) {
  container.innerHTML = '';
  for (const r of reactions || []) {
    const pill = document.createElement('button');
    pill.className = 'reaction' + (me && r.user_ids.includes(me.id) ? ' mine' : '');
    pill.textContent = `${r.emoji} ${r.count}`;
    pill.title = me && r.user_ids.includes(me.id) ? 'Click to remove your reaction' : 'Click to react too';
    pill.onclick = () => toggleReaction(messageId, r.emoji);
    container.appendChild(pill);
  }
  // "+" reveals an inline emoji strip: hover on desktop, tap on touch.
  const zone = document.createElement('span');
  zone.className = 'react-zone';
  const add = document.createElement('button');
  add.className = 'reaction reaction-add';
  add.textContent = '+';
  add.title = 'Add reaction';
  add.onclick = e => { e.stopPropagation(); zone.classList.toggle('open'); };
  zone.appendChild(add);
  const strip = document.createElement('span');
  strip.className = 'emoji-strip';
  for (const emoji of EMOJI_SET) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = emoji;
    b.onclick = e => {
      e.stopPropagation();
      zone.classList.remove('open');
      toggleReaction(messageId, emoji);
    };
    strip.appendChild(b);
  }
  zone.appendChild(strip);
  container.appendChild(zone);
}

async function toggleReaction(messageId, emoji) {
  try {
    const reactions = await api(`/messages/${messageId}/reactions`, {
      method: 'POST', body: JSON.stringify({ emoji }),
    });
    const row = document.querySelector(`.msg[data-id="${messageId}"] .reactions`);
    if (row) renderReactions(row, messageId, reactions);
  } catch (e) { appAlert(e.message); }
}

function updateNotifBadge() {
  const badge = $('notif-badge');
  badge.classList.toggle('hidden', notifUnread === 0);
  badge.textContent = notifUnread > 99 ? '99+' : notifUnread;
  $('notifs-btn').classList.toggle('active', notifUnread > 0);
  // Mirror onto the installed-app icon badge (iOS 16.4+ / desktop PWAs). The
  // service worker sets it from pushes while closed; this keeps it in sync
  // while open, so reading notifications clears the icon number. setAppBadge(0)
  // is the reliable clear (Safari's clearAppBadge() often doesn't take); call
  // clearAppBadge too as a belt-and-suspenders.
  if (navigator.setAppBadge) {
    navigator.setAppBadge(notifUnread).catch(() => {});
    if (notifUnread === 0 && navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  }
}

let taskCountTimer = null;
async function refreshTaskCount() {
  try {
    const tasks = await api('/tasks');
    $('tasks-btn').classList.toggle('active', tasks.length > 0);
    // Update each channel's open-task chip in the sidebar.
    const counts = {};
    for (const t of tasks) counts[t.channel_id] = (counts[t.channel_id] || 0) + 1;
    let changed = false;
    for (const ch of channels) {
      const n = counts[ch.id] || 0;
      if (ch.open_task_count !== n) { ch.open_task_count = n; changed = true; }
    }
    if (changed) renderChannels();
    scheduleInfoPane();
  } catch {}
}
function scheduleTaskCount() {
  clearTimeout(taskCountTimer);
  taskCountTimer = setTimeout(refreshTaskCount, 500);
}

// Threads icon mirrors the threads inbox: lit only when /threads would return
// something (threads you're in with activity in the last 7 days), so tapping a
// lit icon always shows content. (Channel-wide thread_count would light up for
// threads you're not part of.)
let threadCountTimer = null;
async function refreshThreadCount() {
  try {
    const threads = await api('/threads');
    $('threads-btn').classList.toggle('active', threads.length > 0);
  } catch {}
}
function scheduleThreadCount() {
  clearTimeout(threadCountTimer);
  threadCountTimer = setTimeout(refreshThreadCount, 500);
}

async function createReminder(text, due, messageId) {
  await api('/reminders', {
    method: 'POST',
    body: JSON.stringify({
      text,
      due_at: due.toISOString(),
      message_id: messageId || null,
      channel_id: !messageId && currentChannel ? currentChannel.id : null,
    }),
  });
  if (!$('info-pane').classList.contains('hidden')) loadInfoPane();
}

async function remindAboutMessage(m) {
  const due = await pickWhen();
  if (!due) return;
  try {
    const snippet = m.content ? m.content.slice(0, 120) : (m.file ? m.file.filename : 'message');
    await createReminder(`${m.sender.display_name}: "${snippet}"`, due, m.id);
    appAlert(`Reminder set for ${due.toLocaleString()} ✓`);
  } catch (e) { appAlert(e.message); }
}

async function addReminderFromPanel() {
  const text = $('rem-text').value.trim();
  if (!text) { $('rem-text').focus(); return; }
  const due = await pickWhen();
  if (!due) return;
  try {
    await createReminder(text, due, null);
    $('rem-text').value = '';
    await loadReminders();
  } catch (e) { appAlert(e.message); }
}

async function loadReminders() {
  const list = $('rem-list');
  const reminders = await api('/reminders');
  list.innerHTML = '';
  if (reminders.length === 0) {
    const li = document.createElement('li');
    li.className = 'sub';
    li.textContent = 'No pending reminders.';
    list.appendChild(li);
  }
  for (const r of reminders) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = r.text;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = 'due ' + new Date(r.due_at).toLocaleString();
    grow.appendChild(sub);
    li.appendChild(grow);
    const btn = document.createElement('button');
    btn.textContent = 'Cancel';
    btn.onclick = async () => {
      try { await api(`/reminders/${r.id}`, { method: 'DELETE' }); await loadReminders(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function loadNotifications() {
  const list = $('notif-list');
  const notifications = await api('/notifications');
  list.innerHTML = '';
  $('notif-clear').classList.toggle('hidden', notifications.length === 0);
  if (notifications.length === 0) {
    const li = document.createElement('li');
    li.className = 'sub';
    li.textContent = 'Nothing yet.';
    list.appendChild(li);
  }
  for (const n of notifications) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    if (!n.read_at) {
      const dot = document.createElement('span');
      dot.className = 'notif-unread';
      dot.textContent = '●';
      grow.appendChild(dot);
    }
    grow.appendChild(document.createTextNode(n.body));
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `${n.title} · ${relTime(n.created_at)}`;
    grow.appendChild(sub);
    li.appendChild(grow);
    if (n.data && n.data.channel_id) {
      li.classList.add('clickable');
      li.onclick = async () => {
        const ch = channels.find(c => c.id === n.data.channel_id);
        if (!ch) return;
        $('notifs').classList.add('hidden');
        await selectChannel(ch);
        if (n.data.root_id) openThread({ id: n.data.root_id });
      };
    }
    const dismiss = document.createElement('button');
    dismiss.className = 'notif-dismiss';
    dismiss.textContent = '✕';
    dismiss.title = 'Clear this notification';
    dismiss.onclick = async (e) => {
      e.stopPropagation();
      dismiss.disabled = true;
      try {
        await api(`/notifications/${n.id}`, { method: 'DELETE' });
        if (!n.read_at && notifUnread > 0) { notifUnread--; updateNotifBadge(); }
        li.remove();
        if (!list.querySelector('li:not(.sub)')) loadNotifications();
      } catch (err) { dismiss.disabled = false; appAlert(err.message); }
    };
    li.appendChild(dismiss);
    list.appendChild(li);
  }
}

async function clearNotifications() {
  if (!await appConfirm('Clear all notifications?')) return;
  try {
    await api('/notifications', { method: 'DELETE' });
    notifUnread = 0;
    updateNotifBadge();
    loadNotifications();
  } catch (e) { appAlert(e.message); }
}

async function openNotifs() {
  $('notifs').classList.remove('hidden');
  if (window.Notification && Notification.permission === 'default') {
    const perm = await Notification.requestPermission();
    if (perm === 'granted') setupPushSubscription();
  }
  await Promise.all([loadReminders(), loadNotifications()]);
  // Opening the inbox = "I've seen these": always mark all read and clear the
  // badge, even if our local counter already reads 0 (a push delivered while
  // backgrounded can set the app-icon badge without the page's counter knowing,
  // which otherwise left the dock number stuck until a relaunch). read-all is
  // idempotent, so calling it unconditionally is harmless.
  try { await api('/notifications/read-all', { method: 'POST' }); } catch {}
  notifUnread = 0;
  updateNotifBadge();
}

function maybeBrowserNotify(n) {
  if (document.visibilityState === 'visible') return;
  // `new Notification()` is a desktop-only fallback for a foreground-but-hidden
  // tab; iOS doesn't support it. Background/closed delivery is Web Push (the
  // service worker `push` handler), set up via setupPushSubscription().
  if (window.Notification && Notification.permission === 'granted'
      && typeof Notification === 'function') {
    try { new Notification(n.title, { body: n.body }); } catch {}
  }
}

// Web Push subscription: hand the server this device's push endpoint so it can
// deliver notifications while the PWA is backgrounded/closed. No-op unless the
// browser supports push, permission is granted, and the server has VAPID set.
let _pushSetupDone = false;
function urlBase64ToUint8Array(base64) {
  const pad = '='.repeat((4 - base64.length % 4) % 4);
  const raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
async function setupPushSubscription() {
  if (_pushSetupDone || !token) return;
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  if (!window.Notification || Notification.permission !== 'granted') return;
  try {
    const { key } = await api('/push/vapid');
    if (!key) return;  // server hasn't configured web push
    const wantKey = urlBase64ToUint8Array(key);
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    // Self-heal a stale subscription: if it was created with a different VAPID
    // key (e.g. the server's key changed), the push service rejects every push
    // to it. Detect the mismatch, drop the old one (server + browser), and make
    // a fresh subscription bound to the current key.
    if (sub) {
      const have = new Uint8Array(sub.options.applicationServerKey || new ArrayBuffer(0));
      const matches = have.length === wantKey.length && have.every((b, i) => b === wantKey[i]);
      if (!matches) {
        const o = sub.toJSON();
        try {
          await api('/push/subscribe', {
            method: 'DELETE',
            body: JSON.stringify({ endpoint: o.endpoint, keys: o.keys }),
          });
        } catch {}
        await sub.unsubscribe().catch(() => {});
        sub = null;
      }
    }
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: wantKey,
      });
    }
    const j = sub.toJSON();
    await api('/push/subscribe', {
      method: 'POST',
      body: JSON.stringify({ endpoint: j.endpoint, keys: j.keys }),
    });
    _pushSetupDone = true;
  } catch { /* push is best-effort; never break the app */ }
}

// ---------- open tasks ----------

async function openTasks() {
  $('tasks').classList.remove('hidden');
  await loadTasks();
}

async function loadTasks() {
  const list = $('task-list');
  list.innerHTML = '';
  let tasks;
  try { tasks = await api('/tasks'); }
  catch (e) { appAlert(e.message); return; }
  if (tasks.length === 0) {
    const li = document.createElement('li');
    li.className = 'sub';
    li.textContent = 'No open tasks. 🎉';
    list.appendChild(li);
    return;
  }

  // Group tasks under a heading per channel (first-seen order).
  const groups = new Map();
  for (const t of tasks) {
    if (!groups.has(t.channel_id)) {
      groups.set(t.channel_id, {
        label: t.is_dm ? 'Direct message' : '# ' + t.channel_name, items: [],
      });
    }
    groups.get(t.channel_id).items.push(t);
  }

  for (const [, group] of groups) {
    const heading = document.createElement('li');
    heading.className = 'task-group';
    heading.textContent = group.label;
    list.appendChild(heading);

    for (const t of group.items) {
      const li = document.createElement('li');
      const item = document.createElement('div');
      item.className = 'task-item';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.title = 'Mark done';
      cb.onchange = async () => {
        cb.disabled = true;
        try {
          await api(`/messages/${t.message_id}/checkbox`, {
            method: 'POST', body: JSON.stringify({ line: t.line, checked: true }),
          });
          loadTasks();  // re-render (drops empty group headings too)
          refreshTaskCount();
        } catch (e) { cb.checked = false; cb.disabled = false; appAlert(e.message); }
      };
      item.appendChild(cb);

      const col = document.createElement('div');
      col.className = 'task-text';
      col.title = 'Go to message';
      renderInline(t.text, col);
      col.onclick = () => {
        $('tasks').classList.add('hidden');
        jumpToMessage(t.channel_id, t.message_id);
      };
      item.appendChild(col);

      li.appendChild(item);
      list.appendChild(li);
    }
  }
}

// ---------- pinned messages (cross-channel) ----------

async function openPins() {
  $('pins').classList.remove('hidden');
  await loadPins();
}

async function loadPins() {
  const list = $('pins-list');
  list.innerHTML = '';
  let pins;
  try { pins = await api('/pins'); }
  catch (e) { appAlert(e.message); return; }
  if (!pins.length) {
    const li = document.createElement('li');
    li.className = 'sub';
    li.textContent = 'No pinned messages.';
    list.appendChild(li);
    return;
  }
  // Group under a heading per channel (first-seen order).
  const groups = new Map();
  for (const p of pins) {
    if (!groups.has(p.channel_id)) {
      groups.set(p.channel_id, {
        label: p.is_dm ? 'Direct message' : '# ' + p.channel_name, items: [],
      });
    }
    groups.get(p.channel_id).items.push(p);
  }
  for (const [, group] of groups) {
    const heading = document.createElement('li');
    heading.className = 'task-group';
    heading.textContent = group.label;
    list.appendChild(heading);
    for (const p of group.items) {
      const m = p.message;
      const li = document.createElement('li');
      const grow = document.createElement('div');
      grow.className = 'grow';
      grow.title = 'Go to message';
      const b = document.createElement('b');
      b.textContent = m.sender.display_name + ': ';
      grow.appendChild(b);
      grow.appendChild(document.createTextNode(
        m.content ? m.content.slice(0, 120) : (m.file ? m.file.filename : '(message)')));
      grow.onclick = () => { $('pins').classList.add('hidden'); jumpToMessage(p.channel_id, m.id); };
      li.appendChild(grow);
      const unpin = document.createElement('button');
      unpin.textContent = 'Unpin';
      unpin.onclick = async () => {
        unpin.disabled = true;
        try { await api(`/messages/${m.id}/pin`, { method: 'DELETE' }); li.remove(); }
        catch (e) { unpin.disabled = false; appAlert(e.message); }
      };
      li.appendChild(unpin);
      list.appendChild(li);
    }
  }
}

// ---------- threads inbox (cross-channel) ----------

async function openThreadsInbox() {
  $('threads-inbox').classList.remove('hidden');
  await loadThreadsInbox();
}

async function loadThreadsInbox() {
  const list = $('threads-list');
  list.innerHTML = '';
  let threads;
  try { threads = await api('/threads'); }
  catch (e) { appAlert(e.message); return; }
  if (!threads.length) {
    const li = document.createElement('li');
    li.className = 'sub';
    li.textContent = "No active threads. Reply to a message with 💬 to start one.";
    list.appendChild(li);
    return;
  }
  for (const t of threads) {
    const r = t.root;
    const li = document.createElement('li');
    li.className = 'thread-digest clickable';

    const where = document.createElement('div');
    where.className = 'thread-digest-where';
    where.textContent = (t.is_dm ? '💬 ' : '# ') + (t.is_dm ? r.sender.display_name : t.channel_name);
    li.appendChild(where);

    const snippet = document.createElement('div');
    snippet.className = 'thread-digest-snippet';
    renderInline(r.content ? r.content.slice(0, 120) : '(attachment)', snippet);
    li.appendChild(snippet);

    const sub = document.createElement('div');
    sub.className = 'thread-digest-sub';
    if (r.thread_repliers && r.thread_repliers.length) {
      const faces = document.createElement('span');
      faces.className = 'faces';
      r.thread_repliers.forEach(u => faces.appendChild(avatarEl(u)));
      sub.appendChild(faces);
    }
    const count = document.createElement('span');
    const n = r.reply_count;
    count.textContent = `${n} ${n === 1 ? 'reply' : 'replies'}`
      + (r.thread_last_at ? ' · ' + relTime(r.thread_last_at) : '');
    sub.appendChild(count);
    li.appendChild(sub);

    li.onclick = async () => {
      $('threads-inbox').classList.add('hidden');
      const ch = channels.find(c => c.id === t.channel_id);
      if (!ch) return;
      await selectChannel(ch);
      openThread({ id: r.id });
    };
    list.appendChild(li);
  }
}

// ---------- search ----------

async function runSearch(e) {
  e.preventDefault();
  const q = $('search-input').value.trim();
  if (q.length < 2) return;
  try {
    const hits = await api(`/search/messages?q=${encodeURIComponent(q)}`);
    currentChannel = null;
    typers.clear(); renderTyping();
    renderChannels();
    $('app').classList.add('chat-open');
    $('channel-title').textContent = `Search: "${q}"`;
    $('channel-topic').textContent = hits.length ? `${hits.length} result(s)` : 'no results';
    $('channel-actions').classList.add('hidden');
    setComposerEnabled(false);
    const box = $('messages');
    box.innerHTML = '';
    for (const hit of hits) {
      const div = document.createElement('div');
      div.className = 'search-hit';
      const where = document.createElement('div');
      where.className = 'where';
      where.textContent = hit.is_dm ? 'Direct message' : '# ' + hit.channel_name;
      div.appendChild(where);
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${hit.message.sender.display_name} · ${relTime(hit.message.created_at)}`;
      div.appendChild(meta);
      const body = document.createElement('div');
      body.textContent = hit.message.content;
      div.appendChild(body);
      div.onclick = () => {
        const ch = channels.find(c => c.id === hit.channel_id);
        if (ch) selectChannel(ch);
      };
      box.appendChild(div);
    }
  } catch (err) { appAlert(err.message); }
}

// ---------- typing ----------

function renderTyping() {
  const names = [...typers.values()].map(t => t.name);
  $('typing').textContent = names.length === 0 ? ''
    : names.length === 1 ? `${names[0]} is typing…`
    : `${names.join(', ')} are typing…`;
}

function onTypingEvent(data) {
  if (!currentChannel || data.channel_id !== currentChannel.id) return;
  const existing = typers.get(data.user.id);
  if (existing) clearTimeout(existing.timer);
  typers.set(data.user.id, {
    name: data.user.display_name,
    timer: setTimeout(() => { typers.delete(data.user.id); renderTyping(); }, 4000),
  });
  renderTyping();
}

function maybeSendTyping() {
  if (!currentChannel || !sock || sock.readyState !== WebSocket.OPEN) return;
  const now = Date.now();
  if (now - lastTypingSent < 2500) return;
  lastTypingSent = now;
  sock.send(JSON.stringify({ type: 'typing', channel_id: currentChannel.id }));
}

// ---------- realtime ----------

function handleEvent(data) {
  if (data.type === 'ready') {
    presence.clear();
    for (const [id, state] of Object.entries(data.presence || {})) presence.set(id, state);
    onPresenceChanged();
    // Sync this client's state to the fresh socket (server assumes active).
    myPresence = 'active';
    if (document.hidden) sendPresence('away'); else armIdle();
  } else if (data.type === 'presence') {
    if (data.state && data.state !== 'offline') presence.set(data.user_id, data.state);
    else presence.delete(data.user_id);
    onPresenceChanged();
  } else if (data.type === 'typing') {
    onTypingEvent(data);
  } else if (data.type === 'notification') {
    notifUnread++;
    updateNotifBadge();
    maybeBrowserNotify(data.notification);
    if (!$('notifs').classList.contains('hidden')) {
      loadReminders(); loadNotifications();
    }
  } else if (data.type === 'alert') {
    // Live "All"-level ping: a desktop/browser popup when you're away, but no
    // bell badge and no inbox entry — keeps the 🔔 list meaningful.
    maybeBrowserNotify(data);
  } else if (data.type === 'channels.changed') {
    memberCache.clear();  // membership may have changed
    loadChannels();
  } else if (data.type === 'channel.updated') {
    loadChannels();
  } else if (data.type === 'channel.deleted') {
    if (currentChannel && currentChannel.id === data.channel_id) clearChannelView();
    loadChannels();
  } else if (data.type === 'message.created') {
    const m = data.message;
    const ch = channelById(m.channel_id);
    if (m.thread_root_id) {
      // A thread reply: it belongs in the thread view, never the timeline.
      if (threadRootId === m.thread_root_id
          && !$('thread-pane').classList.contains('hidden')) {
        appendThreadReply(m);
        if (currentChannel && m.channel_id === currentChannel.id) markRead(currentChannel.id);
      }
      if (ch) {
        // Thread replies aren't in the channel timeline, so they don't count
        // toward the channel's message count — only unread activity.
        const current = currentChannel && m.channel_id === currentChannel.id;
        if (!current && (!me || m.sender.id !== me.id)) {
          ch.unread_count = (ch.unread_count || 0) + 1;
        }
        renderChannels();
      } else {
        loadChannels();
      }
      scheduleTaskCount();
      scheduleThreadCount();
      return;
    }
    if (currentChannel && m.channel_id === currentChannel.id) {
      typers.delete(m.sender.id); renderTyping();
      renderMessage(m);
      const box = $('messages');
      box.scrollTop = box.scrollHeight;
      if (ch) {
        ch.message_count = (ch.message_count || 0) + 1;
        ch.recent_count = (ch.recent_count || 0) + 1;
      }
      markRead(currentChannel.id);  // keep the read marker current
      renderChannels();
    } else if (ch) {
      ch.message_count = (ch.message_count || 0) + 1;
      ch.recent_count = (ch.recent_count || 0) + 1;
      if (!me || m.sender.id !== me.id) ch.unread_count = (ch.unread_count || 0) + 1;
      renderChannels();
    } else {
      loadChannels();  // a channel we don't have yet
    }
    scheduleTaskCount();
  } else if (data.type === 'message.updated') {
    const id = data.message.id;
    if (currentChannel && data.message.channel_id === currentChannel.id) {
      const el = document.querySelector(`#messages .msg[data-id="${id}"]`);
      if (el) {
        const next = el.nextSibling;
        el.remove();
        renderMessage(data.message);
        const fresh = document.querySelector(`#messages .msg[data-id="${id}"]`);
        if (next && fresh) $('messages').insertBefore(fresh, next);
      }
    }
    const tp = document.querySelector(`#thread-content .msg[data-id="${id}"]`);
    if (tp) {
      const fresh = buildMessageNode(data.message, { inThread: true });
      if (tp.classList.contains('thread-root-msg')) fresh.classList.add('thread-root-msg');
      tp.replaceWith(fresh);
    }
    scheduleTaskCount();
  } else if (data.type === 'message.deleted') {
    document.querySelectorAll(`.msg[data-id="${data.message.id}"]`).forEach(el => el.remove());
    const c = $('thread-count');
    if (c) {
      const n = document.querySelectorAll('#thread-content .msg').length - 1;
      if (n >= 0) c.textContent = `${n} ${n === 1 ? 'reply' : 'replies'}`;
    }
    const ch = channelById(data.message.channel_id);
    // Only top-level (non-thread-reply) messages count toward the channel total.
    if (ch && !data.message.thread_root_id) {
      if (ch.message_count) ch.message_count--;
      if (ch.recent_count) ch.recent_count--;
      renderChannels();
    }
    scheduleTaskCount();
    scheduleThreadCount();
  } else if (data.type === 'message.reacted') {
    document.querySelectorAll(`.msg[data-id="${data.message_id}"] .reactions`)
      .forEach(row => renderReactions(row, data.message_id, data.reactions));
  } else if (data.type === 'message.preview') {
    document.querySelectorAll(`.msg[data-id="${data.message_id}"]`).forEach(node => {
      const main = node.querySelector('.msg-main');
      if (!main || main.querySelector('.link-cards')) return;
      const cards = linkCardsEl(data.previews);
      const rrow = main.querySelector('.reactions');
      if (rrow) main.insertBefore(cards, rrow); else main.appendChild(cards);
    });
  } else if (data.type === 'thread.updated') {
    updateThreadSummary(data.root_id, data);
    if (currentChannel && data.channel_id === currentChannel.id) scheduleInfoPane();
  } else if (data.type === 'pins.changed') {
    if (currentChannel && data.channel_id === currentChannel.id) scheduleInfoPane();
  }
}

// ---------- presence (away on idle / backgrounded) ----------
// Re-render presence dots wherever they appear. DM dots are rebuilt by
// renderChannels; member-list and footer dots carry data-uid and update here.
function onPresenceChanged() {
  renderChannels();
  document.querySelectorAll('.dot[data-uid]').forEach(d => {
    d.className = 'dot ' + presenceOf(d.dataset.uid);
  });
}
let myPresence = 'active';
let idleTimer = null;
const IDLE_MS = 5 * 60 * 1000;
function sendPresence(state) {
  if (state === myPresence) return;
  myPresence = state;
  if (sock && sock.readyState === WebSocket.OPEN) {
    sock.send(JSON.stringify({ type: 'presence', state }));
  }
}
function armIdle() {
  sendPresence('active');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => sendPresence('away'), IDLE_MS);
}
['mousemove', 'keydown', 'touchstart', 'pointerdown'].forEach(
  e => window.addEventListener(e, armIdle, { passive: true })
);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) sendPresence('away'); else armIdle();
});

function connectWs() {
  if (sock) sock.close();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  sock = new WebSocket(`${proto}//${location.host}/api/v1/ws`);
  sock.onopen = () => sock.send(JSON.stringify({ token }));
  sock.onmessage = ev => handleEvent(JSON.parse(ev.data));
  sock.onclose = () => { if (token) setTimeout(connectWs, 3000); };
}

// ---------- settings ----------

function applyCompact(on) {
  document.body.classList.toggle('compact', on);
  localStorage.setItem('compact', on ? '1' : '0');
}

// Theme: 'system' (follow OS), 'light', or 'dark'. The effective light/dark is
// set on <html data-theme>; CSS overrides the palette vars for light.
function effectiveTheme(pref) {
  if (pref === 'light' || pref === 'dark') return pref;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function applyTheme(pref) {
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

async function openAccount() {
  $('account').classList.remove('hidden');
  $('profile-display').value = me.display_name;
  $('profile-status').value = me.status || '';
  $('badge-channels-toggle').checked = me.badge_channel_messages !== false;
  $('compact-toggle').checked = localStorage.getItem('compact') === '1';
  api('/calendar/url').then(r => { $('calendar-url').value = r.url; }).catch(() => {});
  loadPasswordSection();
  await Promise.all([loadPasskeys(), loadSessions()]);
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

async function savePassword() {
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

async function removePassword() {
  if (!await appConfirm('Remove your password? You\'ll sign in with passkeys only.',
      { okText: 'Remove', danger: true })) return;
  try {
    await api('/auth/password', { method: 'DELETE' });
    await loadPasswordSection();
  } catch (e) { appAlert(e.message); }
}

async function copyCalendarUrl() {
  const url = $('calendar-url').value;
  if (!url) return;
  try { await navigator.clipboard.writeText(url); appAlert('Calendar link copied.'); }
  catch { $('calendar-url').select(); document.execCommand('copy'); }
}

async function regenerateCalendarUrl() {
  if (!await appConfirm('Regenerate the calendar link? The old link stops working and you\'ll need to re-subscribe.',
      { okText: 'Regenerate', danger: true })) return;
  try {
    const r = await api('/calendar/regenerate', { method: 'POST' });
    $('calendar-url').value = r.url;
    appAlert('New calendar link created. Re-subscribe with it in your calendar app.');
  } catch (e) { appAlert(e.message); }
}

async function openSettings() {
  $('settings').classList.remove('hidden');
  await Promise.all([loadAdminSpaces(), loadAdminUsers(), loadAdminInvites(), loadAdminChannels()]);
}

async function saveProfile() {
  const display_name = $('profile-display').value.trim();
  if (!display_name) return;
  const status = $('profile-status').value.trim();  // "" clears it server-side
  try {
    me = { ...me, ...(await api('/users/me', { method: 'PATCH', body: JSON.stringify({ display_name, status }) })) };
    renderMeHeader();
  } catch (e) { appAlert(e.message); }
}

async function uploadAvatar() {
  const file = $('avatar-input').files[0];
  $('avatar-input').value = '';
  if (!file) return;
  try {
    const fd = new FormData();
    fd.append('upload', file);
    me = { ...me, ...(await api('/users/me/avatar', { method: 'POST', body: fd })) };
    renderMeHeader();
    appAlert('Photo updated ✓ (it appears on your new messages right away)');
  } catch (e) { appAlert(e.message); }
}

async function loadPasskeys() {
  const list = $('passkey-list');
  const keys = await api('/auth/passkeys');
  list.innerHTML = '';
  for (const k of keys) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = k.label || 'Unnamed passkey';
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `added ${relTime(k.created_at)}`
      + (k.last_used_at ? ` · last used ${relTime(k.last_used_at)}` : '');
    grow.appendChild(sub);
    li.appendChild(grow);
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

async function addPasskey() {
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

// ---------- admin panel ----------

async function loadAdminUsers() {
  const list = $('admin-user-list');
  const users = await api('/admin/users');
  list.innerHTML = '';
  for (const u of users) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = u.display_name;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = '@' + u.username
      + (u.is_admin ? ' · admin' : '') + (u.disabled ? ' · DISABLED' : '')
      + (u.has_password ? ' · password' : '')
      + (u.pending ? ' · not claimed' : '');
    grow.appendChild(sub);
    li.appendChild(grow);
    if (u.id !== me.id) {
      const adminBtn = document.createElement('button');
      adminBtn.textContent = u.is_admin ? 'Demote' : 'Make admin';
      adminBtn.onclick = async () => {
        try {
          await api(`/admin/users/${u.id}`, {
            method: 'PATCH', body: JSON.stringify({ is_admin: !u.is_admin }),
          });
          await loadAdminUsers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(adminBtn);
      const disableBtn = document.createElement('button');
      disableBtn.textContent = u.disabled ? 'Enable' : 'Disable';
      disableBtn.onclick = async () => {
        if (!u.disabled && !await appConfirm(`Disable ${u.display_name}? They are signed out everywhere immediately.`)) return;
        try {
          await api(`/admin/users/${u.id}`, {
            method: 'PATCH', body: JSON.stringify({ disabled: !u.disabled }),
          });
          await loadAdminUsers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(disableBtn);

      const delBtn = document.createElement('button');
      delBtn.textContent = 'Delete';
      delBtn.onclick = async () => {
        const typed = await appPrompt(
          `Permanently delete @${u.username}. Their messages, files, and passkeys are removed; channels they created are kept under your name. This cannot be undone.\n\nType the username to confirm:`,
          { okText: 'Delete', danger: true, placeholder: u.username });
        if (typed === null) return;
        if (typed.trim() !== u.username) { appAlert('Username did not match — nothing was deleted.'); return; }
        try {
          await api(`/admin/users/${u.id}`, { method: 'DELETE' });
          await loadAdminUsers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(delBtn);
    }
    list.appendChild(li);
  }
}

async function loadAdminInvites() {
  const list = $('admin-invite-list');
  const invites = (await api('/admin/invites')).filter(i => !i.used_by);
  list.innerHTML = '';
  if (invites.length === 0) {
    const li = document.createElement('li');
    li.textContent = 'No pending invites.';
    li.className = 'sub';
    list.appendChild(li);
  }
  for (const inv of invites) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    const expired = new Date(inv.expires_at) < new Date();
    grow.textContent = (inv.recover_user_id ? 'Recovery invite' : 'Invite')
      + (expired ? ' (expired)' : '');
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `created ${relTime(inv.created_at)} · expires ${relTime(inv.expires_at)}`;
    grow.appendChild(sub);
    li.appendChild(grow);
    const btn = document.createElement('button');
    btn.textContent = 'Revoke';
    btn.onclick = async () => {
      try { await api(`/admin/invites/${inv.id}`, { method: 'DELETE' }); await loadAdminInvites(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function showInvite(inv) {
  const out = $('invite-out');
  out.innerHTML = '';
  out.appendChild(document.createTextNode(
    (inv.recovery_for ? `Recovery code for ${inv.recovery_for}` : 'Code') + ' (shown once): '));
  const code = document.createElement('code');
  code.textContent = inv.code;
  out.appendChild(code);
}

async function newInvite() {
  try {
    showInvite(await api('/admin/invites', { method: 'POST', body: JSON.stringify({}) }));
  } catch (e) { appAlert(e.message); }
}

async function newRecoveryInvite() {
  const username = await appPrompt('Username of the account to recover:', { okText: 'Create code' });
  if (!username) return;
  try {
    showInvite(await api('/admin/invites', {
      method: 'POST', body: JSON.stringify({ recover_username: username.trim() }),
    }));
  } catch (e) { appAlert(e.message); }
}

async function newUser() {
  const username = await appPrompt('Username for the new user:',
    { placeholder: 'e.g. janedoe', okText: 'Next' });
  if (!username) return;
  const display = await appPrompt('Display name:',
    { value: username.trim(), okText: 'Next' });
  if (display === null) return;
  const password = await appPrompt(
    'Optional starter password (min 8 chars) so they can sign in immediately. Leave blank for passkey-only.',
    { okText: 'Create account' });
  if (password === null) return;
  if (password && password.length < 8) { appAlert('Password must be at least 8 characters (or leave blank).'); return; }
  try {
    const body = { username: username.trim(), display_name: display.trim() || username.trim() };
    if (password) body.password = password;
    const res = await api('/admin/users', { method: 'POST', body: JSON.stringify(body) });
    const out = $('new-user-out');
    out.innerHTML = '';
    let msg = `Created @${res.username}. Add them to spaces/channels below. `;
    msg += password
      ? 'They can sign in now with their username + the password you set. Claim code (for adding a passkey later): '
      : 'Give them this claim code (shown once) to enroll their passkey: ';
    out.appendChild(document.createTextNode(msg));
    const code = document.createElement('code');
    code.textContent = res.claim_code;
    out.appendChild(code);
    await loadAdminUsers();
  } catch (e) { appAlert(e.message); }
}

// ---------- actions ----------

async function newChannel(spaceId) {
  const name = await appPrompt('Channel name (joins it if it already exists):', { okText: 'Create' });
  if (!name) return;
  try {
    try {
      await api('/channels', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), space_id: spaceId }),
      });
    } catch (e) {
      const open = (await api(`/channels/browse?space_id=${spaceId}`))
        .find(c => c.name === name.trim());
      if (!open) throw e;
      await api(`/channels/${open.id}/join`, { method: 'POST' });
    }
    await loadChannels();
  } catch (e) { appAlert(e.message); }
}

// ---------- spaces ----------

async function openSpaceManage(sp) {
  manageSpaceId = sp.id;
  const canManage = sp.my_role === 'manager' || me.is_admin;
  $('space-title').textContent = 'Manage ' + sp.name;
  $('space-name').value = sp.name;
  $('space-name').disabled = !canManage;
  $('space-save').classList.toggle('hidden', !canManage);
  $('space').classList.remove('hidden');
  await loadSpaceMembers(sp);
}

async function loadSpaceMembers(sp) {
  const list = $('space-member-list');
  const canManage = sp.my_role === 'manager' || me.is_admin;
  $('space-add-input').parentElement.classList.toggle('hidden', !canManage);
  const members = await api(`/spaces/${manageSpaceId}/members`);
  list.innerHTML = '';
  for (const m of members) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = m.display_name;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = '@' + m.username + (m.role === 'manager' ? ' · manager' : '');
    grow.appendChild(sub);
    li.appendChild(grow);
    if (canManage && m.id !== me.id) {
      const roleBtn = document.createElement('button');
      roleBtn.textContent = m.role === 'manager' ? 'Make member' : 'Make manager';
      roleBtn.onclick = async () => {
        try {
          await api(`/spaces/${manageSpaceId}/members/${m.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ role: m.role === 'manager' ? 'member' : 'manager' }),
          });
          await loadSpaceMembers(sp);
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(roleBtn);
      if (!sp.is_default) {
        const rm = document.createElement('button');
        rm.textContent = 'Remove';
        rm.onclick = async () => {
          if (!await appConfirm(`Remove ${m.display_name} from ${sp.name}?`)) return;
          try {
            await api(`/spaces/${manageSpaceId}/members/${m.id}`, { method: 'DELETE' });
            await loadSpaceMembers(sp);
          } catch (e) { appAlert(e.message); }
        };
        li.appendChild(rm);
      }
    }
    list.appendChild(li);
  }
}

async function addSpaceMember() {
  const username = $('space-add-input').value.trim().toLowerCase();
  if (!username || !manageSpaceId) return;
  try {
    const user = (await api('/users')).find(u => u.username === username);
    if (!user) throw new Error('No such user');
    await api(`/spaces/${manageSpaceId}/members`, {
      method: 'POST', body: JSON.stringify({ user_id: user.id }),
    });
    $('space-add-input').value = '';
    const sp = spaces.find(s => s.id === manageSpaceId) || { id: manageSpaceId };
    await loadSpaceMembers(sp);
  } catch (e) { appAlert(e.message); }
}

async function saveSpaceName() {
  const name = $('space-name').value.trim();
  if (!name || !manageSpaceId) return;
  try {
    await api(`/spaces/${manageSpaceId}`, { method: 'PATCH', body: JSON.stringify({ name }) });
    $('space').classList.add('hidden');
    await loadChannels();
    // If we came from Settings → Spaces, refresh that list too.
    if (!$('settings').classList.contains('hidden')) await loadAdminSpaces();
  } catch (e) { appAlert(e.message); }
}

async function reorderSpace(ids, i, dir) {
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  const arr = ids.slice();
  [arr[i], arr[j]] = [arr[j], arr[i]];
  try {
    await api('/spaces/order', { method: 'PUT', body: JSON.stringify({ order: arr }) });
    await Promise.all([loadAdminSpaces(), loadChannels()]);
  } catch (e) { appAlert(e.message); }
}

async function loadAdminSpaces() {
  const list = $('admin-space-list');
  const all = await api('/spaces');
  const ids = all.map(s => s.id);
  list.innerHTML = '';
  all.forEach((sp, idx) => {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = sp.name + (sp.is_default ? ' (default)' : '');
    li.appendChild(grow);
    const up = document.createElement('button');
    up.textContent = '↑'; up.title = 'Move up'; up.disabled = idx === 0;
    up.onclick = () => reorderSpace(ids, idx, -1);
    li.appendChild(up);
    const down = document.createElement('button');
    down.textContent = '↓'; down.title = 'Move down'; down.disabled = idx === all.length - 1;
    down.onclick = () => reorderSpace(ids, idx, +1);
    li.appendChild(down);
    const manage = document.createElement('button');
    manage.textContent = 'Manage';
    manage.title = 'Rename this space or add members';
    manage.onclick = () => openSpaceManage(sp);
    li.appendChild(manage);
    if (!sp.is_default) {
      const del = document.createElement('button');
      del.textContent = 'Delete';
      del.onclick = async () => {
        const typed = await appPrompt(
          `Permanently delete the space "${sp.name}" and all its channels, messages, and files.\n\nType the space name to confirm:`,
          { okText: 'Delete', danger: true, placeholder: sp.name });
        if (typed === null) return;
        if (typed.trim() !== sp.name) { appAlert('Name did not match — nothing was deleted.'); return; }
        try {
          await api(`/spaces/${sp.id}`, { method: 'DELETE' });
          await Promise.all([loadAdminSpaces(), loadChannels()]);
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(del);
    }
    list.appendChild(li);
  });
}

async function newSpace() {
  const name = $('new-space-name').value.trim();
  if (!name) return;
  try {
    await api('/spaces', { method: 'POST', body: JSON.stringify({ name }) });
    $('new-space-name').value = '';
    await Promise.all([loadAdminSpaces(), loadChannels()]);
  } catch (e) { appAlert(e.message); }
}

async function newDm() {
  const input = await appPrompt('Username(s) to message — comma-separate for a group:',
    { placeholder: 'alice  or  alice, bob', okText: 'Open DM' });
  if (!input) return;
  try {
    const wanted = input.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    if (!wanted.length) return;
    const all = await api('/users');
    const ids = [];
    for (const name of wanted) {
      const user = all.find(u => u.username === name);
      if (!user) throw new Error(`No such user: ${name}`);
      ids.push(user.id);
    }
    const ch = await api('/dms', { method: 'POST', body: JSON.stringify({ user_ids: ids }) });
    await loadChannels();
    selectChannel(channels.find(c => c.id === ch.id) || ch);
  } catch (e) { appAlert(e.message); }
}

// ---------- app ----------

function renderMeHeader() {
  const span = $('me-name');
  span.innerHTML = '';
  span.appendChild(avatarEl(me));
  span.appendChild(document.createTextNode(me.display_name));
  span.title = me.status || 'Your account';
}

async function showApp() {
  $('auth').classList.add('hidden');
  $('app').classList.remove('hidden');
  renderMeHeader();
  $('settings-btn').classList.toggle('hidden', !me.is_admin);
  await loadChannels();
  connectWs();
  try {
    notifUnread = (await api('/notifications')).filter(n => !n.read_at).length;
    updateNotifBadge();
  } catch {}
  refreshTaskCount();
  refreshThreadCount();
  setupPushSubscription();  // if permission was already granted on a prior visit
  maybeOpenDeepLink();      // a push tapped while the app was closed
}

// On cold launch from a tapped notification the URL carries ?channel=<id>
// (&root=<id>): open it, then strip the params so reloads land on the app.
function maybeOpenDeepLink() {
  const p = new URLSearchParams(location.search);
  const cid = p.get('channel');
  if (!cid) return;
  const rid = p.get('root') || undefined;
  history.replaceState({}, '', location.pathname);
  openChannelFromLink(cid, rid);
}

// ---------- wiring ----------

$('show-register').onclick = e => { e.preventDefault(); authError('');
  $('login-view').classList.add('hidden'); $('register-view').classList.remove('hidden'); };
$('show-login').onclick = e => { e.preventDefault(); authError('');
  $('register-view').classList.add('hidden'); $('login-view').classList.remove('hidden'); };
$('login-btn').onclick = login;
$('register-btn').onclick = register;
$('login-password-btn').onclick = loginWithPassword;
$('register-password-btn').onclick = registerWithPassword;
$('login-password').addEventListener('keydown', e => { if (e.key === 'Enter') loginWithPassword(); });
$('reg-password').addEventListener('keydown', e => { if (e.key === 'Enter') registerWithPassword(); });
$('logout-btn').onclick = signOut;
$('send-form').onsubmit = sendMessage;
$('send-input').oninput = () => {
  maybeSendTyping();
  updateMention();
  saveDraft();
  // Restore the placeholder when the box is visually empty (browsers can
  // leave a stray <br> behind that defeats the :empty CSS selector).
  const box = $('send-input');
  if (box.innerText.trim() === '' && !box.querySelector('code,b,strong,i,em')) {
    box.innerHTML = '';
  }
};
$('send-input').addEventListener('blur', () => setTimeout(closeMention, 150));
// Desktop: Enter sends, Shift+Enter inserts a newline.
// Touch keyboards: Enter inserts a newline; the Send button submits.
const touchKeyboard = window.matchMedia('(pointer: coarse)').matches;
$('send-input').addEventListener('keydown', e => {
  // Mention dropdown captures navigation keys first.
  if (mentionState) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      mentionIndex = (mentionIndex + 1) % mentionItems.length;
      renderMentionBox(); return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      mentionIndex = (mentionIndex - 1 + mentionItems.length) % mentionItems.length;
      renderMentionBox(); return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault(); chooseMention(mentionIndex); return;
    }
    if (e.key === 'Escape') { e.preventDefault(); closeMention(); return; }
  }
  if (e.key === 'Enter' && !e.shiftKey && !touchKeyboard) {
    e.preventDefault();
    $('send-form').requestSubmit();
    return;
  }
  if (e.key === 'Enter' && (e.shiftKey || touchKeyboard)) {
    // Continue lists: a new line after "- item" or "[ ] task" starts
    // pre-prefixed; an empty list line ends the list instead.
    const line = currentLineText();
    let prefix = null;
    let num;
    if (/^\[( |x)\] /i.test(line)) prefix = '[ ] ';
    else if (/^- /.test(line)) prefix = '- ';
    else if ((num = line.match(/^(\d+)\. /))) prefix = (parseInt(num[1], 10) + 1) + '. ';
    if (prefix) {
      e.preventDefault();
      // An empty list item (just the marker) ends the list instead of continuing.
      const empty = /^(\[( |x)\]|-|\d+\.)\s*$/i.test(line.trim());
      if (empty) {
        document.execCommand('insertText', false, '\n');
      } else {
        document.execCommand('insertText', false, '\n' + prefix);
      }
      return;
    }
  }
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey) {
    const key = e.key.toLowerCase();
    if (key === 'b') { e.preventDefault(); execFormat('bold'); }
    else if (key === 'i') { e.preventDefault(); execFormat('italic'); }
  }
});
// Paste as plain text — formatting comes only from our own toolbar.
$('send-input').addEventListener('paste', e => {
  e.preventDefault();
  const text = (e.clipboardData || window.clipboardData).getData('text/plain');
  document.execCommand('insertText', false, text);
});
document.querySelectorAll('#toolbar button').forEach(b => {
  // mousedown would steal focus and collapse the selection before the
  // command runs — suppress it so formatting applies to the selected text.
  b.onmousedown = e => e.preventDefault();
  b.onclick = () => {
    if (b.dataset.insert) insertPrefix(b.dataset.insert);
    else execFormat(b.dataset.cmd || 'code');
  };
});
document.addEventListener('selectionchange', updateToolbar);
$('dialog-ok').onclick = () => resolveDialog(true);
$('dialog-cancel').onclick = () => resolveDialog(false);
$('dialog').onclick = e => { if (e.target === $('dialog')) resolveDialog(false); };
$('dialog-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); resolveDialog(true); }
});
$('dialog-textarea').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); resolveDialog(true); }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('dialog').classList.contains('hidden')) resolveDialog(false);
});
$('edit-cancel').onclick = exitEditMode;
$('reply-cancel').onclick = cancelReply;
$('search-form').onsubmit = runSearch;
$('new-dm').onclick = newDm;
$('new-invite').onclick = newInvite;
$('new-recovery').onclick = newRecoveryInvite;
$('new-user-btn').onclick = newUser;
$('new-space-btn').onclick = newSpace;
$('space-save').onclick = saveSpaceName;
$('space-add-btn').onclick = addSpaceMember;
$('space-close').onclick = () => $('space').classList.add('hidden');
$('space').onclick = e => { if (e.target === $('space')) $('space').classList.add('hidden'); };
$('me-name').onclick = openAccount;
$('account-close').onclick = () => $('account').classList.add('hidden');
$('account').onclick = e => { if (e.target === $('account')) $('account').classList.add('hidden'); };
$('settings-btn').onclick = openSettings;
$('settings-close').onclick = () => $('settings').classList.add('hidden');
$('settings').onclick = e => { if (e.target === $('settings')) $('settings').classList.add('hidden'); };
$('notifs-btn').onclick = openNotifs;
$('notifs-close').onclick = () => $('notifs').classList.add('hidden');
$('notif-clear').onclick = clearNotifications;
$('notifs').onclick = e => { if (e.target === $('notifs')) $('notifs').classList.add('hidden'); };
$('tasks-btn').onclick = openTasks;
$('tasks-close').onclick = () => $('tasks').classList.add('hidden');
$('threads-btn').onclick = openThreadsInbox;
$('threads-close').onclick = () => $('threads-inbox').classList.add('hidden');
$('pins-btn').onclick = openPins;
$('pins-close').onclick = () => $('pins').classList.add('hidden');
$('tasks').onclick = e => { if (e.target === $('tasks')) $('tasks').classList.add('hidden'); };
$('threads-inbox').onclick = e => { if (e.target === $('threads-inbox')) $('threads-inbox').classList.add('hidden'); };
$('pins').onclick = e => { if (e.target === $('pins')) $('pins').classList.add('hidden'); };

// Switcher row in each list-pane header: hop straight to another list without
// closing first. Hide all four panes, then open the target (its opener loads it).
const PANE_OPENERS = {
  'threads-inbox': openThreadsInbox,
  'pins': openPins,
  'tasks': openTasks,
  'notifs': openNotifs,
};
function closeAllListPanes() {
  for (const id of ['notifs', 'tasks', 'threads-inbox', 'pins']) $(id).classList.add('hidden');
}
document.querySelectorAll('.pane-switch').forEach(nav => {
  nav.addEventListener('click', e => {
    const btn = e.target.closest('[data-pane]');
    if (!btn) return;
    const open = PANE_OPENERS[btn.dataset.pane];
    if (!open) return;
    closeAllListPanes();
    open();
  });
});
$('rem-add').onclick = addReminderFromPanel;
$('rem-text').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); addReminderFromPanel(); }
});
document.querySelectorAll('#when .quick-grid button').forEach(b => {
  b.onclick = () => {
    if (b.dataset.mins) return closeWhen(new Date(Date.now() + +b.dataset.mins * 60000));
    const d = new Date();
    if (b.dataset.special === 'evening') {
      d.setHours(18, 0, 0, 0);
      if (d <= new Date()) d.setDate(d.getDate() + 1);
    } else {
      d.setDate(d.getDate() + 1);
      d.setHours(9, 0, 0, 0);
    }
    closeWhen(d);
  };
});
// Separate hour + minute dropdowns (reliable everywhere; datetime-local's
// step is ignored by Safari). Minutes are limited to quarter hours.
(function buildTimeOptions() {
  const hourSel = $('when-hour');
  for (let h = 8; h <= 18; h++) {  // 8 AM through 6 PM
    const ap = h < 12 ? 'AM' : 'PM';
    const h12 = h % 12 === 0 ? 12 : h % 12;
    const opt = document.createElement('option');
    opt.value = String(h);
    opt.textContent = `${h12} ${ap}`;
    hourSel.appendChild(opt);
  }
  const minSel = $('when-min');
  for (const m of [0, 15, 30, 45]) {
    const opt = document.createElement('option');
    opt.value = String(m);
    opt.textContent = ':' + String(m).padStart(2, '0');
    minSel.appendChild(opt);
  }
})();

function submitCustomWhen() {
  const date = $('when-date').value;
  if (!date) return;
  const h = String(+$('when-hour').value).padStart(2, '0');
  const m = String(+$('when-min').value).padStart(2, '0');
  const d = new Date(`${date}T${h}:${m}`);
  if (isNaN(d) || d <= new Date()) { appAlert('Pick a time in the future'); return; }
  closeWhen(d);
}
$('when-set').onclick = submitCustomWhen;
// Picking the minute (the last step) commits it — no separate Save click.
$('when-min').onchange = submitCustomWhen;
$('when-cancel').onclick = () => closeWhen(null);
$('when').onclick = e => { if (e.target === $('when')) closeWhen(null); };
$('viewer-close').onclick = () => { closePreview(); maybeReopenInfoPane(); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closePreview();
});
// Tap-opened emoji strips close when tapping anywhere else.
document.addEventListener('click', () => {
  document.querySelectorAll('.react-zone.open').forEach(z => z.classList.remove('open'));
});
$('back-btn').onclick = () => $('app').classList.remove('chat-open');
$('info-btn').onclick = toggleInfoPane;
$('info-close').onclick = closeInfoPane;
$('webhook-add').onclick = createWebhook;
$('thread-close').onclick = () => { closeThread(); maybeReopenInfoPane(); };
$('thread-form').onsubmit = sendThreadReply;
$('thread-input').addEventListener('input', autoGrowThread);
$('thread-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !touchKeyboard) {
    e.preventDefault();
    $('thread-form').requestSubmit();
  }
});
$('members-btn').onclick = openMembers;
$('members-close').onclick = () => $('members').classList.add('hidden');
$('members').onclick = e => { if (e.target === $('members')) $('members').classList.add('hidden'); };
$('member-add-btn').onclick = addMember;
$('edit-channel-btn').onclick = editChannel;
$('profile-save').onclick = saveProfile;
$('compact-toggle').onchange = e => applyCompact(e.target.checked);
$('theme-seg').addEventListener('click', e => {
  const b = e.target.closest('[data-theme-pref]');
  if (b) applyTheme(b.dataset.themePref);
});
$('badge-channels-toggle').onchange = async e => {
  const on = e.target.checked;
  try {
    me = await api('/users/me', {
      method: 'PATCH',
      body: JSON.stringify({ badge_channel_messages: on }),
    });
  } catch (err) { e.target.checked = !on; appAlert(err.message); }
};
$('push-test-btn').onclick = async () => {
  const out = $('push-test-result');
  $('push-test-btn').disabled = true;
  out.textContent = 'Making sure this device is registered…';
  try {
    _pushSetupDone = false;          // force a fresh (re)subscribe first
    await setupPushSubscription();
    out.textContent = 'Sending test…';
    const r = await api('/push/test', { method: 'POST' });
    if (!r.enabled) { out.textContent = 'Web push is not configured on the server.'; return; }
    if (!r.count) { out.textContent = 'No push subscriptions are registered for your account. (Notification permission may be blocked, or registration failed.)'; return; }
    const ok = r.results.filter(x => x.ok).length;
    const detail = r.results.map(x =>
      `${x.host}: ${x.ok ? 'delivered ✓' : 'FAILED ' + (x.status ?? '') + ' ' + (x.error || '')}`).join('  ·  ');
    out.textContent = `Sent to ${ok}/${r.count} device(s). ${detail}`;
  } catch (e) {
    out.textContent = 'Error: ' + e.message;
  } finally {
    $('push-test-btn').disabled = false;
  }
};
$('badge-test-btn').onclick = async () => {
  const out = $('push-test-result');
  if (!navigator.setAppBadge) {
    out.textContent = "This browser doesn't support app-icon badges.";
    return;
  }
  try {
    await navigator.setAppBadge(7);
    out.textContent = 'Set the icon badge to 7 — look at the Colloqui dock/taskbar icon now. '
      + '(It clears when you open the 🔔 list or read messages.)';
  } catch (e) {
    out.textContent = 'Badge not available here: ' + ((e && e.message) || e);
  }
};
$('calendar-copy').onclick = copyCalendarUrl;
$('calendar-regen').onclick = regenerateCalendarUrl;
$('avatar-btn').onclick = () => $('avatar-input').click();
$('avatar-input').onchange = uploadAvatar;
$('messages').onscroll = () => {
  const box = $('messages');
  stickBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  maybeLoadOlder();
};
$('add-passkey').onclick = addPasskey;
$('pw-save').onclick = savePassword;
$('pw-remove').onclick = removePassword;
$('attach-btn').onclick = () => $('file-input').click();
$('file-input').onchange = uploadAttachment;

applyCompact(localStorage.getItem('compact') === '1');
applyTheme(localStorage.getItem('theme') || 'system');

(async () => {
  if (token) {
    try { me = await api('/auth/me'); await showApp(); return; } catch {}
  }
  $('auth').classList.remove('hidden');
})();
