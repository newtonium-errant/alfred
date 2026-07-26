import type { NextApiRequest, NextApiResponse } from 'next';
import { otpRequestBodySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';

// POST /api/auth/otp/request {email} → relays to transport POST
// /auth/otp/request, which emails a 6-digit one-time passcode (parity #23 —
// the iOS PWA cookie-jar fix: the code is typed INTO the already-open PWA, so
// the session cookie is later set on the PWA's own origin, not Safari's).
// The backend response is UNIFORM ({status:"sent"}) whether or not the email
// is allowlisted — no account enumeration; the BFF collapses it to {ok:true}.
// While web.auth.otp_enabled is off the backend answers 404 otp_disabled and
// that is relayed so the form can degrade gracefully. Peer auth only — no
// session token (the user isn't signed in yet).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const parsed = otpRequestBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({ ok: false, error: 'email_required' });
  }

  try {
    const { status, body } = await callTransport('POST', '/auth/otp/request', {
      body: { email: parsed.data.email },
    });
    if (status === 200) {
      // Uniform success — identical for known and unknown emails.
      return res.status(200).json({ ok: true });
    }
    const err = (body as { error?: unknown } | null)?.error;
    return res
      .status(status)
      .json({ ok: false, error: typeof err === 'string' ? err : 'request_failed' });
  } catch (e) {
    return sendTransportError(res, 'auth/otp/request', e);
  }
}
