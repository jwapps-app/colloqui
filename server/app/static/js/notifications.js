import { api } from './api.js';
import { selectChannel } from './channels.js';
import { appAlert, appConfirm } from './dialogs.js';
import { $, emptyRow } from './dom.js';
import { relTime } from './format.js';
import { loadReminders } from './reminders.js';
import { state } from './state.js';
import { openThread } from './threads.js';

// Dismiss delivered push notifications from the OS shade (all, or one channel's
// by tag). Without this they linger and keep an app-icon dot on Android even
// after the messages have been read in-app.
async function clearDeliveredNotifs(channelId, clearBadge) {
  if (!('serviceWorker' in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    if (reg.active) {
      reg.active.postMessage({ type: 'clear-notifications', channelId, clearBadge: !!clearBadge });
    }
  } catch {}
}

// Opening a channel clears its 🔔 notifications and drops the unread/app badge
// by however many were cleared (so tapping a push, or just reading the channel,
// brings the count down).
export async function clearChannelNotifs(channelId) {
  clearDeliveredNotifs(channelId);  // close that channel's shade notifications
  try {
    const r = await api(`/notifications/read-channel/${channelId}`, { method: 'POST' });
    if (r && r.cleared > 0) {
      state.notifUnread = Math.max(0, state.notifUnread - r.cleared);
      updateNotifBadge();
      if (!$('notifs').classList.contains('hidden')) loadNotifications();
    }
  } catch {}
}

export function updateNotifBadge() {
  const badge = $('notif-badge');
  badge.classList.toggle('hidden', state.notifUnread === 0);
  badge.textContent = state.notifUnread > 99 ? '99+' : state.notifUnread;
  $('notifs-btn').classList.toggle('active', state.notifUnread > 0);
  // Mirror onto the installed-app icon badge (iOS 16.4+ / desktop PWAs). The
  // service worker sets it from pushes while closed; this keeps it in sync
  // while open, so reading notifications clears the icon number. setAppBadge(0)
  // is the reliable clear (Safari's clearAppBadge() often doesn't take); call
  // clearAppBadge too as a belt-and-suspenders.
  if (navigator.setAppBadge) {
    navigator.setAppBadge(state.notifUnread).catch(() => {});
    if (state.notifUnread === 0 && navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  }
}

export async function loadNotifications() {
  const list = $('notif-list');
  const notifications = await api('/notifications');
  list.innerHTML = '';
  $('notif-clear').classList.toggle('hidden', notifications.length === 0);
  if (notifications.length === 0) {
    list.appendChild(emptyRow('Nothing yet.'));
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
        const ch = state.channels.find(c => c.id === n.data.channel_id);
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
        if (!n.read_at && state.notifUnread > 0) { state.notifUnread--; updateNotifBadge(); }
        li.remove();
        if (!list.querySelector('li:not(.sub)')) loadNotifications();
      } catch (err) { dismiss.disabled = false; appAlert(err.message); }
    };
    li.appendChild(dismiss);
    list.appendChild(li);
  }
}

export async function clearNotifications() {
  if (!await appConfirm('Clear all notifications?')) return;
  try {
    await api('/notifications', { method: 'DELETE' });
    state.notifUnread = 0;
    updateNotifBadge();
    loadNotifications();
  } catch (e) { appAlert(e.message); }
}

export async function openNotifs() {
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
  state.notifUnread = 0;
  updateNotifBadge();
  clearDeliveredNotifs(null, true);  // wipe the shade + app-icon badge
}

// Returning to the app clears the OS notification shade (you're looking at it
// now), so Android's app-icon dot doesn't linger after you've opened the app.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) clearDeliveredNotifs();
});

export function maybeBrowserNotify(n) {
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
// The next setupPushSubscription() call (re)registers this device from scratch.
export function resetPushSetup() { _pushSetupDone = false; }
function urlBase64ToUint8Array(base64) {
  const pad = '='.repeat((4 - base64.length % 4) % 4);
  const raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
export async function setupPushSubscription() {
  if (_pushSetupDone || !state.token) return;
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
