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
// which render as PROSE (evidenceBody / <EvidenceBody>), not a one-line row.
const HIDDEN_KEYS: ReadonlySet<string> = new Set(['item_number', 'body', 'truncated']);

// Digest/long-form body is capped at 4000 chars by the producer; mirror that as a
// defensive display cap (the body scrolls within its container either way).
const MAX_BODY_CHARS = 4000;

export interface EvidenceBodyContent {
  text: string;
  truncated: boolean;
}

/**
 * The multiline prose body of an item's evidence (peer_digest et al.), or null
 * when absent/empty. `truncated` mirrors the producer flag — the body may also
 * end with a literal `…[truncated]` marker; both are honoured (the marker renders
 * inline, the flag drives the "full text in the Brief" affordance). Generic: ANY
 * kind's `evidence.body` surfaces this way. Untrusted display text — the caller
 * renders it as escaped React children, never markup.
 */
export function evidenceBody(evidence: unknown): EvidenceBodyContent | null {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return null;
  const ev = evidence as Record<string, unknown>;
  const raw = ev.body;
  if (typeof raw !== 'string' || raw.trim().length === 0) return null;
  return { text: raw.slice(0, MAX_BODY_CHARS), truncated: ev.truncated === true };
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
