import type { NextApiRequest, NextApiResponse } from 'next';
import { loginBodySchema } from '../../../lib/algernon/schemas';
import { TransportConfigError, callTransport } from '../../../lib/algernon/transport';

// POST /api/auth/login {email} → relays to transport POST /auth/login, which
// sends the magic-link email (Resend). The backend's response is UNIFORM
// ({status:"sent"}) regardless of whether the email is allowlisted — no account
// enumeration. Peer auth is sent (Layer 1, required on all routes); no session
// token (the user isn't signed in yet).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const parsed = loginBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({ error: 'email_required' });
  }

  try {
    const { status, body } = await callTransport('POST', '/auth/login', {
      body: { email: parsed.data.email },
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    if (e instanceof TransportConfigError) {
      return res.status(500).json({ error: 'transport_misconfigured', detail: e.message });
    }
    return res
      .status(502)
      .json({ error: 'transport_unreachable', detail: (e as Error).message });
  }
}
