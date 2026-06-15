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
  const jobs = [
    self.registration.showNotification(p.title || 'Colloqui', {
      body: p.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data,
      tag: data.channel_id || undefined,  // coalesce per-channel
    }),
  ];
  // App-icon badge count (the server sends the unread total as p.badge).
  if (self.navigator && self.navigator.setAppBadge && typeof p.badge === 'number') {
    jobs.push(self.navigator.setAppBadge(p.badge).catch(() => {}));
  }
  event.waitUntil(Promise.all(jobs));
});

// Tapping a notification focuses an existing window, or opens the app.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if ('focus' in w) return w.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow('/');
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
