// Defensive flattening of a FeedItem's RAW `evidence` dict for display.
//
// SECURITY: evidence is an unschema'd backend to_dict payload — untrusted
// DISPLAY data. It is only ever rendered as React text children (auto-escaped),
// NEVER via dangerouslySetInnerHTML and NEVER as an href/src (the #22 stored-XSS
// precedent). This helper coerces arbitrary values to safe display strings; the
// card renders the returned {label, value} rows as plain text.

/** One entry of a LIST-valued evidence row, split for display. */
export interface EvidenceListEntry {
  /** The record name — a vault path's basename with its `.md` dropped — else the raw entry. */
  name: string;
  /** The path's leading directory (`'note/'`), or `''` when the entry carries none. */
  prefix: string;
}

export interface EvidenceList {
  /** The entries to draw, capped for display. */
  entries: EvidenceListEntry[];
  /** How many the value ACTUALLY held, so the card can say "+N more" instead of dropping silently. */
  total: number;
}

export interface EvidenceRow {
  key: string;
  value: string;
  /**
   * Present when the raw value was an array of display strings — the card draws
   * one entry per line instead of a JSON blob. Optional: a surface that ignores
   * it renders `value` exactly as it always did.
   */
  list?: EvidenceList;
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
  // The deck rotation's proposal-learning key (`task|due:n|t2`) — the machine
  // coordinate the act path scores rulings under. Plumbing, not information:
  // the operator-facing halves (`proposed_slot`, `proposed_rule`) stay visible
  // rows, because "why this guess" is honest context and this string is not.
  'proposal_shape',
  // #63a attribution: `contested` drives the card's TIER (a contested inference
  // sits under needs-you rather than in the glance pile) and the presence of the
  // "Not right" door. It is plumbing, not information.
  //
  // Hidden in BOTH states, and the false case is the reason this entry exists:
  // `coerceEvidenceValue(false)` is the STRING "false", which is truthy and so
  // survives the empty-value filter — every uncontested attribution card would
  // otherwise carry a "Contested: false" row saying nothing.
  'contested',
  // The MOC reader's plumbing (2026-08-20). Same class as `proposal_shape`
  // above: keys the CLIENT needs and the operator does not.
  //
  // ENUMERATED, not sampled — the producer stamps 14 evidence keys and each
  // was classified against its readers. The other 11 stay VISIBLE because
  // they are the suggester's case for itself, which is exactly what a
  // proposal card owes the operator: `reasoning`, `mapping_signal`,
  // `mapping_score`, `cluster_tags`, `cluster_size`, `members`,
  // `applicable_count`, `ineligible_count`, `already_applied_count`,
  // `partial_retry`, `last_apply_error`.
  //
  // `suggestion_id` — the queue row's internal id. Plumbing by definition.
  'suggestion_id',
  // `moc_choices` — the hold selector's option list. It RENDERS, as the
  // sheet; a raw row would print the same options a second time as JSON.
  'moc_choices',
  // `proposed_target` — the act path's scoring coordinate. Judgement call,
  // and it goes the same way as `proposal_shape` for a different reason:
  // not that the value is machine noise, but that the operator already
  // reads it in HUMAN form on the card's own face ("Add 2 notes to Roman
  // Philosophy MOC?"). A `MOC/Roman Philosophy MOC.md` row would restate
  // the title in a machine spelling. Suppressed rather than relabelled
  // because a relabelled row would be a SECOND rendering of the title.
  'proposed_target',
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
const MAX_LIST_ENTRIES = 8;
const MAX_ENTRY_CHARS = 200;

/**
 * Split a list entry into the parts a card wants to draw at different weights.
 *
 * The entries this exists for are VAULT RECORD PATHS (`note/Some Record.md`), so
 * the name is the part that identifies the record and the directory is provenance
 * — the same split the deck's correction picker already draws (item text loud,
 * `record` quiet). An entry that isn't a path degrades to `prefix: ''` and the
 * whole string as the name: nothing is invented, and nothing is dropped.
 */
function splitListEntry(entry: string): EvidenceListEntry {
  const trimmed = entry.trim().slice(0, MAX_ENTRY_CHARS);
  const cut = trimmed.lastIndexOf('/');
  const prefix = cut >= 0 ? trimmed.slice(0, cut + 1) : '';
  let name = cut >= 0 ? trimmed.slice(cut + 1) : trimmed;
  if (name.toLowerCase().endsWith('.md')) name = name.slice(0, -3);
  // A trailing-slash entry ('note/') would otherwise render as a blank line —
  // an entry that shows nothing is worse than one that shows its raw self.
  if (!name) return { name: trimmed, prefix: '' };
  return { name, prefix };
}

/**
 * The LIST view of a raw evidence value, or null when the value isn't one.
 *
 * Strict on purpose: an array of non-empty strings and nothing else. A mixed or
 * numeric array (`[1,2]`) keeps its JSON rendering, which is already legible at
 * that size — the defect this addresses is a long array of long strings crammed
 * into a narrow column, not the presence of brackets as such.
 */
export function evidenceList(value: unknown): EvidenceList | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  if (!value.every((e) => typeof e === 'string' && e.trim().length > 0)) return null;
  return {
    entries: (value as string[]).slice(0, MAX_LIST_ENTRIES).map(splitListEntry),
    total: value.length,
  };
}

/**
 * Flatten an evidence dict into bounded display rows (empty values + hidden
 * plumbing keys dropped). Order-stable (insertion order), capped so a pathological
 * payload can't blow up the card. Pure + DOM-free → unit-testable.
 *
 * `value` is ALWAYS populated, including for list-valued rows: it is the flat
 * fallback every existing consumer (feed row, rings header, slot board) still
 * renders. `list` is additive — a surface adopts it by reading it, and one that
 * doesn't is unchanged.
 */
export function evidenceRows(evidence: unknown): EvidenceRow[] {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return [];
  const rows: EvidenceRow[] = [];
  for (const [key, raw] of Object.entries(evidence as Record<string, unknown>)) {
    if (HIDDEN_KEYS.has(key)) continue;
    const value = coerceEvidenceValue(raw).trim();
    if (!value) continue;
    const list = evidenceList(raw);
    rows.push({ key, value: value.slice(0, MAX_VALUE_CHARS), ...(list ? { list } : {}) });
    if (rows.length >= MAX_ROWS) break;
  }
  return rows;
}
