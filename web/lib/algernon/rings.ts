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

// --- slot lifecycle stage (suggested → planned → done | snoozed) -------------
export type RingItemStage = 'suggested' | 'planned' | 'done' | 'snoozed';

/** The verb the store stamps on a snoozed item (`acted_action`). */
export const SNOOZE_ACTED_VERB = 'snooze';

/**
 * THE VERB → STAGE MAP. The whole point of this table is that it is a LOOKUP and
 * not a binary: the bug it replaces was
 * `acted_action === 'accept' ? 'planned' : 'done'`, which routed every verb that
 * was not `accept` — including `snooze` — into the DONE rendering. On 2026-08-16
 * the operator snoozed three overdue T1 duties and the home page struck them
 * through as "✓ DONE", "3/3 done", "All done here today": a delay recorded as a
 * completion, on the surface he reads first every morning.
 *
 * THE VERB SPACE, enumerated from the source rather than from memory. The
 * capability ceiling for `slot_suggestion` is `FEED_ACTIONS['slot_suggestion']`
 * (daily_sync/action_router.py) — exactly: done, done_1d, done_2d, done_3d,
 * undo_done, accept, snooze_1d, snooze_3d, snooze_7d, snooze_until_i_say,
 * unsnooze, and the three sort verbs (which never persist — the board sort
 * dispatcher deliberately touches no state). Of those, the verbs that can
 * PERSIST as `acted_action` are these:
 *   - `done`   → DONE     (DONE_ACTION — the plain ✓, done today)
 *   - `done_1d` / `done_2d` / `done_3d`
 *              → DONE     (BACKDATE_DONE_ACTIONS, 2026-08-20 — the when-family
 *                          rungs. The stamp is the TRUE verb, NOT collapsed to
 *                          `done` like the snooze rungs, because the undo path
 *                          derives the date to remove from it. A completed-
 *                          yesterday item is exactly as finished as a
 *                          completed-today one.)
 *   - `accept` → PLANNED  (ACCEPT_ACTION — committed, not
 *                          completed; ✓ stays ENABLED so he can finish what he
 *                          accepted this morning, and no undo is rendered)
 *   - `snooze` → SNOOZED  (ALL FOUR duration rungs collapse
 *                          to this one verb at the stamp site — the durations
 *                          differ but the STATE is one, and the until lives in
 *                          the sidecar; the done rungs are the opposite call
 *                          because their writes genuinely differ)
 * `undo_done` and `unsnooze` never appear here: both set STATE_OPEN, and a
 * non-acted transition clears the verb (feed/store.py:180). The generic act path
 * (action_router.py:2239) stamps other kinds' verbs — ack / confirm / reject /
 * spam / adopt / … — but no non-slot kind reaches this function today: every
 * `FeedRow` call site that supplies a completion hook is gated on
 * `kind === 'slot_suggestion'`. That gating is at the CALL SITES, not in here,
 * which is exactly why the fallback below is not decoration.
 *
 * :2239 IS NOT THE ONLY GENERIC STAMP — named here so the next auditor meets the
 * surprise instead of re-deriving it. `action_router.py:1075`
 * (`_dispatch_contact_pattern`) is a second
 * `set_state(..., STATE_ACTED, action=action_id)`, stamping `adopt` / `ignore`.
 * The claim above survives it: that dispatcher serves the `pattern_surfaced`
 * kind ONLY and is reached through a kind gate at :2075, so no slot can arrive
 * there and neither verb can land on an item this function sees. Verified at
 * source, not assumed — and it is the reason the enumeration above is phrased as
 * "the verbs that can PERSIST on a slot", not "the verbs the router can stamp".
 *
 * ADDING A VERB: add it HERE, with its stage. Do not re-introduce a branch.
 */
export const ACTED_VERB_STAGE: Readonly<Record<string, RingItemStage>> = {
  accept: 'planned',
  done: 'done',
  // The backdated rungs (2026-08-20). Absent from this map, the PLANNED
  // fallback below would hand a just-backdated item a live ✓ — completion
  // offered on something already complete, the accepted-then-ignored wall.
  done_1d: 'done',
  done_2d: 'done',
  done_3d: 'done',
  [SNOOZE_ACTED_VERB]: 'snoozed',
};

/**
 * The ruled default for an acted verb this map does not know — a future verb, or
 * a non-slot kind reaching a surface that grew a completion hook.
 *
 * IT IS NOT 'done', AND THAT IS THE RULING. An unknown verb rendering as
 * completion is precisely the 2026-08-16 bug for a verb that does not exist yet:
 * the failure would be silent, would land on the morning surface, and would claim
 * the operator finished something he did not. PLANNED is the honest degrade —
 * "something happened to this and it is not finished" — and it fails toward
 * showing work rather than toward erasing it.
 */
export const ACTED_VERB_FALLBACK_STAGE: RingItemStage = 'planned';

/**
 * ABSENT `acted_action` — distinct from an unknown verb, and deliberately still
 * DONE. This is the pre-verb-stamp legacy shape (feed/store.py: "A legacy /
 * verbless acted event (e.g. reconcile-decided) → None"), which C1b has always
 * read as a completion; the verb stamp is FORWARD ONLY, so old events on disk are
 * verbless and stay so. `null`/`undefined` is detectably different from a string
 * this map has not heard of, so the two cases do not have to share a default.
 */
export const ACTED_LEGACY_STAGE: RingItemStage = 'done';

/**
 * The lifecycle stage of a slot_suggestion — the single choke-point (one seam)
 * that drives which verbs each surface shows, and whether a row renders as
 * finished. Precedence DONE > (verb) > SUGGESTED > PLANNED:
 *   1. evidence.done === true  → DONE (vault-completed, still emitted)
 *   2. acted                   → ACTED_VERB_STAGE[verb], with the two ruled
 *                                defaults above for absent / unknown verbs
 *   3. open + candidate        → SUGGESTED; open otherwise → PLANNED
 *
 * `candidate` does NOT discriminate acted items: the backend gate allows ✓ done on
 * an acted-by-accept item, and the dispatcher never mutates evidence, so a
 * genuinely done-after-accept item keeps `candidate=true` — keying on it would
 * misread it as PLANNED (the exact flow the verb stamp exists for).
 */
export function ringItemStage(item: FeedItem): RingItemStage {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  if (ev.done === true) return 'done';
  if (item.state === 'acted') {
    const verb = item.acted_action;
    if (verb == null) return ACTED_LEGACY_STAGE;
    return ACTED_VERB_STAGE[verb] ?? ACTED_VERB_FALLBACK_STAGE;
  }
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
 * (either absent) share this single implementation. This MUST stay the only copy — a 5th
 * stage or a precedence change lands HERE, once, and both surfaces move in lockstep.
 *
 * SNOOZED OUTRANKS THE ACCEPT OPTIMISM, and that ordering is load-bearing rather
 * than incidental. `accept.accepted` is a SESSION-LOCAL set of ids the operator
 * accepted since page load; `snoozed` comes from the verb the SERVER stamped. Both
 * can be true of one item, and the sequence that does it is ordinary: accept a
 * candidate (it becomes planned, and a planned row is exactly what the Snooze
 * control is offered on), then snooze it. With the accept check first, that item
 * rendered PLANNED again — the snooze erased, back in the committed denominator,
 * offering ✓ Done on something just pushed out of today. That is this lane's bug
 * reappearing inside its own fix, one seam below where it was found.
 *
 * The rule the order encodes: the accept optimism is a stand-in for a server stamp
 * that has not arrived yet, so it must never outrank a stamp that HAS arrived. The
 * accept necessarily happened EARLIER than the snooze — you cannot snooze a row you
 * have not yet put on the plan — so the newer fact wins.
 *
 * `completion.effectiveDone` stays above both: it is a genuine completion the
 * operator just performed. The two cannot legitimately co-occur anyway (the router
 * refuses a snooze on a done item, and no snoozed row renders a ✓), so this is the
 * safe direction on an unreachable pair rather than a live precedence.
 *
 * The one way to become snoozed is the stamped verb, and the one way to stop being
 * snoozed is `unsnooze`, which returns the item to `open` server-side.
 */
export function effectiveStageOf(
  item: FeedItem,
  completion: { effectiveDone: (it: FeedItem) => boolean },
  accept: { accepted: (id: string) => boolean } | null | undefined,
): RingItemStage {
  if (completion.effectiveDone(item)) return 'done';
  const base = ringItemStage(item);
  if (base === 'snoozed') return 'snoozed';
  if (accept?.accepted(item.id)) return 'planned';
  return base === 'done' ? 'planned' : base;
}

/**
 * Whether a ring item is complete — the single choke-point for green/strikethrough,
 * now STAGE-derived so it can never disagree with `ringItemStage`. (Pre-C2 this was
 * `state==='acted' || evidence.done`; the ONLY behaviour change is that an
 * acted-but-still-candidate item — a just-accepted, not-yet-reconciled slot — reads
 * as NOT done, since accept means committed/planned, not completed.)
 *
 * A SNOOZED item is NOT done. That is the whole fix: every green tick, every
 * strikethrough and every done tally on every surface reads this one predicate (or
 * the stage behind it), so a delay stops being reported as a completion in one
 * place rather than in nine.
 */
export function ringItemDone(item: FeedItem): boolean {
  return ringItemStage(item) === 'done';
}

/** A SUGGESTED slot — an auto-surfaced candidate not yet on today's plan (deck-dealt; shows Accept). */
export function ringItemSuggested(item: FeedItem): boolean {
  return ringItemStage(item) === 'suggested';
}

/**
 * A SNOOZED slot — the operator pushed it to a later day. It is neither work owed
 * today nor anything he finished, so it is excluded from BOTH sides of every
 * done/total ratio (see `ringItemCommitted`), and it renders with its own
 * treatment rather than borrowing done's.
 */
export function ringItemSnoozed(item: FeedItem): boolean {
  return ringItemStage(item) === 'snoozed';
}

/**
 * A COMMITTED slot — on today's plan (planned or done). The ring COUNT tallies
 * these; candidates are excluded.
 *
 * SNOOZED IS EXCLUDED TOO, and it is excluded from the DENOMINATOR on purpose. A
 * snoozed item is not owed today — the operator decided that himself — so counting
 * it would produce "0/3 done" for a morning he deliberately cleared, which is a nag
 * dressed as a fact. Counting it in the numerator would be the original bug. It
 * belongs to neither half of the ratio, so it leaves the ratio entirely.
 */
export function ringItemCommitted(item: FeedItem): boolean {
  const stage = ringItemStage(item);
  return stage !== 'suggested' && stage !== 'snoozed';
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
