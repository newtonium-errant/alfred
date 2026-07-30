import type { NextApiRequest, NextApiResponse } from 'next';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransportFeed, isFeedConfigured } from '../../../lib/algernon/transport';
import { FEED_LIST_FILTER_KEYS } from '../../../lib/algernon/schemas';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/feed/list → relays the folded feed store from the HOME transport
// GET /feed/items, using the server-side `web_feed` peer token (BFF-only; the
// browser never sees it). The feed is the operator's own attention surface, so
// it is session + owner gated exactly like ingest/submit.ts.
//
// Gates, in order:
//   1. method (405)
//   2. session present (401 invalid_session) — fail-closed, not signed in
//   3. OWNER-ONLY (403 forbidden) — role from the display identity cookie
//      (the cookie is display/provenance; the peer token is the read authority)
//   4. feed configured? (503 not_configured) — deploy-inert until the operator
//      sets ALFRED_WEB_FEED_TOKEN, same fail-closed posture as ingest/shortcut
// Query filters state/mode/kind pass through (allowlisted — any other query key
// is dropped, so a client can't forward arbitrary query to the transport).
// Transport status codes relay verbatim; transport/config failures map via
// sendTransportError (no topology leak).
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const identity = readDisplayIdentity(req);
  if (!identity || identity.role !== 'owner') {
    return res.status(403).json({ error: 'forbidden' });
  }

  if (!isFeedConfigured()) {
    console.warn('[bff:feed/list] reject reason=not_configured');
    return res.status(503).json({ error: 'not_configured' });
  }

  // Allowlist the forwarded filters — never relay arbitrary client query keys.
  const query: Record<string, string> = {};
  for (const key of FEED_LIST_FILTER_KEYS) {
    const v = req.query[key];
    if (typeof v === 'string' && v.trim()) {
      query[key] = v;
    }
  }

  try {
    const { status, body } = await callTransportFeed('GET', '/feed/items', { query });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'feed/list', e);
  }
}
