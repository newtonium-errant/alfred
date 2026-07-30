import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransportFeed, isFeedConfigured } from '../../../lib/algernon/transport';
import { feedActBodySchema } from '../../../lib/algernon/schemas';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/feed/act → relays {id, action_id} to the HOME transport
// POST /feed/act, using the server-side `web_feed` peer token. The transport's
// (kind, action) map is the capability ceiling — the BFF only relays and
// surfaces the outcome verbatim.
//
// Gates, in order:
//   1. method (405)
//   2. session present (401 invalid_session)
//   3. OWNER-ONLY (403 forbidden)
//   4. feed configured? (503 not_configured — deploy-inert)
//   5. body shape (zod, 400 invalid_request) — z.object STRIPS unknowns, so a
//      client can't smuggle extra fields into the transport body
//
// Provenance the BFF owns — `via: "deck"` + the acting user — is LOGGED
// server-side (one structured line per act, no evidence content). It is NOT
// added to the relay body: the transport /feed/act contract defines only
// {id, action_id}, and B1 refuses unknown fields.
//
// NO RETRY ON TIMEOUT: a timed-out act MAY have dispatched at the transport;
// retrying could re-drive a resolver and reopen the double-dispatch B1's
// per-item lock closed. So a TransportTimeoutError surfaces as-is (504 via
// sendTransportError) — the deck's stale/already_acted handling covers the
// retry UX, never an automatic resend. Transport status codes relay verbatim
// (200/400/401/409/422/503 per the B1 contract); a SET-but-WRONG `web_feed`
// token → the transport 401s `feed_wrong_peer`, relayed as an error (never a
// silent success).
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

  if (!isFeedConfigured()) {
    console.warn('[bff:feed/act] reject reason=not_configured');
    return res.status(503).json({ error: 'not_configured' });
  }

  const parsed = feedActBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { id, action_id } = parsed.data;

  try {
    const { status, body } = await callTransportFeed('POST', '/feed/act', {
      body: { id, action_id },
    });
    // BFF-owned provenance — id/action/outcome/user, no evidence content. `via`
    // captures the deck provenance the transport body deliberately doesn't carry.
    const outcome =
      body && typeof body === 'object' && 'status' in body
        ? String((body as { status?: unknown }).status)
        : `http_${status}`;
    console.log(
      `[bff:feed/act] act id=${id} action_id=${action_id} via=deck ` +
        `outcome=${outcome} upstream=${status} user=${identity.name}`,
    );
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'feed/act', e);
  }
}
