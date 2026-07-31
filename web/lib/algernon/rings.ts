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
export const COMPLETION_UNAVAILABLE_HINT = 'Completion arrives later';

/**
 * Whether a ring item is complete — the single choke-point for green/strikethrough.
 * TWO signals, per the board-completion contract: an item completed VIA the board
 * persists as `state === "acted"` (the router's set_state); an item completed in
 * the vault (and still emitted) carries `evidence.done === true`. Compute both —
 * a board-completed item that compute then suppresses won't re-emit `done`, so
 * `done` alone would lose it.
 */
export function ringItemDone(item: FeedItem): boolean {
  if (item.state === 'acted') return true;
  return (item.evidence as Record<string, unknown> | null | undefined)?.done === true;
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

function instanceDayKey(d: Date): string {
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
 * Whether a slot item belongs on TODAY's rings. Completion changes an item's
 * STAGE (colour), not its EXISTENCE — so a board-done item (state=acted) STAYS on
 * the ring all day (green) instead of vanishing and leaving a false red-empty
 * ring. `open` items (planned, or open-with-evidence.done) always show; `acted`
 * items show ONLY if completed TODAY (acted_at in the instance tz) — stable keys
 * persist across days, so yesterday's acted items must not count. Anything else
 * (acked / expired / acted-not-today) has left the day.
 */
export function ringItemVisibleToday(item: FeedItem, now: Date = new Date()): boolean {
  if (item.state === 'open') return true;
  if (item.state === 'acted') return isTodayInstanceTz(item.acted_at, now);
  return false;
}

/**
 * Group `slot_suggestion` feed items into the three tier rings, in
 * TIER_RING_ORDER. Includes today's DONE items (state=acted, acted_at today) as
 * well as open ones, per `ringItemVisibleToday` — so the rings show
 * planned + done all day, never dropping a completion. Non-slot / invalid-tier /
 * not-today items are dropped (defensive). Always returns exactly three buckets
 * so an empty tier renders its own (red) empty ring rather than vanishing.
 */
export function tierRingBuckets(items: FeedItem[], now: Date = new Date()): RingBucket[] {
  const byTier = new Map<number, FeedItem[]>();
  for (const t of TIER_RING_ORDER) byTier.set(t, []);
  for (const it of items) {
    if (it.kind !== 'slot_suggestion') continue;
    if (!ringItemVisibleToday(it, now)) continue;
    const tier = ringTierOf(it);
    if (tier == null) continue;
    byTier.get(tier)?.push(it);
  }
  return TIER_RING_ORDER.map((t) => ({
    key: String(t),
    tier: t,
    label: TIER_RING_LABEL[t],
    items: byTier.get(t) ?? [],
  }));
}
