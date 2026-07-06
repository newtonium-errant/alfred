import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { voiceOfferBodySchema } from '../../../lib/algernon/schemas';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/voice/offer → WebRTC signalling. 405 → session (401) → zod (400) →
// verbatim relay of the transport's POST /voice/offer (the SDP answer + minted
// voice_session_id). The peer token is injected server-side by callTransport; the
// browser never sees it. No instance selector, no cross-instance path (home-only).
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

  try {
    const { status, body } = await callTransport('POST', '/voice/offer', {
      body: parsed.data,
      sessionToken,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'voice/offer', e);
  }
}
