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

// Delayed-act window for LIGHT verbs: the undo toast lifetime. The POST fires
// when this expires OR the next commit lands — never an un-act.
//
// SIX seconds (D8), up from 3.5. The mechanism was never the weak part — a
// delayed act that CANCELS is strictly stronger than a post-hoc undo, because
// nothing has been written yet to reverse. What was weak was the visibility: a
// 3.5s window with no indication of how much of it is left asks the operator to
// notice a toast, read it, and decide, inside a span they cannot see. The
// draining bar (`deck-toast-bar`) is the other half of the same ruling — the
// remaining time is on screen, so the deadline is a fact rather than a gamble.
export const UNDO_MS = 6000;

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
//
// The FAMILY clock: the #14 hold turned out to be the first member of the
// affirm-with-hold-modifier family, and GESTURE_HOLD_MS (below) ALIASES this
// constant — one number serves every hold, and this name stays for its
// existing consumers.
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

// --- the gesture hold band, direction-general (affirm-with-hold-modifier) -----
// The platform pattern the operator ratified 2026-08-19: a gesture commits its
// DEFAULT on release, and HOLDING a partial swipe in the band where that
// gesture's stamp is already fading in opens the alternatives — "affirm as
// suggested, or swipe hold to change the suggestion while affirming".
//
// DIRECTION IS A PARAMETER, not baked-in affirm-ness: the same band shape holds
// on every axis (primary-axis distance between STAMP_FADE_START and that axis's
// commit threshold, cross-axis drift bounded). Only the AFFIRM direction is
// WIRED this lane; 'reject' is defined here because the geometry is one rule
// (ratified-in-principle, not built — nothing arms it), and 'snooze' delegates
// to the shipped #14 band, which is PRIOR ART of this same family (default = the
// indefinite rung, held-open alternatives = the duration menu). Convergence
// note: the snooze band's detection mechanics are identical in shape and are a
// converge candidate; its MENU content stays client-constant today because the
// snooze rungs ship unlabelled and ungrouped on the wire (see SNOOZE_ACTIONS).
export type HoldDirection = 'affirm' | 'reject' | 'snooze';

// One clock for the whole family — a true ALIAS of the #14 hold constant
// (never a second literal): existing consumers keep SNOOZE_HOLD_MS, new
// consumers of the pattern take the family name, and there is one number.
export const GESTURE_HOLD_MS = SNOOZE_HOLD_MS;

// Cross-axis tolerance for the HORIZONTAL bands. STAMP_FADE_START on purpose:
// the snooze band claims |dy| ≥ STAMP_FADE_START (its `up` lower bound), so
// bounding the horizontal bands' |dy| STRICTLY BELOW it makes the bands
// disjoint BY CONSTRUCTION — no (dx, dy) can arm two holds at once. Pinned.
export const HOLD_CROSS_TOLERANCE = STAMP_FADE_START;

/**
 * Whether a live drag sits in `direction`'s hold band. Same self-explanatory
 * affordance as the #14 band: the operator is looking at that direction's
 * stamp (it starts fading in at STAMP_FADE_START) when the alternatives
 * arrive, and every in-band coordinate sits strictly inside every
 * `verdictForDrag` threshold, so a release in-band resolves to null — the
 * hold can never race a commit.
 */
export function inGestureHoldBand(direction: HoldDirection, dx: number, dy: number): boolean {
  if (direction === 'snooze') return inSnoozeHoldBand(dx, dy);
  const along = direction === 'affirm' ? dx : -dx;
  return along >= STAMP_FADE_START && along <= SWIPE_X_THRESHOLD && Math.abs(dy) < HOLD_CROSS_TOLERANCE;
}

// --- the deck's verbs, READ FROM THE WIRE ------------------------------------
// The shape the deck consumes: the action_id its affirm (right / ✓) and reject
// (left / ✕) swipes POST, plus the presentation each carries. `null` = that
// direction is unavailable for this item (e.g. pending has no reject — only
// "noted"). The deck is a simplified 2-way surface over the transport's
// capability ceiling (richer tier calibration stays on the reply grammar), and
// it now learns the ceiling's answer from `item.actions` instead of asserting
// its own — see `verbsFromActions`. An item served no gesture-bearing verb
// renders but exposes only the ↑ snooze.
/**
 * How much a single verb costs to get wrong (step-2 §3).
 *
 * LIGHT — the wrong swipe is cheap corpus noise: idempotent, correctable, or
 * both. Commits on the gesture, with the undo window to take it back.
 * HEAVY — the verb writes or destroys something durable. It ARMS on the
 * gesture, showing what it will write, and commits on a second tap. A
 * mutation-bearing verb never fires on a single motion.
 */
export type VerbWeight = 'light' | 'heavy';

export interface DeckVerbs {
  affirm: string | null;
  reject: string | null;
  affirmLabel: string;
  rejectLabel: string;
  /**
   * PER-VERB, not per-kind. The two directions of one card are genuinely
   * different transactions and the catalog has always said so: an attribution
   * CONFIRM records agreement, while its REJECT strips the marked section out
   * of the record's body (`vault/attribution.py::reject_marker` — it drops the
   * marked line range and removes the audit entry). One boolean per KIND cannot
   * express that, so it did not: attribution shipped as `heavy: false` and its
   * reject committed on a single left swipe, protected only by the undo window.
   *
   * The weights are the SERVER's, per verb, arriving in `item.actions` — the
   * §3 catalog is now read rather than transcribed.
   */
  affirmWeight: VerbWeight;
  rejectWeight: VerbWeight;
  /**
   * What a HEAVY verb will actually write, shown on the arm stage (D4: "the arm
   * shows exactly what will be written"). Omitted for a light verb, which has no
   * arm stage to show it on.
   */
  affirmArmNote?: string;
  rejectArmNote?: string;
  /**
   * The LEFT (reject) gesture DEFERS instead of declining: there's no backend
   * decline path for slots v1, so a skipped candidate is set aside client-side
   * (no POST) and may resurface. `reject` stays null (no action_id).
   *
   * THE ONE FLAG THAT CANNOT COME FROM THE WIRE, and the reason is structural
   * rather than incidental: this is a client MECHANISM with no ceiling verb
   * behind it. `FEED_ACTIONS` describes what the router will ACCEPT, and a Skip
   * POSTs nothing at all — so there is no entry for the server to serve, and
   * inventing one would advertise a verb the router must then refuse. It stays
   * client-side until slots gain a real decline path.
   *
   * Deliberately NOT called a snooze, and not routed through the snooze verb:
   * Skip and Snooze are near-opposites — *not this one* versus *yes, but later*
   * — and the ruling that opened #14 is explicit that no single control may
   * mean both. They share a session-local mechanism; they do not share a name,
   * a label, or a toast. Copy must never promise "won't show again."
   */
  rejectDefers?: boolean;
}

// The kind whose LEFT gesture is a client-side set-aside rather than a verb.
// See `rejectDefers` on DeckVerbs for why this cannot come from the wire.
const SLOT_KIND = 'slot_suggestion';

// The gesture names on the wire (`ACTION_META`'s gesture field, the §4 contract
// amendment). The deck's two horizontal axes, and nothing else: a served verb
// with no gesture is a MENU verb, not a swipe verb.
const GESTURE_AFFIRM = 'affirm';
const GESTURE_REJECT = 'reject';

/** One served verb, as it arrives in `item.actions` (§4 + the gesture field,
 *  + the `group` field — the affirm-with-hold-modifier amendment). */
interface ServedAction {
  verb: string;
  label: string;
  weight: VerbWeight;
  gesture?: string;
  note?: string;
  /**
   * A CO-EQUAL CHOICE GROUP (the affirm-with-hold-modifier pattern,
   * operator-ratified 2026-08-19). Verbs sharing a group are alternatives of
   * one decision; the gesture-bearing member is the SUGGESTED one. The
   * hold-selector's option list derives from this — wire-served, never a
   * client per-kind map (the 1b-ii lesson, kept).
   */
  group?: string;
}

/**
 * Read `item.actions` defensively — it is a bare cast off the wire.
 *
 * `FeedItem.actions` is declared REQUIRED, but the declaration is a compile-time
 * claim about a runtime value nothing validates: `pages/api/feed/list.ts` relays
 * the transport body verbatim and the browser side casts. So a half-deployed box
 * really can hand us an item with no `actions` key at all, and the type says
 * otherwise. Anything that isn't a well-formed entry is dropped rather than
 * coerced — a verb we cannot read is a verb we must not offer.
 */
function servedActions(item: FeedItem): ServedAction[] {
  const raw: unknown = (item as { actions?: unknown }).actions;
  if (!Array.isArray(raw)) return [];
  const out: ServedAction[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const e = entry as Record<string, unknown>;
    if (typeof e.verb !== 'string' || !e.verb) continue;
    out.push({
      verb: e.verb,
      // The server already falls back to the raw id for an unlabelled ceiling
      // verb, so an empty label here means the payload itself is malformed.
      label: typeof e.label === 'string' && e.label ? e.label : e.verb,
      weight: e.weight === 'heavy' ? 'heavy' : 'light',
      gesture: typeof e.gesture === 'string' ? e.gesture : undefined,
      note: typeof e.note === 'string' && e.note ? e.note : undefined,
      group: typeof e.group === 'string' && e.group ? e.group : undefined,
    });
  }
  return out;
}

/**
 * The deck's verbs for ONE item, read from what the server SERVED it (#102 1b-ii).
 *
 * THE ONE SEAM. This replaces the hand-written per-kind `DECK_VERBS` map, whose
 * own comment conceded the coupling it could not enforce ("these action_ids MUST
 * be members of the B1 transport FEED_ACTIONS map for the kind"). A mirror
 * maintained by vigilance drifts the day either side is edited, silently and in
 * both directions: a verb the client offers that the ceiling refuses 400s in the
 * operator's hand, and a verb the ceiling gains that the client never shows is a
 * capability nobody can reach. The client no longer holds an opinion about which
 * verbs exist — it renders the answer.
 *
 * GESTURE-DRIVEN, and this is the load-bearing part. The deck is a two-way swipe
 * surface; a served verb becomes a swipe verb ONLY by carrying a `gesture`. That
 * is not a shortcut, it is the contract: of `slot_suggestion`'s ceiling verbs,
 * exactly ONE (`accept`) carries a gesture. Every other one is gesture-free BY
 * DESIGN, in two groups for two different reasons — the board/hold-menu verbs
 * (done, undo_done, the snoozes, unsnooze) because they belong to those surfaces
 * rather than to a swipe, and the sort verbs because the three slots are
 * CO-EQUAL and gesturing one would make it the "yes" (see `ACTION_META`).
 *
 * A LABEL IS NOT A GESTURE, and the distinction is the whole rule: the sort
 * verbs ship labelled and still do not swipe, while the board verbs ship under
 * raw ids. Presentation and gesture are independent, and only the gesture makes
 * a swipe verb. Mapping by position or by name instead would hand the deck every
 * verb the kind has, on a surface that renders one.
 *
 * Returns null when the item was served no gesture-bearing verb at all — an FYI
 * kind, a kind the ceiling does not know, or a degraded payload. Null means "no
 * swipe verbs", which is what the old unmapped-kind answer meant, so every
 * `verbs?.` consumer keeps its existing behaviour.
 */
export function verbsFromActions(item: FeedItem): DeckVerbs | null {
  const served = servedActions(item);
  const affirm = served.find((a) => a.gesture === GESTURE_AFFIRM);
  const reject = served.find((a) => a.gesture === GESTURE_REJECT);
  if (!affirm && !reject) return null;
  // PRESENTATIONAL, client-side, and it cannot come from the wire: there is no
  // ceiling verb for it. A slot's LEFT gesture is a session-local set-aside that
  // POSTs nothing (slots v1 have no backend decline path), so no `FEED_ACTIONS`
  // entry could ever describe it. Deliberately NOT borrowed from the snooze
  // verb either — the #14 ruling is explicit that no one control may mean both
  // *not this one* and *yes, but later*.
  const rejectDefers = item.kind === SLOT_KIND && !reject;
  return {
    affirm: affirm?.verb ?? null,
    reject: reject?.verb ?? null,
    affirmLabel: affirm?.label ?? '',
    rejectLabel: reject?.label ?? (rejectDefers ? 'Skip' : ''),
    affirmWeight: affirm?.weight ?? 'light',
    rejectWeight: reject?.weight ?? 'light',
    affirmArmNote: affirm?.note,
    rejectArmNote: reject?.note,
    ...(rejectDefers ? { rejectDefers: true } : {}),
  };
}

/**
 * Whether this item arrived carrying NO verbs at all — the degraded payload.
 *
 * WHY THIS IS A SEPARATE QUESTION FROM `isDeckDealt`. Both a committed slot and
 * a verbless item fail `isDeckDealt`, but for opposite reasons, and the operator
 * needs them told apart:
 *
 *   - A committed slot is WORKING AS DESIGNED. It carries a full verb list
 *     (done, undo_done, the snoozes); none of them is a swipe verb, because it
 *     belongs to the board rather than the deck.
 *   - A verbless item is a FAULT somewhere upstream — a half-deployed box, the
 *     serve side's `actions_unavailable` / `actions_stamp_failed` degradation,
 *     or a decide kind the ceiling has no entry for. Nothing is wrong with the
 *     item; the controls for it never arrived.
 *
 * Telling the operator "these are on your worklist" in the second case is a lie
 * with a plausible shape — they would go to the Feed and find items they still
 * cannot act on. The two populations are disjoint and together they exhaust the
 * non-dealt set, so one predicate splits them cleanly.
 *
 * Deliberately asks about the WIRE (`actions`), not about gestures: an item with
 * verbs that merely lack gestures is the worklist case, and folding the two
 * together is exactly the distinction this exists to preserve.
 */
export function arrivedVerbless(item: FeedItem): boolean {
  return servedActions(item).length === 0;
}

// --- the hold-selector's choices (affirm-with-hold-modifier, wire-derived) ----

/** One option in a hold-selector: a co-equal alternative that COMMITS when
 *  chosen. `suggested` marks the one the plain gesture would have committed. */
export interface HoldChoice {
  verb: string;
  label: string;
  suggested: boolean;
}

/**
 * The co-equal choices behind `direction`'s hold, or null when the gesture has
 * none (no gestured verb, no group on it, or a group of one — a selector with
 * one option is a confirm stage wearing a menu, which this pattern forbids).
 *
 * DERIVED FROM THE WIRE and from nothing else: the gestured verb's `group`
 * names the family, the family is every served verb sharing it. A future
 * suggestion-bearing kind gets the selector by serving a group — no client
 * change, no per-kind map (the 1b-ii lesson).
 *
 * THE ONE-INTERACTION INVARIANT (operator ruling): choosing from the selector
 * IS the gesture's commit — same verb wire, same delayed-act path, never
 * choose-then-confirm. Consumers route a pick through the SAME commit the
 * gesture uses (`useDeck.affirmWith`), and nothing may put a second
 * confirmation between the pick and the act.
 *
 * `direction` is 'affirm' | 'reject' only: the ↑ snooze menu is this family's
 * prior art but its rungs ship ungrouped (see the band note above), so it
 * keeps its own door until the wire serves them as a group.
 */
export function holdChoicesFor(item: FeedItem, direction: 'affirm' | 'reject'): HoldChoice[] | null {
  const served = servedActions(item);
  const wanted = direction === 'affirm' ? GESTURE_AFFIRM : GESTURE_REJECT;
  const gestured = served.find((a) => a.gesture === wanted);
  if (!gestured?.group) return null;
  const family = served.filter((a) => a.group === gestured.group);
  if (family.length < 2) return null;
  return family.map((a) => ({
    verb: a.verb,
    label: a.label,
    suggested: a.verb === gestured.verb,
  }));
}

/**
 * Whether this item's AFFIRM is a suggested choice — the mark of a suggestion
 * card (the deck rotation's sort card is the first; any kind serving a grouped
 * affirm is one).
 */
export function hasSuggestedChoice(item: FeedItem): boolean {
  return holdChoicesFor(item, 'affirm') !== null;
}

/**
 * The deck's CANDIDATE population — the page-level question, distinct from
 * `isDeckDealt` (can this item be dealt) below.
 *
 * Decide-mode items are candidates as they always were. A QUIET suggestion
 * card (fyi/fyi, grouped affirm) is ALSO a candidate: its home is the deck —
 * the suggestion grammar exists for deck gestures — while its tier keeps the
 * phone silent (`isNeedsYouItem` and the push predicate are untouched; this
 * widens what the deck deals, never what rings). MEASURED necessity, not
 * taste: the deck page fetched `mode=decide` and the board partition is
 * needs-you-only, so without this predicate a quiet suggestion card would be
 * produced, served, verb-carrying — and rendered nowhere a gesture exists
 * (the accepted-then-ignored wall the 85aed5a5 measurements closed the lane
 * over). Deliberately NOT `isDeckDealt ∧ fyi`: an fyi attribution row also
 * serves gestured verbs, and dealing every glance row would undo the
 * operator's own demotion ruling — the suggestion GROUP is what marks a card
 * as deck-homed.
 *
 * All three deck surfaces (the /deck fetch, the feed page's deck link, the
 * home pill) compose their counts from this ONE predicate, so the link counts
 * exactly what the deck deals.
 */
export function isDeckCandidate(item: FeedItem): boolean {
  return item.mode === 'decide' || hasSuggestedChoice(item);
}

/**
 * The weight of ONE direction — the predicate the deck arms on.
 *
 * Takes the item's DERIVED verbs (`verbsFromActions`) rather than its kind: the
 * weight is now a per-verb fact carried on the wire, and two items of one kind
 * can legitimately be served different verbs.
 *
 * No verbs, or a direction with no verb, answers 'light': there is nothing to
 * arm, and returning 'heavy' would put a confirm stage in front of a gesture
 * that does nothing at all.
 *
 * `snooze` / `skip` are always light. A defer is reversible by construction (the
 * card comes back), so arming it would spend a tap to protect nothing.
 */
export function verbWeightFor(verbs: DeckVerbs | null, verdict: Verdict): VerbWeight {
  if (!verbs) return 'light';
  if (verdict === 'affirm') return verbs.affirm === null ? 'light' : verbs.affirmWeight;
  if (verdict === 'reject') {
    // A rejectDefers lane writes nothing at all — it is a session-local
    // set-aside — so it is light regardless of the declared weight.
    if (verbs.rejectDefers || verbs.reject === null) return 'light';
    return verbs.rejectWeight;
  }
  return 'light';
}

/** Whether this direction must ARM rather than commit (D4). */
export function isHeavyVerb(verbs: DeckVerbs | null, verdict: Verdict): boolean {
  return verbWeightFor(verbs, verdict) === 'heavy';
}

/**
 * What the armed verb will write — the sentence the arm stage shows (D4).
 *
 * Null when the verb is light (no arm stage) or when a heavy verb has declared
 * no note. That second case is deliberately visible rather than papered over
 * with a generic "this writes a record": a heavy verb whose consequence nobody
 * wrote down should read as missing, not as reassuring.
 */
export function armNoteFor(verbs: DeckVerbs | null, verdict: Verdict): string | null {
  if (!isHeavyVerb(verbs, verdict)) return null;
  if (!verbs) return null;
  const note = verdict === 'affirm' ? verbs.affirmArmNote : verbs.rejectArmNote;
  return note ?? null;
}

/** The label of one direction — what the arm stage's commit button says. */
export function verbLabelFor(verbs: DeckVerbs | null, verdict: Verdict): string {
  if (!verbs) return '';
  return verdict === 'affirm' ? verbs.affirmLabel : verbs.rejectLabel;
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
  // Slots keep a LIFECYCLE clause, and it CANNOT come from the wire.
  //
  // Dealing a slot is a question about its STAGE, not its verbs: only a
  // SUGGESTED candidate is a card, while planned and done slots are worklist /
  // glance items. `ringItemStage` answers that from `state` + `acted_action` +
  // `evidence.candidate`, and the served verb list cannot substitute for it —
  // an ACTED slot still carries `candidate: true` in its evidence (accept means
  // committed, not un-candidate), and a COMMITTED one still legitimately
  // advertises done / undo_done / the snoozes. Either would deal as a card on a
  // verbs-are-non-empty test. The verb list narrows what a slot may DO; the
  // stage decides whether it is a card at all.
  if (item.kind === SLOT_KIND) return ringItemSuggested(item) && verbsFromActions(item) !== null;
  return verbsFromActions(item) !== null;
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
 * The affirm verb label for an item, WITH per-item context — a hook over the
 * SERVED label. email_tier appends its assigned tier ("Confirm HIGH"); every
 * other kind returns the served label unchanged. Null when the item has no
 * affirm verb.
 *
 * PRESENTATIONAL-ONLY, and stays client-side on purpose: the served label is a
 * per-KIND string, while the suffix here is a fact about THIS item's evidence
 * (its classifier tier, its ring tier). Pushing it onto the wire would mean the
 * server rendering operator-facing copy per item — a different contract from the
 * one §4 describes.
 */
export function affirmLabelFor(item: FeedItem): string | null {
  const verbs = verbsFromActions(item);
  if (!verbs || verbs.affirm === null) return null;
  // A slot candidate's Accept carries its tier on the face — "Take it — T2".
  if (item.kind === SLOT_KIND) {
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
  sort_suggestion: 'Sort',
  health: 'Health',
  event: 'Event',
  ticket_notice: 'Ticket',
  radar: 'Radar',
  friction: 'Friction',
  notegen_readout: 'Note',
  peer_digest: 'Peer digest',
  weather: 'Weather',
  ops_notable: 'Ops',
  pattern_surfaced: 'Pattern',
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] || kind.replace(/_/g, ' ').toUpperCase();
}

// The universal FYI ack action (feed page + FYI rows) — sets the item `acked`.
export const ACK_ACTION = 'ack';

// The QUICK defer verb id — `action_router.DEFER_NEXT_RENDER`'s spelling. A
// hand-kept cross-language copy of the kind the house normally deletes, held
// for the same reason as SORT_ACTION_BY_SLOT's: the client needs it BEFORE any
// response exists — here, to say the honest sentence after a reject gesture
// whose served verb is a defer ("not now — it comes back", never "Rejected.",
// which would claim a judgment the operator did not make). A cross-language
// drift pin reads THIS constant from the Python side
// (tests/feed/test_sort_suggestion_act.py). Do not add a third copy.
export const DEFER_QUICK_ACTION = 'defer';

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
/**
 * The verb the store stamps on a snoozed item (`acted_action`).
 *
 * RE-EXPORTED, NOT DECLARED: it now lives in `rings.ts`, beside the verb→stage map
 * that is its only real consumer. It was declared HERE, with a docstring claiming
 * "the staged list reads it", and read by nothing at all — the staged list keys off
 * the session-local `useSnooze` hook instead. A verb constant shipped without being
 * threaded into the stage seam is exactly how a snoozed item went on rendering as
 * DONE across two surfaces; keeping one owner is what stops it recurring.
 */
export { SNOOZE_ACTED_VERB } from './rings';

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

/** A canonical slot key — the three, and never `unslotted`. */
export type BoardSlot = 'duty' | 'rhythm' | 'fuel';

/**
 * Slot → the `action_id` that records it (2026-08-19, the sort affordance).
 *
 * MIRRORS `SORT_ACTION_BY_SLOT` in `daily_sync/action_router.py`, which is the
 * ceiling and therefore the authority. TypeScript cannot import a Python dict,
 * so this is a hand-kept copy of the kind the house normally deletes — and it
 * is kept here for the same reason the snooze ladder's copy is: the tap has to
 * name a verb before any response comes back, so there is nothing on the wire
 * to derive it from at the moment it is needed. A cross-language drift pin
 * reads the OTHER language's source rather than duplicating the expectation a
 * third time (`tests/tier/test_sort_affordance.py`).
 *
 * The verb ids carry the slot rather than taking it as a parameter because the
 * ceiling is a STATIC map: a single `sort` verb would need its target as
 * per-request data, which `FEED_ACTIONS` cannot express.
 */
export const SORT_ACTION_BY_SLOT: Record<BoardSlot, string> = {
  duty: 'sort_duty',
  rhythm: 'sort_rhythm',
  fuel: 'sort_fuel',
};
