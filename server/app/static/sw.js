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
