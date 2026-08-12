import type { FeedItem } from './feed';

// The TIME SUBSTRATE for the Awareness feed's timeline view (D7 — "time-extent
// rendering adopted everywhere applicable"; the Waterline's portable insight,
// graduated by the operator as a VIEW of the feed rather than a new surface).
//
// Everything here is pure and DOM-free so the layout maths — which is the part
// that can silently lie about the operator's day — is unit-pinned without a
// renderer. The view module does placement and paint only.
//
// TRUST: `starts_at` / `ends_at` are typed strings on the wire but arrive from
// the same unschema'd backend payload as `evidence`, so every reader here takes
// `unknown` and validates. A wrong interval moves weather on the operator's
// timeline, which is worse than showing none.

/** Milliseconds-epoch interval. `end: null` = NO KNOWN END, never zero-length. */
export interface Extent {
  start: number;
  end: number | null;
}

/** The rendered band's bounds, in milliseconds-epoch. */
export interface TimeWindow {
  from: number;
  to: number;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

// How far either side of NOW the substrate may reach. The band is a LINEAR time
// axis, so its honesty depends on staying bounded: one item dated next month
// would otherwise compress the whole day into a few pixels and the view would
// claim to show a day while showing a smear. Items outside these bounds are not
// dropped — `splitByWindow` hands them back as their own register so the view
// can say they exist and are elsewhere in time.
export const WINDOW_PAST_MS = 12 * HOUR;
export const WINDOW_FUTURE_MS = 36 * HOUR;

// Floor for the band's span. Without it a feed holding one 20-minute item would
// render an axis with a single tick, where every position reads as "now-ish".
export const MIN_WINDOW_MS = 4 * HOUR;

// Target tick count — the hour step widens (1h → 2 → 3 → 6 → 12) until the axis
// fits under this, so a 36-hour band doesn't stack unreadable labels.
const MAX_TICKS = 14;
const TICK_STEPS_HOURS = [1, 2, 3, 6, 12, 24];

/**
 * Parse an ISO-8601 instant to milliseconds-epoch, or `null`.
 *
 * `null` for anything that is not a non-empty string parsing to a finite time —
 * a number, a bool, a bare `"2026-08-12"`-shaped value that `Date.parse` would
 * silently read as UTC midnight, or garbage. The date-only rejection is
 * deliberate: producers stamp full instants with an offset, so a date-only
 * string means something upstream lost the time, and placing it at midnight
 * would invent a position the data never asserted.
 */
export function parseInstant(raw: unknown): number | null {
  if (typeof raw !== 'string') return null;
  const text = raw.trim();
  if (!text) return null;
  // Require at least `YYYY-MM-DDThh:mm` — a date alone carries no time of day.
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text)) return null;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * An item's own extent, or `null` when it has no time dimension at all.
 *
 * THE THREE SHAPES, per the backend model's contract:
 *   - no parseable `starts_at`         → `null` (untimed; the view gives it a
 *     register of its own rather than a fabricated position — see the sketch's
 *     own "why it might be wrong": a merge proposal has no hour, and inventing
 *     one would make the substrate assert something false)
 *   - `starts_at`, no `ends_at`        → `{ start, end: null }` — a MOMENT
 *   - `starts_at` + `ends_at`          → a real interval
 *
 * A `ends_at` that does not FOLLOW `starts_at` is degraded to `end: null`
 * rather than honoured or clamped. An end before its start is not a short
 * interval, it is a broken one, and "no known end" is the only reading that
 * doesn't put a bar somewhere the data never claimed. Equal instants degrade
 * too, for the same reason a zero-length span is forbidden downstream.
 */
export function itemExtent(item: Pick<FeedItem, 'starts_at' | 'ends_at'>): Extent | null {
  const start = parseInstant(item.starts_at);
  if (start === null) return null;
  const end = parseInstant(item.ends_at);
  return { start, end: end !== null && end > start ? end : null };
}

/** Does this item sit on the time substrate at all? */
export function isTimed(item: Pick<FeedItem, 'starts_at' | 'ends_at'>): boolean {
  return itemExtent(item) !== null;
}

/** The instant an extent occupies last — its end, or its start for a moment. */
export function extentEnd(extent: Extent): number {
  return extent.end ?? extent.start;
}

// --- weather sub-intervals ---------------------------------------------------

/**
 * One forecast block off a `weather` item's `evidence.periods[]`.
 *
 * `possible` is the producer's own flag for a PROBabilistic block. It exists
 * because a PROB30 window says there is a 30% chance of thunderstorms in it,
 * NOT that thunderstorms occupy it — the producer deliberately keeps those
 * blocks OUT of the item's extent and notes that a renderer should "draw the
 * maybe-window in its own register". This type is the seam where that
 * instruction is honoured; a view that painted a maybe identically to a will
 * would state as fact what the data explicitly qualifies.
 */
export interface WeatherPeriod {
  extent: Extent;
  /** 'BASE' | 'FM' | 'TEMPO' | 'PROB' | 'BECMG' — free-form; display only. */
  change: string;
  /** Percent, when the block is probabilistic. */
  probability: number | null;
  possible: boolean;
  /** When a BECMG transition COMPLETES — inside the block, never its start. */
  becomingAt: number | null;
  wx: string;
  visibility: string;
}

/**
 * Parse `evidence.periods` defensively into placeable sub-intervals.
 *
 * Blocks with no parseable start are dropped rather than defaulted: a forecast
 * block with no time cannot be laid out, and a guessed one would move weather.
 * Never throws on a hostile payload — every field is validated, and a non-array
 * `periods` yields `[]`.
 */
export function weatherPeriods(evidence: unknown): WeatherPeriod[] {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return [];
  const raw = (evidence as Record<string, unknown>).periods;
  if (!Array.isArray(raw)) return [];
  const out: WeatherPeriod[] = [];
  for (const block of raw) {
    if (!block || typeof block !== 'object' || Array.isArray(block)) continue;
    const b = block as Record<string, unknown>;
    const extent = itemExtent({ starts_at: b.starts_at as string | null, ends_at: b.ends_at as string | null });
    if (!extent) continue;
    const probability = typeof b.probability === 'number' && Number.isFinite(b.probability) ? b.probability : null;
    out.push({
      extent,
      change: typeof b.change === 'string' && b.change.trim() ? b.change.trim() : 'BASE',
      probability,
      // Trust the producer's own flag when present; fall back to the presence
      // of a probability so a payload written before the flag still renders in
      // the right register (fail toward "maybe", never toward "will").
      possible: typeof b.possible === 'boolean' ? b.possible : probability !== null,
      becomingAt: parseInstant(b.becoming_at),
      wx: typeof b.wx === 'string' ? b.wx.trim() : '',
      visibility: typeof b.visibility === 'string' || typeof b.visibility === 'number' ? String(b.visibility) : '',
    });
  }
  return out;
}

// --- the band ----------------------------------------------------------------

export interface TimeSplit<T> {
  /** Placeable on the substrate — timed AND intersecting the reachable span. */
  onBand: T[];
  /** Timed, but outside the reachable span (a next-week event). */
  beyond: T[];
  /** No time dimension at all — never given a fabricated position. */
  untimed: T[];
}

/**
 * Sort items into the three registers the view renders.
 *
 * The reachable span is NOW ± the window caps. An item counts as reachable when
 * its extent INTERSECTS that span, not merely when its start falls inside it —
 * a fog window that began before the band opens is still happening, and
 * dropping it would hide live weather.
 */
export function splitByWindow<T extends Pick<FeedItem, 'starts_at' | 'ends_at'>>(
  items: readonly T[],
  now: number,
): TimeSplit<T> {
  const lo = now - WINDOW_PAST_MS;
  const hi = now + WINDOW_FUTURE_MS;
  const split: TimeSplit<T> = { onBand: [], beyond: [], untimed: [] };
  for (const it of items) {
    const extent = itemExtent(it);
    if (!extent) {
      split.untimed.push(it);
    } else if (extent.start <= hi && extentEnd(extent) >= lo) {
      split.onBand.push(it);
    } else {
      split.beyond.push(it);
    }
  }
  return split;
}

const floorHour = (ms: number): number => Math.floor(ms / HOUR) * HOUR;
const ceilHour = (ms: number): number => Math.ceil(ms / HOUR) * HOUR;

/**
 * The band's bounds for a set of on-band items.
 *
 * Always CONTAINS `now` — the substrate's whole claim is "here is where you
 * are in the day", so a band that excluded the present would be a chart, not a
 * sensor log. Grows to hold every on-band extent, snaps out to whole hours so
 * the gridlines land on labelled ticks, and is floored at `MIN_WINDOW_MS`.
 *
 * Callers pass items already filtered by `splitByWindow`; passing unfiltered
 * items cannot blow the span open beyond the caps, because the same clamp is
 * applied here.
 */
export function timelineWindow(
  items: readonly Pick<FeedItem, 'starts_at' | 'ends_at'>[],
  now: number,
): TimeWindow {
  const clampLo = now - WINDOW_PAST_MS;
  const clampHi = now + WINDOW_FUTURE_MS;
  let from = now;
  let to = now;
  for (const it of items) {
    const extent = itemExtent(it);
    if (!extent) continue;
    const start = Math.max(extent.start, clampLo);
    const end = Math.min(extentEnd(extent), clampHi);
    if (start < from) from = start;
    if (end > to) to = end;
  }
  from = floorHour(from);
  to = ceilHour(to);
  if (to - from < MIN_WINDOW_MS) to = from + MIN_WINDOW_MS;
  return { from, to };
}

/** Window span in ms. Never zero — `timelineWindow` floors it. */
export const windowSpan = (w: TimeWindow): number => Math.max(w.to - w.from, MINUTE);

/**
 * Where an instant sits in the band, as a percentage from its start (0..100).
 *
 * CLAMPED on purpose: an interval that begins before the band opens draws from
 * its top edge rather than off-screen. The view marks a clipped edge so the
 * clamp is visible rather than a quiet lie about when something started.
 */
export function positionPct(ms: number, w: TimeWindow): number {
  const pct = ((ms - w.from) / windowSpan(w)) * 100;
  return Math.min(100, Math.max(0, pct));
}

export interface BandPlacement {
  /** Distance from the band's top edge, in percent. */
  topPct: number;
  /** Height in percent. `0` for a moment — the view draws a marker, not a bar. */
  heightPct: number;
  /** The extent starts before the band opens (drawn edge is not the real one). */
  clippedStart: boolean;
  /** The extent ends after the band closes. */
  clippedEnd: boolean;
}

/**
 * Lay an extent out on the band. Time flows DOWN, so `topPct` is the start.
 *
 * A MOMENT (`end: null`) gets `heightPct: 0` — the caller must draw it as a
 * marker. That is the whole reason `end: null` survives this far intact rather
 * than being collapsed to `end === start` upstream: a zero-height BAR would
 * read as "this ended the instant it began", which is exactly the misreading
 * the model's producer contract forbids.
 */
export function placeExtent(extent: Extent, w: TimeWindow): BandPlacement {
  const topPct = positionPct(extent.start, w);
  if (extent.end === null) {
    return { topPct, heightPct: 0, clippedStart: extent.start < w.from, clippedEnd: extent.start > w.to };
  }
  return {
    topPct,
    heightPct: Math.max(0, positionPct(extent.end, w) - topPct),
    clippedStart: extent.start < w.from,
    clippedEnd: extent.end > w.to,
  };
}

/**
 * Labelled hour ticks across the band, widening the step until the axis holds
 * at most `MAX_TICKS`. Ticks land on wall-clock hour multiples so the labels
 * read 06:00 / 09:00 / 12:00 rather than drifting off the operator's clock.
 */
export function hourTicks(w: TimeWindow): number[] {
  const span = windowSpan(w);
  const stepHours =
    TICK_STEPS_HOURS.find((h) => span / (h * HOUR) <= MAX_TICKS) ??
    TICK_STEPS_HOURS[TICK_STEPS_HOURS.length - 1];
  const step = stepHours * HOUR;
  const ticks: number[] = [];
  // Align to a wall-clock multiple of the step, using LOCAL midnight as the
  // origin (UTC-epoch alignment would put a 3-hour tick at 02:00 in a half-hour
  // offset zone, and the operator reads a wall clock, not an epoch).
  const first = new Date(w.from);
  first.setMinutes(0, 0, 0);
  while (first.getHours() % stepHours !== 0) first.setHours(first.getHours() + 1);
  for (let t = first.getTime(); t <= w.to; t += step) ticks.push(t);
  return ticks;
}

// --- display formatting: BEHIND THE SWAP SEAM, deliberately minimal ----------
//
// OWNERSHIP (team-lead ruling, cross-lane): the human-readable extent STRING
// belongs to the deck lane's shared layer, which is building a portable
// formatter; the POSITIONING MATHS above is this module's. The four helpers
// below exist because the timeline had to render something before that layer
// landed, and they are kept to exactly what the band's own labels need.
//
// DO NOT GROW THEM into general display formatting — relative times, date
// headers, "in 3 hours", locale variants. The merge that lands second unifies
// on the shared formatter, and every extra behaviour added here is another
// thing that unification has to reconcile or silently diverge from.

/** Local wall-clock `HH:MM`. The operator reads their own clock, not UTC. */
export function formatClock(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/**
 * A human span for an extent: `"09:30"` for a moment, `"11:00 – 15:00"` for an
 * interval, with a `+1d`-style suffix when the end lands on a later date (a TAF
 * validity window routinely crosses midnight, and a bare `"18:00 – 06:00"`
 * would read as a twelve-hour trip backwards).
 */
export function formatExtent(extent: Extent): string {
  if (extent.end === null) return formatClock(extent.start);
  const startDay = new Date(extent.start);
  const endDay = new Date(extent.end);
  const dayDelta = Math.round(
    (new Date(endDay.getFullYear(), endDay.getMonth(), endDay.getDate()).getTime() -
      new Date(startDay.getFullYear(), startDay.getMonth(), startDay.getDate()).getTime()) /
      (24 * HOUR),
  );
  const suffix = dayDelta > 0 ? ` +${dayDelta}d` : '';
  return `${formatClock(extent.start)} – ${formatClock(extent.end)}${suffix}`;
}

/**
 * Duration in whole minutes, or `null` for a moment. Display-only.
 */
export function extentMinutes(extent: Extent): number | null {
  if (extent.end === null) return null;
  return Math.round((extent.end - extent.start) / MINUTE);
}

/** Compact duration: `"4h"`, `"45m"`, `"1h 30m"`; `null` for a moment. */
export function formatDuration(extent: Extent): string | null {
  const mins = extentMinutes(extent);
  if (mins === null) return null;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

// --- lane packing ------------------------------------------------------------

/**
 * Minimum VISUAL height an entry occupies, in percent of the band. A moment has
 * no duration, and two 09:30 items would otherwise be judged non-overlapping
 * and drawn on top of each other. Overlap has to be decided in the space the
 * operator actually sees, not in the space the data occupies.
 */
export const MIN_TRACE_HEIGHT_PCT = 7;

export interface LanedTrace<T> {
  item: T;
  placement: BandPlacement;
  /** Column index, 0-based. */
  lane: number;
}

export interface LanePacking<T> {
  traces: LanedTrace<T>[];
  /** How many columns the band needs. `0` when there is nothing to place. */
  laneCount: number;
}

/**
 * Assign side-by-side columns so overlapping extents never cover each other.
 *
 * The standard calendar sweep: walk entries in time order and drop each into
 * the FIRST column whose previous occupant has visually finished. A new column
 * opens only when every existing one is still busy, so the common sparse case
 * (a run at 09:30, a fog window at 11:00) stays a single full-width column and
 * only genuinely concurrent items pay the narrowing.
 *
 * Pure, and separated from the view for the same reason the rest of this module
 * is: a packing bug hides an item behind another one, which on an awareness
 * surface is indistinguishable from the item not existing.
 */
export function packLanes<T>(
  entries: readonly { item: T; placement: BandPlacement }[],
  minHeightPct: number = MIN_TRACE_HEIGHT_PCT,
): LanePacking<T> {
  const laneBottoms: number[] = [];
  const traces: LanedTrace<T>[] = [];
  for (const entry of entries) {
    const { topPct, heightPct } = entry.placement;
    const bottom = topPct + Math.max(heightPct, minHeightPct);
    let lane = laneBottoms.findIndex((b) => b <= topPct);
    if (lane === -1) {
      lane = laneBottoms.length;
      laneBottoms.push(bottom);
    } else {
      laneBottoms[lane] = bottom;
    }
    traces.push({ item: entry.item, placement: entry.placement, lane });
  }
  return { traces, laneCount: laneBottoms.length };
}

/**
 * Sort items into reading order for the band: earliest start first, and for a
 * shared start the LONGER extent first so a wide interval renders behind the
 * moments that sit inside it. Ties break on id so the order is stable across
 * renders (an unstable sort here would make rows jump on every poll).
 */
export function byTimeOrder<T extends Pick<FeedItem, 'id' | 'starts_at' | 'ends_at'>>(
  items: readonly T[],
): T[] {
  return [...items].sort((a, b) => {
    const ea = itemExtent(a);
    const eb = itemExtent(b);
    if (!ea || !eb) return !ea && !eb ? a.id.localeCompare(b.id) : ea ? -1 : 1;
    if (ea.start !== eb.start) return ea.start - eb.start;
    const da = extentEnd(ea) - ea.start;
    const db = extentEnd(eb) - eb.start;
    if (da !== db) return db - da;
    return a.id.localeCompare(b.id);
  });
}
