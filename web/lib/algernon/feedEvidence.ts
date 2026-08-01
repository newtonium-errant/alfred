// Defensive flattening of a FeedItem's RAW `evidence` dict for display.
//
// SECURITY: evidence is an unschema'd backend to_dict payload — untrusted
// DISPLAY data. It is only ever rendered as React text children (auto-escaped),
// NEVER via dangerouslySetInnerHTML and NEVER as an href/src (the #22 stored-XSS
// precedent). This helper coerces arbitrary values to safe display strings; the
// card renders the returned {label, value} rows as plain text.

export interface EvidenceRow {
  key: string;
  value: string;
}

// Keys hidden from the key:value rows: internal plumbing, plus `body`/`truncated`
// which render as PROSE (evidenceBody / <EvidenceBody>), not a one-line row. The
// email-tier fields (#26) are hidden too: `body`/`body_truncated` drive the prose +
// "more" cue, `gmail_url` renders as the "Open in Gmail" anchor, and `message_id` is
// internal plumbing (the raw id, not display) — none belong as a raw key:value row.
const HIDDEN_KEYS: ReadonlySet<string> = new Set([
  'item_number',
  'body',
  'truncated',
  'body_truncated',
  'message_id',
  'gmail_url',
  // #27 email_urgent: `high_source` renders as the on-face provenance chip
  // ("Priority sender" / "Classifier: high"), never as a raw key:value row.
  'high_source',
]);

// Digest/long-form body is capped at 4000 chars by the producer; mirror that as a
// defensive display cap (the body scrolls within its container either way).
const MAX_BODY_CHARS = 4000;

export interface EvidenceBodyContent {
  text: string;
  truncated: boolean;
}

/**
 * The multiline prose body of an item's evidence (peer_digest, email_tier, et al.),
 * or null when absent/empty. Truncation is flagged by EITHER `truncated` (peer_digest)
 * OR `body_truncated` (email_tier, #26) — both mean "the body prose was clipped"; the
 * body may also end with a literal `…[truncated]` marker (honoured inline). Generic:
 * ANY kind's `evidence.body` surfaces this way. Untrusted display text — the caller
 * renders it as escaped React children, never markup.
 */
export function evidenceBody(evidence: unknown): EvidenceBodyContent | null {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return null;
  const ev = evidence as Record<string, unknown>;
  const raw = ev.body;
  if (typeof raw !== 'string' || raw.trim().length === 0) return null;
  const truncated = ev.truncated === true || ev.body_truncated === true;
  return { text: raw.slice(0, MAX_BODY_CHARS), truncated };
}

// The one host a feed-evidence external link may point at: the server-built Gmail
// deep-link (#26). Kept as a strict prefix allowlist — see evidenceExternalLink.
const GMAIL_URL_PREFIX = 'https://mail.google.com/';

export interface EvidenceExternalLink {
  href: string;
  label: string;
}

/**
 * A safe external link for the evidence surface, or null. The ONLY current source is
 * the email-tier `gmail_url` (#26) — a SERVER-built deep-link the card renders as an
 * "Open in Gmail" anchor.
 *
 * SECURITY (the deliberate, scoped exception to the #22 no-href-from-item-data rule,
 * blessed by team-lead): the href is rendered VERBATIM — never reconstructed client-side
 * — and is gated by a strict `https://mail.google.com/` prefix allowlist. Anything else
 * (a javascript:/data: scheme, another host, a non-string) → null → NO anchor rendered.
 * So a hostile or malformed value can never become a clickable href.
 */
export function evidenceExternalLink(evidence: unknown): EvidenceExternalLink | null {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return null;
  const raw = (evidence as Record<string, unknown>).gmail_url;
  if (typeof raw !== 'string' || !raw.startsWith(GMAIL_URL_PREFIX)) return null;
  return { href: raw, label: 'Open in Gmail' };
}

/**
 * Does this evidence come from an email_tier card (#26)? Email always carries
 * sender + subject + classifier_priority (email_section.to_dict emits them
 * unconditionally); peer_digest / other kinds never do. Used to keep the truncation
 * copy HONEST when there's no Gmail deep-link: a truncated email with no message_id
 * (⟹ blank gmail_url ⟹ no link) has no linkable full text and is NOT "in the Brief"
 * (the Brief never renders email bodies) — so it must not fall to the generic Brief
 * copy. Key-presence, not value (an email may have an empty subject/classifier).
 */
export function isEmailEvidence(evidence: unknown): boolean {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return false;
  const ev = evidence as Record<string, unknown>;
  return 'sender' in ev && 'subject' in ev && 'classifier_priority' in ev;
}

// A compact, human-ish label for an evidence key (snake_case → Title words).
export function evidenceLabel(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

// Coerce ANY value to a bounded, single-line display string. Objects/arrays are
// JSON-stringified (bounded); primitives are String()'d; nullish → ''. Never
// throws (a circular ref falls back to the type name), never returns markup.
export function coerceEvidenceValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return `[${typeof value}]`;
  }
}

const MAX_ROWS = 12;
const MAX_VALUE_CHARS = 400;

/**
 * Flatten an evidence dict into bounded display rows (empty values + hidden
 * plumbing keys dropped). Order-stable (insertion order), capped so a pathological
 * payload can't blow up the card. Pure + DOM-free → unit-testable.
 */
export function evidenceRows(evidence: unknown): EvidenceRow[] {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return [];
  const rows: EvidenceRow[] = [];
  for (const [key, raw] of Object.entries(evidence as Record<string, unknown>)) {
    if (HIDDEN_KEYS.has(key)) continue;
    const value = coerceEvidenceValue(raw).trim();
    if (!value) continue;
    rows.push({ key, value: value.slice(0, MAX_VALUE_CHARS) });
    if (rows.length >= MAX_ROWS) break;
  }
  return rows;
}
