import type { NextApiRequest, NextApiResponse } from 'next';
import { loginBodySchema } from '../../../lib/algernon/schemas';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/auth/login {email, next?} → relays to transport POST /auth/login,
// which sends the magic-link email (Resend). `next` (optional deep-link target)
// is relayed only when present; the backend embeds it in the link and is the
// authority on sanitising it. The backend's response is UNIFORM
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
      body: {
        email: parsed.data.email,
        ...(parsed.data.next ? { next: parsed.data.next } : {}),
      },
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'auth/login', e);
  }
}
