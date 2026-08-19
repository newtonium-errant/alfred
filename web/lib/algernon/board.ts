import type { FeedItem } from './feed';
import { SLOT_LABELS, SLOT_ORDER } from './feedConstants';
import {
  instanceDayKey,
  isTodayInstanceTz,
  ringItemVisibleToday,
  type RingItemStage,
} from './rings';

// ═══════════ THE DAY BOARD — Duty / Rhythm / Fuel (Phase C, lane C1) ════════
//
// The pure model behind the slot board. No DOM, no hooks, no fetch: grouping,
// carryover detection, attention ranking and the caps are all decidable from a
// list of feed items plus a stage function, so they are unit-testable on their
// own and the component stays a render. Same split rings.ts/ringGeometry.ts use.
//
// ── WHY SLOT, NOT A RENAMED TIER ────────────────────────────────────────────
// The board groups by the `slot` axis the backend classifier stamps, NOT by a
// relabelling of T1/T2/T3. `src/alfred/tier/slots.py` settles this in its own
// words: "Slot is a second axis, orthogonal to tier. Tier answers *when does
// this press*; slot answers *what does this do for the day*. Both survive;
// neither replaces the other. […] G9's rename-only reading (Duty=T1, Rhythm=T2,
// Fuel=T3) is SUPERSEDED."
//
// The consequence that matters here: a cadence routine item that happens to be
// due today is T1 by urgency and Rhythm by kind. Under a rename the board would
// call it Duty — a confident wrong answer printed on top of data that already
// holds the right one, and the balance this feature exists to measure would be
// unmeasurable. So `TIER_RING_LABEL` keeps saying T1/T2/T3 (it labels tiers, and
// that is what tiers are called) and the STACKS below the rings speak slots.
//
// Nothing new is fetched to do it. `brief/feed_producer.py` has emitted
// `evidence.slot` + `evidence.slot_rule` since the classifier's stage 1,
// explicitly so "the FE can be built and the rings swapped (stage 2) without a
// second producer change". This module is that consumer.

/**
 * The honest residue. NOT a slot — an admission that no classifier rule fired.
 * Mirrors `SLOT_UNSLOTTED` in `tier/slots.py`, and carries its discipline: an
 * unslotted item renders honestly, is EXCLUDED from every board count, and is
 * never defaulted into Duty to make the stacks look full.
 */
export const BOARD_UNSLOTTED = 'unslotted';

/** Carryover shown per slot before the rest is demoted to browse (ratified). */
export const CARRYOVER_CAP = 3;

/** Suggestion candidates offered per slot — the "1-3 yes/no" of §2 job 6. */
export const CANDIDATE_CAP = 3;

/**
 * The slot a board item belongs to — THE GROUPING CHOKE-POINT. Every stack, count
 * and label resolves through this one function, so the axis is changed in exactly
 * one place if it is ever changed at all.
 *
 * Mirrors `normalize_slot` in `tier/slots.py`: case- and whitespace-insensitive
 * (it reads hand-edited frontmatter by way of the producer), and anything
 * unrecognised degrades to `unslotted` rather than inventing a fourth category.
 * Evidence is unschema'd on the wire, so a non-string is coerced defensively the
 * same way `ringTierOf` coerces its tier.
 */
export function boardSlotOf(item: FeedItem): string {
  const raw = (item.evidence as Record<string, unknown> | null | undefined)?.slot;
  if (typeof raw !== 'string') return BOARD_UNSLOTTED;
  const value = raw.trim().toLowerCase();
  return SLOT_ORDER.includes(value) ? value : BOARD_UNSLOTTED;
}

/** The operator-facing name of a slot key. `unslotted` is not a slot — see below. */
export function boardSlotLabel(slot: string): string {
  return SLOT_LABELS[slot] ?? UNSLOTTED_LABEL;
}

// ── THE BOARD'S VOICE (voicing pass, operator-released 2026-08-12) ──────────
//
// Every operator-visible string in this module and in SlotBoard.tsx is written
// to one rule, from the step-1 taxonomy ruling: the three slots are a PERMISSION
// SYSTEM, not a priority stack. Duty / Rhythm / Fuel are co-equal, and Fuel
// exists because restorative things felt "indulgent" when they are what makes
// the operator happier and more productive.
//
// So the copy STATES FACTS ABOUT THE DAY AND NEVER VERDICTS ABOUT THE OPERATOR.
// Where a shortfall is real, the sentence names the MECHANISM as its subject
// ("the slot rules couldn't place…"), never the person. The ILB refinement holds
// underneath: the SIGNAL always renders — only its voicing is gentle. Honesty is
// never softened into vagueness, and warmth is never inflated into cheerleading.
//
// Vocabulary is taken from `tier/slots.py` rather than invented: Duty holds
// obligations (a `due_pattern` routine, a dated task), Rhythm holds practice
// (`target_cadence_days` — a cadence, NOT a schedule), Fuel holds restoration
// (`self_care`). Copy that calls Rhythm "scheduled" would be describing Duty.
//
// ── THE FENCE: ARRANGEMENT, NOT GOAL (a STAGING guard, not a permanent ban) ─
// SLOTS ARE HOW THE DAY IS ARRANGED. The balanced-day CLAIM keys to what the
// server computes, and that metric is still TIER-based until stage 3 of the
// classifier rollout flips it. So no string on this board may assert a
// slot-based GOAL or target — not "aim for all three", not "a balanced day
// needs one of each", and not scoring vocabulary that implies the slots feed a
// tally the server does not yet keep on that axis.
//
// The permissible register is standing and placement: what the slots ARE to one
// another, where an item sits, what is on the board. `boardSlotsWithADone` is
// deliberately named apart from `balanced_day` for this reason.
//
// THE GOAL FRAME IS THE RATIFIED DESTINATION, NOT THE ENEMY. The operator's own
// step-1 ruling is that the "Daily goal: balanced day" scoreline is "the real
// centerpiece, promote to headline". This fence is the STAGING discipline that
// gets there honestly — it bars the claim only while the metric behind it is
// still tier-based, so the board never promises a flip that has not shipped.
//
// SO, TO WHOEVER SHIPS STAGE 3: this is your instruction, not merely your
// prohibition. When the metric flips to the slot axis, the work is to PROMOTE
// THE SCORELINE WITH THE GOAL FRAMING, per the ruling — not to leave these
// strings as they are because a comment once said "don't". Re-read every string
// below against the flipped metric; several become sayable that are not now,
// and the scoreline is meant to become the headline.
//
// WHAT THE HOST SCORELINE MAY SAY, MEANWHILE. "N of 3 slots have something
// done" is PERMITTED: it states the day as a fact, and a count of stacks is an
// observation about the board. What is barred is framing the 3 as a TARGET —
// no "only N of 3", no progress-toward language, nothing that makes the
// remainder read as a shortfall.
//
// TWO ADJUDICATED EXCEPTIONS (ruled KEEP on review — do not "fix" them):
//   1. The residue note's "they don't count for or against your day"
//      (`SlotBoard.tsx`). "Count for or against" is the RECKONING idiom — held
//      for or against someone — not the tallying sense barred here, and the
//      held-against-him half is the line's entire value.
//   2. The coverage warning's "the balance below counts only the rest". "The
//      balance BELOW" is deictic: it points at the on-screen line, which is
//      `boardSlotsWithADone`, so it states this render's denominator and claims
//      nothing about the server metric.
// Both were rewritten once for surface consistency with this fence and both
// were reverted. The fence is about the CLAIM, not about the word "count".
export const UNSLOTTED_LABEL = 'Not sorted yet';

/**
 * The empty-stack line, PER SLOT — the highest-stakes copy on the board.
 *
 * A single flat "Nothing here today." renders under Fuel every single morning
 * (production runs {duty:2, rhythm:1}, so Fuel is empty daily), and a bare
 * nothing-here under the restorative slot reads as a verdict on the operator —
 * on the exact slot this taxonomy was built to protect. Duty and Rhythm get the
 * terse factual line their emptiness deserves (an empty Duty is relief, an empty
 * Rhythm is a fact); Fuel deliberately breaks the parallel and spends its extra
 * words on PERMISSION, because that is the whole reason the slot exists.
 *
 * All three are true of the mechanism, and none of them may imply the operator
 * fell short. A stack empties two ways, neither of which is a miss: nothing was
 * classified into that slot, or its items were SNOOZED out of view on an earlier
 * day (`ringItemVisibleToday` drops an acted item once `acted_at` is no longer
 * today, and snoozing is an act). Completion is NOT one of the ways — a done
 * item keeps its stack non-empty and settles into the drill — so an empty stack
 * can never be the record of something left undone.
 *
 * The snooze path leaves every "today" here true: the shortest rung is
 * `snooze_1d`, so a snoozed item is never due back on the day it left, and the
 * day it was snoozed it was still on the board. A sub-day rung would break that
 * and these lines would need re-checking.
 *
 * ── WHICH POPULATION EACH BRANCH GOVERNS ────────────────────────────────────
 * Spelled out because "snoozing is an act" is true but underspecified, and that
 * gap let three successive walks of this paragraph diverge — each following one
 * handler in a verb space that has two. Anchored below to the lines that WRITE
 * the state rather than to the comments describing it: every wrong revision of
 * this paragraph came from reasoning over prose.
 *
 * A slot snooze sets `state=acted` with `acted_action='snooze'` — literally
 * `feed_store.set_state(id, STATE_ACTED, action=SNOOZE_ACTED_VERB)` in
 * `_dispatch_slot_snooze` (`daily_sync/action_router.py:911`; the verb is
 * `"snooze"` at :124). So the item stays visible the day it is snoozed and
 * drops the following day, via `ringItemVisibleToday`'s `acted_at` check.
 *
 * THE TRAP, NAMED — because a correct description does not stop the next reader
 * rediscovering the wrong answer. `FeedStore.defer` / `STATE_DEFERRED`
 * (`feed/store.py`) is real, reachable and correct — FOR OTHER KINDS. It cannot
 * apply to anything on this board:
 * `DEFER_EXCLUDED_KINDS = frozenset({"slot_suggestion"})` (`action_router.py`)
 * is consulted where FEED_ACTIONS and ACTION_META are built, so a `defer_*` verb
 * is never ATTACHED to the board's only kind — unreachable at the capability
 * ceiling, not merely unused. It is the plausible wrong answer one file from the
 * right one, and it has already misled every pass over this paragraph that began
 * from the function name. Check the exclusion set before believing it applies.
 *
 * The surface fact that composes with both: home fetches `feedApi.list({})` with
 * NO state filter, deliberately (`pages/index.tsx` — the rings need today's done
 * items), so every state reaches the board and `ringItemVisibleToday` is the
 * whole gate here. The `state: 'open'` fetch is the FEED page's, not this one's.
 */
export const SLOT_EMPTY_COPY: Record<string, string> = {
  duty: 'Nothing owed today.',
  // Keyed to the CADENCE, not to the operator's doing: "No practice today."
  // has a second reading ("you didn't practice") on the one slot whose items
  // are things he does repeatedly. A practice "comes round" — that is what
  // `target_cadence_days` means. "Scheduled" is barred here; scheduling is
  // Duty's basis (slots.py rule 4, `due_pattern`), so it would name the wrong slot.
  rhythm: 'Nothing comes round today.',
  fuel: 'Nothing here yet — there’s room when you want it.',
};

/**
 * Fallback for the empty line. Unreachable for `unslotted` in practice — the
 * residue stack is appended only when it holds items, and every member lands in
 * a rendered section — but the lookup is total rather than trusting that proof.
 */
export const SLOT_EMPTY_FALLBACK = 'Nothing here today.';

/**
 * Whether a committed item was first seen BEFORE today — i.e. it is carryover.
 *
 * `created_at` is the right field and it is already exact: the feed store keeps
 * it as the EPISODE first-seen and explicitly preserves it across syncs for open
 * and deferred items ("Resetting first-seen on return would erase that he has
 * been carrying this for a week and make a long-deferred item read as brand new
 * — precisely the age signal an attention policy needs most", `feed/store.py`).
 * So no new field, no new endpoint: the age signal has been on the wire all along.
 */
export function boardIsCarryover(item: FeedItem, now: Date = new Date()): boolean {
  return !isTodayInstanceTz(item.created_at, now);
}

/**
 * Whether a slot item's deadline has already passed.
 *
 * `due_iso` is a BARE date string — `tier/compute.py` builds it with
 * `due.isoformat()` on a `date` — so this compares DAY KEYS, never timestamps.
 * Measured: `new Date('2026-08-12')` is UTC midnight, whose Halifax day key is
 * `2026-08-11`, so a timestamp compare marks a task due TODAY as overdue. ISO
 * day keys are lexicographically ordered, which makes the string compare exact.
 * Absent / empty / malformed → not overdue (never a guessed urgency).
 */
export function boardIsOverdue(item: FeedItem, now: Date = new Date()): boolean {
  const raw = (item.evidence as Record<string, unknown> | null | undefined)?.due_iso;
  if (typeof raw !== 'string') return false;
  const due = raw.trim();
  if (!/^\d{4}-\d{2}-\d{2}/.test(due)) return false;
  return due.slice(0, 10) < instanceDayKey(now);
}

/**
 * Why a carryover item is worth the operator's attention, as a sort rank
 * (lower = more attention-worthy). The cap then does the limiting.
 *
 *   0  it broke through a snooze — `snooze_breakthrough` records that the item
 *      came back EARLY for a stated reason (`crossed_due` / `moved_earlier`).
 *      The system already decided this one could not wait; that outranks age.
 *   1  overdue — the deadline is behind us.
 *   2  everything else, oldest first.
 *
 * Every input is a field the producer already stamps. Nothing here is a new
 * judgment about the operator's day; it is an ORDERING over existing evidence.
 */
export function carryoverRank(item: FeedItem, now: Date = new Date()): number {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  const breakthrough = typeof ev.snooze_breakthrough === 'string' ? ev.snooze_breakthrough.trim() : '';
  if (breakthrough) return 0;
  if (boardIsOverdue(item, now)) return 1;
  return 2;
}

/** The short "why this is still here" note for a carryover row, or null. */
export function carryoverReason(item: FeedItem, now: Date = new Date()): string | null {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  const breakthrough = typeof ev.snooze_breakthrough === 'string' ? ev.snooze_breakthrough.trim() : '';
  // Every reason takes the ITEM as its subject, not the operator — "past ITS due
  // date", not "you are overdue". `Overdue` was the one word here shaped like a
  // verdict, and it is the same fact either way; the frame is what changes.
  if (breakthrough === 'crossed_due') return 'Back early — its due date passed';
  if (breakthrough === 'moved_earlier') return 'Back early — it moved sooner';
  if (breakthrough) return 'Back early';
  if (boardIsOverdue(item, now)) return 'Past its due date';
  // Deliberately left as the vaguest line here, NOT sharpened to something like
  // "from an earlier day". This is a total fallback and the browse-on-swap call
  // site passes OVERFLOW items through it — including demoted candidates created
  // today, for which any age claim would be false. A wrong-but-confident reason
  // is worse than a thin one. (Mechanism finding, reported with the ship.)
  return 'Carried over';
}

/** One slot's stack, in the order the board renders it. */
export interface BoardStack {
  /** `duty` | `rhythm` | `fuel` | `unslotted`. */
  key: string;
  label: string;
  /** Committed today, still to do — the day's own plan. Never capped. */
  today: FeedItem[];
  /** Committed before today, still to do. Attention-ranked, capped. */
  carryover: FeedItem[];
  /** Suggested candidates awaiting a yes/no. Capped. */
  candidates: FeedItem[];
  /** Completed (any stage-done item visible today). Behind the done drill. */
  done: FeedItem[];
  /**
   * DELAYED — pushed to a later day and due back. Behind its own drill, beside
   * the done one and never inside it: "I finished this" and "I moved this" are
   * opposite facts about the day, and the whole reason this bucket exists is that
   * they were being rendered as the same one.
   */
  snoozed: FeedItem[];
  /** The long tail the caps demoted — browse-on-swap. */
  overflow: FeedItem[];
  /**
   * The committed denominator: everything on the plan today, capped or not,
   * done or not. Candidates are excluded — a candidate is not a commitment.
   * Snoozed items are excluded too: they are no longer owed today, so they are
   * in neither half of the ratio (see `ringItemCommitted`).
   */
  committedCount: number;
  /** How many of `committedCount` are done. */
  doneCount: number;
}

function createdAtAsc(a: FeedItem, b: FeedItem): number {
  return String(a.created_at ?? '').localeCompare(String(b.created_at ?? ''));
}

export interface BoardStackOptions {
  /**
   * Items the operator completed on THIS board, this session — they stay where
   * they were (struck through, green) instead of jumping into the done drill.
   *
   * COMPLETION IS A STAGE, NOT A DISAPPEARANCE. Without this, tapping ✓ makes
   * the row vanish from the stack the instant it turns green: the operator sees
   * their commitment erased rather than satisfied, and during the undo-grace
   * window the thing they might want to take back is no longer on screen. It is
   * the same rule `ringItemVisibleToday` enforces for the rings, one level in.
   *
   * Items that arrived from the server ALREADY done are a different case and do
   * go to the drill — they are yesterday's news, not this minute's. So an
   * in-place item settles into the drill on the next load, once the render that
   * confirms it has come back.
   */
  stayInPlace?: (item: FeedItem) => boolean;
  /**
   * The slot an item belongs to, OVERRIDING `boardSlotOf` — the optimistic seam
   * for the sort affordance, and the exact counterpart of `stageOf` above.
   *
   * A sort writes the operator's ruling to the backing record, and the item does
   * not carry its new `evidence.slot` until the next producer emit — which for
   * this kind is tomorrow morning. Without this seam the row would sit under
   * "Not sorted yet" for the rest of the day after he had just told the system
   * where it goes, which reads as the tap having done nothing. That is the same
   * failure the whole affordance exists to remove, so the override belongs here
   * rather than in the component: this is the GROUPING choke-point, and a
   * component that re-bucketed rows after the model had bucketed them would be a
   * second opinion about the axis.
   *
   * Defaults to `boardSlotOf`, so a caller with no overrides is byte-identical.
   */
  slotOf?: (item: FeedItem) => string;
}

/**
 * Build the board: one stack per canonical slot in `SLOT_ORDER`, plus the
 * `unslotted` residue LAST and only when it is non-empty.
 *
 * Always returns the three canonical stacks even when empty — an empty Duty
 * stack is a fact about the day and says so, rather than vanishing and leaving
 * the operator to infer whether the board ran (the same reason `tierRingBuckets`
 * always returns three buckets).
 *
 * `stageOf` is threaded rather than imported so the caller supplies the OPTIMISTIC
 * stage (its completion + accept overlays, and the board's own undo-grace) — the
 * model never re-derives a stage the surface has already resolved.
 */
export function boardStacks(
  items: FeedItem[],
  stageOf: (item: FeedItem) => RingItemStage,
  now: Date = new Date(),
  opts: BoardStackOptions = {},
): BoardStack[] {
  const stayInPlace = opts.stayInPlace ?? (() => false);
  const slotOf = opts.slotOf ?? boardSlotOf;
  const visible = items.filter(
    (it) => it.kind === 'slot_suggestion' && ringItemVisibleToday(it, now),
  );

  const bySlot = new Map<string, FeedItem[]>();
  for (const key of SLOT_ORDER) bySlot.set(key, []);
  for (const it of visible) {
    const key = slotOf(it);
    if (!bySlot.has(key)) bySlot.set(key, []);
    bySlot.get(key)?.push(it);
  }

  const build = (key: string, members: FeedItem[]): BoardStack => {
    // `inPlace` holds the rows that render where they already were: everything
    // not yet done, PLUS anything completed on this board this session.
    const done: FeedItem[] = [];
    const snoozed: FeedItem[] = [];
    const inPlace: FeedItem[] = [];
    const suggested: FeedItem[] = [];
    let committedCount = 0;
    let doneCount = 0;
    for (const it of members) {
      const stage = stageOf(it);
      if (stage === 'suggested') {
        suggested.push(it);
        continue;
      }
      // SNOOZED leaves the ratio entirely — before `committedCount` is bumped,
      // which is the line that matters. A delayed item is not owed today (so it
      // is not the denominator) and certainly not finished (so it is not the
      // numerator). It still renders, in its own drill, because an item that
      // vanished the moment it was moved would be a lost commitment.
      if (stage === 'snoozed') {
        snoozed.push(it);
        continue;
      }
      // Committed = on today's plan, done or not. Counted before the display
      // partition so the scoreline is identical whether a completion is sitting
      // in place or has settled into the drill.
      committedCount += 1;
      if (stage === 'done') {
        doneCount += 1;
        if (stayInPlace(it)) inPlace.push(it);
        else done.push(it);
      } else {
        inPlace.push(it);
      }
    }

    // CARRYOVER-FIRST is about the PLANNING order (carryover is answered before
    // new suggestions are offered), so carryover items are partitioned OUT of
    // `today` rather than listed twice.
    const carriedAll = inPlace
      .filter((it) => boardIsCarryover(it, now))
      .sort((a, b) => carryoverRank(a, now) - carryoverRank(b, now) || createdAtAsc(a, b));
    const today = inPlace.filter((it) => !boardIsCarryover(it, now)).sort(createdAtAsc);

    const carryover = carriedAll.slice(0, CARRYOVER_CAP);
    const candidates = suggested.slice(0, CANDIDATE_CAP);
    // The caps DEMOTE, they never drop: everything past a cap is reachable in
    // browse-on-swap. A capped item that simply disappeared would be the same
    // lost-commitment failure the rings' date-scoping was written to avoid.
    const overflow = [...carriedAll.slice(CARRYOVER_CAP), ...suggested.slice(CANDIDATE_CAP)];

    return {
      key,
      label: boardSlotLabel(key),
      today,
      carryover,
      candidates,
      done,
      snoozed,
      overflow,
      committedCount,
      doneCount,
    };
  };

  const stacks = SLOT_ORDER.map((key) => build(key, bySlot.get(key) ?? []));
  const residue = bySlot.get(BOARD_UNSLOTTED) ?? [];
  // The residue is appended only when it exists. An empty `unslotted` stack is
  // not an ILB case — there is nothing to report about a category that is,
  // correctly, empty; the canonical three always render regardless.
  if (residue.length > 0) stacks.push(build(BOARD_UNSLOTTED, residue));
  return stacks;
}

/**
 * The balanced-day scoreline: how many of the THREE CANONICAL slots have at
 * least one thing done. `unslotted` is excluded by construction (it is not in
 * `SLOT_ORDER`), which is the exclusion `tier/slots.py` calls for — an
 * unreachable daily goal reads as personal failure, not as a migration artifact.
 *
 * This is a RENDER of the day, not the `balanced_day` metric: that metric is
 * still tier-based server-side and stage 3 of the classifier rollout is what
 * flips it. Naming them apart here so nobody reads this as the flip.
 */
export function boardSlotsWithADone(stacks: BoardStack[]): number {
  return stacks.filter((s) => SLOT_ORDER.includes(s.key) && s.doneCount > 0).length;
}

/**
 * Every item a stack holds, across all of its sections. One owner for the sum.
 *
 * `snoozed` is counted here — this is EXISTENCE, not the done ratio. A snoozed
 * item leaves the tally (`committedCount`/`doneCount`) but it has not left the
 * board: it is on screen, one drill in, and it is reachable. Omitting it would
 * make a morning with three pushed duties report "Nothing on the board yet today"
 * and would deflate `boardCoverage`'s denominator — trading the bug this lane
 * fixes for a quieter one in the same sentence.
 */
export function boardStackSize(stack: BoardStack): number {
  return (
    stack.today.length +
    stack.carryover.length +
    stack.candidates.length +
    stack.done.length +
    stack.snoozed.length +
    stack.overflow.length
  );
}

/**
 * The coverage floor below which the board admits it is working from part of the
 * day. **Operator-ratified at 0.80 on 2026-08-12** ("use the 80% default and
 * we'll see how it looks in practice"), filling the dial the 2026-08-04
 * slot-classifier design §8 specified and left unset. It is a DIAL, not a
 * constant of nature — moving it is a one-line edit plus its boundary pin.
 *
 * The comparison is `< SLOT_COVERAGE_WARN`, so exactly 80% does NOT warn.
 * Measured rather than assumed, because that edge is a float question: every
 * ratio reducing to 4/5 (4/5, 8/10, 16/20, 40/50, 80/100 …) is exactly `0.8` in
 * IEEE double, so the boundary is exact and needs no epsilon.
 */
export const SLOT_COVERAGE_WARN = 0.8;

/** How much of today the slot classifier could actually answer for. */
export interface BoardCoverage {
  /** Items in a canonical slot. */
  slotted: number;
  /** Items the classifier refused to guess at. */
  unslotted: number;
  total: number;
  /**
   * Slotted share, 0..1 — or **null when there is nothing to measure**.
   *
   * The empty board is a THIRD state, expressed in the type so that a warning
   * about it is unrepresentable rather than merely unrendered: with no
   * denominator there is no claim to make.
   *
   * DELIBERATE DIVERGENCE from the backend's `SlotCoverage.coverage_pct`, which
   * returns 100.0 on an empty day ("nothing was left unanswered, so nothing is
   * missing"). That is right for a LOG metric — 0.0 would read as total
   * classifier failure on a quiet day — and wrong for a UI claim. Note the two
   * agree on every rendered outcome, including this one (100.0 clears the
   * threshold, null cannot reach it, neither warns); they differ only in what
   * they ASSERT. Don't "fix" either to match the other.
   */
  fraction: number | null;
}

/**
 * Coverage from what the board already holds — the same populations
 * `boardStacks` split, so this reads no new input and cannot disagree with what
 * is on screen. Anything outside `SLOT_ORDER` counts as residue (matching
 * `boardSlotsWithADone`'s exclusion), so a future fourth canonical slot is
 * counted as covered the moment it joins the order.
 */
export function boardCoverage(stacks: BoardStack[]): BoardCoverage {
  let slotted = 0;
  let unslotted = 0;
  for (const stack of stacks) {
    const n = boardStackSize(stack);
    if (SLOT_ORDER.includes(stack.key)) slotted += n;
    else unslotted += n;
  }
  const total = slotted + unslotted;
  return { slotted, unslotted, total, fraction: total > 0 ? slotted / total : null };
}

/** Whether the board should admit it is missing part of the day. */
export function boardCoverageIsLow(coverage: BoardCoverage): boolean {
  return coverage.fraction !== null && coverage.fraction < SLOT_COVERAGE_WARN;
}
