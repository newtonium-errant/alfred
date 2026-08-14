import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/day/state → the contact-surface router's day state from the home
// instance's GET /day/state (C4). Session-authed exactly like
// /api/chat/notifications: the httpOnly session cookie is relayed as
// X-Alfred-Session and the transport peer-pins + recipient-pins server-side.
//
// NOT the `web_feed` peer path that /api/feed/* uses — that token carries no
// user identity, and every field here is the operator's own (their unresolved
// notices, when they were last here, which surfaces they use).
//
// An unwired instance is the backend's ILB 200 `{configured: false, ...}` — a
// real state, passed through verbatim, never an error. The client reads it as
// "do not route" and stays where it is.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  try {
    const { status, body } = await callTransport('GET', '/day/state', {
      sessionToken,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'day/state', e);
  }
}
