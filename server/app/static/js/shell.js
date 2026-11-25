import { api } from './api.js';
import { loadChannels, openChannelFromLink } from './channels.js';
import { $ } from './dom.js';
import { loadMeta } from './meta.js';
import { setupPushSubscription, updateNotifBadge } from './notifications.js';
import { refreshTaskCount, refreshThreadCount } from './panes.js';
import { connectWs } from './realtime.js';
import { renderRecurrenceOptions } from './reminders.js';
import { renderMeHeader } from './settings.js';
import { state } from './state.js';

export async function showApp() {
  $('auth').classList.add('hidden');
  $('app').classList.remove('hidden');
  renderMeHeader();
  $('settings-btn').classList.toggle('hidden', !state.me.is_admin);
  // Fire the independent startup fetches together instead of serially, so the
  // channel list and badges paint sooner. connectWs runs immediately so live
  // events start flowing while the initial data loads.
  connectWs();
  const loadNotifCount = (async () => {
    try {
      state.notifUnread = (await api('/notifications')).filter(n => !n.read_at).length;
      updateNotifBadge();
    } catch {}
  })();
  await Promise.all([
    loadMeta().then(renderRecurrenceOptions),
    loadChannels(), loadNotifCount, refreshTaskCount(), refreshThreadCount(),
  ]);
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
