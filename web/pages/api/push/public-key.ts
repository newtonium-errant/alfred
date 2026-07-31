import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { readVapidConfig } from '../../../lib/algernon/pushConfig';
import { ensurePushPoller } from '../../../lib/algernon/pushNotifier';

// GET /api/push/public-key → the VAPID PUBLIC key the client needs to subscribe.
// The public key is public BY DESIGN (it's the applicationServerKey the browser
// sends to the push service), but the route is still session-gated — only a
// signed-in operator sets up push, and it avoids a fully-anonymous endpoint. It
// is NOT owner-gated: reading a public key is not a write.
//
// Gates: method (405) → session (401) → configured (503 not_configured). Absent
// VAPID → deploy-inert 503, same fail-closed posture as the feed routes.
//
// A GET also KICKS the poller singleton (a no-op unless PUSH_ENABLED): the client
// probes this on every PWA load, so the background sender resumes after a server
// restart without waiting for a re-subscribe.
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const vapid = readVapidConfig();
  if (!vapid) {
    console.warn('[bff:push/public-key] reject reason=not_configured');
    return res.status(503).json({ error: 'not_configured' });
  }

  try {
    ensurePushPoller();
  } catch (e) {
    console.warn(`[bff:push/public-key] poller_kick_failed err=${(e as Error)?.message ?? 'unknown'}`);
  }
  return res.status(200).json({ publicKey: vapid.publicKey });
}
