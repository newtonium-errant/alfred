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
 *   - origin === "task"  → false (v1 UNSUPPORTED — no talker task-completion writer)
 *   - routine_record set → true  (routine-item lane → routine_done writer)
 *   - tier === 3         → true  (free-text T3 lane → tier_done writer)
 *   - otherwise          → false (unknown origin → never a guessed write)
 * A false lane keeps the ✓ honestly disabled with its "not completable here yet"
 * microcopy; the router is the ground truth and returns `unsupported_item` if the
 * client and backend ever disagree.
 */
export function ringItemCompletable(item: FeedItem): boolean {
  const ev = (item.evidence as Record<string, unknown> | null | undefined) ?? {};
  if (ev.origin === 'task') return false;
  if (ev.routine_record) return true;
  if (ev.tier === 3 || ev.tier === '3') return true;
  return false;
}

/**
 * Group open `slot_suggestion` feed items into the three tier rings, in
 * TIER_RING_ORDER. Non-slot_suggestion items and items with a missing / invalid
 * tier are dropped (defensive). Always returns exactly three buckets so an empty
 * tier renders its own (red) empty ring rather than vanishing.
 */
export function tierRingBuckets(items: FeedItem[]): RingBucket[] {
  const byTier = new Map<number, FeedItem[]>();
  for (const t of TIER_RING_ORDER) byTier.set(t, []);
  for (const it of items) {
    if (it.kind !== 'slot_suggestion') continue;
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
