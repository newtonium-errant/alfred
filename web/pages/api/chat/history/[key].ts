import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveIdentity } from '../../../../lib/algernon/identity';
import { sessionKeySchema } from '../../../../lib/algernon/schemas';
import { TransportConfigError, callTransport } from '../../../../lib/algernon/transport';

// GET /api/chat/history/{key} → relays to transport GET /chat/history/{key}.
// Returns the current active session's transcript (flattened to {role,text,ts}).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const identity = resolveIdentity(req);
  if (!identity) {
    return res.status(401).json({ error: 'not_authenticated' });
  }

  const raw = req.query.key;
  const parsed = sessionKeySchema.safeParse(Array.isArray(raw) ? raw[0] : raw);
  if (!parsed.success) {
    return res.status(400).json({ error: 'invalid_session_key' });
  }

  try {
    const { status, body } = await callTransport(
      'GET',
      `/chat/history/${encodeURIComponent(parsed.data)}`,
      identity,
    );
    return res.status(status).json(body ?? { turns: [] });
  } catch (e) {
    if (e instanceof TransportConfigError) {
      return res.status(500).json({ error: 'transport_misconfigured', detail: e.message });
    }
    return res
      .status(502)
      .json({ error: 'transport_unreachable', detail: (e as Error).message });
  }
}
