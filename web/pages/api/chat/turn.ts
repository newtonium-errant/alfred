import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { chatTurnBodySchema } from '../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/chat/turn → validates the body (zod, the trust boundary) then relays
// one turn through `run_turn`. Home instance: the existing session-token path.
// Cross-instance selector: gate session (401) → owner-only (403) → known target
// (400), then relay over that target's peer token + the asserted X-Alfred-User.
// The `instance` field is BFF-only (stripped before relay); `idempotency_key`
// relays verbatim for backend retry-safety dedup.
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
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  // M1 is text-first: normalise kind to "text" unless an explicit "voice" was
  // sent (forward-compat with M2). The backend treats kind only as a counter tag.
  // `instance` is NOT relayed (BFF-only routing selector).
  const payload = {
    session_key: parsed.data.session_key,
    message: parsed.data.message,
    kind: parsed.data.kind === 'voice' ? 'voice' : 'text',
    ...(parsed.data.idempotency_key ? { idempotency_key: parsed.data.idempotency_key } : {}),
  };

  if (isHomeInstance(parsed.data.instance)) {
    try {
      const { status, body } = await callTransport('POST', '/chat/turn', {
        body: payload,
        sessionToken,
      });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'chat/turn', e);
    }
  }

  const gate = gateCrossInstance(req, parsed.data.instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'POST', '/chat/turn', {
      body: payload,
      userName: gate.userName,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'chat/turn', e);
  }
}
