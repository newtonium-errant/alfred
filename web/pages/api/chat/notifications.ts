import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/chat/notifications → the operator's notification tray from the home
// instance's GET /chat/notifications (parity #22 KAL-LE ticket → PWA notify,
// POLL slice — no push channel). Session-authed like /api/chat/history: the
// httpOnly session cookie is relayed as X-Alfred-Session; the transport
// verifies it AND peer-pins + recipient-pins server-side. Home-instance only
// (the tray lives on the login instance; cross-instance trays are out of the
// v1 single-user ruling). An empty tray is the backend's ILB 200
// { notifications: [], unread: 0 } — passed through verbatim, never an error.
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
    const { status, body } = await callTransport('GET', '/chat/notifications', {
      sessionToken,
    });
    return res.status(status).json(body ?? { notifications: [], unread: 0 });
  } catch (e) {
    return sendTransportError(res, 'chat/notifications', e);
  }
}
