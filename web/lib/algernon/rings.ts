import type { FeedItem } from './feed';

// Ring DATA binding for the B-phase segmented rings (B3-4), kept separate from
// the pure geometry (ringGeometry.ts) so grouping is unit-testable on its own.
//
// WHY TIER, NOT duty/rhythm/fuel: the operator sketch draws three "balanced day"
// rings (duty / rhythm / fuel), and feedConstants.SLOT_LABELS carries that
// vocabulary — but the Phase-A `slot_suggestion` producer
// (src/alfred/brief/feed_producer.py) emits NO such bucket. Its evidence carries
// `tier` ∈ {1,2,3} (T1/T2/T3 urgency lanes) and no completion flag. There is also
// no transport `tier_curation` READ route. So the only 3-way grouping real data
// supports today is the tier, and every segment is "planned" (no done signal).
// Realising the duty/rhythm/fuel vision needs a backend `slot` classifier — a
// separate slice. See the B3-4 report. When that lands, swap the grouping key
// here; the geometry and the RingsHeader render are untouched.

export const TIER_RING_ORDER = [1, 2, 3] as const;
export const TIER_RING_LABEL: Record<number, string> = { 1: 'T1', 2: 'T2', 3: 'T3' };

export interface RingBucket {
  /** Stable bucket id — the tier as a string ("1" | "2" | "3"). */
  key: string;
  tier: number;
  /** Short ring label ("T1"). */
  label: string;
  items: FeedItem[];
}

/**
 * The tier (1/2/3) a slot_suggestion feed item belongs to, or null when the
 * evidence tier is missing / out of range. Evidence is unschema'd, so coerce
 * defensively — a number or a numeric string both resolve.
 */
export function ringTierOf(item: FeedItem): number | null {
  const raw = (item.evidence as Record<string, unknown> | null | undefined)?.tier;
  const n = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isInteger(n) && n >= 1 && n <= 3 ? n : null;
}

// The completion action verbs the board sends for a slot item (Phase C).
export const RING_ACTION_DONE = 'done';
export const RING_ACTION_UNDO = 'undo_done';

// The honest per-lane truth for a NON-board-completable lane — after C1b wired the
// task-completion writer, that's now ONLY a slot with an unknown / unstamped origin
// (no origin, no routine_record, tier < 3). Shared by every completion surface
// (rings panel + feed row) so the copy can't drift. Task / routine / free-text T3
// are all board-completable now, so none of them surface this note.
export const COMPLETION_UNAVAILABLE_HINT = "Completion isn't available for this item";

// --- C2 slot lifecycle stage (suggested → planned → done) --------------------
export type RingItemStage = 'suggested' | 'planned' | 'done';

/**
 * The C2 lifecycle stage of a slot_suggestion — the single choke-point (one seam)
 * that drives which verbs each surface shows. Precedence DONE > SUGGESTED > PLANNED.
 *
 * The `state==='acted'` OVERLOAD (a C1b completion AND a C2 accept both set acted) is
 * resolved by the VERB stamped on the state event — `item.acted_action`
 * ("accept" | "done" | absent-legacy), added in the C2 backend round:
 *   1. evidence.done === true                 → DONE  (vault-completed, still emitted)
 *   2. acted && acted_action === 'accept'     → PLANNED (accepted/committed — ✓ ENABLED
 *        so the operator can complete what he accepted this morning; NO undo rendered)
 *   3. acted otherwise (done / absent-legacy) → DONE  (a completion; C1b unchanged)
 *   4. open + candidate === true              → SUGGESTED; open otherwise → PLANNED
 * `candidate` does NOT discriminate acted items: the backend gate allows ✓ done on an
 * acted-by-accept item, and the dispatcher never mutates evidence, so a genuinely
 * done-after-accept item keeps `candidate=true` — keying on it would misread it as
 * PLANNED (the exact flow the verb stamp exists for). ABSENT acted_action degrades to
 * legacy DONE (same never-a-guess discipline as the render-present accept gating).
 */
export function ringItemStage(item: FeedItem): RingItemStage {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  if (ev.done === true) return 'done';
  if (item.state === 'acted') return item.acted_action === 'accept' ? 'planned' : 'done';
  return ev.candidate === true ? 'suggested' : 'planned';
}

/**
 * The C2 stage OVERLAY (#16 item 8 — ONE seam, was duplicated ~6 lines in RingsHeader +
 * FeedRow): the optimistic hooks layered over the base `ringItemStage`. Precedence:
 *   1. completion.effectiveDone → DONE   (a just-completed item flips green)
 *   2. accept.accepted          → PLANNED (an accepted candidate flips candidate→committed)
 *   3. base ringItemStage, with an optimistic UNDO (raw base 'done' but completion overrode
 *      it not-done) returning the item to PLANNED, not falling through to the raw 'done'.
 * The hooks are structural + optional so the rings panel (both present) and a feed row
 * (either absent) share this single implementation. This MUST stay the only copy — a 4th
 * stage or a precedence change lands HERE, once, and both surfaces move in lockstep.
 */
export function effectiveStageOf(
  item: FeedItem,
  completion: { effectiveDone: (it: FeedItem) => boolean },
  accept: { accepted: (id: string) => boolean } | null | undefined,
): RingItemStage {
  if (completion.effectiveDone(item)) return 'done';
  if (accept?.accepted(item.id)) return 'planned';
  const base = ringItemStage(item);
  return base === 'done' ? 'planned' : base;
}

/**
 * Whether a ring item is complete — the single choke-point for green/strikethrough,
 * now STAGE-derived so it can never disagree with `ringItemStage`. (Pre-C2 this was
 * `state==='acted' || evidence.done`; the ONLY behaviour change is that an
 * acted-but-still-candidate item — a just-accepted, not-yet-reconciled slot — reads
 * as NOT done, since accept means committed/planned, not completed.)
 */
export function ringItemDone(item: FeedItem): boolean {
  return ringItemStage(item) === 'done';
}

/** A SUGGESTED slot — an auto-surfaced candidate not yet on today's plan (deck-dealt; shows Accept). */
export function ringItemSuggested(item: FeedItem): boolean {
  return ringItemStage(item) === 'suggested';
}

/** A COMMITTED slot — on today's plan (planned or done). The ring COUNT tallies these; candidates are excluded. */
export function ringItemCommitted(item: FeedItem): boolean {
  return ringItemStage(item) !== 'suggested';
}

/**
 * Whether a slot item's lane can be completed FROM THE BOARD (enables the ✓), per
 * the completion-semantics matrix — computed client-side from the producer's own
 * stamped evidence fields:
 *   - origin === "task"  → true  (C1b: task-completion writer wired — DONE-only;
 *                                  see ringItemUndoable for the no-board-undo carve)
 *   - routine_record set → true  (routine-item lane → routine_done writer)
 *   - tier === 3         → true  (free-text T3 lane → tier_done writer)
 *   - otherwise          → false (unknown origin → never a guessed write)
 * A false lane surfaces the honest COMPLETION_UNAVAILABLE_HINT (disabled ✓ in the
 * rings panel, a plain note in the feed) — never a dead control that pretends to
 * work; the router is the ground truth and returns `unsupported_item` if the
 * client and backend ever disagree.
 */
export function ringItemCompletable(item: FeedItem): boolean {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  if (ev.origin === 'task') return true;
  if (ev.routine_record) return true;
  if (ev.tier === 3 || ev.tier === '3') return true;
  return false;
}

/**
 * Whether a DONE slot item's lane can be UN-done from the board (gates the Undo
 * control on a done row). Task-backed items are completable but NOT board-undoable
 * in v1: `undo_done` on a task returns `unsupported_item` (422, "undo isn't
 * available for tasks from the board yet — undo via chat"), so the board never
 * surfaces the control — nothing dead to click, the 422 stays a belt for a stale
 * client. Routine + free-text T3 lanes are both completable AND undoable. The
 * `ringItemCompletable` conjunct is load-bearing: it keeps unknown / missing-origin
 * lanes (not completable) also non-undoable.
 */
export function ringItemUndoable(item: FeedItem): boolean {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  return ringItemCompletable(item) && ev.origin !== 'task';
}

// The instance's local timezone for day-scoping (same America/Halifax the composer
// uses for compose-mode). One deploy = one instance; flagged, not per-instance-
// configurable in the web yet.
const RING_TZ = 'America/Halifax';

/**
 * YYYY-MM-DD in the instance tz. EXPORTED because the board's overdue test has
 * to compare a BARE `due_iso` date ("2026-08-12", what `date.isoformat()` emits)
 * against today, and the only correct instrument for that is a day-key string
 * compare. Parsing a bare date with `new Date()` lands it at UTC midnight, which
 * west of Greenwich is the PREVIOUS calendar day — so a task due today reads as
 * overdue. Measured, not reasoned: `new Date('2026-08-12')` → its Halifax day key
 * is `2026-08-11`. One owner for the day key; see `boardIsOverdue`.
 */
export function instanceDayKey(d: Date): string {
  // YYYY-MM-DD in the instance tz — the calendar-day identity for date-scoping.
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: RING_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

/** Whether a UTC ISO timestamp falls on TODAY in the instance timezone. */
export function isTodayInstanceTz(iso: string | null | undefined, now: Date = new Date()): boolean {
  if (!iso) return false;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return false;
  return instanceDayKey(t) === instanceDayKey(now);
}

/**
 * Whether a slot item belongs on TODAY's rings. Completion changes an item's STAGE
 * (colour), not its EXISTENCE — a board item (state=acted) STAYS on the ring all day
 * instead of vanishing and leaving a false red-empty ring. `open` items always show;
 * `acted` items show ONLY if acted TODAY (acted_at in the instance tz) — stable keys
 * persist across days, so yesterday's acted items must not count. Anything else
 * (acked / expired / acted-not-today) has left the day.
 *
 * NOTE: the accept-acted PHANTOM (a T3/5%-task accept's spent old id, which persists
 * as acted-by-accept in the raw list forever) is suppressed by the Case-B dedup in
 * `tierRingBuckets` — an OPEN committed sibling supersedes it — NOT here, because a
 * TRANSIENT accepted item with no open sibling yet must still show (as PLANNED)
 * during the accept→re-emit window.
 */
export function ringItemVisibleToday(item: FeedItem, now: Date = new Date()): boolean {
  if (item.state === 'open') return true;
  if (item.state === 'acted') return isTodayInstanceTz(item.acted_at, now);
  return false;
}

/**
 * The Case-B dedup key: two slot items are the SAME logical commitment when they
 * share (tier, evidence.name). A T3 / 5%-task accept re-emits the committed item
 * under a NEW id (text:-keyed) while the spent acted-by-accept OLD id persists in the
 * raw store list forever.
 *
 * ORIGIN DROPS OUT (verified against c2's T3 before/after at 94a6dcfd): a T3 TASK
 * candidate's committed re-emit FLIPS origin (task → routine_item — the free-text
 * T3Entry is re-read as routine_item regardless of the candidate's origin), so
 * keying on origin would MISS the task-lane phantom (the routine lane keeps origin,
 * the task lane doesn't). `name` is stable across the flip in both lanes. Accepted
 * false-positive: a routine item + a same-name task in one tier (one accepted, one
 * open) — worst case a transient chip hides behind an identically-named open item
 * for a few hours (an existence-vs-stage non-event, not a lost commitment).
 */
function slotDedupKey(item: FeedItem): string {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  return `${ringTierOf(item) ?? ''}|${String(ev.name ?? '')}`;
}

/**
 * Group `slot_suggestion` feed items into the three tier rings, in TIER_RING_ORDER.
 * Includes today's suggested (open+candidate) / planned / done items per
 * `ringItemVisibleToday` — so the rings show all three stages all day, never
 * dropping a completion.
 *
 * CASE-B DEDUP: an accepted item's committed re-emit (an OPEN sibling on the same
 * (tier, name) — see slotDedupKey for why origin drops out) supersedes the spent
 * acted-by-accept phantom — the new-id T3 / 5%-task path leaves BOTH in the raw list
 * (the FE list route returns all states; compute-dedup doesn't cover it). Drop the
 * acted-by-accept phantom so the committed denominator isn't double-counted; a
 * TRANSIENT accepted item with no open sibling yet is kept (it shows as PLANNED
 * during the accept→re-emit window).
 *
 * Non-slot / invalid-tier / not-today items are dropped (defensive). Always returns
 * exactly three buckets so an empty tier renders its own (red) empty ring.
 */
export function tierRingBuckets(items: FeedItem[], now: Date = new Date()): RingBucket[] {
  const visible = items.filter(
    (it) => it.kind === 'slot_suggestion' && ringItemVisibleToday(it, now) && ringTierOf(it) != null,
  );
  const openKeys = new Set(visible.filter((it) => it.state === 'open').map(slotDedupKey));
  const deduped = visible.filter(
    (it) => !(it.state === 'acted' && it.acted_action === 'accept' && openKeys.has(slotDedupKey(it))),
  );
  const byTier = new Map<number, FeedItem[]>();
  for (const t of TIER_RING_ORDER) byTier.set(t, []);
  for (const it of deduped) {
    const tier = ringTierOf(it);
    if (tier != null) byTier.get(tier)?.push(it);
  }
  return TIER_RING_ORDER.map((t) => ({
    key: String(t),
    tier: t,
    label: TIER_RING_LABEL[t],
    items: byTier.get(t) ?? [],
  }));
}
