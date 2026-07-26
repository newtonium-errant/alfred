import { getJson, postJson } from './http';
import type { SessionUser } from './types';

// BROWSER-side auth client. Talks ONLY to the same-origin BFF (`/api/auth/*`).
// The BFF relays to the transport's /auth/* and owns the httpOnly session cookie;
// the browser never sees the session token. Errors surface as `ApiError`.

export const authApi = {
  // POST /auth/login {email, next?} → uniform { status:"sent" } (no account
  // enumeration). `next` is relayed only when present so the no-deep-link path
  // sends the same body it always did.
  login: (email: string, next?: string): Promise<{ status: string }> =>
    postJson<{ status: string }>('/api/auth/login', { email, ...(next ? { next } : {}) }),

  // POST /auth/otp/request {email} → uniform { ok:true } (no enumeration).
  // Parity #23 (iOS PWA re-auth): request an emailed 6-digit code. Throws
  // ApiError(404 otp_disabled) when the instance hasn't enabled OTP —
  // the form degrades gracefully on that code.
  otpRequest: (email: string): Promise<{ ok: boolean }> =>
    postJson<{ ok: boolean }>('/api/auth/otp/request', { email }),

  // POST /auth/otp/verify {email, code} → { ok:true } with the session cookie
  // set SERVER-SIDE on this same-origin response (the cookie-jar fix — the
  // browser never sees the session token). Throws ApiError(401
  // invalid_or_expired) on any rejection (uniform — no oracle).
  otpVerify: (email: string, code: string): Promise<{ ok: boolean }> =>
    postJson<{ ok: boolean }>('/api/auth/otp/verify', { email, code }),

  // Clears the session cookie server-side.
  logout: (): Promise<{ status: string }> =>
    postJson<{ status: string }>('/api/auth/logout', {}),

  // The current signed-in user (display only), or throws ApiError(401) if none.
  me: (): Promise<SessionUser> => getJson<SessionUser>('/api/auth/me'),
};
