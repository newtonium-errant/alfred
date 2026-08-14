import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/day/contact {rule, surface} → records one app-open (C4).
//
// The body is relayed with only a TYPE check, never a vocabulary check: the
// transport validates `rule` against its own armed set and `surface` against its
// own vocabulary, and it is the authority on both. A second allowlist here would
// be a copy that drifts — and the failure mode of the drift is a client whose
// legitimate contact is rejected by a BFF holding a stale list.
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
  const rule = body?.rule;
  const surface = body?.surface;
  if (typeof rule !== 'string' || typeof surface !== 'string') {
    return res.status(400).json({ error: 'invalid_body' });
  }

  try {
    const { status, body: out } = await callTransport('POST', '/day/contact', {
      sessionToken,
      body: { rule, surface },
    });
    return res.status(status).json(out ?? { contact_id: '', recorded: false });
  } catch (e) {
    return sendTransportError(res, 'day/contact', e);
  }
}
