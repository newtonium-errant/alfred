import type { NextApiRequest, NextApiResponse } from 'next';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { appendTrialRow, isTrialEnabled } from '../../../lib/algernon/pushTrial';

// POST /api/push/receipt → record that the operator TAPPED a trial push.
//
// Called by the service worker's notificationclick handler (same-origin, so the
// session + identity cookies ride along automatically). This is the ONLY signal
// that separates "arrived and he saw it" from "never arrived" — without it every
// unanswered slot is indistinguishable from a delivery failure, which is the
// ambiguity the whole trial exists to remove.
//
// NOT AN ASSERTED-IDENTITY SURFACE. Identity comes from the session + identity
// COOKIES via the same two helpers subscribe.ts uses — nothing is trusted from a
// caller-supplied header, so the transport peer-pin rule (which governs routes
// deriving identity from `X-Alfred-User`-style assertions) does not apply here.
// The gates below are subscribe.ts's, in the same order and for the same
// reasons:
//   1. method (405, POST)
//   2. session present (401 invalid_session) — fail-closed
//   3. OWNER-ONLY (403 forbidden)
//   4. trial running? (404 not_found) — inert when the trial is off, so the
//      route cannot be used to grow a ledger outside a trial window
//   5. body shape (400 invalid_request)
//
// A receipt write failure IS a real error (500). Unlike a fire-and-forget log, a
// silently-dropped receipt would later read as a delivery failure and corrupt
// the trial's only positive evidence.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
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

  if (!isTrialEnabled()) {
    console.warn('[bff:push/receipt] reject reason=trial_not_running');
    return res.status(404).json({ error: 'not_found' });
  }

  const body = (req.body ?? {}) as { push_id?: unknown };
  const pushId = typeof body.push_id === 'string' ? body.push_id.trim() : '';
  // Bounded + charset-fenced: this value is written into a ledger the status
  // surface reads, and an unbounded caller-supplied string is how a log grows a
  // record it did not author.
  if (!pushId || pushId.length > 64 || !/^[A-Za-z0-9_-]+$/.test(pushId)) {
    return res.status(400).json({ error: 'invalid_request', detail: 'push_id' });
  }

  try {
    await appendTrialRow({
      type: 'receipt',
      push_id: pushId,
      received_ts: new Date().toISOString(),
    });
  } catch (e) {
    console.warn(`[bff:push/receipt] store_error err=${(e as Error)?.message ?? 'unknown'}`);
    return res.status(500).json({ error: 'store_error' });
  }

  console.log(`[bff:push/receipt] recorded push_id=${pushId}`);
  return res.status(201).json({ ok: true });
}
