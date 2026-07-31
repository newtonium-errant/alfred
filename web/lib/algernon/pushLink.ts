// Same-origin deep-link guard for push payloads + notification clicks. Mirrors
// safeNextPath's rule: accept ONLY a same-origin ABSOLUTE path, and reject the
// protocol-relative (`//host`) and backslash (`/\host`) forms that a browser
// resolves OFF-origin — a bare `startsWith('/')` would let those through.
// Anything else falls back to /feed.
//
// This logic is DUPLICATED verbatim into public/sw.js (which is served as a
// static file and cannot import a module). pushLink.test.ts extracts the sw.js
// copy and runs it against the same cases, so the two can never drift.
export function sanitizeDeepLink(url: unknown): string {
  return typeof url === 'string' && url.startsWith('/') && url[1] !== '/' && url[1] !== '\\'
    ? url
    : '/feed';
}
