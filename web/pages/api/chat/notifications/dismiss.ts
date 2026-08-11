import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../../lib/algernon/identity';
import { notificationsDismissBodySchema } from '../../../../lib/algernon/schemas';
import { callTransport } from '../../../../lib/algernon/transport';
import { sendTransportError } from '../../../../lib/algernon/bffError';

// POST /api/chat/notifications/dismiss { ids: [...] } → clears the given tray
// entries on the home instance (#86). Mirrors the ack door exactly — session
// gate, bounded id list validated at the trust boundary, backend re-validates
// as the authority and dismisses idempotently ({ dismissed, unread }).
//
// A SEPARATE route from ack rather than a flag on it. "I have seen this" and
// "I am done with this" differ in reversibility: a mistaken ack costs nothing,
// a mistaken dismiss takes the notice off both the tray and the daily brief.
// One endpoint with a mode switch is how a client bug turns the first into the
// second. Dismissal is still non-destructive at the store — the entry stays
// for audit — which is what keeps that mistake recoverable rather than final.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const parsed = notificationsDismissBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  try {
    const { status, body } = await callTransport(
      'POST',
      '/chat/notifications/dismiss',
      { body: { ids: parsed.data.ids }, sessionToken },
    );
    return res.status(status).json(body ?? { dismissed: 0, unread: 0 });
  } catch (e) {
    return sendTransportError(res, 'chat/notifications/dismiss', e);
  }
}
