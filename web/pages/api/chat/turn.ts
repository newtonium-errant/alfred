import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { chatTurnBodySchema } from '../../../lib/algernon/schemas';
import { TransportConfigError, callTransport } from '../../../lib/algernon/transport';

// POST /api/chat/turn → validates the body (zod, the trust boundary) then relays
// to transport POST /chat/turn, which runs one turn through `run_turn`.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = chatTurnBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i) => i.message).join('; '),
    });
  }

  // M1 is text-first: normalise kind to "text" unless an explicit "voice" was
  // sent (forward-compat with M2). The backend treats kind only as a counter tag.
  const payload = {
    session_key: parsed.data.session_key,
    message: parsed.data.message,
    kind: parsed.data.kind === 'voice' ? 'voice' : 'text',
  };

  try {
    const { status, body } = await callTransport('POST', '/chat/turn', {
      body: payload,
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
