// SERVER-ONLY. The BFF's call into the Algernon transport. Holds the peer token
// + base URL (server-side env, NEVER NEXT_PUBLIC_), so the browser never sees
// them. Imported only by `pages/api/*` route handlers.
//
// Transport auth (src/alfred/transport/server.py auth_middleware) requires on
// EVERY route (incl. /auth/*):
//   - Authorization: Bearer <peer token>     (Layer 1: "this front-end may talk")
//   - X-Alfred-Client: <peer/client name>    (allowlist enforcement)
// User identity (Layer 2, B3 live contract) on /chat/* rides on:
//   - X-Alfred-Session: <instance-signed session token>   (verified server-side)
// The /auth/* routes carry NO session token (the user isn't signed in yet).

// The peer/client name the transport knows this front-end by. Must match the
// backend's web peer entry in `auth.tokens`. Config-driven so a backend rename is
// a config change, not a code change. Defaults to "web".
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

export interface CallOptions {
  /** JSON request body (POST). Omit for a GET / empty-body request. */
  body?: unknown;
  /** Instance-signed session token → X-Alfred-Session. Omit for /auth/* routes. */
  sessionToken?: string | null;
}

export interface TransportResult {
  status: number;
  body: unknown;
}

export async function callTransport(
  method: 'GET' | 'POST',
  path: string,
  opts: CallOptions = {},
): Promise<TransportResult> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${peerToken()}`,
    'X-Alfred-Client': PEER_CLIENT,
    Accept: 'application/json',
  };
  if (opts.sessionToken) {
    headers['X-Alfred-Session'] = opts.sessionToken;
  }
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(`${baseUrl()}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
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
