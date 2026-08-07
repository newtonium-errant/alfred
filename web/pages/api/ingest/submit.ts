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
//
// #33 — the last sibling of the bare-`||` whitespace class closed on the bearer
// door (#27/#29). `||` only catches '': a whitespace-only value is TRUTHY and
// would stamp a BLANK `origin_instance` on a real write. Trim-then-fallback, and
// say so when it degrades — a silent fallback lets the operator go on believing
// a broken setting is honoured (the #29 WARN-1 doctrine).
//
// UNSET is deliberately silent: that is the documented default, not a
// misconfiguration, and warning on it would make every stock deploy shout on
// every request.
//
// Read per-request, mirroring the shortcut door, so it is settable in tests.
// This is the SECOND copy of this six-line shape; a THIRD site should extract it
// to a shared module rather than duplicate again.
function originInstance(): string {
  const raw = process.env.NEXT_PUBLIC_INSTANCE_NAME;
  if (raw === undefined) return 'Algernon';
  const trimmed = raw.trim();
  if (trimmed) return trimmed;
  console.warn('[bff:ingest/submit] env_degraded var=NEXT_PUBLIC_INSTANCE_NAME reason=blank');
  return 'Algernon';
}

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

  const { target, record_type, title, body, source, body_format, body_b64 } = parsed.data;

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
        // A PDF relays its BYTES and the box extracts; text relays verbatim.
        // Spread rather than always-send so a text upload's request shape is
        // byte-identical to pre-#57 — an added `body_format: undefined` key
        // would still serialise differently and is exactly the kind of quiet
        // wire change that costs a debugging session later.
        ...(body_format === 'pdf'
          ? { body_format: 'pdf', body_b64 }
          : { body }),
        source,
        ingested_by: identity.name,
        ingested_at: new Date().toISOString(),
        correlation_id: randomUUID(),
        set_fields: { ingested_via: 'web', origin_instance: originInstance() },
      },
      headers: { 'X-Alfred-Ingest-User': identity.name },
    });
    return res.status(status).json(respBody ?? {});
  } catch (e) {
    return sendTransportError(res, 'ingest/submit', e);
  }
}
