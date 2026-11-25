// Entry point: connect the page's controls to the modules that implement them,
// then start the app. This file holds wiring only. Behaviour lives in js/.
import {
  addSpaceMember,
  createApiKey,
  newInvite,
  newRecoveryInvite,
  newSpace,
  newUser,
  openSettings,
  saveSpaceName,
} from './js/admin.js';
import { api } from './js/api.js';
import {
  authError,
  login,
  loginWithPassword,
  register,
  registerWithPassword,
  signOut,
} from './js/auth.js';
import { addMember, editChannel, maybeLoadOlder, newDm, openMembers } from './js/channels.js';
import { exitEditMode, wireComposer } from './js/composer.js';
import { appAlert, resolveDialog } from './js/dialogs.js';
import { $, touchKeyboard } from './js/dom.js';
import { closePreview, toggleViewerExpand } from './js/files.js';
import { initDialogSemantics, initNavIcons } from './js/icons.js';
import {
  closeInfoPane,
  createWebhook,
  maybeReopenInfoPane,
  toggleInfoPane,
} from './js/infopane.js';
import { sendMessage, uploadAttachment } from './js/messages.js';
import {
  clearNotifications,
  openNotifs,
  resetPushSetup,
  setupPushSubscription,
} from './js/notifications.js';
import {
  closeAllListPanes,
  openPins,
  openTasks,
  openThreadsInbox,
  PANE_OPENERS,
  runSearch,
} from './js/panes.js';
import { addReminderFromPanel, closeWhen, submitCustomWhen } from './js/reminders.js';
import {
  addPasskey,
  applyCompact,
  applyTheme,
  confirmTotpEnroll,
  copyCalendarUrl,
  disableTotp,
  loadTotpSection,
  openAccount,
  regenerateCalendarUrl,
  removePassword,
  savePassword,
  saveProfile,
  startTotpEnroll,
  uploadAvatar,
} from './js/settings.js';
import { showApp } from './js/shell.js';
import { state } from './js/state.js';
import { autoGrowThread, closeThread, sendThreadReply } from './js/threads.js';

import './js/update.js';     // service worker + auto-update (runs on import)
import './js/viewport.js';   // keeps the layout height right on iOS (runs on import)

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
wireComposer();
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
$('search-form').onsubmit = runSearch;
$('new-dm').onclick = newDm;
$('new-invite').onclick = newInvite;
$('new-recovery').onclick = newRecoveryInvite;
$('new-user-btn').onclick = newUser;
$('new-space-btn').onclick = newSpace;
$('apikey-create').onclick = createApiKey;
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
$('when-set').onclick = submitCustomWhen;
// Picking the minute (the last step) commits it — no separate Save click.
$('when-min').onchange = submitCustomWhen;
$('when-cancel').onclick = () => closeWhen(null);
$('when').onclick = e => { if (e.target === $('when')) closeWhen(null); };
$('viewer-close').onclick = () => { closePreview(); maybeReopenInfoPane(); };
$('viewer-expand').onclick = toggleViewerExpand;
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const pane = $('preview-pane');
  if (pane.classList.contains('hidden')) return;
  // Escape backs out one level: full window -> side pane -> closed.
  if (pane.classList.contains('expanded')) toggleViewerExpand();
  else { closePreview(); maybeReopenInfoPane(); }
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
    state.me = await api('/users/me', {
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
    resetPushSetup();                // force a fresh (re)subscribe first
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
  state.stickBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  maybeLoadOlder();
};
$('add-passkey').onclick = addPasskey;
$('pw-save').onclick = savePassword;
$('pw-remove').onclick = removePassword;
$('totp-enable').onclick = startTotpEnroll;
$('totp-confirm').onclick = confirmTotpEnroll;
$('totp-cancel').onclick = () => loadTotpSection();
$('totp-disable').onclick = disableTotp;
$('totp-recovery-done').onclick = () => loadTotpSection();
$('attach-btn').onclick = () => $('file-input').click();
$('file-input').onchange = uploadAttachment;

applyCompact(localStorage.getItem('compact') === '1');
applyTheme(localStorage.getItem('theme') || 'system');

// ---- start ----

(async () => {
  if (state.token) {
    try { state.me = await api('/auth/me'); await showApp(); return; } catch {}
  }
  $('auth').classList.remove('hidden');
})();
initNavIcons();
initDialogSemantics();
