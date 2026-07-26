import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../../lib/algernon/identity';
import { notificationsAckBodySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';

// POST /api/chat/notifications/ack { ids: [...] } → marks the given tray
// entries read on the home instance (parity #22, POLL slice). Validates the
// body at the BFF trust boundary (bounded id list) before relaying; the
// backend re-validates as the authority and acks idempotently ({ acked,
// unread } — re-acking acks 0, never errors).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = notificationsAckBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  try {
    const { status, body } = await callTransport('POST', '/chat/notifications/ack', {
      body: { ids: parsed.data.ids },
      sessionToken,
    });
    return res.status(status).json(body ?? { acked: 0, unread: 0 });
  } catch (e) {
    return sendTransportError(res, 'chat/notifications/ack', e);
  }
}
