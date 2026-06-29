import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../../lib/algernon/identity';
import { sessionKeySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';

// GET /api/chat/history/{key} → relays to transport GET /chat/history/{key}.
// Returns the current active session's transcript (flattened to {role,text,ts}).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
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
      { sessionToken },
    );
    return res.status(status).json(body ?? { turns: [] });
  } catch (e) {
    return sendTransportError(res, 'chat/history', e);
  }
}
