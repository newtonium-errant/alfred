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

// TODO(voicing): every operator-visible string in this module and in SlotBoard.tsx
// is a plain placeholder. The permission-granting no-shame voice is the
// prompt-tuner's to write; the SIGNAL is what ships here. Listed in the C1 report.
export const UNSLOTTED_LABEL = 'Not sorted yet';

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
  // TODO(voicing) — placeholder copy.
  if (breakthrough === 'crossed_due') return 'Back early — its due date passed';
  if (breakthrough === 'moved_earlier') return 'Back early — it moved sooner';
  if (breakthrough) return 'Back early';
  if (boardIsOverdue(item, now)) return 'Overdue';
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
  /** The long tail the caps demoted — browse-on-swap. */
  overflow: FeedItem[];
  /**
   * The committed denominator: everything on the plan today, capped or not,
   * done or not. Candidates are excluded — a candidate is not a commitment.
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
  const visible = items.filter(
    (it) => it.kind === 'slot_suggestion' && ringItemVisibleToday(it, now),
  );

  const bySlot = new Map<string, FeedItem[]>();
  for (const key of SLOT_ORDER) bySlot.set(key, []);
  for (const it of visible) {
    const key = boardSlotOf(it);
    if (!bySlot.has(key)) bySlot.set(key, []);
    bySlot.get(key)?.push(it);
  }

  const build = (key: string, members: FeedItem[]): BoardStack => {
    // `inPlace` holds the rows that render where they already were: everything
    // not yet done, PLUS anything completed on this board this session.
    const done: FeedItem[] = [];
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
