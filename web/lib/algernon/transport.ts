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

  return { status: res.status, body: await parseJsonOrNull(res) };
}

// A non-JSON body (e.g. an upstream 502 HTML page) → null; the BFF maps the
// status. Don't throw: a bad-shaped error response must not mask the status.
async function parseJsonOrNull(res: Response): Promise<unknown> {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

// --- Web STT binary relay (BUILD_DECISIONS §4) ------------------------------
// Relays a raw audio Buffer to the transport's POST /stt/transcribe. Same Layer-1
// peer auth + X-Alfred-Client + relayed session token as callTransport, but the
// Content-Type is the AUDIO mime and the body is the raw Buffer (NOT JSON). The
// browser never sees the peer token — it's injected here, server-side. STT rides
// the SAME transport env as chat (ALFRED_WEB_TRANSPORT_URL / _PEER_TOKEN).
export interface BinaryCallOptions {
  body: Buffer;
  contentType: string;
  sessionToken?: string | null;
}

export async function callTransportBinary(
  method: 'POST',
  path: string,
  opts: BinaryCallOptions,
): Promise<TransportResult> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${peerToken()}`,
    'X-Alfred-Client': PEER_CLIENT,
    Accept: 'application/json',
    'Content-Type': opts.contentType,
  };
  if (opts.sessionToken) {
    headers['X-Alfred-Session'] = opts.sessionToken;
  }

  const res = await fetch(`${baseUrl()}${path}`, {
    method,
    headers,
    // Re-wrap as a fresh Uint8Array view so the fetch body is a plain ArrayBuffer
    // (undici rejects a Node Buffer subarray that aliases a larger pool buffer).
    body: new Uint8Array(opts.body),
  });

  return { status: res.status, body: await parseJsonOrNull(res) };
}

// --- Cross-instance ingest target resolution (BUILD_DECISIONS §2 / §3) ------
// Each ingest target has its OWN server-side env pair (NEVER NEXT_PUBLIC_):
//   ALFRED_WEB_INGEST_<NAME>_URL    — the target transport base URL (loopback)
//   ALFRED_WEB_INGEST_<NAME>_TOKEN  — that target's dedicated `web_ingest` peer token
//   ALFRED_WEB_INGEST_<NAME>_LABEL  — (optional) display label; defaults to <NAME>
// The BFF is the SOLE holder of every target token. A target is "configured" only
// when BOTH its URL and token are present (fail-closed) — a half-configured target
// never appears in the picker and never resolves.
const INGEST_ENV_PREFIX = 'ALFRED_WEB_INGEST_';
// Default ingest record types — mirrors the backend `WEB_INGEST_CREATE_TYPES`
// (BUILD_DECISIONS decision B). Intentional cross-instance constant.
const INGEST_DEFAULT_RECORD_TYPES = ['document', 'note', 'source'];

export interface IngestTargetMeta {
  name: string;
  label: string;
  recordTypes: string[];
}

export interface ResolvedIngestTarget {
  baseUrl: string;
  token: string;
  client: string;
}

// A target name must be a safe env-key segment so it can't be used to read
// arbitrary process env. Letters/digits/underscore only (the picker round-trips
// the exact `name` from listIngestTargets).
function isValidTargetName(name: string): boolean {
  return /^[A-Za-z0-9_]+$/.test(name);
}

/**
 * The configured ingest targets, derived from env. Scans for every
 * `ALFRED_WEB_INGEST_<NAME>_URL` that also has a matching `_TOKEN`. Returns
 * metadata ONLY (name/label/recordTypes) — never a URL or token. Sorted by label
 * for a stable picker. Empty array when nothing is configured (the page renders an
 * explicit "no ingest targets configured" empty state — intentionally-left-blank).
 */
export function listIngestTargets(): IngestTargetMeta[] {
  const out: IngestTargetMeta[] = [];
  const seen = new Set<string>();
  for (const key of Object.keys(process.env)) {
    const m = key.match(/^ALFRED_WEB_INGEST_([A-Z0-9_]+)_URL$/);
    if (!m) continue;
    const name = m[1];
    if (seen.has(name)) continue;
    const url = process.env[`${INGEST_ENV_PREFIX}${name}_URL`];
    const token = process.env[`${INGEST_ENV_PREFIX}${name}_TOKEN`];
    if (!url || !url.trim() || !token || !token.trim()) continue; // fail-closed
    seen.add(name);
    const label = (process.env[`${INGEST_ENV_PREFIX}${name}_LABEL`] || name).trim();
    out.push({ name, label, recordTypes: [...INGEST_DEFAULT_RECORD_TYPES] });
  }
  out.sort((a, b) => a.label.localeCompare(b.label));
  return out;
}

/**
 * Resolve a target name to its server-side URL + token. Throws
 * TransportConfigError when the name is malformed or the env pair is missing
 * (→ the BFF maps to a generic 500 transport_misconfigured, leaking no topology).
 * The BFF validates the name against listIngestTargets() FIRST (→ 400 for an
 * unknown target) so this is the missing-env / misconfig path.
 */
export function resolveIngestTarget(name: string): ResolvedIngestTarget {
  if (!isValidTargetName(name)) {
    throw new TransportConfigError(`invalid ingest target name: ${name}`);
  }
  const key = name.toUpperCase();
  const url = process.env[`${INGEST_ENV_PREFIX}${key}_URL`];
  const token = process.env[`${INGEST_ENV_PREFIX}${key}_TOKEN`];
  if (!url || !url.trim()) {
    throw new TransportConfigError(`${INGEST_ENV_PREFIX}${key}_URL is not set`);
  }
  if (!token || !token.trim()) {
    throw new TransportConfigError(`${INGEST_ENV_PREFIX}${key}_TOKEN is not set`);
  }
  return { baseUrl: url.replace(/\/+$/, ''), token, client: PEER_CLIENT };
}

export interface IngestCallOptions {
  body?: unknown;
  /** Extra headers (e.g. X-Alfred-Ingest-User provenance assertion). */
  headers?: Record<string, string>;
}

/**
 * Relay a JSON call to a CHOSEN ingest target (not the default chat transport).
 * Uses that target's dedicated `web_ingest` peer token + base URL. Possession of
 * the target token IS the write authority (the BFF is the sole holder).
 */
export async function callTransportTo(
  targetName: string,
  method: 'GET' | 'POST',
  path: string,
  opts: IngestCallOptions = {},
): Promise<TransportResult> {
  const target = resolveIngestTarget(targetName);
  const headers: Record<string, string> = {
    Authorization: `Bearer ${target.token}`,
    'X-Alfred-Client': target.client,
    Accept: 'application/json',
    ...(opts.headers || {}),
  };
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(`${target.baseUrl}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  return { status: res.status, body: await parseJsonOrNull(res) };
}
