import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { createHash, randomUUID, timingSafeEqual } from 'crypto';
import { SHORTCUT_MAX_TITLE_CHARS, shortcutIngestBodySchema } from '../../../lib/algernon/schemas';
import { callTransportTo, listIngestTargets } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';
import { loadDirectiveAliases, matchLeadingDirective } from '../../../lib/algernon/shortcutDirective';

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
// VOICE-DIRECTIVE ROUTING (deterministic, no LLM): a capture that OPENS with a
// directive ("message for Hypatia, …", "for Hypatia: …", "Hypatia: …") to a
// KNOWN + CONFIGURED instance is re-homed to that target and the prefix stripped;
// see `lib/algernon/shortcutDirective.ts`. Fail-safe by construction — an unknown
// name (or a recognised-but-unconfigured one) keeps the words fully INTACT in the
// default inbox, so a misheard name never loses content. Aliases (STT mangling)
// are config-layer via SHORTCUT_DIRECTIVE_ALIASES.
//
// OPERATOR SETUP: set SHORTCUT_INGEST_TOKEN (a long random secret) in the web
// server env; the Shortcut sends `Authorization: Bearer <token>` and a JSON body
// `{ text, title?, record_type?, target? }`. The Shortcut recipe is operator-owned.
// NON-iOS TENDRILS: nothing here is iOS-specific — any client that can send a
// bearer POST (Android Tasker, a desktop hotkey) uses this same door. Set
// SHORTCUT_INGEST_SOURCE_LABEL so its captures stamp their TRUE origin instead of
// inheriting the 'iOS Shortcut' default.

// --- operator-tunable env, one contract for all of it ------------------------
// Every value below is read PER-REQUEST and follows the same shape:
//   process.env.X?.trim() || <default>
//
// The trim is not decoration. A bare `||` only catches '' — a whitespace-only
// value is TRUTHY, so `SHORTCUT_INGEST_USER='   '` would sail through and stamp
// a blank `ingested_by` AND send a blank `X-Alfred-Ingest-User` (an asserted
// identity header on a privileged relay). Same failure the source label had
// before #27; fixing one field and leaving its neighbours is how that bug
// survives. Unset behaviour is byte-identical to the module-const form these
// replaced. Per-request rather than module-scope so each is settable in tests
// without module-registry gymnastics — same reasoning as rateLimitMax().

// Public (non-secret) instance display name stamped into provenance —
// parameterised (NOT a hardcoded literal) so a different deploy stamps its own.
function originInstance(): string {
  return process.env.NEXT_PUBLIC_INSTANCE_NAME?.trim() || 'Algernon';
}

// Who the capture is attributed to. Rides BOTH the relayed `ingested_by` and the
// `X-Alfred-Ingest-User` header, so a blank here is a blank asserted identity.
function ingestedBy(): string {
  return process.env.SHORTCUT_INGEST_USER?.trim() || 'Andrew (shortcut)';
}

// Default ingest target when the Shortcut omits one. Matched case-insensitively
// against the configured targets.
function defaultTarget(): string {
  return process.env.SHORTCUT_INGEST_DEFAULT_TARGET?.trim() || 'salem';
}

// Provenance label stamped into the record's `source:` frontmatter. The route is
// named for the iOS Shortcuts tendril it was built for, but the wire contract is
// just "bearer POST" — an Android Tasker recipe or a desktop hotkey hits the SAME
// door. A hardcoded 'iOS Shortcut' would stamp every one of those with a false
// origin, on a field the operator is meant to trust. Default preserved, so an
// unset env is byte-identical to the pre-parameterised behavior.
function sourceLabel(): string {
  return process.env.SHORTCUT_INGEST_SOURCE_LABEL?.trim() || 'iOS Shortcut';
}

/**
 * The configured device secret, whitespace-normalised.
 *
 * A secret gets NO fallback default — the fail-closed 503 is the whole point of
 * the not_configured gate. What it does need is trim SYMMETRY with
 * `extractBearer`, which trims the token it parses off the header: without it,
 * a `SHORTCUT_INGEST_TOKEN` carrying a stray trailing space or newline (routine
 * in .env files, copy-paste and mounted secrets) can NEVER match a correctly
 * sent token, and the door answers a permanent 401 that reads exactly like a
 * wrong credential. Returns '' when unset or blank so the caller's guard fires.
 */
function expectedToken(): string {
  return process.env.SHORTCUT_INGEST_TOKEN?.trim() || '';
}

/**
 * Test-only: the effective provenance label. Pins the garbage-env fallback at the
 * unit level; the relayed-body pins in the route tests are the load-bearing ones.
 */
export function __shortcutSourceLabelForTest(): string {
  return sourceLabel();
}

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

  const expected = expectedToken();
  if (!expected) {
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

  // Leading voice-directive routing (deterministic, no LLM). If the capture
  // OPENS with a directive to a KNOWN + CONFIGURED instance, re-home it there and
  // strip the prefix. A recognised-but-unconfigured name keeps the text INTACT in
  // the default inbox + logs the intent (for the re-homing loop). An unknown name
  // is not a directive → intact. Fail-safe: a misheard name never loses words.
  const directive = matchLeadingDirective(text, loadDirectiveAliases());
  let requested = (target || defaultTarget()).trim();
  let bodyText = text;
  // Resolved ONCE and reused by both provenance shapes below — the plain label and
  // the spoken-form variant must never be able to drift apart (they were two
  // independent hardcoded copies of the same literal before parameterisation).
  const label = sourceLabel();
  let source = label;
  let routedViaDirective = false;
  if (directive) {
    const directiveTarget = listIngestTargets().find(
      (t) => t.name.toLowerCase() === directive.canonical.toLowerCase(),
    );
    if (directiveTarget) {
      requested = directiveTarget.name;
      bodyText = directive.rest;
      // spokenForm is already whitespace-collapsed + length-bounded by the parser
      // (SPOKEN_FORM_MAX_CHARS) — provenance only; `rest` rides verbatim.
      source = `${label} (spoken: "${directive.spokenForm}")`;
      routedViaDirective = true;
      if (directive.spokenFormTruncated) {
        // A bounded provenance string is a LOSSY transform — say so rather than
        // silently shortening what the operator said.
        console.warn(
          `[bff:ingest/shortcut] spoken_form_truncated target=${directiveTarget.name} ` +
            `kept_chars=${directive.spokenForm.length}`,
        );
      }
    } else {
      console.warn(
        `[bff:ingest/shortcut] directive_target_unconfigured target=${directive.canonical}`,
      );
    }
  }

  // Resolve the (possibly directive-overridden) target case-insensitively against
  // the CONFIGURED set. Checked BEFORE any env lookup so a bogus target can't
  // probe server env.
  const match = listIngestTargets().find((t) => t.name.toLowerCase() === requested.toLowerCase());
  if (!match) {
    console.warn('[bff:ingest/shortcut] reject reason=unknown_target');
    return res.status(400).json({ error: 'unknown_target' });
  }

  const now = new Date();
  const finalTitle = title ?? deriveTitle(bodyText, now);
  // Resolved ONCE and used for BOTH the relayed field and the asserted-identity
  // header — they name the same person and must not be able to disagree.
  const ingester = ingestedBy();

  try {
    const { status, body: respBody } = await callTransportTo(match.name, 'POST', '/vault/ingest', {
      body: {
        record_type,
        title: finalTitle,
        body: bodyText,
        source,
        ingested_by: ingester,
        ingested_at: now.toISOString(),
        correlation_id: randomUUID(),
        set_fields: {
          ingested_via: 'shortcut',
          origin_instance: originInstance(),
          ...(routedViaDirective ? { routed_via: 'directive' } : {}),
        },
      },
      headers: { 'X-Alfred-Ingest-User': ingester },
    });
    console.log(
      `[bff:ingest/shortcut] accept target=${match.name} record_type=note ` +
        `text_chars=${bodyText.length} title_chars=${finalTitle.length} ` +
        `directive=${routedViaDirective} upstream=${status}`,
    );
    return res.status(status).json(respBody ?? {});
  } catch (e) {
    return sendTransportError(res, 'ingest/shortcut', e);
  }
}
