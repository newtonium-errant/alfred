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
