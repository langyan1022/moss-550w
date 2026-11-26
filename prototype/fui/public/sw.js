const MOSS_CACHE = 'moss-fui-v7-shell-v2';
const PRECACHE_URLS = [
  '/mobile',
  '/vendor/fui.css',
  '/styles/console.css',
  '/styles/fonts.css',
  '/vendor/logos/550w.svg',
];

const IMMUTABLE_EXT_RE = /\.(?:glb|wasm|tflite|task|svg|woff2?|png|webp)$/i;
const SHELL_EXT_RE = /\.(?:css|js|mjs|html)$/i;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(MOSS_CACHE).then((cache) => cache.addAll(PRECACHE_URLS).catch(() => {})).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== MOSS_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/') || url.pathname.startsWith('/drafts/')) return;
  if (url.pathname === '/sw.js' || url.pathname === '/manifest.webmanifest') return;

  // Heavy 3D binaries, WASM, models, SVGs, and fonts: Cache-First + background revalidation
  if (IMMUTABLE_EXT_RE.test(url.pathname)) {
    event.respondWith(
      caches.open(MOSS_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        const net = fetch(req).then((resp) => {
          if (resp && resp.ok && resp.status === 200) {
            void cache.put(req, resp.clone());
          }
          return resp;
        }).catch(() => hit);
        return hit || net;
      }),
    );
    return;
  }

  // App shell HTML/JS/CSS (preserving fresh updates on upgrade with offline cache fallback)
  if (url.pathname.startsWith('/conversation-assets/') || SHELL_EXT_RE.test(url.pathname) || url.pathname === '/mobile') {
    event.respondWith(
      caches.open(MOSS_CACHE).then(async (cache) => {
        try {
          const resp = await fetch(req);
          if (resp && resp.ok && resp.status === 200) {
            void cache.put(req, resp.clone());
          }
          return resp;
        } catch {
          const hit = await cache.match(req);
          if (hit) return hit;
          throw new Error('Offline and not cached');
        }
      }),
    );
  }
});

self.addEventListener('push', (event) => {
  let payload = { title: 'MOSS 550W', body: '有一条新的状态提醒', url: '/mobile', kind: 'pulse' };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch {
      payload.body = event.data.text() || payload.body;
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title || 'MOSS 550W', {
      body: payload.body || '',
      icon: '/vendor/logos/550w.svg',
      badge: '/vendor/logos/550w.svg',
      tag: payload.dedup_key || `moss-${payload.kind || 'pulse'}`,
      renotify: true,
      data: { url: payload.url || '/mobile', alert_id: payload.alert_id || null },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || '/mobile';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
      return null;
    }),
  );
});
