/* Algernon PWA service worker — hand-rolled, no build-time manifest, no deps.
 *
 * Goal: make the app installable + give it an offline shell, WITHOUT ever caching
 * live or session-scoped data. The hard rule is below in the fetch handler:
 * requests to /api/* (incl. the SSE relay /api/chat/stream) and /auth/* (the
 * magic-link callback that sets session cookies) are NEVER intercepted or cached —
 * they always hit the network untouched so auth/session/chat/SSE keep working.
 *
 * Caching strategy:
 *   - /_next/static/* and static icons/manifest → cache-first (immutable, hashed).
 *   - navigations (HTML) + other same-origin GETs → network-first, falling back to
 *     cache, then to the cached "/" shell, so the SPA still boots offline.
 *
 * Updates: bump CACHE_VERSION on any shell-shape change. install→skipWaiting and
 * activate→clients.claim roll the new worker out to open tabs immediately, and
 * activate prunes every older algernon-shell-* cache.
 */

// Bump this to invalidate the whole shell cache and force a clean roll-out.
const CACHE_VERSION = 'v1';
const CACHE_NAME = `algernon-shell-${CACHE_VERSION}`;

// SPA shell routes — cached at install so the app boots offline after first visit.
const SHELL_ROUTES = ['/', '/login', '/ingest'];

// Static, immutable assets served from /public.
const STATIC_ASSETS = [
  '/manifest.webmanifest',
  '/icon.svg',
  '/icon-192.png',
  '/icon-512.png',
  '/icon-maskable-512.png',
  '/apple-touch-icon.png',
  '/favicon.ico',
  '/favicon-32.png',
  '/favicon-16.png',
];

const PRECACHE_URLS = [...SHELL_ROUTES, ...STATIC_ASSETS];

// Precache the shell. Individual puts via allSettled so one failure (e.g. a route
// that redirects) never aborts the whole install.
self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      await Promise.allSettled(
        PRECACHE_URLS.map(async (url) => {
          try {
            const res = await fetch(url, { credentials: 'same-origin' });
            if (res && res.ok && res.type === 'basic' && !res.redirected) {
              await cache.put(url, res.clone());
            }
          } catch {
            /* offline at install or route unavailable — runtime cache fills it later. */
          }
        }),
      );
      // Take over without waiting for existing tabs to close.
      await self.skipWaiting();
    })(),
  );
});

// Drop stale shell caches, then claim open clients so the new worker controls them.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n.startsWith('algernon-shell-') && n !== CACHE_NAME)
          .map((n) => caches.delete(n)),
      );
      await self.clients.claim();
    })(),
  );
});

// Let the page trigger an immediate activation after an update is found.
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;
  const res = await fetch(request);
  if (res && res.ok && res.type === 'basic' && !res.redirected) {
    cache.put(request, res.clone());
  }
  return res;
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const res = await fetch(request);
    if (res && res.ok && res.type === 'basic' && !res.redirected) {
      cache.put(request, res.clone());
    }
    return res;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    // Offline navigation with no exact match → fall back to the cached app shell.
    if (request.mode === 'navigate') {
      const shell = await cache.match('/');
      if (shell) return shell;
    }
    throw err;
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Only GET is cacheable. POST/PUT/etc. (incl. the SSE relay POST to
  // /api/chat/stream) fall straight through to the network.
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Only same-origin. Anything cross-origin is left entirely to the browser.
  if (url.origin !== self.location.origin) return;

  // HARD REQUIREMENT — never intercept or cache live/session-scoped endpoints.
  // /api/* (incl. /api/chat/stream SSE) and /auth/* (magic-link cookie set) must
  // always hit the network untouched. Returning without respondWith = no SW
  // involvement = default network fetch, nothing cached.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) {
    return;
  }

  // Never cache the worker script itself (would block its own updates).
  if (url.pathname === '/sw.js') return;

  // Immutable hashed build assets + our static icons/manifest → cache-first.
  if (url.pathname.startsWith('/_next/static/') || STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // Navigations and other same-origin GETs → network-first with offline fallback.
  event.respondWith(networkFirst(request));
});
