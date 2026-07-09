import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { voiceCloseBodySchema } from '../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/voice/close → idempotent, owner-bound teardown. 405 → session (401) →
// zod (400) → verbatim relay of the transport's POST /voice/close ({closed} /
// {closed:false, reason:'not_found'}). Called both by the in-app hangup and by the
// pagehide/unmount keepalive beacon (a Blob typed application/json — a plain JSON
// string body would arrive as text/plain and 400 at the transport's zod boundary;
// SECURITY W7). Home (absent / home selector): the session-token path. Cross-
// instance: the close MUST reach the backend that minted the session, so the FE
// routes it by the session's OWN instance (captured at offer time), NOT the
// currently-selected one — else a switch strands/leaks the old session. The
// `instance` selector is BFF-only, stripped before relay.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = voiceCloseBodySchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { instance, ...relayBody } = parsed.data;

  if (isHomeInstance(instance)) {
    try {
      const { status, body } = await callTransport('POST', '/voice/close', {
        body: relayBody,
        sessionToken,
      });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'voice/close', e);
    }
  }

  const gate = gateCrossInstance(req, instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'POST', '/voice/close', {
      body: relayBody,
      userName: gate.userName,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'voice/close', e);
  }
}
