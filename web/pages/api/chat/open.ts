import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { chatOpenBodySchema } from '../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/chat/open → opens a fresh session (archives+closes the prior one).
// Home instance (absent / home selector): the existing session-token path,
// UNCHANGED. Cross-instance selector: gate session (401) → owner-only (403) →
// known target (400), then relay to that instance over its peer token + the
// asserted X-Alfred-User. The `instance` field is BFF-only (stripped before relay).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  // Boundary-validate the (optional) instance selector via zod, matching the
  // other chat routes. The cross-instance allowlist + isValidTargetName still gate
  // the relay; this is the trust-boundary edge guard for shape consistency.
  const parsed = chatOpenBodySchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }
  const instance = parsed.data.instance;

  if (isHomeInstance(instance)) {
    try {
      const { status, body } = await callTransport('POST', '/chat/open', {
        body: {},
        sessionToken,
      });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'chat/open', e);
    }
  }

  const gate = gateCrossInstance(req, instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'POST', '/chat/open', {
      body: {},
      userName: gate.userName,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'chat/open', e);
  }
}
