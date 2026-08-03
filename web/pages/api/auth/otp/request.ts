import type { NextApiRequest, NextApiResponse } from 'next';
import { otpRequestBodySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';
import { logAuthOutcome } from '../../../../lib/algernon/authLog';

// POST /api/auth/otp/request {email} → relays to transport POST
// /auth/otp/request, which emails a 6-digit one-time passcode (parity #23 —
// the iOS PWA cookie-jar fix: the code is typed INTO the already-open PWA, so
// the session cookie is later set on the PWA's own origin, not Safari's).
// The backend response is UNIFORM ({status:"sent"}) whether or not the email
// is allowlisted — no account enumeration; the BFF collapses it to {ok:true}.
// While web.auth.otp_enabled is off the backend answers 404 otp_disabled and
// that is relayed so the form can degrade gracefully. Peer auth only — no
// session token (the user isn't signed in yet).
//
// Every path logs its outcome (see authLog.ts) — the issuance half of the pair
// with verify.ts. Without it a journal shows a verify with no matching request,
// and there is no way to tell "the code was never asked for" from "the BFF
// answered and the browser lost it". Never the code (the BFF never sees it) and
// never the full email.
export const OTP_REQUEST_ROUTE = 'auth/otp/request';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    logAuthOutcome(OTP_REQUEST_ROUTE, { outcome: 'method_not_allowed' });
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const parsed = otpRequestBodySchema.safeParse(req.body);
  if (!parsed.success) {
    logAuthOutcome(OTP_REQUEST_ROUTE, { outcome: 'bad_request' });
    return res.status(400).json({ ok: false, error: 'email_required' });
  }

  try {
    const { status, body } = await callTransport('POST', '/auth/otp/request', {
      body: { email: parsed.data.email },
    });
    if (status === 200) {
      // Uniform success — identical for known and unknown emails. The log is
      // uniform too: the backend never tells us which it was, so this line
      // records that a code was ISSUED, not that a user exists.
      logAuthOutcome(OTP_REQUEST_ROUTE, {
        outcome: 'sent',
        upstream: status,
        email: parsed.data.email,
      });
      return res.status(200).json({ ok: true });
    }
    const err = (body as { error?: unknown } | null)?.error;
    const code = typeof err === 'string' ? err : 'request_failed';
    logAuthOutcome(OTP_REQUEST_ROUTE, {
      outcome: 'rejected',
      upstream: status,
      email: parsed.data.email,
      extra: { error: code },
    });
    return res.status(status).json({ ok: false, error: code });
  } catch (e) {
    logAuthOutcome(OTP_REQUEST_ROUTE, {
      outcome: 'transport_error',
      email: parsed.data.email,
      extra: { error_name: (e as Error)?.name ?? 'unknown' },
      level: 'warn',
    });
    return sendTransportError(res, OTP_REQUEST_ROUTE, e);
  }
}
