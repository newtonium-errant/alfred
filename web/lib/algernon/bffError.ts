import type { NextApiResponse } from 'next';
import { TransportConfigError, TransportTimeoutError } from './transport';

// SERVER-ONLY. Maps a transport-call failure to a CLIENT-SAFE BFF error response.
// The specific cause — TransportConfigError messages name internal env vars
// (e.g. "ALFRED_WEB_PEER_TOKEN is not set"), and raw fetch errors expose
// internal topology — is logged server-side ONLY and never returned to the
// browser. The client receives just the generic code (no `detail`); useChat /
// the login page map that code to user copy. (Reviewer note on FE-2: don't leak
// internal config/topology even on an auth-gated route.)
export function sendTransportError(
  res: NextApiResponse,
  route: string,
  e: unknown,
): void {
  if (e instanceof TransportConfigError) {
    console.error(`[bff:${route}] transport misconfigured: ${e.message}`);
    res.status(500).json({ error: 'transport_misconfigured' });
    return;
  }
  if (e instanceof TransportTimeoutError) {
    // The turn MAY still be finishing server-side — a distinct 504 lets the FE
    // map to the recovery/reconcile path, not the hard-down 502 (CONTRACT S8).
    console.error(`[bff:${route}] transport timed out: ${e.message}`);
    res.status(504).json({ error: 'gateway_timeout' });
    return;
  }
  console.error(`[bff:${route}] transport unreachable: ${(e as Error).message}`);
  res.status(502).json({ error: 'transport_unreachable' });
}

/**
 * Session-RELAYING routes (the brief family: /web/outbound, /web/brief/audio,
 * /web/brief/narration relay the client's X-Alfred-Session) get an AMBIGUOUS post-auth
 * upstream 401 — unlike feed/*, whose post-auth 401 is unambiguously a peer misconfig and
 * so can be blanket-mapped. Discriminate on the upstream `error` field:
 *   - `wrong_peer` → the BFF's own peer token is misconfigured (a server fault) → map to
 *     502 + an `upstream_auth_misconfig` warn. Relaying the bare 401 would make the PWA
 *     fake a LOGOUT on a server fault (the B2 harm).
 *   - anything else (esp. `invalid_session`) → the operator's session genuinely expired →
 *     the caller RELAYS the 401 (the legit re-login path; mapping it would break recovery
 *     from a normal expiry — the reason a blanket map is WRONG for this family).
 * Returns true when it SENT the 502 (caller returns immediately); false → caller relays.
 * All three brief-family routes emit exactly {error:"wrong_peer"} / {error:"invalid_session"}
 * at 401 (verified against routes_brief.py + routes_brief_audio.py).
 */
export function mapUpstreamWrongPeer(res: NextApiResponse, route: string, status: number, body: unknown): boolean {
  if (status !== 401) return false;
  const code = body && typeof body === 'object' ? (body as { error?: unknown }).error : undefined;
  if (code === 'wrong_peer') {
    console.warn(`[bff:${route}] upstream_auth_misconfig`);
    res.status(502).json({ error: 'brief_upstream_unavailable' });
    return true;
  }
  return false; // invalid_session / other → caller relays the 401 (legit re-login)
}
