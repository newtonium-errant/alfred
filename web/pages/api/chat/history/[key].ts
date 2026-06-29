import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../../lib/algernon/identity';
import { sessionKeySchema } from '../../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../../lib/algernon/bffError';

// GET /api/chat/history/{key}?instance=<name> → the active session transcript for
// the chosen instance. Home (absent / home selector): the existing session-token
// path. Cross-instance: gate session (401) → owner-only (403) → known target
// (400), then relay over that target's peer token + the asserted X-Alfred-User.
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

  const rawInstance = req.query.instance;
  const instance =
    typeof rawInstance === 'string'
      ? rawInstance
      : Array.isArray(rawInstance)
        ? rawInstance[0]
        : undefined;

  const path = `/chat/history/${encodeURIComponent(parsed.data)}`;

  if (isHomeInstance(instance)) {
    try {
      const { status, body } = await callTransport('GET', path, { sessionToken });
      return res.status(status).json(body ?? { turns: [] });
    } catch (e) {
      return sendTransportError(res, 'chat/history', e);
    }
  }

  const gate = gateCrossInstance(req, instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'GET', path, {
      userName: gate.userName,
    });
    return res.status(status).json(body ?? { turns: [] });
  } catch (e) {
    return sendTransportError(res, 'chat/history', e);
  }
}
