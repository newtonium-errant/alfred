import type { FeedItem } from './feed';
import { ringItemSuggested, ringTierOf } from './rings';

// Feed / deck constants + pure helpers. Kept OUT of the components so the
// gesture math, the per-kind verb map, and the display labels are unit-testable
// without a DOM, and so per-user vocabulary (rings labels, kind names) lives in
// ONE place rather than scattered through JSX (team-lead's constants-file rule).

// --- swipe geometry (ported from the ratified deck sketch) -------------------
export const SWIPE_X_THRESHOLD = 90; // |dx| past this on release → affirm/reject
export const PARK_Y_THRESHOLD = 80; // upward dy past this (with small dx) → park
export const PARK_X_TOLERANCE = 70; // park only when |dx| is under this
export const DRAG_Y_CLAMP = 30; // downward drag is clamped (cards don't fall)
export const STAMP_FADE_START = 40; // px of drag before a verdict stamp appears
export const STAMP_FADE_RANGE = 60; // px over which it fades fully in

// Delayed-act window for LIGHT kinds: the undo toast lifetime. The POST fires
// when this expires OR the next commit lands — never an un-act.
export const UNDO_MS = 3500;

export type Verdict = 'affirm' | 'reject' | 'park' | null;

/**
 * The verdict a drag release resolves to — the SAME thresholds the sketch used.
 * Park wins on a mostly-vertical upward flick; otherwise a horizontal past the
 * threshold is affirm (right) / reject (left); anything short springs back (null).
 * Pure + DOM-free so the threshold contract is unit-pinned.
 */
export function verdictForDrag(dx: number, dy: number): Verdict {
  if (dy < -PARK_Y_THRESHOLD && Math.abs(dx) < PARK_X_TOLERANCE) return 'park';
  if (dx > SWIPE_X_THRESHOLD) return 'affirm';
  if (dx < -SWIPE_X_THRESHOLD) return 'reject';
  return null;
}

/**
 * Whether a drag VERDICT actually maps to an action for these verbs — so the deck can
 * SPRING BACK (resetVisual) on a no-op swipe instead of leaving the card stuck half-
 * dragged. An ACK-only kind (reject: null, e.g. email_urgent) reject → false; an
 * affirm-less kind affirm → false; park is always available. (#16 item 12.)
 */
export function swipeActsFor(verbs: DeckVerbs | null, verdict: Verdict): boolean {
  if (verdict === 'affirm') return !!verbs?.affirm;
  if (verdict === 'reject') return !!(verbs?.reject || verbs?.rejectParks);
  return verdict === 'park';
}

/** Verdict-stamp opacity during a drag (0..1), mirroring the sketch's fade. */
export function stampOpacity(distance: number): number {
  if (distance <= STAMP_FADE_START) return 0;
  return Math.min((distance - STAMP_FADE_START) / STAMP_FADE_RANGE, 1);
}

// --- per-kind deck verbs -----------------------------------------------------
// Maps a decide-mode kind to the action_id its affirm (right / ✓) and reject
// (left / ✕) swipes POST. `null` = that direction is unavailable for the kind
// (e.g. pending has no reject — only "noted"). These action_ids MUST be members
// of the B1 transport FEED_ACTIONS map for the kind — the deck is a simplified
// 2-way surface over the same capability ceiling (richer tier calibration stays
// on the reply grammar). Unmapped kinds render but expose only Park.
export interface DeckVerbs {
  affirm: string | null;
  reject: string | null;
  affirmLabel: string;
  rejectLabel: string;
  heavy: boolean;
  /**
   * C2 skip=park: the LEFT (reject) gesture is a CLIENT-side park (no POST), not a
   * decline — there's no backend decline path for slots v1, so a skipped candidate
   * may resurface. `reject` stays null (no action_id); useDeck routes the gesture to
   * park() when this is set. Copy must never promise "won't show again."
   */
  rejectParks?: boolean;
}

// Heavy kinds create/mutate a durable record → a right-swipe does NOT commit;
// it reveals a confirm-tap stage (two-step). Recurrence is future-proofed
// (arrives with the board work); proposal is live today.
export const HEAVY_KINDS: ReadonlySet<string> = new Set(['proposal', 'recurrence']);

export const DECK_VERBS: Record<string, DeckVerbs> = {
  email_tier: { affirm: 'confirm', reject: 'spam', affirmLabel: 'Confirm', rejectLabel: 'Spam', heavy: false },
  // #27 email_urgent — the INTERRUPT card. ACK-only: right/✓ acknowledges (POST "ack" →
  // acted → flip); no reject/left (re-tier lives on the calibration card — two cards, two
  // claims); park works as ever (kind-generic). Having this entry is what makes
  // isDeckDealt(email_urgent) true → the C2-era generic deck/needs-you paths deal + count it.
  email_urgent: { affirm: 'ack', reject: null, affirmLabel: 'Got it', rejectLabel: '', heavy: false },
  attribution: { affirm: 'confirm', reject: 'reject', affirmLabel: 'Confirm', rejectLabel: 'Reject', heavy: false },
  routine_match: { affirm: 'confirm', reject: 'reject', affirmLabel: "That's it", rejectLabel: 'No', heavy: false },
  proposal: { affirm: 'confirm', reject: 'reject', affirmLabel: 'Confirm', rejectLabel: 'Reject', heavy: true },
  recurrence: { affirm: 'confirm', reject: 'reject', affirmLabel: 'Promote', rejectLabel: 'Reject', heavy: true },
  pending: { affirm: 'noted', reject: null, affirmLabel: 'Noted', rejectLabel: '', heavy: false },
  // C2 slot candidate: Accept (right, POST "accept") / Skip (left, client-park, no
  // POST — rejectParks) / Park (up). Only SUGGESTED slots are dealt (see isDeckDealt).
  slot_suggestion: { affirm: 'accept', reject: null, affirmLabel: 'Take it', rejectLabel: 'Skip', heavy: false, rejectParks: true },
};

/** Deck verbs for a kind, or null when the kind has no deck action mapping. */
export function deckVerbsFor(kind: string): DeckVerbs | null {
  return Object.prototype.hasOwnProperty.call(DECK_VERBS, kind) ? DECK_VERBS[kind] : null;
}

/**
 * Whether a feed item is DEALT to the swipe deck — the ONE predicate the deck's
 * dealing AND the deck link/pill count both use, so "the link counts exactly what
 * the deck deals" holds (never hand-roll a divergent count). A classic decision kind
 * is dealt when it has a wired verb; a slot_suggestion is dealt ONLY when SUGGESTED
 * (planned/done slots are worklist/glance items, never deck cards). A suggested slot
 * is legitimately BOTH deck-dealt (the gesture surface) AND inline-actionable (the
 * feed row / rings panel worklist) — same item, two surfaces.
 */
export function isDeckDealt(item: FeedItem): boolean {
  if (item.kind === 'slot_suggestion') return ringItemSuggested(item);
  return deckVerbsFor(item.kind) !== null;
}

// --- email-tier priority (on-face tier badge + dynamic affirm label) ----------
// The email-tier feed producer stamps the classifier's assigned tier as
// `evidence.classifier_priority` (VERIFIED against daily_sync/email_section.py —
// NOT `priority`; the value is lowercase high/medium/low/spam). ALL FOUR real
// tiers — spam included — surface as a badge + a "Confirm HIGH"-style verb, so a
// spam-classified email shows what it was tagged rather than asking for a blind
// confirm (operator ruling 2026-07-31: face honesty especially for spam; that
// both verbs then write spam is a legible quirk, not a UI lie). Empty / anything
// unrecognised → no badge, plain verb.
export const EMAIL_PRIORITY_TIERS = ['low', 'medium', 'high', 'spam'] as const;
export type EmailPriority = (typeof EMAIL_PRIORITY_TIERS)[number];

/** The assigned priority tier of an email_tier item, or null (no badge, plain verb). */
export function emailPriority(item: FeedItem): EmailPriority | null {
  if (item.kind !== 'email_tier') return null;
  const raw = (item.evidence as Record<string, unknown> | null | undefined)?.classifier_priority;
  const v = typeof raw === 'string' ? raw.trim().toLowerCase() : '';
  return (EMAIL_PRIORITY_TIERS as readonly string[]).includes(v) ? (v as EmailPriority) : null;
}

/**
 * The affirm verb label for an item, WITH per-item context — a kind-generic hook
 * over the static DECK_VERBS label. email_tier appends its assigned tier
 * ("Confirm HIGH"); every other kind returns its static label unchanged. Null
 * when the kind has no affirm action.
 */
export function affirmLabelFor(item: FeedItem): string | null {
  const verbs = deckVerbsFor(item.kind);
  if (!verbs || verbs.affirm === null) return null;
  // A slot candidate's Accept carries its tier on the face — "Take it — T2".
  if (item.kind === 'slot_suggestion') {
    const t = ringTierOf(item);
    return t ? `${verbs.affirmLabel} — T${t}` : verbs.affirmLabel;
  }
  const p = emailPriority(item);
  return p ? `${verbs.affirmLabel} ${p.toUpperCase()}` : verbs.affirmLabel;
}

// --- display labels ----------------------------------------------------------
// Human kind names for the card chip. Unmapped kinds fall back to the raw kind
// (upper-cased) so a new backend kind still renders a sensible chip.
export const KIND_LABELS: Record<string, string> = {
  email_tier: 'Email tier',
  email_urgent: 'Urgent',
  attribution: 'Attribution',
  proposal: 'Proposal',
  recurrence: 'Recurrence',
  pending: 'Pending',
  routine_match: 'Routine match',
  slot_suggestion: 'Slot',
  health: 'Health',
  event: 'Event',
  ticket_notice: 'Ticket',
  radar: 'Radar',
  friction: 'Friction',
  notegen_readout: 'Note',
  peer_digest: 'Peer digest',
  weather: 'Weather',
  ops_notable: 'Ops',
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] || kind.replace(/_/g, ' ').toUpperCase();
}

// The universal FYI ack action (feed page + FYI rows) — sets the item `acked`.
export const ACK_ACTION = 'ack';

// --- rings (used by the rings header, B3-4) ----------------------------------
// Slot bucket vocabulary. Per-user vocabulary comes later; centralised here so
// it is never hardcoded inline in JSX. Keyed by the slot value a slot_suggestion
// feed item carries.
export const SLOT_LABELS: Record<string, string> = {
  duty: 'Duty',
  rhythm: 'Rhythm',
  fuel: 'Fuel',
};
export const SLOT_ORDER: readonly string[] = ['duty', 'rhythm', 'fuel'];
