// Network-first service worker. Its whole job is to defeat iOS standalone's
// aggressive page cache: every launch fetches the latest app shell from the
// network, falling back to cache only when offline. API/websocket traffic is
// passed straight through (never cached) so live data is always fresh.

const CACHE = 'colloqui-shell-v1';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Web Push: show the notification the server sent (works when the PWA is
// backgrounded or closed — the only way iOS delivers notifications at all).
self.addEventListener('push', (event) => {
  let p = {};
  try { p = event.data ? event.data.json() : {}; } catch (e) {}
  const data = p.data || {};
  event.waitUntil((async () => {
    // Always keep the app-icon badge in sync (server sends the unread total).
    if (self.navigator && self.navigator.setAppBadge && typeof p.badge === 'number') {
      try { await self.navigator.setAppBadge(p.badge); } catch (e) {}
    }
    // Don't pop a banner if an app window is FOCUSED (frontmost) — you're
    // already looking at it. We check focused, not merely visible, so a desktop
    // PWA window open in the background (while you work in another app) still
    // gets banners. This also stops the iOS double-notification: after you tap a
    // banner to open the app, iOS re-delivers the same push to the now-active
    // (and focused) service worker, which would otherwise show it again.
    // A test notification always shows (so you can verify display even with the
    // app focused); real ones are suppressed only when a window is focused.
    if (!data.test) {
      const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      if (wins.some(w => w.focused)) return;
    }
    await self.registration.showNotification(p.title || 'Colloqui', {
      body: p.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data,
      tag: data.channel_id || undefined,  // coalesce per-channel
    });
  })());
});

// Tapping a notification jumps to its channel: focus an existing window and
// tell it which channel to open, or open the app pointed at that channel.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const d = event.notification.data || {};
  const cid = d.channel_id;
  const rid = d.root_id || d.thread_root_id;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if ('focus' in w) {
        await w.focus();
        if (cid) w.postMessage({ type: 'open-channel', channelId: cid, rootId: rid });
        return;
      }
    }
    if (self.clients.openWindow) {
      let url = '/';
      if (cid) {
        url = '/?channel=' + encodeURIComponent(cid);
        if (rid) url += '&root=' + encodeURIComponent(rid);
      }
      return self.clients.openWindow(url);
    }
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Only handle GETs for our own app shell; let everything else (API writes,
  // file fetches, websockets) use the browser's default handling.
  if (req.method !== 'GET' || url.origin !== self.location.origin
      || url.pathname.startsWith('/api/')) {
    return;
  }
  event.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req))
  );
});
