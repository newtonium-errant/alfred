import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../../lib/algernon/identity';
import { chatCaptureExtractBodySchema } from '../../../../lib/algernon/schemas';
import { callChatTo, callTransport } from '../../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../../lib/algernon/bffError';

// POST /api/chat/capture/extract → runs extraction on one closed capture span
// (R1 — the extraction offer's accept path). Awaited through to the backend
// (two LLM calls, seconds); the backend's named refusals (span_open /
// already_extracted / extraction_in_flight) relay verbatim so the client can
// speak them honestly. `instance` is BFF-only (stripped before relay).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = chatCaptureExtractBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const payload = {
    session_key: parsed.data.session_key,
    span_index: parsed.data.span_index,
  };

  if (isHomeInstance(parsed.data.instance)) {
    try {
      const { status, body } = await callTransport('POST', '/chat/capture/extract', {
        body: payload,
        sessionToken,
      });
      return res.status(status).json(body ?? {});
    } catch (e) {
      return sendTransportError(res, 'chat/capture/extract', e);
    }
  }

  const gate = gateCrossInstance(req, parsed.data.instance as string);
  if (!gate.ok) {
    return res.status(gate.status).json(gate.body);
  }

  try {
    const { status, body } = await callChatTo(
      gate.targetName,
      'POST',
      '/chat/capture/extract',
      { body: payload, userName: gate.userName },
    );
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'chat/capture/extract', e);
  }
}
