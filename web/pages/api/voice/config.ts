import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callChatTo, callTransport } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/voice/config?instance=<name> → the voice capability/ICE/own-sessions
// probe for the chosen instance. Session-gated (fail-closed 401). Home (absent /
// home selector): the existing session-token path. Cross-instance: gate owner-only
// (403) → known target (400), then relay over that target's peer token + the
// asserted X-Alfred-User. When voice is unmounted server-side (feature off) the
// backend answers 404 — relayed so the client maps it to "voice unavailable" (the
// natural per-instance hide: a no-web.voice instance returns available:false/404).
// This route holds NO secret; the peer token is injected server-side.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const rawInstance = req.query.instance;
  const instance =
    typeof rawInstance === 'string'
      ? rawInstance
      : Array.isArray(rawInstance)
        ? rawInstance[0]
        : undefined;

  if (isHomeInstance(instance)) {
    try {
      const { status, body } = await callTransport('GET', '/voice/config', { sessionToken });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'voice/config', e);
    }
  }

  const gate = gateCrossInstance(req, instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'GET', '/voice/config', {
      userName: gate.userName,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'voice/config', e);
  }
}
