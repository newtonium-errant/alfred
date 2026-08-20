import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { chatCaptureBodySchema } from '../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/chat/capture → toggles capture mode for the live session (R1
// 2026-08-20). Body { session_key, on } relays verbatim; `instance` is the
// BFF-only routing selector (stripped before relay), same pattern as
// /api/chat/turn. The backend owns the span state — the toggle's outcome
// (capture_active, spans, closed_span) comes back from server truth.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = chatCaptureBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const payload = {
    session_key: parsed.data.session_key,
    on: parsed.data.on,
  };

  if (isHomeInstance(parsed.data.instance)) {
    try {
      const { status, body } = await callTransport('POST', '/chat/capture', {
        body: payload,
        sessionToken,
      });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'chat/capture', e);
    }
  }

  const gate = gateCrossInstance(req, parsed.data.instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(gate.targetName, 'POST', '/chat/capture', {
      body: payload,
      userName: gate.userName,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'chat/capture', e);
  }
}
