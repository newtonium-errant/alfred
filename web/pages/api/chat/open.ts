import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { TransportConfigError, callTransport } from '../../../lib/algernon/transport';

// POST /api/chat/open → relays to transport POST /chat/open. Archives+closes any
// prior session for this user and opens a fresh one (the backend's behaviour).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  try {
    const { status, body } = await callTransport('POST', '/chat/open', {
      body: {},
      sessionToken,
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
