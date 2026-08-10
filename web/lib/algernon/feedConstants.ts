import type { FeedItem } from './feed';
import { ringItemSuggested, ringTierOf } from './rings';

// Feed / deck constants + pure helpers. Kept OUT of the components so the
// gesture math, the per-kind verb map, and the display labels are unit-testable
// without a DOM, and so per-user vocabulary (rings labels, kind names) lives in
// ONE place rather than scattered through JSX (team-lead's constants-file rule).

// --- swipe geometry (ported from the ratified deck sketch) -------------------
export const SWIPE_X_THRESHOLD = 90; // |dx| past this on release → affirm/reject
export const SNOOZE_Y_THRESHOLD = 80; // upward dy past this (with small dx) → snooze
export const SNOOZE_X_TOLERANCE = 70; // the ↑ verdict needs |dx| under this
export const DRAG_Y_CLAMP = 30; // downward drag is clamped (cards don't fall)
export const STAMP_FADE_START = 40; // px of drag before a verdict stamp appears
export const STAMP_FADE_RANGE = 60; // px over which it fades fully in

// Delayed-act window for LIGHT kinds: the undo toast lifetime. The POST fires
// when this expires OR the next commit lands — never an un-act.
export const UNDO_MS = 3500;

// 'skip' rides the same session-local set-aside as an unbacked 'snooze' but is
// a distinct verdict on purpose: Skip means *not this one*, Snooze means *yes,
// but later*, and the #14 ruling is explicit that no one control may mean both.
// Keeping them apart here is what lets the toast and the staged list say which
// verb the operator actually used.
export type Verdict = 'affirm' | 'reject' | 'snooze' | 'skip' | null;

/**
 * The verdict a drag release resolves to — the SAME thresholds the sketch used.
 * Snooze wins on a mostly-vertical upward flick; otherwise a horizontal past the
 * threshold is affirm (right) / reject (left); anything short springs back (null).
 * Pure + DOM-free so the threshold contract is unit-pinned.
 */
export function verdictForDrag(dx: number, dy: number): Verdict {
  if (dy < -SNOOZE_Y_THRESHOLD && Math.abs(dx) < SNOOZE_X_TOLERANCE) return 'snooze';
  if (dx > SWIPE_X_THRESHOLD) return 'affirm';
  if (dx < -SWIPE_X_THRESHOLD) return 'reject';
  return null;
}

/**
 * Whether a drag VERDICT actually maps to an action for these verbs — so the deck can
 * SPRING BACK (resetVisual) on a no-op swipe instead of leaving the card stuck half-
 * dragged. An ACK-only kind (reject: null, e.g. email_urgent) reject → false; an
 * affirm-less kind affirm → false; snooze is always available. (#16 item 12.)
 */
export function swipeActsFor(verbs: DeckVerbs | null, verdict: Verdict): boolean {
  if (verdict === 'affirm') return !!verbs?.affirm;
  if (verdict === 'reject') return !!(verbs?.reject || verbs?.rejectDefers);
  // 'skip' is never produced by verdictForDrag (the LEFT drag resolves to
  // 'reject', and useDeck.reject re-routes it for a rejectDefers lane), so the
  // gesture-level answer for the ↑ direction is the only one left.
  return verdict === 'snooze';
}

// --- the snooze hold-band (#14) ----------------------------------------------
// A full ↑ swipe is a quick defer. A PARTIAL ↑ swipe held still in the band
// between "the stamp starts showing" and "the swipe would commit" opens the
// duration menu instead.
//
// The band is not a new number: it is exactly the span the Snooze stamp fades in
// over (STAMP_FADE_START → SNOOZE_Y_THRESHOLD). Holding where the stamp is
// already visible is what makes the affordance self-explanatory — the operator
// is looking at the word Snooze when the menu arrives.
export const SNOOZE_HOLD_MS = 450;
// How far the finger may drift and still count as "held". Generous enough for a
// thumb that isn't a tripod, tight enough that a slow full swipe reads as a
// swipe: drift past this re-anchors the hold rather than firing it.
export const SNOOZE_HOLD_MOVE_TOLERANCE = 12;

/**
 * Whether a live drag is sitting in the hold band — a partial ↑ with the same
 * horizontal tolerance the ↑ verdict itself uses, so a diagonal never opens a
 * menu the release wouldn't have honoured either.
 */
export function inSnoozeHoldBand(dx: number, dy: number): boolean {
  const up = -dy;
  return up >= STAMP_FADE_START && up <= SNOOZE_Y_THRESHOLD && Math.abs(dx) < SNOOZE_X_TOLERANCE;
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
// on the reply grammar). Unmapped kinds render but expose only the ↑ snooze.
export interface DeckVerbs {
  affirm: string | null;
  reject: string | null;
  affirmLabel: string;
  rejectLabel: string;
  heavy: boolean;
  /**
   * The LEFT (reject) gesture DEFERS instead of declining: there's no backend
   * decline path for slots v1, so a skipped candidate is set aside client-side
   * (no POST) and may resurface. `reject` stays null (no action_id).
   *
   * Deliberately NOT called a snooze, and not routed through the snooze verb:
   * Skip and Snooze are near-opposites — *not this one* versus *yes, but later*
   * — and the ruling that opened #14 is explicit that no single control may
   * mean both. They share a session-local mechanism; they do not share a name,
   * a label, or a toast. Copy must never promise "won't show again."
   */
  rejectDefers?: boolean;
}

// Heavy kinds create/mutate a durable record → a right-swipe does NOT commit;
// it reveals a confirm-tap stage (two-step). Recurrence is future-proofed
// (arrives with the board work); proposal is live today.
export const HEAVY_KINDS: ReadonlySet<string> = new Set(['proposal', 'recurrence']);

export const DECK_VERBS: Record<string, DeckVerbs> = {
  email_tier: { affirm: 'confirm', reject: 'spam', affirmLabel: 'Confirm', rejectLabel: 'Spam', heavy: false },
  // #27 email_urgent — the INTERRUPT card. ACK-only: right/✓ acknowledges (POST "ack" →
  // acted → flip); no reject/left (re-tier lives on the calibration card — two cards, two
  // claims); the ↑ snooze gesture works as ever (kind-generic). Having this entry is what makes
  // isDeckDealt(email_urgent) true → the C2-era generic deck/needs-you paths deal + count it.
  email_urgent: { affirm: 'ack', reject: null, affirmLabel: 'Got it', rejectLabel: '', heavy: false },
  attribution: { affirm: 'confirm', reject: 'reject', affirmLabel: 'Confirm', rejectLabel: 'Reject', heavy: false },
  routine_match: { affirm: 'confirm', reject: 'reject', affirmLabel: "That's it", rejectLabel: 'No', heavy: false },
  proposal: { affirm: 'confirm', reject: 'reject', affirmLabel: 'Confirm', rejectLabel: 'Reject', heavy: true },
  recurrence: { affirm: 'confirm', reject: 'reject', affirmLabel: 'Promote', rejectLabel: 'Reject', heavy: true },
  pending: { affirm: 'noted', reject: null, affirmLabel: 'Noted', rejectLabel: '', heavy: false },
  // C2 slot candidate: Accept (right, POST "accept") / Skip (left, a client-side
  // set-aside, no POST — rejectDefers) / Snooze (up). Only SUGGESTED slots are
  // dealt (see isDeckDealt).
  slot_suggestion: { affirm: 'accept', reject: null, affirmLabel: 'Take it', rejectLabel: 'Skip', heavy: false, rejectDefers: true },
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

// --- attribution contest (#63a) ----------------------------------------------
// The operator ruled attribution confirmations down to the FYI/glance tier: they
// are consistently correct, so reviewing them in the deck spent his attention
// and returned nothing. They now auto-confirm after 24h.
//
// This is the door back out. "Not right" says the inference is wrong, which
// stops that item's auto-confirm clock for good and returns it to needs-you for
// a deliberate confirm or reject. It is also the correction signal the platform's
// self-correcting standard requires — the server records it in the audit corpus,
// so a demotion that would otherwise have hidden a systematic error still
// surfaces one.
export const ATTRIBUTION_KIND = 'attribution';
export const CONTEST_ACTION = 'contest';

/**
 * Whether this row should offer the contest door.
 *
 * Attribution only (it is the only kind the backend's capability ceiling admits
 * `contest` for — offering it elsewhere would be a button that 400s in the
 * operator's hand), and only while the item is still FYI. An attribution item
 * already sitting in needs-you has either been contested or was never demoted,
 * and in both cases the operator is already being asked to decide it; a second
 * control meaning "ask me about this" would be noise.
 *
 * That second clause is also what makes the half-deployed state honest: against
 * a backend that has not yet demoted the tier, every attribution item arrives as
 * needs-you and the door simply never draws.
 */
export function contestableItem(item: FeedItem): boolean {
  if (item.kind !== ATTRIBUTION_KIND) return false;
  return !(item.attention === 'needs_you' || item.mode === 'decide');
}

// --- which section was wrong (#72 item 4) ------------------------------------
// A contest says "this inference is wrong". This says WHICH PART of the capture
// summary produced it, so the per-section statistic can answer "which section
// produces bad inferences" rather than only "how many were wrong".
//
// MUST equal `alfred.telegram.capture_sections.SUMMARY_SECTIONS` — same order,
// same spellings. A Python-side drift pin parses THIS array out of the TS
// source and holds it against that tuple, the same shape as the CONTEST_ACTION
// pin above, because neither side can notice the drift alone. Rename a heading
// in the renderer only and this picker keeps offering the old spelling: the
// router normalises the unrecognised value to `''`, so the contest still lands
// (correct — the correction must never be lost to a UI detail) but files under
// `unknown`, and the statistic loses a dimension with every test on both sides
// still green.
//
// This IS a second spelling of the vocabulary, deliberately — TypeScript cannot
// import a Python tuple. The pin is what makes the copy safe. Do not add a
// third copy; import from here.
export const CONTEST_SECTIONS = [
  'Topics',
  'Decisions',
  'Open Questions',
  'Action Items',
  'Key Insights',
  'Raw Contradictions',
  'Discarded Noise',
  'Re-encounters',
] as const;

export type ContestSection = (typeof CONTEST_SECTIONS)[number];

// The body-gate bound for `contested_section` lives with its siblings in
// schemas.ts (`CONTEST_SECTION_MAX_CHARS`), not here — the relay bounds are one
// group and splitting them across two files is how one of them gets missed.

/** The picker's stable per-section test/DOM id fragment (no spaces). */
export function contestSectionSlug(section: string): string {
  return section.toLowerCase().replace(/\s+/g, '-');
}

// --- snooze, the one defer verb (#14) ----------------------------------------
// The duration ladder, in menu order. MUST equal `alfred.tier.snooze
// .SNOOZE_DURATIONS`' keys — a rung offered here that the router's FEED_ACTIONS
// ceiling doesn't admit is a button that 400s in the operator's hand. A pin in
// tests/tier/test_board_snooze.py reads THIS array and asserts the two agree,
// because the drift is silent on both sides otherwise.
export const SNOOZE_ACTIONS = ['snooze_1d', 'snooze_3d', 'snooze_7d', 'snooze_until_i_say'] as const;
export type SnoozeAction = (typeof SNOOZE_ACTIONS)[number];

// The quick-defer rung: what a full ↑ swipe records, and the old Park's
// semantics. No end date — it holds until the operator says otherwise (or the
// urgency delta breaks it through, which fires on undated entries too).
export const SNOOZE_INDEFINITE_ACTION: SnoozeAction = 'snooze_until_i_say';
export const UNSNOOZE_ACTION = 'unsnooze';
/** The verb the store stamps on a snoozed item (`acted_action`) — the staged list reads it. */
export const SNOOZE_ACTED_VERB = 'snooze';

export const SNOOZE_LABELS: Record<SnoozeAction, string> = {
  snooze_1d: '1 day',
  snooze_3d: '3 days',
  snooze_7d: '7 days',
  snooze_until_i_say: 'Until I say',
};

// Kinds the BACKEND can actually snooze. `FEED_ACTIONS` admits `snooze_*` under
// `slot_suggestion` alone, so this is one entry — but it is a SET rather than an
// equality check because the honest answer is per-kind and will grow.
//
// The ↑ gesture stays available on every card either way; what varies is whether
// it persists. On a snooze-capable kind it POSTs and survives a reload; on the
// others it sets the card aside for the session exactly as it always did, and
// the copy on that path must not promise otherwise. Offering the durations menu
// on a kind with no store behind it would be the accepted-then-ignored failure
// this whole round set out to cure.
export const SNOOZE_CAPABLE_KINDS: ReadonlySet<string> = new Set(['slot_suggestion']);

/** Whether a ↑ on this item reaches the backend (vs. a session-local set-aside). */
export function snoozeIsBacked(item: FeedItem): boolean {
  return SNOOZE_CAPABLE_KINDS.has(item.kind);
}

/**
 * The action_id a snooze of `action` should POST for this item, or `null` when
 * the kind has no backend snooze verb (→ the caller commits a client-side
 * set-aside instead, with no POST).
 */
export function snoozeActionFor(item: FeedItem, action: SnoozeAction): string | null {
  return snoozeIsBacked(item) ? action : null;
}

// --- routine_match correction (#13) ------------------------------------------
// A left-swipe reject says "not that item" and stops there. These two actions are
// the richer doors behind the card's "What did this mean?" tap: `correct` carries
// the item the operator picked, `one_off` says the phrase means no routine item at
// all. Both are rejects server-side; both write the corpus through the same
// resolver the reply grammar uses.
export const ROUTINE_MATCH_KIND = 'routine_match';
export const CORRECT_ACTION = 'correct';
export const ONE_OFF_ACTION = 'one_off';

export interface RoutineCandidate {
  text: string;
  record: string;
}

/**
 * The pick-list for a routine_match card, read from `evidence.candidates` (the
 * producer stamps every ACTIVE routine item, the suggestion's own record first).
 *
 * Defensive by contract: evidence is an untrusted `to_dict` payload, so rows
 * missing `text` are dropped rather than rendered as blank buttons. An empty
 * result is a legitimate state (no vault path wired) — the caller shows the
 * one-off door alone rather than an empty picker.
 *
 * Display only. The server re-reads the vault and validates the pick before
 * writing, so a stale list can cost a retry but can never poison the corpus.
 */
export function routineCandidatesFor(item: FeedItem): RoutineCandidate[] {
  if (item.kind !== ROUTINE_MATCH_KIND) return [];
  const raw = (item.evidence as Record<string, unknown> | null | undefined)?.candidates;
  if (!Array.isArray(raw)) return [];
  const out: RoutineCandidate[] = [];
  for (const row of raw) {
    if (!row || typeof row !== 'object') continue;
    const r = row as Record<string, unknown>;
    const text = typeof r.text === 'string' ? r.text.trim() : '';
    if (!text) continue;
    out.push({ text, record: typeof r.record === 'string' ? r.record : '' });
  }
  return out;
}

/** The item a routine_match card proposed — excluded from the picker (saying "no,
 *  it means the thing I just rejected" is contradictory, and the server refuses it). */
export function routineProposedItem(item: FeedItem): string {
  if (item.kind !== ROUTINE_MATCH_KIND) return '';
  const raw = (item.evidence as Record<string, unknown> | null | undefined)?.matched_to;
  return typeof raw === 'string' ? raw.trim() : '';
}

/** Case/whitespace-insensitive compare, APPROXIMATING the server's target
 *  normalisation so the picker hides the option the resolver would refuse.
 *
 *  Not an exact mirror, and the comment used to claim it was. The server folds
 *  with Python's `str.casefold()`; JS has no equivalent, and `toLowerCase()`
 *  differs on the ß-class: `'Straße'.toLowerCase()` is `'straße'` where
 *  casefold gives `'strasse'`. Consequence is cosmetic and fails SAFE — the
 *  picker would offer an option the server then refuses, so the operator sees a
 *  refusal rather than a wrong write. Left approximate rather than hand-rolling
 *  a partial casefold, which would drift from CPython's table on the next
 *  Unicode revision; if this ever matters, normalise server-side and send the
 *  folded form. */
export function sameRoutineItem(a: string, b: string): boolean {
  return a.trim().replace(/\s+/g, ' ').toLowerCase() === b.trim().replace(/\s+/g, ' ').toLowerCase();
}

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
