import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/voice/config → the voice capability/ICE/own-sessions probe. Session-
// gated (fail-closed 401), then a verbatim relay of the transport's GET
// /voice/config. When voice is unmounted server-side (feature off) the transport
// answers 404 — relayed as 404 so the client maps it to "voice unavailable". This
// route holds NO secret; callTransport injects the peer token server-side. No
// instance selector, no cross-instance path (V0 is home-only by construction).
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
    const { status, body } = await callTransport('GET', '/voice/config', { sessionToken });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'voice/config', e);
  }
}
