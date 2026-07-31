import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { pushSubscriptionSchema, pushUnsubscribeBodySchema } from '../../../lib/algernon/schemas';
import { isPushConfigured } from '../../../lib/algernon/pushConfig';
import { addSubscription, removeSubscription } from '../../../lib/algernon/pushStore';
import { ensurePushPoller } from '../../../lib/algernon/pushNotifier';

// POST /api/push/subscribe  → store a browser PushSubscription.
// DELETE /api/push/subscribe → remove one by endpoint.
//
// Gates, in order (session + owner EXACTLY like ingest/submit + feed/*):
//   1. method (405, POST|DELETE)
//   2. session present (401 invalid_session) — fail-closed
//   3. OWNER-ONLY (403 forbidden) — the display-identity role guard
//   4. push configured? (503 not_configured) — VAPID unset → deploy-inert
//   5. body shape (zod, 400 invalid_request) — z.object STRIPS unknown keys
//
// The subscription store is server-side JSONL, deduped on endpoint; the browser
// never sees any server secret. A successful POST kicks the poller singleton
// (a no-op unless PUSH_ENABLED). Store failures are REAL errors (500) — unlike
// the fire-and-forget composer-log, a failed subscribe must tell the client.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST' && req.method !== 'DELETE') {
    res.setHeader('Allow', 'POST, DELETE');
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

  if (!isPushConfigured()) {
    console.warn('[bff:push/subscribe] reject reason=not_configured');
    return res.status(503).json({ error: 'not_configured' });
  }

  if (req.method === 'DELETE') {
    const parsed = pushUnsubscribeBodySchema.safeParse(req.body);
    if (!parsed.success) {
      return res.status(400).json({
        error: 'invalid_request',
        detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
      });
    }
    try {
      const removed = await removeSubscription(parsed.data.endpoint);
      return res.status(200).json({ ok: true, removed });
    } catch (e) {
      console.warn(`[bff:push/subscribe] delete_store_error err=${(e as Error)?.message ?? 'unknown'}`);
      return res.status(500).json({ error: 'store_error' });
    }
  }

  const parsed = pushSubscriptionSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  try {
    await addSubscription({
      user: identity.name,
      endpoint: parsed.data.endpoint,
      p256dh: parsed.data.keys.p256dh,
      auth: parsed.data.keys.auth,
      at: new Date().toISOString(),
    });
  } catch (e) {
    console.warn(`[bff:push/subscribe] add_store_error err=${(e as Error)?.message ?? 'unknown'}`);
    return res.status(500).json({ error: 'store_error' });
  }

  // Kick the poller (no-op unless PUSH_ENABLED). Never lets a poller hiccup fail
  // the subscribe — the subscription is already persisted.
  try {
    ensurePushPoller();
  } catch (e) {
    console.warn(`[bff:push/subscribe] poller_kick_failed err=${(e as Error)?.message ?? 'unknown'}`);
  }
  return res.status(201).json({ ok: true });
}
