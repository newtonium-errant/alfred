import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/day/override {contact_id, surface} → records a one-tap override (C4).
//
// The correction signal — the input to pattern-surfacing, and the reason the
// router can improve rather than stay a static rule set. Type-checked here,
// value-validated at the transport (same single-authority split as
// /api/day/contact).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const body = req.body as Record<string, unknown> | undefined;
  const contactId = body?.contact_id;
  const surface = body?.surface;
  if (typeof contactId !== 'string' || typeof surface !== 'string') {
    return res.status(400).json({ error: 'invalid_body' });
  }

  try {
    const { status, body: out } = await callTransport('POST', '/day/override', {
      sessionToken,
      body: { contact_id: contactId, surface },
    });
    return res.status(status).json(out ?? { recorded: false, patterns_surfaced: 0 });
  } catch (e) {
    return sendTransportError(res, 'day/override', e);
  }
}
