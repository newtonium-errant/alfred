import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { voiceOfferBodySchema } from '../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/voice/offer → WebRTC signalling. 405 → session (401) → zod (400) →
// verbatim relay of the transport's POST /voice/offer (the SDP answer + minted
// voice_session_id). Home (absent / home selector): the existing session-token
// path (callTransport injects the peer token; the browser never sees it). Cross-
// instance: gate owner-only (403) → known target (400), then relay over that
// target's peer token + the asserted X-Alfred-User. The `instance` selector is
// BFF-only — stripped before relay.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = voiceOfferBodySchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { instance, ...relayBody } = parsed.data;

  if (isHomeInstance(instance)) {
    try {
      const { status, body } = await callTransport('POST', '/voice/offer', {
        body: relayBody,
        sessionToken,
      });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'voice/offer', e);
    }
  }

  const gate = gateCrossInstance(req, instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'POST', '/voice/offer', {
      body: relayBody,
      userName: gate.userName,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'voice/offer', e);
  }
}
