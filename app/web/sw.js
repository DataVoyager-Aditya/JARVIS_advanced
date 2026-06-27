// JARVIS service worker — offline shell, live API. Build: 6c80e431ff
const CACHE = 'jarvis-6c80e431ff';
const ASSETS = [
  '/manifest.webmanifest',
  '/static/dc-runtime.js',
  '/static/vendor/react.production.min.js',
  '/static/vendor/react-dom.production.min.js',
  '/static/vendor/babel.min.js',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png'
];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // Never touch live API/voice traffic.
  if (url.pathname === '/chat' || url.pathname === '/ticker' || url.pathname.startsWith('/memory') || url.pathname.startsWith('/voice') || url.pathname.startsWith('/admin')) return;
  // App shell (navigations + '/') -> network-first so rebuilds show up immediately.
  if (e.request.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(fetch(e.request).catch(() => caches.match('/') || caches.match(e.request)));
    return;
  }
  // Immutable assets -> cache-first, populate on miss.
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      if (res.ok && url.pathname.startsWith('/static')) {
        const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return res;
    }))
  );
});
