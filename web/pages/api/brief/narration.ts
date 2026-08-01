import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/brief/narration → the day's speakable narration JSON (c2's C3a
// GET /web/brief/narration): {brief_date, segments[], total_words, empty}, or the
// backend's ILB 200 {state:"no_brief"} for an absent/corrupt spool (never a 404).
// Session-authed exactly like /api/brief/latest — the httpOnly session cookie is
// relayed as X-Alfred-Session; the transport verifies it (the BFF holds no allowlist).
// Cheap read, no synth, no cost.

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  try {
    const { status, body } = await callTransport('GET', '/web/brief/narration', { sessionToken });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'brief/narration', e);
  }
}
