import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { randomUUID } from 'crypto';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { ingestBodySchema } from '../../../lib/algernon/schemas';
import { callTransportTo, listIngestTargets } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// The origin instance label stamped into ingest provenance. Server-side read of
// the (public, non-secret) instance display name — parameterised, NOT a hardcoded
// "Salem", so a different deploy stamps its own origin.
const ORIGIN_INSTANCE = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// POST /api/ingest/submit → relays a VERBATIM artifact to the CHOSEN target
// instance's transport /vault/ingest, using that target's server-side peer token.
// Gates, in order:
//   1. method (405)
//   2. session present (401) — fail-closed, not signed in
//   3. OWNER-ONLY (403) — read role from the display identity cookie
//      (BUILD_DECISIONS decision C). The cookie is provenance/display, never the
//      WRITE authority — the peer token (BFF-only) is. This is a defence-in-depth
//      BFF role guard layered on top of the peer-token authz.
//   4. body shape (zod, 400)
//   5. target is a configured target (400 unknown_target) — checked BEFORE any
//      env lookup so a bogus `target` can't probe server env.
// `ingested_by` is the verified display name (provenance metadata only). Backend
// errors (409 title_collision, 413 body_too_large, …) relay through verbatim;
// transport/config failures map via sendTransportError (no topology leak).
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

  const parsed = ingestBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { target, record_type, title, body, source } = parsed.data;

  // The target must be one the server actually configured. Distinguishes a bad
  // target NAME (400) from a configured-but-misconfigured env (→ 500 below).
  const known = listIngestTargets().some((t) => t.name === target);
  if (!known) {
    return res.status(400).json({ error: 'unknown_target' });
  }

  try {
    const { status, body: respBody } = await callTransportTo(target, 'POST', '/vault/ingest', {
      body: {
        record_type,
        title,
        body,
        source,
        ingested_by: identity.name,
        ingested_at: new Date().toISOString(),
        correlation_id: randomUUID(),
        set_fields: { ingested_via: 'web', origin_instance: ORIGIN_INSTANCE },
      },
      headers: { 'X-Alfred-Ingest-User': identity.name },
    });
    return res.status(status).json(respBody ?? {});
  } catch (e) {
    return sendTransportError(res, 'ingest/submit', e);
  }
}
