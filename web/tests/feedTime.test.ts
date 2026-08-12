import { describe, expect, it } from 'vitest';
import {
  MIN_WINDOW_MS,
  WINDOW_FUTURE_MS,
  WINDOW_PAST_MS,
  byTimeOrder,
  extentEnd,
  formatClock,
  formatDuration,
  formatExtent,
  hourTicks,
  isTimed,
  itemExtent,
  parseInstant,
  placeExtent,
  positionPct,
  splitByWindow,
  timelineWindow,
  weatherPeriods,
} from '../lib/algernon/feedTime';

// The time substrate under the timeline view (D7). These pins are TIMEZONE-
// AGNOSTIC by construction: every fixture instant is built from LOCAL date parts
// and stamped with the machine's own offset, so the suite asserts the same
// wall-clock facts whether it runs in Halifax or UTC. Nothing here reads a
// hardcoded `Z` and hopes.

const HOUR = 3_600_000;
const pad = (n: number) => String(Math.abs(Math.trunc(n))).padStart(2, '0');

/** Local wall-clock parts → an ISO instant carrying THIS machine's offset. */
function localIso(y: number, mo: number, d: number, h: number, mi = 0): string {
  const dt = new Date(y, mo - 1, d, h, mi, 0, 0);
  const offMin = -dt.getTimezoneOffset();
  const sign = offMin >= 0 ? '+' : '-';
  return `${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}:00${sign}${pad(offMin / 60)}:${pad(offMin % 60)}`;
}
/** The same instant as milliseconds — the expected value side of a pin. */
const localMs = (y: number, mo: number, d: number, h: number, mi = 0) =>
  new Date(y, mo - 1, d, h, mi, 0, 0).getTime();

const timed = (id: string, starts_at: string | null, ends_at: string | null = null) => ({
  id,
  starts_at,
  ends_at,
});

describe('parseInstant', () => {
  it('parses a full ISO instant to its epoch milliseconds', () => {
    expect(parseInstant(localIso(2026, 8, 12, 9, 30))).toBe(localMs(2026, 8, 12, 9, 30));
  });

  it('accepts an explicit UTC instant (the weather producer stamps these)', () => {
    expect(parseInstant('2026-08-12T18:00:00+00:00')).toBe(Date.UTC(2026, 7, 12, 18));
  });

  it('REJECTS a date-only string rather than placing it at midnight', () => {
    // The refusal and its admissible neighbour in one test: a bare date carries
    // no time of day, and Date.parse would silently read it as UTC midnight —
    // a position the data never asserted. Adding a time makes it placeable.
    expect(parseInstant('2026-08-12')).toBeNull();
    expect(parseInstant('2026-08-12T00:00:00Z')).toBe(Date.UTC(2026, 7, 12, 0));
  });

  it('returns null for every non-instant shape, and never throws', () => {
    for (const bad of [null, undefined, 42, true, false, '', '   ', 'tomorrow', {}, [], NaN]) {
      expect(parseInstant(bad)).toBeNull();
    }
    // Well-shaped but impossible → Date.parse yields NaN, which is not finite.
    expect(parseInstant('2026-13-45T99:99:00Z')).toBeNull();
  });
});

describe('itemExtent — the three shapes', () => {
  it('no parseable start → null (untimed; the view must not fabricate an hour)', () => {
    expect(itemExtent({ starts_at: null, ends_at: null })).toBeNull();
    expect(itemExtent({ starts_at: undefined, ends_at: undefined })).toBeNull();
    // An END with no start is still untimed — an interval needs its beginning.
    expect(itemExtent({ starts_at: null, ends_at: localIso(2026, 8, 12, 15) })).toBeNull();
    expect(isTimed({ starts_at: null, ends_at: null })).toBe(false);
  });

  it('start with no end → a MOMENT (end null), never a zero-length span', () => {
    const moment = itemExtent({ starts_at: localIso(2026, 8, 12, 9, 30), ends_at: null });
    expect(moment).not.toBeNull();
    expect(moment!.start).toBe(localMs(2026, 8, 12, 9, 30));
    expect(moment!.end).toBeNull();
    // extentEnd resolves a moment to its own start for span arithmetic.
    expect(extentEnd(moment!)).toBe(moment!.start);
    expect(isTimed({ starts_at: localIso(2026, 8, 12, 9, 30), ends_at: null })).toBe(true);
  });

  it('start + a LATER end → a real interval', () => {
    const fog = itemExtent({ starts_at: localIso(2026, 8, 12, 11), ends_at: localIso(2026, 8, 12, 15) });
    expect(fog).toEqual({ start: localMs(2026, 8, 12, 11), end: localMs(2026, 8, 12, 15) });
    expect(extentEnd(fog!)).toBe(localMs(2026, 8, 12, 15));
  });

  it('an end that does NOT follow its start degrades to "no known end"', () => {
    // The refusal…
    const inverted = itemExtent({ starts_at: localIso(2026, 8, 12, 15), ends_at: localIso(2026, 8, 12, 11) });
    expect(inverted).toEqual({ start: localMs(2026, 8, 12, 15), end: null });
    const equal = itemExtent({ starts_at: localIso(2026, 8, 12, 15), ends_at: localIso(2026, 8, 12, 15) });
    expect(equal!.end).toBeNull();
    // …and its nearest ADMISSIBLE neighbour, one minute later, which is honoured.
    // Without this the pin would pass identically against a build that discarded
    // every end it was given.
    const ok = itemExtent({ starts_at: localIso(2026, 8, 12, 15), ends_at: localIso(2026, 8, 12, 15, 1) });
    expect(ok!.end).toBe(localMs(2026, 8, 12, 15, 1));
  });

  it('an unparseable end leaves the start standing as a moment', () => {
    const item = itemExtent({ starts_at: localIso(2026, 8, 12, 9), ends_at: 'not-a-time' as unknown as string });
    expect(item).toEqual({ start: localMs(2026, 8, 12, 9), end: null });
  });
});

describe('weatherPeriods — the maybe-window keeps its own register', () => {
  const base = {
    starts_at: localIso(2026, 8, 12, 6),
    ends_at: localIso(2026, 8, 12, 12),
    change: 'BASE',
    probability: null,
    possible: false,
    becoming_at: null,
    wx: '',
    visibility: 6,
  };
  const prob = {
    starts_at: localIso(2026, 8, 12, 19),
    ends_at: localIso(2026, 8, 12, 22),
    change: 'PROB',
    probability: 30,
    possible: true,
    becoming_at: null,
    wx: 'TSRA',
    visibility: 2,
  };

  it('parses blocks and preserves the possible/will distinction', () => {
    const periods = weatherPeriods({ periods: [base, prob] });
    expect(periods).toHaveLength(2);
    expect(periods[0].possible).toBe(false);
    expect(periods[0].probability).toBeNull();
    expect(periods[0].extent).toEqual({ start: localMs(2026, 8, 12, 6), end: localMs(2026, 8, 12, 12) });
    expect(periods[1].possible).toBe(true);
    expect(periods[1].probability).toBe(30);
    expect(periods[1].wx).toBe('TSRA');
    expect(periods[1].visibility).toBe('2');
  });

  it('keeps becoming_at as its own instant, never folded into the start', () => {
    const becmg = weatherPeriods({
      periods: [{ ...base, change: 'BECMG', starts_at: localIso(2026, 8, 12, 19), ends_at: localIso(2026, 8, 12, 21), becoming_at: localIso(2026, 8, 12, 19, 40) }],
    });
    expect(becmg[0].extent.start).toBe(localMs(2026, 8, 12, 19));
    expect(becmg[0].becomingAt).toBe(localMs(2026, 8, 12, 19, 40));
  });

  it('a probability with no `possible` flag still reads as a MAYBE (fails toward doubt)', () => {
    const legacy = weatherPeriods({ periods: [{ ...prob, possible: undefined }] });
    expect(legacy[0].possible).toBe(true);
    // Positive control on the same axis: no probability and no flag → a will.
    const certain = weatherPeriods({ periods: [{ ...base, possible: undefined }] });
    expect(certain[0].possible).toBe(false);
  });

  it('drops a block with no placeable start, and keeps its placeable sibling', () => {
    const periods = weatherPeriods({ periods: [{ ...base, starts_at: null }, prob] });
    expect(periods).toHaveLength(1);
    expect(periods[0].change).toBe('PROB');
  });

  it('survives every hostile payload shape and never throws', () => {
    for (const bad of [null, undefined, 42, 'periods', [], {}, { periods: null }, { periods: 'no' }, { periods: {} }]) {
      expect(weatherPeriods(bad)).toEqual([]);
    }
    expect(weatherPeriods({ periods: [null, 7, 'x', [], { starts_at: 5 }] })).toEqual([]);
    // Positive control: the same call shape DOES yield a period when given one.
    expect(weatherPeriods({ periods: [null, base] })).toHaveLength(1);
  });

  it('defaults a missing change label to BASE rather than an empty chip', () => {
    expect(weatherPeriods({ periods: [{ ...base, change: '  ' }] })[0].change).toBe('BASE');
  });
});

describe('splitByWindow — three registers, nothing dropped', () => {
  const now = localMs(2026, 8, 12, 10);

  it('sorts timed-and-reachable, timed-but-far, and untimed apart', () => {
    const split = splitByWindow(
      [
        timed('near', localIso(2026, 8, 12, 14)),
        timed('far', new Date(now + WINDOW_FUTURE_MS + 4 * HOUR).toISOString()),
        timed('stale', new Date(now - WINDOW_PAST_MS - 4 * HOUR).toISOString()),
        timed('untimed', null),
      ],
      now,
    );
    expect(split.onBand.map((i) => i.id)).toEqual(['near']);
    expect(split.beyond.map((i) => i.id).sort()).toEqual(['far', 'stale']);
    expect(split.untimed.map((i) => i.id)).toEqual(['untimed']);
    // Nothing is silently discarded — every input lands in exactly one register.
    expect(split.onBand.length + split.beyond.length + split.untimed.length).toBe(4);
  });

  it('an interval that STARTED before the window but is still running stays on the band', () => {
    // The intersect rule, not a start-only rule: live weather that began
    // yesterday is still happening, and hiding it would be the worst miss the
    // substrate can make.
    const running = timed(
      'running',
      new Date(now - WINDOW_PAST_MS - 6 * HOUR).toISOString(),
      new Date(now + 2 * HOUR).toISOString(),
    );
    // Its nearest inadmissible neighbour: the same long interval, already OVER.
    const finished = timed(
      'finished',
      new Date(now - WINDOW_PAST_MS - 6 * HOUR).toISOString(),
      new Date(now - WINDOW_PAST_MS - 1 * HOUR).toISOString(),
    );
    const split = splitByWindow([running, finished], now);
    expect(split.onBand.map((i) => i.id)).toEqual(['running']);
    expect(split.beyond.map((i) => i.id)).toEqual(['finished']);
  });

  it('an empty feed yields three empty registers, not a throw', () => {
    expect(splitByWindow([], now)).toEqual({ onBand: [], beyond: [], untimed: [] });
  });
});

describe('timelineWindow', () => {
  const now = localMs(2026, 8, 12, 10);

  it('always contains NOW, even with no items at all', () => {
    const w = timelineWindow([], now);
    expect(w.from).toBeLessThanOrEqual(now);
    expect(w.to).toBeGreaterThanOrEqual(now);
    expect(w.to - w.from).toBeGreaterThanOrEqual(MIN_WINDOW_MS);
  });

  it('grows to hold an interval, snapped out to whole hours', () => {
    const w = timelineWindow([timed('fog', localIso(2026, 8, 12, 11, 20), localIso(2026, 8, 12, 15, 40))], now);
    expect(w.from).toBe(localMs(2026, 8, 12, 10)); // now, floored to its hour
    expect(w.to).toBe(localMs(2026, 8, 12, 16)); // 15:40 ceiled out to 16:00
  });

  it('reaches BACK for an item earlier than now', () => {
    const w = timelineWindow([timed('early', localIso(2026, 8, 12, 4, 10))], now);
    expect(w.from).toBe(localMs(2026, 8, 12, 4));
  });

  it('never exceeds the caps, however far out an item claims to be', () => {
    const w = timelineWindow(
      [
        timed('ancient', new Date(now - 400 * HOUR).toISOString()),
        timed('distant', new Date(now + 400 * HOUR).toISOString()),
      ],
      now,
    );
    expect(now - w.from).toBeLessThanOrEqual(WINDOW_PAST_MS + HOUR);
    expect(w.to - now).toBeLessThanOrEqual(WINDOW_FUTURE_MS + HOUR);
  });

  it('untimed items cannot move the band', () => {
    const bare = timelineWindow([], now);
    expect(timelineWindow([timed('u1', null), timed('u2', null)], now)).toEqual(bare);
  });
});

describe('placeExtent — a moment is a marker, not a zero-height bar', () => {
  const w = { from: localMs(2026, 8, 12, 6), to: localMs(2026, 8, 12, 18) };

  it('places a moment at its time with NO height', () => {
    const p = placeExtent({ start: localMs(2026, 8, 12, 12), end: null }, w);
    expect(p.topPct).toBeCloseTo(50, 6);
    expect(p.heightPct).toBe(0);
    expect(p.clippedStart).toBe(false);
    expect(p.clippedEnd).toBe(false);
  });

  it('gives an interval a height proportional to its real duration', () => {
    // 11:00–15:00 in a 06:00–18:00 band: starts 5/12 in, spans 4/12.
    const p = placeExtent({ start: localMs(2026, 8, 12, 11), end: localMs(2026, 8, 12, 15) }, w);
    expect(p.topPct).toBeCloseTo((5 / 12) * 100, 6);
    expect(p.heightPct).toBeCloseTo((4 / 12) * 100, 6);
  });

  it('flags a clipped start and clipped end instead of drawing off-band', () => {
    const early = placeExtent({ start: localMs(2026, 8, 12, 2), end: localMs(2026, 8, 12, 8) }, w);
    expect(early.topPct).toBe(0);
    expect(early.clippedStart).toBe(true);
    expect(early.clippedEnd).toBe(false);

    const late = placeExtent({ start: localMs(2026, 8, 12, 16), end: localMs(2026, 8, 12, 23) }, w);
    expect(late.clippedEnd).toBe(true);
    expect(late.topPct + late.heightPct).toBeCloseTo(100, 6);
  });

  it('positionPct clamps to the band rather than returning an off-screen number', () => {
    expect(positionPct(localMs(2026, 8, 11, 22), w)).toBe(0);
    expect(positionPct(localMs(2026, 8, 13, 4), w)).toBe(100);
  });
});

describe('hourTicks', () => {
  it('ticks every hour on a short band, landing on wall-clock hours', () => {
    const ticks = hourTicks({ from: localMs(2026, 8, 12, 6), to: localMs(2026, 8, 12, 10) });
    expect(ticks.map(formatClock)).toEqual(['06:00', '07:00', '08:00', '09:00', '10:00']);
  });

  it('widens the step so a long band never stacks unreadable labels', () => {
    const ticks = hourTicks({ from: localMs(2026, 8, 12, 0), to: localMs(2026, 8, 13, 12) });
    expect(ticks.length).toBeLessThanOrEqual(14);
    expect(ticks.length).toBeGreaterThan(2);
    // Whatever step it chose, the labels are still whole wall-clock hours.
    for (const t of ticks) expect(formatClock(t).endsWith(':00')).toBe(true);
    // …and the step is uniform (a drifting axis would misplace every item).
    const gaps = ticks.slice(1).map((t, i) => t - ticks[i]);
    expect(new Set(gaps).size).toBe(1);
  });
});

describe('formatting', () => {
  it('a moment reads as one clock time; an interval reads as a span', () => {
    expect(formatExtent({ start: localMs(2026, 8, 12, 9, 30), end: null })).toBe('09:30');
    expect(formatExtent({ start: localMs(2026, 8, 12, 11), end: localMs(2026, 8, 12, 15) })).toBe('11:00 – 15:00');
  });

  it('a span crossing midnight says so, instead of reading as a trip backwards', () => {
    expect(formatExtent({ start: localMs(2026, 8, 12, 18), end: localMs(2026, 8, 13, 6) })).toBe('18:00 – 06:00 +1d');
  });

  it('durations are compact, and a moment has none', () => {
    expect(formatDuration({ start: localMs(2026, 8, 12, 11), end: localMs(2026, 8, 12, 15) })).toBe('4h');
    expect(formatDuration({ start: localMs(2026, 8, 12, 11), end: localMs(2026, 8, 12, 11, 45) })).toBe('45m');
    expect(formatDuration({ start: localMs(2026, 8, 12, 11), end: localMs(2026, 8, 12, 12, 30) })).toBe('1h 30m');
    expect(formatDuration({ start: localMs(2026, 8, 12, 11), end: null })).toBeNull();
  });
});

describe('byTimeOrder', () => {
  it('orders by start, then longest-first, then stably by id', () => {
    const ordered = byTimeOrder([
      timed('c', localIso(2026, 8, 12, 12)),
      timed('a-long', localIso(2026, 8, 12, 9), localIso(2026, 8, 12, 17)),
      timed('a-short', localIso(2026, 8, 12, 9), localIso(2026, 8, 12, 10)),
      timed('b', localIso(2026, 8, 12, 11)),
    ]);
    expect(ordered.map((i) => i.id)).toEqual(['a-long', 'a-short', 'b', 'c']);
  });

  it('is stable across repeated calls on equal keys (rows must not jump on poll)', () => {
    const items = [timed('z', localIso(2026, 8, 12, 9)), timed('y', localIso(2026, 8, 12, 9))];
    expect(byTimeOrder(items).map((i) => i.id)).toEqual(['y', 'z']);
    expect(byTimeOrder([...items].reverse()).map((i) => i.id)).toEqual(['y', 'z']);
  });

  it('does not mutate its input', () => {
    const items = [timed('b', localIso(2026, 8, 12, 12)), timed('a', localIso(2026, 8, 12, 9))];
    byTimeOrder(items);
    expect(items.map((i) => i.id)).toEqual(['b', 'a']);
  });
});
