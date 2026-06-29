import type { NextApiRequest } from 'next';
import { readDisplayIdentity } from './identity';
import { listCrossInstanceChatTargets } from './transport';
import { isHomeInstance } from './instance';

// SERVER-ONLY. The cross-instance chat routing seam shared by the /api/chat/*
// BFF routes. The home instance keeps the existing session-token path; a
// cross-instance selector relays over that target's peer token + an asserted
// X-Alfred-User (Model B — trust-the-relay, mirroring ingest).

export { isHomeInstance };

export type CrossInstanceGate =
  | { ok: true; targetName: string; userName: string }
  | { ok: false; status: number; body: { error: string } };

/**
 * Apply the cross-instance gates in CONTRACT order — owner-only (403) → known
 * target (400) — after the caller has already enforced session-present (401).
 * Returns the canonical target name to relay to + the verified display name to
 * assert as X-Alfred-User. The identity cookie role is a defence-in-depth BFF
 * guard (the peer token is the real authority); the BFF asserts only the NAME,
 * never the role — the target re-resolves the role against its own web.users
 * (non-escalating, decision M3/M8).
 */
export function gateCrossInstance(req: NextApiRequest, instance: string): CrossInstanceGate {
  const identity = readDisplayIdentity(req);
  if (!identity || identity.role !== 'owner') {
    return { ok: false, status: 403, body: { error: 'forbidden' } };
  }
  const wanted = instance.trim().toUpperCase();
  const match = listCrossInstanceChatTargets().find((t) => t.name.toUpperCase() === wanted);
  if (!match) {
    return { ok: false, status: 400, body: { error: 'unknown_target' } };
  }
  return { ok: true, targetName: match.name, userName: identity.name };
}
