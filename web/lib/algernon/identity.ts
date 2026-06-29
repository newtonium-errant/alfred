import type { NextApiRequest } from 'next';

// SERVER-ONLY. Session-identity helpers for the BFF.
//
// B3 (live) auth contract: /chat/* identity is the verified, instance-signed
// session token, relayed to the transport as `X-Alfred-Session`. The token is
// minted by the backend's POST /auth/verify and stored by the BFF in an
// httpOnly cookie (set in FE-3's /auth/callback). The BFF holds NO allowlist and
// never decodes the token — the transport verifies the signature and derives the
// named user from it. The frontend can neither fabricate nor read it.

// httpOnly cookie carrying the instance-signed session token → X-Alfred-Session.
export const SESSION_COOKIE = 'algernon_session';
// httpOnly cookie carrying display-only {name, role} (NEVER used for authz — the
// token is the sole authority; this is just what the header shows). Set in FE-3.
export const IDENTITY_COOKIE = 'algernon_identity';

/**
 * The session token for a /chat/* request, or null (→ BFF 401 invalid_session).
 *
 * Primary source is the httpOnly session cookie (set after login). For local dev
 * BEFORE the login UI exists, `ALFRED_WEB_DEV_SESSION_TOKEN` is a fallback so
 * /chat can be exercised against the real backend using a token obtained via the
 * backend's dev handshake (no Resend email round-trip).
 */
export function resolveSessionToken(req: NextApiRequest): string | null {
  const fromCookie = req.cookies?.[SESSION_COOKIE];
  if (fromCookie && fromCookie.trim()) return fromCookie;
  const dev = (process.env.ALFRED_WEB_DEV_SESSION_TOKEN || '').trim();
  return dev || null;
}
