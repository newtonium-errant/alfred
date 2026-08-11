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

import { HOME_INSTANCE_NAME, isHomeInstance } from './instance';
import { STT_IDEMPOTENCY_HEADER } from './schemas';

// The peer/client name the transport knows this front-end by. Must match the
// backend's web peer entry in `auth.tokens`. Config-driven so a backend rename is
// a config change, not a code change. Defaults to "web".
const PEER_CLIENT = process.env.ALFRED_WEB_PEER_CLIENT || 'web';

/** Thrown when required transport env is missing — surfaced as a 500 by the BFF. */
export class TransportConfigError extends Error {}

/** Thrown when a buffered BFF→transport call exceeds its timeout — BFF maps to 504. */
export class TransportTimeoutError extends Error {}

// Buffered BFF→transport timeout (CONTRACT S8). A wedged transport returns a
// clean 504 rather than a hung/dropped socket. Generous default (~60s) vs the
// observed 10–23s turns; env-overridable. Streaming (callTransportStream/
// callChatStream) is EXEMPT — it uses SSE keep-alive, not a turn-length budget.
function transportTimeoutMs(): number {
  const raw = parseInt(process.env.ALFRED_WEB_TRANSPORT_TIMEOUT_MS || '', 10);
  return Number.isFinite(raw) && raw > 0 ? raw : 60000;
}

async function fetchJsonWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const ms = transportTimeoutMs();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (e) {
    if (controller.signal.aborted) {
      throw new TransportTimeoutError(`transport call timed out after ${ms}ms`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

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

  const res = await fetchJsonWithTimeout(`${baseUrl()}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  return { status: res.status, body: await parseJsonOrNull(res) };
}

/**
 * RAW home/session transport GET — returns the fetch Response UNPARSED so the caller
 * can forward a non-JSON body. The C3b audio route (GET /web/brief/audio) returns
 * `audio/mpeg` bytes on a cache hit/render OR a JSON ILB state (no_brief /
 * tts_not_configured / …); the BFF proxy branches on content-type. Home `web` peer
 * token + relayed session token (same auth as callTransport); Accept covers both.
 * Timeout-bounded like the buffered path. DO NOT parseJsonOrNull here (it would
 * consume the audio body).
 *
 * Returns the upstream STATUS verbatim — a raw caller MUST map a `wrong_peer` 401 → 502
 * itself (the B2 fake-logout guard, via bffError.mapUpstreamWrongPeer); this helper does
 * NOT do it (it can't discriminate the body without consuming the stream).
 */
export async function callTransportRaw(method: 'GET', path: string, opts: CallOptions = {}): Promise<Response> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${peerToken()}`,
    'X-Alfred-Client': PEER_CLIENT,
    Accept: 'audio/mpeg, application/json',
  };
  if (opts.sessionToken) {
    headers['X-Alfred-Session'] = opts.sessionToken;
  }
  return fetchJsonWithTimeout(`${baseUrl()}${path}`, { method, headers });
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

// --- Feed surface (Feed Phase B) --------------------------------------------
// The Decide deck / Awareness feed read + act on the HOME instance's own feed —
// the SAME transport as chat (`ALFRED_WEB_TRANSPORT_URL`), but a DISTINCT peer
// token: `web_feed` (decide-cards-only; the transport's (kind, action) map is
// the capability ceiling), NOT the full-chat `web` token. So this mirrors the
// home path (callTransport/peerToken) with a sibling token env — it is NOT a
// per-target family (the feed is single-home-instance, unlike ingest/chat
// targets). Matches the Phase B deploy checklist: "add web_feed token to salem
// config + BFF env" (one BFF env addition).

function feedToken(): string {
  const token = process.env.ALFRED_WEB_FEED_TOKEN;
  if (!token || !token.trim()) {
    throw new TransportConfigError('ALFRED_WEB_FEED_TOKEN is not set');
  }
  return token;
}

/**
 * True when the feed transport env is fully configured — the home transport URL
 * AND the `web_feed` peer token are both present. The BFF checks this at the top
 * of each feed route and returns 503 `not_configured` (deploy-inert) when false,
 * the same fail-closed posture as ingest/shortcut. A SET-but-WRONG token is NOT
 * caught here (it looks configured) — the transport then 401s `feed_wrong_peer`,
 * which the BFF relays as an error (never a silent success).
 */
export function isFeedConfigured(): boolean {
  const url = process.env.ALFRED_WEB_TRANSPORT_URL;
  const token = process.env.ALFRED_WEB_FEED_TOKEN;
  return Boolean(url && url.trim() && token && token.trim());
}

export interface FeedCallOptions {
  /** JSON request body (POST /feed/act). Omit for GET. */
  body?: unknown;
  /** Allowlisted query filters (GET /feed/items): state/mode/kind. */
  query?: Record<string, string>;
}

/**
 * Relay a JSON call to the HOME transport's feed routes using the server-side
 * `web_feed` peer token. GET /feed/items (with optional state/mode/kind query)
 * and POST /feed/act. Buffered + timeout-bounded (a wedged transport → a clean
 * TransportTimeoutError → the BFF's 504); the browser never sees the token.
 */
export async function callTransportFeed(
  method: 'GET' | 'POST',
  path: string,
  opts: FeedCallOptions = {},
): Promise<TransportResult> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${feedToken()}`,
    'X-Alfred-Client': PEER_CLIENT,
    Accept: 'application/json',
  };
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  const qs =
    opts.query && Object.keys(opts.query).length
      ? `?${new URLSearchParams(opts.query).toString()}`
      : '';

  const res = await fetchJsonWithTimeout(`${baseUrl()}${path}${qs}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  return { status: res.status, body: await parseJsonOrNull(res) };
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
  /** Relayed verbatim as the STT idempotency header when present (BFF-allowlisted). */
  idempotencyKey?: string;
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
  if (opts.idempotencyKey) {
    headers[STT_IDEMPOTENCY_HEADER] = opts.idempotencyKey;
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

// --- Bulk scan intake (#83) -------------------------------------------------
// Its OWN dedicated token, NEVER the chat peer token. The box peer-pins
// `web_batch` on POST /vault/batch precisely because the chat `web` token and
// this one can both carry `allowed_clients: [web]` — so reusing the chat token
// here would be refused by the pin, and reusing THIS one for chat would be an
// escalation. One token, one door.
const BATCH_PEER_CLIENT = process.env.ALFRED_WEB_BATCH_PEER_CLIENT || 'web';

function batchToken(): string {
  const token = process.env.ALFRED_WEB_BATCH_TOKEN;
  if (!token) {
    throw new TransportConfigError('ALFRED_WEB_BATCH_TOKEN is not set');
  }
  return token;
}

/** True when the HOME instance is wired for bulk upload (needs BOTH). */
export function isBatchConfigured(): boolean {
  return Boolean(process.env.ALFRED_WEB_TRANSPORT_URL && process.env.ALFRED_WEB_BATCH_TOKEN);
}

// --- Per-instance batch targets (#90) ---------------------------------------
// Mirrors the chat + ingest target families, with its OWN env prefix and its own
// per-instance token:
//   ALFRED_WEB_BATCH_<NAME>_URL    — that instance's transport base URL
//   ALFRED_WEB_BATCH_<NAME>_TOKEN  — that instance's dedicated `web_batch` token
//   ALFRED_WEB_BATCH_<NAME>_LABEL  — (optional) display label; defaults to <NAME>
// Fail-closed: a target counts as configured only when BOTH URL and token are
// present, so a half-finished deploy never appears in the picker.
//
// WHERE THIS DIFFERS FROM THE CHAT FAMILY, deliberately. Chat excludes the home
// instance from its target list because home rides the SESSION path — a
// genuinely different auth mechanism, so relay env for home would be a
// misconfiguration. Batch has no such split: every target, home included, is
// reached with a `web_batch` peer token. So home IS resolvable here, from the
// unprefixed ALFRED_WEB_BATCH_TOKEN pair, and the picker's default. One
// resolution path for every instance is simpler than two, and it is available
// precisely because the auth story is uniform.
const BATCH_ENV_PREFIX = 'ALFRED_WEB_BATCH_';

export interface BatchTargetMeta {
  name: string;
  label: string;
  home: boolean;
}

/**
 * Every batch target the operator can submit to: the home instance (when
 * configured) plus each fully-configured `ALFRED_WEB_BATCH_<NAME>_*` pair.
 * Metadata ONLY — never a URL or token. Home first, then the rest by label.
 *
 * An empty list is a real answer (nothing wired), and the page renders an
 * explicit empty state for it rather than an inert picker.
 */
export function listBatchTargets(): BatchTargetMeta[] {
  const out: BatchTargetMeta[] = [];
  if (isBatchConfigured()) {
    out.push({ name: HOME_INSTANCE_NAME, label: HOME_INSTANCE_NAME, home: true });
  }
  const cross: BatchTargetMeta[] = [];
  const seen = new Set<string>();
  for (const key of Object.keys(process.env)) {
    const m = key.match(/^ALFRED_WEB_BATCH_([A-Z0-9_]+)_URL$/);
    if (!m) continue;
    const name = m[1];
    if (seen.has(name)) continue;
    const url = process.env[`${BATCH_ENV_PREFIX}${name}_URL`];
    const token = process.env[`${BATCH_ENV_PREFIX}${name}_TOKEN`];
    if (!url || !url.trim() || !token || !token.trim()) continue; // fail-closed
    // A per-instance pair naming the HOME instance would shadow the unprefixed
    // home pair with a second source of truth for the same target. Dropped, so
    // "which env did this use?" always has one answer.
    if (name.toUpperCase() === HOME_INSTANCE_NAME.toUpperCase()) continue;
    seen.add(name);
    const label = (process.env[`${BATCH_ENV_PREFIX}${name}_LABEL`] || name).trim();
    cross.push({ name, label, home: false });
  }
  cross.sort((a, b) => a.label.localeCompare(b.label));
  return [...out, ...cross];
}

/**
 * Resolve a batch target name to its server-side URL + token.
 *
 * An empty / home name resolves to the home pair; anything else to that
 * instance's prefixed pair. Throws `TransportConfigError` when the env is
 * missing — the BFF validates the name against `listBatchTargets()` FIRST and
 * answers `unknown_target`, so reaching a throw here means a target that was
 * listed and then could not be resolved, which is a server misconfiguration
 * rather than a client error.
 */
export function resolveBatchTarget(name: string): ResolvedChatTarget {
  if (isHomeInstance(name)) {
    return {
      baseUrl: baseUrl().replace(/\/+$/, ''),
      token: batchToken(),
      client: BATCH_PEER_CLIENT,
    };
  }
  if (!isValidTargetName(name)) {
    throw new TransportConfigError(`invalid batch target name: ${name}`);
  }
  const key = name.toUpperCase();
  const url = process.env[`${BATCH_ENV_PREFIX}${key}_URL`];
  const token = process.env[`${BATCH_ENV_PREFIX}${key}_TOKEN`];
  if (!url || !url.trim()) {
    throw new TransportConfigError(`${BATCH_ENV_PREFIX}${key}_URL is not set`);
  }
  if (!token || !token.trim()) {
    throw new TransportConfigError(`${BATCH_ENV_PREFIX}${key}_TOKEN is not set`);
  }
  return {
    baseUrl: url.replace(/\/+$/, ''),
    token,
    client: BATCH_PEER_CLIENT,
  };
}

export interface BatchCallOptions {
  /** The multipart body, relayed VERBATIM. */
  body: Buffer;
  /** The inbound Content-Type, including its multipart boundary. */
  contentType: string;
  /** Verified display name → X-Alfred-Batch-User (provenance only, never authz). */
  user?: string;
  /**
   * Which instance to submit to (#90). Absent / home → the home pair; any
   * other name → that instance's own URL + `web_batch` token. The BFF has
   * already validated the name against `listBatchTargets()`.
   */
  target?: string;
}

/**
 * Relay a multipart batch submission to the box's `POST /vault/batch`.
 *
 * A BYTE PIPE, deliberately: the BFF does not parse the multipart body, it
 * forwards it unchanged with its original Content-Type (boundary and all).
 * Parsing here would mean a second multipart implementation whose limits could
 * disagree with the box's, and the box is the authority on those limits — it
 * has to be, since it is the only layer an attacker cannot skip.
 *
 * No `fetchJsonWithTimeout`: a 128 MiB upload legitimately outlasts the
 * buffered-call budget, which is sized for chat turns. The route is save-only
 * (no model call), so the box answers as fast as it can write to disk.
 */
export async function callTransportBatch(
  path: string,
  opts: BatchCallOptions,
): Promise<TransportResult> {
  // One resolution path for every instance, home included (#90) — see
  // `resolveBatchTarget` for why batch can do that where chat cannot.
  const resolved = resolveBatchTarget(opts.target || '');
  const headers: Record<string, string> = {
    Authorization: `Bearer ${resolved.token}`,
    'X-Alfred-Client': resolved.client,
    Accept: 'application/json',
    'Content-Type': opts.contentType,
  };
  if (opts.user) {
    headers['X-Alfred-Batch-User'] = opts.user;
  }

  const res = await fetch(`${resolved.baseUrl}${path}`, {
    method: 'POST',
    headers,
    // Re-wrap so the body is a plain ArrayBuffer view — undici rejects a Node
    // Buffer subarray that aliases a larger pool buffer.
    body: new Uint8Array(opts.body),
  });

  return { status: res.status, body: await parseJsonOrNull(res) };
}

// --- In-app bug reporting (#95) ---------------------------------------------
// Its OWN dedicated token, NEVER the chat / ingest / batch peer token. The box
// peer-pins `web_bugreport` on POST /vault/bugreport for the same reason every
// sibling route pins its own: all four tokens can legitimately carry
// `allowed_clients: [web]`, so `allowed_clients` cannot tell them apart, and a
// token that opens two doors is a token that opens the wrong one. One token,
// one door.
//
// SINGLE-HOME, like feed and unlike ingest/chat/batch. A bug report is about
// the app the operator is looking at, and the PWA is one deployment pointed at
// one home instance — so there is no target to pick and no per-instance env
// family here. The report records which instance the reporter was VIEWING in
// its context block; that is metadata about the screen, not a routing decision.
const BUGREPORT_PEER_CLIENT = process.env.ALFRED_WEB_BUGREPORT_PEER_CLIENT || 'web';

function bugReportToken(): string {
  const token = process.env.ALFRED_WEB_BUGREPORT_TOKEN;
  if (!token || !token.trim()) {
    throw new TransportConfigError('ALFRED_WEB_BUGREPORT_TOKEN is not set');
  }
  return token;
}

/**
 * True when the home instance is wired to receive bug reports — needs BOTH the
 * transport URL and the dedicated token.
 *
 * The BFF checks this at the top of the submit route and answers 503
 * `not_configured` when false, so the feature is DEPLOY-INERT: it ships dark
 * and lights up when the operator adds the token. A SET-but-WRONG token is not
 * caught here (it looks configured) — the box then 401s `wrong_peer`, which the
 * BFF relays as an error rather than a silent success.
 */
export function isBugReportConfigured(): boolean {
  const url = process.env.ALFRED_WEB_TRANSPORT_URL;
  const token = process.env.ALFRED_WEB_BUGREPORT_TOKEN;
  return Boolean(url && url.trim() && token && token.trim());
}

export interface BugReportCallOptions {
  /** The JSON report body. */
  body: unknown;
  /** Verified display name → X-Alfred-BugReport-User (provenance only, never authz). */
  user?: string;
}

/**
 * Relay a bug report to the home transport's `POST /vault/bugreport` using the
 * server-side `web_bugreport` peer token. Buffered + timeout-bounded (a wedged
 * transport → a clean TransportTimeoutError → the BFF's 504); the browser never
 * sees the token.
 */
export async function callTransportBugReport(
  path: string,
  opts: BugReportCallOptions,
): Promise<TransportResult> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${bugReportToken()}`,
    'X-Alfred-Client': BUGREPORT_PEER_CLIENT,
    Accept: 'application/json',
    'Content-Type': 'application/json',
  };
  if (opts.user) {
    headers['X-Alfred-BugReport-User'] = opts.user;
  }

  const res = await fetchJsonWithTimeout(`${baseUrl()}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(opts.body),
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

  const res = await fetchJsonWithTimeout(`${target.baseUrl}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  return { status: res.status, body: await parseJsonOrNull(res) };
}

// --- Streaming (SSE) transport helpers (CONTRACT §1 / hardening) -------------
// Return the RAW fetch Response so the BFF can pass `res.body` straight through
// without buffering. Accept: text/event-stream; an AbortSignal tears down the
// relay on client disconnect (the backend detaches and keeps run_turn running —
// decision S4). DO NOT call parseJsonOrNull here (it would buffer the stream).

export interface StreamCallOptions {
  body?: unknown;
  /** Instance-signed session token → X-Alfred-Session (home/session path). */
  sessionToken?: string | null;
  /** Aborts the BFF↔transport relay when the browser disconnects. */
  signal?: AbortSignal;
}

/** Home/session-path SSE relay — injects the home peer token + session token. */
export async function callTransportStream(
  method: 'POST',
  path: string,
  opts: StreamCallOptions = {},
): Promise<Response> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${peerToken()}`,
    'X-Alfred-Client': PEER_CLIENT,
    Accept: 'text/event-stream',
  };
  if (opts.sessionToken) {
    headers['X-Alfred-Session'] = opts.sessionToken;
  }
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  return fetch(`${baseUrl()}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
}

export interface ChatStreamCallOptions {
  body?: unknown;
  /** The verified display name asserted as X-Alfred-User (relay path). */
  userName: string;
  signal?: AbortSignal;
}

/** Cross-instance SSE relay — injects the TARGET peer token + the asserted user. */
export async function callChatStream(
  targetName: string,
  method: 'POST',
  path: string,
  opts: ChatStreamCallOptions,
): Promise<Response> {
  const target = resolveChatTarget(targetName);
  const headers: Record<string, string> = {
    Authorization: `Bearer ${target.token}`,
    'X-Alfred-Client': target.client,
    'X-Alfred-User': opts.userName,
    Accept: 'text/event-stream',
  };
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  return fetch(`${target.baseUrl}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
}

// --- Cross-instance chat target resolution (Model B — trust-the-relay) -------
// Mirrors the ingest target block above, with a DISTINCT env prefix + a DISTINCT
// per-instance peer token. Chat uses the target's `web` peer token (full talker
// scope via run_turn); ingest keeps `web_ingest` (deterministic-create-only) —
// two distinct tokens per instance (decision M4). The BFF is the SOLE holder.
//   ALFRED_WEB_CHAT_<NAME>_URL    — the target transport base URL (loopback)
//   ALFRED_WEB_CHAT_<NAME>_TOKEN  — that target's dedicated `web` peer token
//   ALFRED_WEB_CHAT_<NAME>_LABEL  — (optional) display label; defaults to <NAME>
// A target is "configured" only when BOTH its URL and token are present
// (fail-closed). These are CROSS-INSTANCE relay targets; the HOME instance is
// NOT listed here (it rides the existing session path via callTransport) — the
// BFF synthesises the home entry separately (see web/pages/api/chat/targets.ts).
const CHAT_ENV_PREFIX = 'ALFRED_WEB_CHAT_';

export interface ChatTargetMeta {
  name: string;
  label: string;
}

/**
 * The configured CROSS-INSTANCE chat relay targets, derived from env. Scans every
 * `ALFRED_WEB_CHAT_<NAME>_URL` that also has a matching `_TOKEN`. Returns metadata
 * ONLY (name/label) — never a URL or token. Sorted by label. Empty array when none
 * is configured (single-instance deploys still work — the home target is added by
 * the route layer). The home instance is intentionally excluded here even if an
 * `ALFRED_WEB_CHAT_<HOME>_*` pair is set: the home rides the session path, so its
 * relay env would be a misconfiguration and must not shadow the session route.
 */
export function listCrossInstanceChatTargets(): ChatTargetMeta[] {
  const out: ChatTargetMeta[] = [];
  const seen = new Set<string>();
  for (const key of Object.keys(process.env)) {
    const m = key.match(/^ALFRED_WEB_CHAT_([A-Z0-9_]+)_URL$/);
    if (!m) continue;
    const name = m[1];
    if (seen.has(name)) continue;
    const url = process.env[`${CHAT_ENV_PREFIX}${name}_URL`];
    const token = process.env[`${CHAT_ENV_PREFIX}${name}_TOKEN`];
    if (!url || !url.trim() || !token || !token.trim()) continue; // fail-closed
    seen.add(name);
    const label = (process.env[`${CHAT_ENV_PREFIX}${name}_LABEL`] || name).trim();
    out.push({ name, label });
  }
  out.sort((a, b) => a.label.localeCompare(b.label));
  return out;
}

export interface ResolvedChatTarget {
  baseUrl: string;
  token: string;
  client: string;
}

/**
 * Resolve a cross-instance chat target name to its server-side URL + token.
 * Throws TransportConfigError when the name is malformed or the env pair is
 * missing (→ the BFF maps to a generic 500, leaking no topology). The BFF
 * validates the name against listCrossInstanceChatTargets() FIRST (→ 400 unknown
 * target) so this is the missing-env / misconfig path.
 */
export function resolveChatTarget(name: string): ResolvedChatTarget {
  if (!isValidTargetName(name)) {
    throw new TransportConfigError(`invalid chat target name: ${name}`);
  }
  const key = name.toUpperCase();
  const url = process.env[`${CHAT_ENV_PREFIX}${key}_URL`];
  const token = process.env[`${CHAT_ENV_PREFIX}${key}_TOKEN`];
  if (!url || !url.trim()) {
    throw new TransportConfigError(`${CHAT_ENV_PREFIX}${key}_URL is not set`);
  }
  if (!token || !token.trim()) {
    throw new TransportConfigError(`${CHAT_ENV_PREFIX}${key}_TOKEN is not set`);
  }
  return { baseUrl: url.replace(/\/+$/, ''), token, client: PEER_CLIENT };
}

export interface ChatRelayOptions {
  body?: unknown;
  /** The verified display name asserted to the target as X-Alfred-User (Model B). */
  userName: string;
}

/**
 * Relay a buffered JSON chat call to a CHOSEN cross-instance target. Uses that
 * target's dedicated `web` peer token + base URL, and asserts the verified user
 * via X-Alfred-User (NOT a session token — the target is in relay mode and
 * re-resolves the name against its own web.users). Possession of the target token
 * IS the chat authority (the BFF is the sole holder).
 */
export async function callChatTo(
  targetName: string,
  method: 'GET' | 'POST',
  path: string,
  opts: ChatRelayOptions,
): Promise<TransportResult> {
  const target = resolveChatTarget(targetName);
  const headers: Record<string, string> = {
    Authorization: `Bearer ${target.token}`,
    'X-Alfred-Client': target.client,
    'X-Alfred-User': opts.userName,
    Accept: 'application/json',
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
