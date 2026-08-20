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
// v2: added /share to the shell (Web Share Target handler).
// v3: Phase B FE — sensor-log + console identities changed the shell shape.
// v4: the feed adopted Layout's surface prop — the surface attribute and the
//     definitions/painting split moved onto the shell root.
// v5: Phase C — home's top module became the Duty/Rhythm/Fuel day board. `/` is
//     a SHELL_ROUTE, so its precached HTML (the offline boot, and the fallback
//     for any unmatched navigation) is exactly what changed shape here.
// v6: the console-completion registers. Home, chat and the two utility rooms
//     took surface registers, so Layout renders the console hull as their
//     chrome — the SHELL's own shape, on three of the four SHELL_ROUTES (`/`,
//     `/login`, `/ingest`). The trigger here is the RENDER change, not an edit
//     to this file: precached HTML carrying the old warm chrome would boot the
//     app into a shell the running code no longer paints.
// v7: the bright-slab sweep. TWO triggers, both MEASURED by building the tree
//     at the merge base and at this commit and diffing the prerendered HTML
//     with build ids and asset hashes normalised:
//       * `/login` is a SHELL_ROUTE and its precached markup CHANGED — the
//         sign-in card took the `ui-panel` marker, so a v6 shell serves a card
//         the crt register can no longer reach.
//       * every route's stylesheet URL changed, and that is the bigger one.
//         The marker rules this lane added live ENTIRELY in that stylesheet,
//         `/_next/static/*` is cache-first here, and precached HTML names the
//         file by hash — so a v6 client would pair old HTML with the old CSS
//         and render precisely the bright slabs this lane exists to remove.
//     WHAT DID NOT CHANGE, checked rather than assumed: the markup of `/`,
//     `/ingest` and `/share` is byte-identical after normalisation, as are the
//     404/500 controls. Every authed route prerenders its pre-auth branch, so
//     the restyled components are not in the prerendered output at all. The
//     bump is owed by the two facts above, not by the whole shell moving.
// v8: the composer deletion. ONE trigger, and it is the stylesheet URL again —
//     no markup moved this time. Deleting `components/chat/Composer.tsx` took
//     with it the Tailwind classes only that component used, so the generated
//     stylesheet shrank (29556 → 29379 bytes, `npx tailwindcss -c
//     tailwind.config.cjs -i styles/globals.css --minify`) and its content hash
//     changed with it. All FOUR SHELL_ROUTES name that file by hash in their
//     precached HTML, so a v7 client pairs its cached shells with a stylesheet
//     URL this build no longer serves.
//     THE NOISE FLOOR WAS ESTABLISHED FIRST, because without it this diff
//     proves nothing: the base tree was built TWICE and the two outputs
//     compared. Across identical source the per-page chunk hashes DO move
//     (`chunks/pages/index-*.js`) while the stylesheet hash does NOT. So the
//     chunk churn carries no signal and the CSS hash carries all of it.
//     WHAT DID NOT CHANGE, checked rather than assumed: the prerendered <body>
//     of all four SHELL_ROUTES is byte-identical, and so is `/chat`'s — which
//     is prerendered but is NOT a SHELL_ROUTE, and prerenders its pre-auth
//     branch, where no composer of either kind has ever appeared.
// v9: the deck rotation's components. ONE trigger, the stylesheet URL again —
//     the sort-rotation lane's new deck components (HoldSelector and the
//     suggestion-card face work) brought new Tailwind utility classes, so the
//     generated stylesheet GREW (29379 → 29403 bytes, same instrument as v8:
//     `npx tailwindcss -c tailwind.config.cjs -i styles/globals.css
//     --minify`; content hash 89abe9c5 → 62f9ad9c) and every SHELL_ROUTE's
//     precached HTML names that file by hash — a v8 client would pair its
//     cached shells with a stylesheet URL this build no longer serves.
//     METHOD (noise-floor, the v8 discipline): master at 2f9a23b9 rebuilt to
//     the standing baseline hash EXACTLY (29379 / 89abe9c5, stable through
//     trains 14-17), so the whole delta is this lane's; the lane figure was
//     reproduced independently on the lane tree (29403 / 62f9ad9c, md5
//     8-char prefix).
//     WHAT DID NOT CHANGE, checked rather than assumed: no SHELL_ROUTE's
//     markup — the lane's page edits (`/` among them) all sit behind the
//     pre-auth early-return (verified at pages/index.tsx:178: the deck-pill
//     composition feeds only the post-auth branch), so the prerendered
//     pre-auth HTML the shell precaches carries none of the new components.
// v10: the capture toggle (R1). ONE trigger, and for the first time it is a
//     JS CHUNK, not the stylesheet: the lane's edits to shared lib modules
//     (`lib/algernon/schemas.ts` + `client.ts`, which the ingest/share
//     bundles also import) moved shared chunk 847's content hash
//     (b1cb2233… → 708d9f00…), and `/ingest` + `/share` — BOTH precached
//     SHELL_ROUTES — name that chunk by hash in their prerendered HTML. A
//     v9 client would pair its cached shells with a chunk URL this build no
//     longer serves; `/_next/static/*` is cache-first here.
//     METHOD (the v8 noise-floor discipline, plus two instrument artifacts
//     named): the SAME worktree was built at baseline content (C3 stashed)
//     and at C3, same absolute path, and the prerendered shell-route asset
//     refs diffed. Artifact 1: `_buildManifest`/`_ssgManifest` carry a
//     random per-build id — excluded (an identical-source rebuild pair
//     proved chunk refs otherwise stable). Artifact 2: a CROSS-DIRECTORY
//     build comparison moves EVERY chunk hash (path-dependent hashing —
//     the lane-vs-main-repo diff was pure noise and was discarded).
//     WHAT DID NOT CHANGE, checked rather than assumed: `/` and `/login`
//     asset refs are identical; the global stylesheet is byte-identical
//     (tailwind CLI over both trees, same hash 8168ae01…) — the new UI uses
//     only already-emitted utilities; and `/chat` (not a SHELL_ROUTE)
//     prerenders its pre-auth branch, which carries none of the capture
//     components.
const CACHE_VERSION = 'v10';
const CACHE_NAME = `algernon-shell-${CACHE_VERSION}`;

// SPA shell routes — cached at install so the app boots offline after first visit.
// `/share` is the manifest's share_target action: the system share sheet
// navigates here with the payload in the query string, so the shell must be
// available promptly (and the navigation is network-first with a cache fallback,
// which keeps a shared capture reaching the form on a flaky connection).
const SHELL_ROUTES = ['/', '/login', '/ingest', '/share'];

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

// --- Web Push (B4) ----------------------------------------------------------
// Same-origin deep-link guard — mirrors safeNextPath: only an ABSOLUTE path, and
// NOT the protocol-relative (`//host`) / backslash (`/\host`) forms a browser
// resolves OFF-origin (a bare startsWith('/') lets those through). DUPLICATED
// from lib/algernon/pushLink.ts (a service worker is a static file and cannot
// import a module); pushLink.test.ts extracts + runs THIS copy for parity.
function sanitizeDeepLink(url) {
  return typeof url === 'string' && url.startsWith('/') && url[1] !== '/' && url[1] !== '\\'
    ? url
    : '/feed';
}

// Show a notification for a server push. The payload is {title, kind, url} ONLY
// (lock-screen privacy — never evidence content; the server guarantees this).
// Defensive: a malformed/empty push still shows a minimal notification rather
// than throwing, and `url` is sanitised to a same-origin absolute path (an
// off-origin or protocol-relative value can never be opened on click).
self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_e) {
    data = {};
  }
  const title = (data && typeof data.title === 'string' && data.title) || 'Algernon';
  const kind = (data && typeof data.kind === 'string' && data.kind) || '';
  const url = sanitizeDeepLink(data && data.url);
  // A server-supplied body wins; without one the worker composes its own from
  // the kind, exactly as before. The digest needs this because "digest · needs
  // you" says less than nothing.
  const body =
    (data && typeof data.body === 'string' && data.body) || (kind ? `${kind} · needs you` : 'needs you');
  // COLLAPSE KEY. The fallback is the deep link, which is what this always used
  // — and that was the bug: every needs-you push resolves to `/deck`, so each
  // one silently REPLACED the last in the tray. The sender now names the tag, so
  // a digest rolls (one live summary) while an item that must ring alone carries
  // its own id and cannot be overwritten by the next digest.
  const tag = (data && typeof data.tag === 'string' && data.tag) || url;
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      data: { url },
      icon: '/icon-192.png',
      badge: '/icon-192.png',
    }),
  );
});

// Click → focus an existing same-origin tab (routing it to the deep link) or open
// a new one. Only ever navigates to the relative path stored on the notification.
// Extract a trial push_id from a deep link (`/feed?push=trial-d1-w2`). Returns
// '' for anything that isn't one, so a normal doorbell click posts no receipt.
// Charset-fenced to match the receipt route's validator — a value that route
// would reject is not worth a request.
function trialPushId(url) {
  if (typeof url !== 'string') return '';
  const q = url.indexOf('?');
  if (q < 0) return '';
  const params = new URLSearchParams(url.slice(q + 1));
  const id = params.get('push') || '';
  return /^[A-Za-z0-9_-]{1,64}$/.test(id) ? id : '';
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = sanitizeDeepLink(data.url);
  // DELIVERY-TRIAL RECEIPT. A tap is the only evidence that a push actually
  // reached the phone; posted from here rather than from page code because the
  // tap is the event being measured, and a page-load signal would miss a tap
  // that focused an already-open tab. Same-origin, so the session + identity
  // cookies ride along. Fire-and-forget: the receipt must NEVER be able to cost
  // the operator the navigation he asked for.
  const pushId = trialPushId(target);
  if (pushId) {
    event.waitUntil(
      fetch('/api/push/receipt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ push_id: pushId }),
      }).catch(() => undefined),
    );
  }
  event.waitUntil(
    (async () => {
      const clientsArr = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of clientsArr) {
        if ('focus' in client) {
          try {
            await client.focus();
            if ('navigate' in client) await client.navigate(target);
            return;
          } catch (_e) {
            /* fall through to opening a fresh window */
          }
        }
      }
      if (self.clients.openWindow) await self.clients.openWindow(target);
    })(),
  );
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
