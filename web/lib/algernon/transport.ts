import type { WebIdentity } from './identity';

// SERVER-ONLY. The BFF's call into the Algernon transport. Holds the peer token
// + base URL (server-side env, NEVER NEXT_PUBLIC_), so the browser never sees
// them. Imported only by `pages/api/*` route handlers.
//
// Transport auth (src/alfred/transport/server.py auth_middleware) requires:
//   - Authorization: Bearer <peer token>     (Layer 1: "this front-end may talk")
//   - X-Alfred-Client: <peer/client name>    (allowlist enforcement)
// The web user identity (Layer 2, Sub-arc A) rides on:
//   - X-Web-User: <name>                      (resolved + relayed server-side)

// The peer/client name the transport knows this front-end by. Must match the
// backend's web peer entry in `auth.tokens` (Sub-arc B3). Config-driven so a
// backend rename is a config change, not a code change.
const PEER_CLIENT = process.env.ALFRED_WEB_PEER_CLIENT || 'web';

/** Thrown when required transport env is missing — surfaced as a 500 by the BFF. */
export class TransportConfigError extends Error {}

function baseUrl(): string {
  const url = process.env.ALFRED_WEB_TRANSPORT_URL;
  if (!url) {
    throw new TransportConfigError('ALFRED_WEB_TRANSPORT_URL is not set');
  }
  return url.replace(/\/+$/, '');
}

function peerToken(): string {
  const token = process.env.ALFRED_WEB_PEER_TOKEN;
  if (!token) {
    throw new TransportConfigError('ALFRED_WEB_PEER_TOKEN is not set');
  }
  return token;
}

export interface TransportResult {
  status: number;
  body: unknown;
}

export async function callTransport(
  method: 'GET' | 'POST',
  path: string,
  identity: WebIdentity,
  jsonBody?: unknown,
): Promise<TransportResult> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${peerToken()}`,
    'X-Alfred-Client': PEER_CLIENT,
    'X-Web-User': identity.user,
    Accept: 'application/json',
  };
  if (jsonBody !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(`${baseUrl()}${path}`, {
    method,
    headers,
    body: jsonBody !== undefined ? JSON.stringify(jsonBody) : undefined,
  });

  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // A non-JSON body (e.g. an upstream 502 HTML page) → null; the BFF maps the
    // status. Don't throw: a bad-shaped error response must not mask the status.
    body = null;
  }
  return { status: res.status, body };
}
