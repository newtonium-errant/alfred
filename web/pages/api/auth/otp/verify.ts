import type { NextApiRequest, NextApiResponse } from 'next';
import { setSessionCookies } from '../../../../lib/algernon/identity';
import { otpVerifyBodySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';
import { logAuthOutcome } from '../../../../lib/algernon/authLog';
import type { AuthVerifyResponse } from '../../../../lib/algernon/types';

// POST /api/auth/otp/verify {email, code} → relays to transport POST
// /auth/otp/verify. On success the session token is STRIPPED from the JSON and
// stored in the httpOnly session cookie SERVER-SIDE via the SAME
// setSessionCookies helper the magic-link callback uses — THIS is the iOS PWA
// cookie-jar fix (parity #23): the Set-Cookie rides this same-origin response
// to the already-open PWA, so the session lands in the PWA's own jar (a magic
// link tapped from Mail would set it in Safari's separate jar). The browser JS
// NEVER sees the token — the response body is just {ok:true}.
// Every backend rejection (bad code / expired / unknown email / exhausted)
// arrives as the uniform 401 invalid_or_expired and is relayed as
// {ok:false, error:'invalid_or_expired'}; a malformed body gets the same
// uniform shape at the edge (no oracle). 404 = otp_disabled (feature off).
//
// Every path logs its outcome (see authLog.ts): this route is NON-IDEMPOTENT —
// an upstream 200 has already burned the one-time code — so when a sign-in goes
// wrong the BFF's own account of what it returned is the only way to tell a lost
// response apart from a genuine rejection. It carried none until now.
export const OTP_VERIFY_ROUTE = 'auth/otp/verify';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    logAuthOutcome(OTP_VERIFY_ROUTE, { outcome: 'method_not_allowed' });
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const parsed = otpVerifyBodySchema.safeParse(req.body);
  if (!parsed.success) {
    logAuthOutcome(OTP_VERIFY_ROUTE, { outcome: 'bad_request' });
    return res.status(401).json({ ok: false, error: 'invalid_or_expired' });
  }

  try {
    const { status, body } = await callTransport('POST', '/auth/otp/verify', {
      body: { email: parsed.data.email, code: parsed.data.code },
    });
    const v = (body ?? {}) as Partial<AuthVerifyResponse>;
    if (status === 200 && typeof v.session_token === 'string' && v.session_token) {
      // SERVER-SIDE cookie set (the cookie-jar fix) — token never in the JSON.
      setSessionCookies(res, {
        session_token: v.session_token,
        name: typeof v.name === 'string' ? v.name : '',
        role: typeof v.role === 'string' ? v.role : 'owner',
        exp: typeof v.exp === 'number' ? v.exp : undefined,
      });
      logAuthOutcome(OTP_VERIFY_ROUTE, {
        outcome: 'ok',
        upstream: status,
        email: parsed.data.email,
        extra: { cookie_set: true },
      });
      return res.status(200).json({ ok: true });
    }
    if (status === 200) {
      // Upstream ACCEPTED the code — which means it is already burned and the
      // operator can never reuse it — but the body carried no usable session
      // token, so we cannot mint the cookie. A server fault wearing a rejection's
      // clothes, and previously indistinguishable from one: it fell through to
      // the shared 401 below and logged nothing, which is what made this branch
      // unfalsifiable during the first-attempt incidents.
      //
      // The RESPONSE stays the uniform 401 on purpose. Answering 5xx here would
      // tell a caller "the code you sent was correct" (only a correct code earns
      // an upstream 200), reintroducing the no-oracle hole the uniform shape
      // exists to close. The fix for the blindness is the log line, not the
      // status — do not "upgrade" this to a distinct status without re-deciding
      // that tradeoff.
      logAuthOutcome(OTP_VERIFY_ROUTE, {
        outcome: 'upstream_ok_no_token',
        upstream: status,
        email: parsed.data.email,
        extra: { cookie_set: false },
        level: 'warn',
      });
      return res.status(401).json({ ok: false, error: 'invalid_or_expired' });
    }
    if (status === 404) {
      logAuthOutcome(OTP_VERIFY_ROUTE, {
        outcome: 'otp_disabled',
        upstream: status,
        email: parsed.data.email,
      });
      return res.status(404).json({ ok: false, error: 'otp_disabled' });
    }
    // Uniform failure — collapse every other rejection to the single shape.
    logAuthOutcome(OTP_VERIFY_ROUTE, {
      outcome: 'rejected',
      upstream: status,
      email: parsed.data.email,
      extra: { cookie_set: false },
    });
    return res.status(401).json({ ok: false, error: 'invalid_or_expired' });
  } catch (e) {
    // The relay itself failed. Whether the transport had already committed is
    // UNKNOWABLE from here, so say so rather than implying a clean rejection.
    logAuthOutcome(OTP_VERIFY_ROUTE, {
      outcome: 'transport_error',
      email: parsed.data.email,
      extra: { error_name: (e as Error)?.name ?? 'unknown' },
      level: 'warn',
    });
    return sendTransportError(res, OTP_VERIFY_ROUTE, e);
  }
}
