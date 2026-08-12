import { describe, expect, it } from 'vitest';
import {
  extentProgress,
  formatDuration,
  formatTimeExtent,
  readTimeExtent,
} from '../lib/algernon/timeExtent';

// D7 — an interval renders as its EXTENT, not as a timestamp.
//
// Every assertion that touches a clock passes an explicit `timeZone`, because
// the alternative is a suite that passes in Halifax and fails in CI.

const TZ = { timeZone: 'UTC', locale: 'en-GB' };

describe('readTimeExtent — what kind of time is this', () => {
  it('reads no time at all as `none`', () => {
    expect(readTimeExtent({}).kind).toBe('none');
    expect(readTimeExtent(null).kind).toBe('none');
    expect(readTimeExtent({ starts_at: null, ends_at: null }).kind).toBe('none');
    // Blank strings are absence, not a malformed time.
    expect(readTimeExtent({ starts_at: '   ' }).kind).toBe('none');
  });

  it('reads a start with no end as an INSTANT, never a zero-length span', () => {
    // The backend contract is explicit that `ends_at = null` means "no known
    // end" and never "ends immediately". The 09:30 run is a moment.
    const r = readTimeExtent({ starts_at: '2026-08-12T09:30:00Z' });
    expect(r.kind).toBe('instant');
    expect(extentProgress(r)).toBeNull(); // nothing to be part-way through
  });

  it('reads a start and a later end as an INTERVAL', () => {
    const r = readTimeExtent({ starts_at: '2026-08-12T11:00:00Z', ends_at: '2026-08-12T15:00:00Z' });
    expect(r.kind).toBe('interval');
    expect(r.start).not.toBeNull();
    expect(r.end).not.toBeNull();
  });

  it('reads a date-only value as ALL DAY rather than midnight', () => {
    // The trap the backend hit and pinned on its side: `new Date("2026-08-11")`
    // succeeds and invents midnight UTC, which silently turns an all-day event
    // into a zero-length one at 00:00. Discriminated on the STRING for exactly
    // that reason.
    const r = readTimeExtent({ starts_at: '2026-08-12' });
    expect(r.kind).toBe('all_day');
    const shown = formatTimeExtent(r, TZ);
    expect(shown?.label).not.toContain('00:00');
    expect(shown?.duration).toBeNull();
  });

  it('reports a time it cannot read rather than pretending there is none', () => {
    // ILB. "This item has no time" and "we were told a time and could not read
    // it" are different facts; collapsing them makes a producer bug look like
    // a data absence.
    for (const bad of [
      { starts_at: 'the fourteenth' },
      { ends_at: '2026-08-12T15:00:00Z' }, // an end with no beginning
      { starts_at: '2026-08-12T15:00:00Z', ends_at: '2026-08-12T11:00:00Z' }, // backwards
    ]) {
      expect(readTimeExtent(bad).kind, JSON.stringify(bad)).toBe('unparseable');
    }
  });

  it('reads an end equal to its start as an instant, not a zero-length interval', () => {
    const r = readTimeExtent({ starts_at: '2026-08-12T09:30:00Z', ends_at: '2026-08-12T09:30:00Z' });
    expect(r.kind).toBe('instant');
  });
});

describe('formatTimeExtent — the extent is what gets drawn', () => {
  it('draws an interval as its span and its duration', () => {
    const shown = formatTimeExtent(
      readTimeExtent({ starts_at: '2026-08-12T11:00:00Z', ends_at: '2026-08-12T15:00:00Z' }),
      TZ,
    );
    expect(shown?.label).toBe('11:00 – 15:00');
    expect(shown?.duration).toBe('4h');
  });

  it('draws an instant as a single time with NO duration', () => {
    // The positive/negative pair that makes "renders as an extent" mean
    // something: the interval above gets a duration, and this one must not —
    // otherwise every item would be showing a span whether it had one or not.
    const shown = formatTimeExtent(readTimeExtent({ starts_at: '2026-08-12T09:30:00Z' }), TZ);
    expect(shown?.label).toBe('09:30');
    expect(shown?.duration).toBeNull();
  });

  it('names both days when an interval crosses midnight', () => {
    const shown = formatTimeExtent(
      readTimeExtent({ starts_at: '2026-08-12T22:00:00Z', ends_at: '2026-08-13T04:00:00Z' }),
      TZ,
    );
    expect(shown?.duration).toBe('6h');
    // Without the dates this would read as a six-hour window inside one day.
    expect(shown?.label).toContain('12');
    expect(shown?.label).toContain('13');
  });

  it('draws NOTHING when the item has no time dimension', () => {
    // Deliberate, not defensive: most feed items have no time, and a row of
    // blank time slots would bury the ones that do.
    expect(formatTimeExtent(readTimeExtent({}), TZ)).toBeNull();
  });

  it('says so, and shows the raw value, when the time is unreadable', () => {
    const shown = formatTimeExtent(readTimeExtent({ starts_at: 'the fourteenth' }), TZ);
    expect(shown).not.toBeNull();
    expect(shown?.label.toLowerCase()).toContain('unreadable');
    expect(shown?.description).toContain('the fourteenth');
  });

  it('describes every extent in a sentence for assistive tech', () => {
    // The visible label uses an en dash, which some engines announce as
    // nothing at all — "11:00 15:00" is not a span.
    const shown = formatTimeExtent(
      readTimeExtent({ starts_at: '2026-08-12T11:00:00Z', ends_at: '2026-08-12T15:00:00Z' }),
      TZ,
    );
    expect(shown?.description).toContain('11:00');
    expect(shown?.description).toContain('15:00');
    expect(shown?.description).not.toBe(shown?.label);
  });
});

describe('formatDuration', () => {
  it('uses the coarsest honest unit', () => {
    expect(formatDuration(45 * 60_000)).toBe('45m');
    expect(formatDuration(60 * 60_000)).toBe('1h');
    expect(formatDuration(90 * 60_000)).toBe('1h 30m');
    expect(formatDuration(4 * 60 * 60_000)).toBe('4h');
    expect(formatDuration(24 * 60 * 60_000)).toBe('1d');
    expect(formatDuration(30 * 60 * 60_000)).toBe('1d 6h');
  });
});

describe('extentProgress — how much of the window is spent', () => {
  const read = readTimeExtent({ starts_at: '2026-08-12T11:00:00Z', ends_at: '2026-08-12T15:00:00Z' });

  it('is the fraction elapsed inside the window', () => {
    // This is what makes the render an extent rather than a label: a four-hour
    // window that is two hours old should look half spent.
    expect(extentProgress(read, new Date('2026-08-12T13:00:00Z'))).toBeCloseTo(0.5, 5);
    expect(extentProgress(read, new Date('2026-08-12T12:00:00Z'))).toBeCloseTo(0.25, 5);
  });

  it('clamps outside the window instead of overflowing', () => {
    expect(extentProgress(read, new Date('2026-08-12T06:00:00Z'))).toBe(0); // not yet open
    expect(extentProgress(read, new Date('2026-08-12T23:00:00Z'))).toBe(1); // long closed
  });

  it('has no answer for a kind with no span', () => {
    expect(extentProgress(readTimeExtent({ starts_at: '2026-08-12T09:30:00Z' }))).toBeNull();
    expect(extentProgress(readTimeExtent({}))).toBeNull();
  });
});
