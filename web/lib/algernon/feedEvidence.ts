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

// Keys that are noise on a card (internal plumbing) — hidden from the expand.
const HIDDEN_KEYS: ReadonlySet<string> = new Set(['item_number']);

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
