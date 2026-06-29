import type { NextApiRequest } from 'next';
import type { AuthVerifyResponse, SessionUser } from './types';

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

/** Read the display-only {name, role} from the identity cookie (null if absent/bad). */
export function readDisplayIdentity(req: NextApiRequest): SessionUser | null {
  const raw = req.cookies?.[IDENTITY_COOKIE];
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<SessionUser>;
    if (parsed && typeof parsed.name === 'string' && typeof parsed.role === 'string') {
      return { name: parsed.name, role: parsed.role };
    }
  } catch {
    /* malformed cookie → treat as no identity */
  }
  return null;
}

// --- Cookie writing (server-only) ------------------------------------------
// Accepts both NextApiResponse and getServerSideProps' ServerResponse (both have
// setHeader) via a structural type, so callback.tsx's GSSP and the API routes
// share one implementation.
type ResLike = { setHeader(name: string, value: string | string[]): unknown };

interface CookieOpts {
  maxAge?: number;
  httpOnly?: boolean;
  sameSite?: 'Lax' | 'Strict' | 'None';
  secure?: boolean;
  path?: string;
}

function serializeCookie(name: string, value: string, opts: CookieOpts): string {
  // encodeURIComponent is REQUIRED: the identity cookie's JSON contains ',' and
  // ':' which are cookie delimiters; Next's req.cookies decodes on read so this
  // round-trips. (The token is base64url but encoded too, for safety.)
  const parts = [`${name}=${encodeURIComponent(value)}`];
  parts.push(`Path=${opts.path ?? '/'}`);
  if (opts.maxAge !== undefined) parts.push(`Max-Age=${Math.max(0, Math.floor(opts.maxAge))}`);
  if (opts.httpOnly) parts.push('HttpOnly');
  if (opts.sameSite) parts.push(`SameSite=${opts.sameSite}`);
  if (opts.secure) parts.push('Secure');
  return parts.join('; ');
}

/**
 * Set the httpOnly session cookies after a successful /auth/verify. The session
 * token (authz) and the display identity are separate cookies, both httpOnly —
 * the token is never JS-readable, and the display name/role are never used for
 * authorization (the token is the sole authority).
 */
export function setSessionCookies(res: ResLike, verify: AuthVerifyResponse): void {
  const secure = process.env.NODE_ENV === 'production';
  const maxAge =
    typeof verify.exp === 'number'
      ? verify.exp - Math.floor(Date.now() / 1000)
      : undefined;
  const opts: CookieOpts = { path: '/', httpOnly: true, sameSite: 'Lax', secure, maxAge };
  res.setHeader('Set-Cookie', [
    serializeCookie(SESSION_COOKIE, verify.session_token, opts),
    serializeCookie(
      IDENTITY_COOKIE,
      JSON.stringify({ name: verify.name, role: verify.role }),
      opts,
    ),
  ]);
}

/** Clear both session cookies (sign-out). */
export function clearSessionCookies(res: ResLike): void {
  const secure = process.env.NODE_ENV === 'production';
  const opts: CookieOpts = { path: '/', httpOnly: true, sameSite: 'Lax', secure, maxAge: 0 };
  res.setHeader('Set-Cookie', [
    serializeCookie(SESSION_COOKIE, '', opts),
    serializeCookie(IDENTITY_COOKIE, '', opts),
  ]);
}
