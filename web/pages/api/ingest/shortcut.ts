import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { createHash, randomUUID, timingSafeEqual } from 'crypto';
import { SHORTCUT_MAX_TITLE_CHARS, shortcutIngestBodySchema } from '../../../lib/algernon/schemas';
import { callTransportTo, listIngestTargets } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/ingest/shortcut — a BEARER-ONLY capture door for the iOS Shortcuts
// tendril (Action Button → Dictate → POST). Sibling of ingest/submit.ts, but for
// a client with NO durable cookie jar: it authenticates a device token and NEVER
// mints a session.
//
// SECURITY THESIS — SCOPE BY CONSTRUCTION. This route relays to the SAME transport
// POST /vault/ingest via the SAME `web_ingest` peer token as ingest/submit — a peer
// that is DETERMINISTIC-CREATE-ONLY at the transport (web_ingest scope +
// WEB_INGEST_CREATE_TYPES). It can create {note} records and nothing else. So even
// a STOLEN shortcut token inherits exactly that blast radius: it cannot chat, read,
// edit, or delete anything. The shortcut token gates WHO may POST a capture; the
// peer token (BFF-only, never on the device) is the write authority and its scope
// is the hard ceiling.
//
// Gates, in order:
//   1. method (405)
//   2. SHORTCUT_INGEST_TOKEN configured? (503 not_configured — deploy-inert until
//      the operator sets the secret; there is NO cookie fallback, ever)
//   3. bearer token present + CONSTANT-TIME match (401 invalid_token) — no oracle:
//      a missing token and a wrong token return the identical 401
//   4. rate limit (429 rate_limited) — an abuse ceiling, keyed by token-hash
//   5. body shape (zod, 400 invalid_request) — note-only for v1
//   6. target is configured (400 unknown_target) — checked BEFORE any env lookup
//      so a bogus target can't probe server env
// Transport/config failures map via sendTransportError (no topology leak).
//
// OPERATOR SETUP: set SHORTCUT_INGEST_TOKEN (a long random secret) in the web
// server env; the Shortcut sends `Authorization: Bearer <token>` and a JSON body
// `{ text, title?, record_type?, target? }`. The Shortcut recipe is operator-owned.

// Public (non-secret) instance display name stamped into provenance — parameterised
// (NOT a hardcoded literal) so a different deploy stamps its own origin.
const ORIGIN_INSTANCE = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';
// Provenance label for shortcut captures. Overridable so a non-Salem deploy stamps
// its own owner; defaults to the Salem operator.
const INGESTED_BY = process.env.SHORTCUT_INGEST_USER || 'Andrew (shortcut)';
// Default ingest target when the Shortcut omits one. Overridable; matched
// case-insensitively against the configured targets (a hand-typed / defaulted name).
const DEFAULT_TARGET = process.env.SHORTCUT_INGEST_DEFAULT_TARGET || 'salem';

// --- Rate limiter (fixed window, in-memory) ---------------------------------
// An abuse ceiling for a single-operator capture path — NOT a product feature.
// Default 30 captures/hour per token-hash (operator-tunable via env).
//
// NOT the #38 shape (and it must not be): #38's per-EMAIL keyed lockout store was
// floodable — an attacker varying the email could grow/evict the map and knock out
// a real user's live counter. Here the limiter runs AFTER the constant-time auth
// gate, so the ONLY key it ever sees is the hash of the one valid token: the Map
// holds ≤1 entry and cannot be flooded into eviction or unbounded growth (a wrong
// token is 401'd before it can touch this map). Per-process, in-memory — fine for
// the single `next start` server behind the tunnel; a multi-replica deploy would
// need a shared store (out of scope, and not the current topology).
const RATE_WINDOW_MS = 60 * 60 * 1000;

type RateBucket = { windowStart: number; count: number };
const rateBuckets = new Map<string, RateBucket>();

function rateLimitMax(): number {
  const raw = parseInt(process.env.SHORTCUT_INGEST_RATE_MAX || '', 10);
  return Number.isFinite(raw) && raw > 0 ? raw : 30;
}

function rateLimitOk(key: string, now: number): boolean {
  const b = rateBuckets.get(key);
  if (!b || now - b.windowStart >= RATE_WINDOW_MS) {
    rateBuckets.set(key, { windowStart: now, count: 1 });
    return true;
  }
  if (b.count >= rateLimitMax()) return false;
  b.count += 1;
  return true;
}

/** Test-only: reset the in-memory limiter between cases. */
export function __resetShortcutRateLimitForTest(): void {
  rateBuckets.clear();
}

/**
 * Test-only: number of live rate-limit buckets. Pins the anti-flood invariant —
 * distinct WRONG tokens must never populate the Map (they're 401'd before the
 * limiter runs), so a flood of them leaves this at 0. Reddens if the limiter is
 * ever ordered before the auth gate (the #38 flood vector).
 */
export function __shortcutRateLimitBucketCountForTest(): number {
  return rateBuckets.size;
}

/**
 * Test-only: the effective per-window ceiling the limiter uses. Pins the
 * garbage-env fallback — a non-positive / non-numeric SHORTCUT_INGEST_RATE_MAX
 * must degrade to the default 30, never to 0 / NaN / a negative (which would
 * make the limiter trip immediately or never).
 */
export function __shortcutRateLimitMaxForTest(): number {
  return rateLimitMax();
}

// --- constant-time bearer check ---------------------------------------------
function extractBearer(req: NextApiRequest): string | null {
  const h = req.headers['authorization'];
  if (typeof h !== 'string') return null;
  const m = /^Bearer\s+(.+)$/i.exec(h.trim());
  return m ? m[1].trim() : null;
}

// Compare via SHA-256 digests so timingSafeEqual gets equal-length buffers (it
// throws on a length mismatch) AND the comparison leaks neither the token nor its
// length. A standard constant-time credential compare for a high-entropy secret.
function tokensMatch(provided: string, expected: string): boolean {
  const a = createHash('sha256').update(provided).digest();
  const b = createHash('sha256').update(expected).digest();
  return timingSafeEqual(a, b);
}

function tokenHash(token: string): string {
  return createHash('sha256').update(token).digest('hex');
}

// --- title derivation --------------------------------------------------------
function halifaxStamp(d: Date): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Halifax',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}${get('minute')}`;
}

// Missing title → first ~8 words of the capture + " — voice capture <stamp>"
// (America/Halifax). Bounded to the transport's 300-char title ceiling.
function deriveTitle(text: string, now: Date): string {
  const lead = text.trim().split(/\s+/).slice(0, 8).join(' ') || 'Voice capture';
  return `${lead} — voice capture ${halifaxStamp(now)}`.slice(0, SHORTCUT_MAX_TITLE_CHARS);
}

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const expected = process.env.SHORTCUT_INGEST_TOKEN;
  if (!expected || !expected.trim()) {
    // Deploy-inert until the operator sets the secret. Fail CLOSED.
    console.warn('[bff:ingest/shortcut] reject reason=not_configured');
    return res.status(503).json({ error: 'not_configured' });
  }

  const provided = extractBearer(req);
  // Single 401 for both missing and mismatched — no oracle. The constant-time
  // compare runs whenever a token was actually presented.
  if (!provided || !tokensMatch(provided, expected)) {
    console.warn(`[bff:ingest/shortcut] reject reason=invalid_token present=${provided !== null}`);
    return res.status(401).json({ error: 'invalid_token' });
  }

  if (!rateLimitOk(tokenHash(provided), Date.now())) {
    console.warn('[bff:ingest/shortcut] reject reason=rate_limited');
    return res.status(429).json({ error: 'rate_limited' });
  }

  const parsed = shortcutIngestBodySchema.safeParse(req.body);
  if (!parsed.success) {
    console.warn(
      `[bff:ingest/shortcut] reject reason=invalid_request issues=${parsed.error.issues.length}`,
    );
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { text, title, target } = parsed.data;
  // record_type is note-only for v1 (schema enum). Pin it here so the relayed
  // value is unambiguous whether or not the client sent the field.
  const record_type = 'note';

  // Resolve the target case-insensitively against the CONFIGURED set (the Shortcut
  // sends a human-typed name or relies on the default). Checked BEFORE any env
  // lookup so a bogus target can't probe server env.
  const requested = (target || DEFAULT_TARGET).trim();
  const match = listIngestTargets().find((t) => t.name.toLowerCase() === requested.toLowerCase());
  if (!match) {
    console.warn('[bff:ingest/shortcut] reject reason=unknown_target');
    return res.status(400).json({ error: 'unknown_target' });
  }

  const now = new Date();
  const finalTitle = title ?? deriveTitle(text, now);

  try {
    const { status, body: respBody } = await callTransportTo(match.name, 'POST', '/vault/ingest', {
      body: {
        record_type,
        title: finalTitle,
        body: text,
        source: 'iOS Shortcut',
        ingested_by: INGESTED_BY,
        ingested_at: now.toISOString(),
        correlation_id: randomUUID(),
        set_fields: { ingested_via: 'shortcut', origin_instance: ORIGIN_INSTANCE },
      },
      headers: { 'X-Alfred-Ingest-User': INGESTED_BY },
    });
    console.log(
      `[bff:ingest/shortcut] accept target=${match.name} record_type=note ` +
        `text_chars=${text.length} title_chars=${finalTitle.length} upstream=${status}`,
    );
    return res.status(status).json(respBody ?? {});
  } catch (e) {
    return sendTransportError(res, 'ingest/shortcut', e);
  }
}
