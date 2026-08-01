import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { mapUpstreamWrongPeer, sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/brief/latest?kind=brief|daily_sync → the latest daemon-spooled
// outbound artifact ({kind, date, markdown}) from the home instance's
// GET /web/outbound/{kind}/latest (#30 READ-ON-OPEN). Session-authed like the
// other /api routes: the httpOnly session cookie is relayed as
// X-Alfred-Session; the transport verifies it (the BFF holds no allowlist).
// An empty spool is the backend's ILB 200 {date:null, markdown:null} — passed
// through verbatim so the page renders "no brief yet today", never an error.

// The kinds the route relays. Anything else → 400 unknown_kind BEFORE any
// transport call (mirrors the backend's OUTBOUND_KINDS allowlist — the kind is
// interpolated into the transport path, so it must never be attacker-shaped).
export const OUTBOUND_KINDS = ['brief', 'daily_sync'] as const;
export type OutboundKind = (typeof OUTBOUND_KINDS)[number];

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const raw = req.query.kind;
  const kind = typeof raw === 'string' ? raw : Array.isArray(raw) ? raw[0] : '';
  if (!(OUTBOUND_KINDS as readonly string[]).includes(kind)) {
    return res.status(400).json({ error: 'unknown_kind' });
  }

  try {
    const { status, body } = await callTransport('GET', `/web/outbound/${kind}/latest`, {
      sessionToken,
    });
    // A post-auth wrong_peer 401 (BFF peer misconfig) → 502, never a fake logout; a real
    // invalid_session 401 relays for re-login. Fence-widened here alongside brief/audio +
    // brief/narration (deliberate — this #30-era route had the same relay gap; consistency
    // over per-lane bolt-ons, flagged to the reviewer).
    if (mapUpstreamWrongPeer(res, 'brief/latest', status, body)) return;
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'brief/latest', e);
  }
}
