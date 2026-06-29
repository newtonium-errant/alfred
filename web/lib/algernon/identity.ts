import type { NextApiRequest } from 'next';

// SERVER-ONLY. Resolves the driving user for a BFF request — the single identity
// seam, so FE-3 (auth) localises its swap here.
//
// FE-2 (now): the asserted identity is the configured dev user
// (`ALFRED_WEB_DEV_USER`). The BFF relays it as `X-Web-User`; the BACKEND
// validates it against `web.users` (returns 403 unknown_user if absent) — the
// frontend holds NO allowlist (single source of truth = backend), per the
// team-lead's decision.
//
// FE-3 will read a verified, instance-signed session cookie FIRST here, falling
// back to the dev user for local dev. When backend Sub-arc B lands, the cookie
// will carry the backend-signed token and the relay header becomes
// `X-Alfred-Session` — that swap is contained to this file + transport.ts.

export interface WebIdentity {
  /** The named user the request is acting as. Validated server-side downstream. */
  user: string;
}

export function resolveIdentity(_req: NextApiRequest): WebIdentity | null {
  // FE-3 seam: read + verify the session cookie from `_req` here first.
  const devUser = (process.env.ALFRED_WEB_DEV_USER || '').trim();
  if (!devUser) return null;
  return { user: devUser };
}
