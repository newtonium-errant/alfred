import type { NextApiRequest, NextApiResponse } from 'next';
import { setSessionCookies } from '../../../../lib/algernon/identity';
import { otpVerifyBodySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';
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
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const parsed = otpVerifyBodySchema.safeParse(req.body);
  if (!parsed.success) {
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
      return res.status(200).json({ ok: true });
    }
    if (status === 404) {
      return res.status(404).json({ ok: false, error: 'otp_disabled' });
    }
    // Uniform failure — collapse every other rejection to the single shape.
    return res.status(401).json({ ok: false, error: 'invalid_or_expired' });
  } catch (e) {
    return sendTransportError(res, 'auth/otp/verify', e);
  }
}
