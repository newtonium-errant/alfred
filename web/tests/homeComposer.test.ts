import { describe, expect, it } from 'vitest';
import {
  briefCardDetail,
  briefCardHeadline,
  briefFreshness,
  composeMode,
  composeModeForDate,
  halifaxDayKey,
  halifaxHour,
} from '../lib/algernon/composer';

// Pins the home-composer rule + its exact boundary hours. The time-based cases
// use July (America/Halifax = ADT, UTC-3) UTC instants so the Halifax-local hour
// is deterministic: local = UTC - 3.

describe('composeMode (by hour)', () => {
  // #51 boundary sweep. The old rule was `hour < 10` with NO floor, so 00:00
  // composed as morning — the 00:07 screenshot led with "Your morning brief is
  // ready" beside correctly-rolled-over empty rings. These two night cases are
  // the regression; the rest pin the unchanged edges.
  it('is feed through the night — midnight is not morning', () => {
    expect(composeMode(0)).toBe('feed');
    expect(composeMode(4)).toBe('feed');
  });
  it('is brief from 05:00 up to (not incl.) 10:00', () => {
    expect(composeMode(5)).toBe('brief');
    expect(composeMode(9)).toBe('brief');
  });
  it('is feed across the 10:00–10:59 band (ratified)', () => {
    expect(composeMode(10)).toBe('feed');
  });
  it('is check-in from 11:00 up to (not incl.) 14:00', () => {
    expect(composeMode(11)).toBe('checkin');
    expect(composeMode(13)).toBe('checkin');
  });
  it('is feed at and after 14:00', () => {
    expect(composeMode(14)).toBe('feed');
    expect(composeMode(23)).toBe('feed');
  });
});

describe('halifaxHour', () => {
  it('converts a UTC instant to the Halifax-local hour (ADT = UTC-3 in July)', () => {
    expect(halifaxHour(new Date('2026-07-30T13:00:00Z'))).toBe(10);
    expect(halifaxHour(new Date('2026-07-30T02:30:00Z'))).toBe(23); // prev-day 23:30 local
  });
});

describe('composeModeForDate — the exact boundary times', () => {
  const cases: Array<[string, string]> = [
    ['2026-07-30T03:07:00Z', 'feed'], //   00:07 local — the screenshot instant
    ['2026-07-30T07:59:00Z', 'feed'], //   04:59 local
    ['2026-07-30T08:00:00Z', 'brief'], //  05:00 local
    ['2026-07-30T12:59:00Z', 'brief'], //  09:59 local
    ['2026-07-30T13:00:00Z', 'feed'], //   10:00 local
    ['2026-07-30T13:59:00Z', 'feed'], //   10:59 local
    ['2026-07-30T14:00:00Z', 'checkin'], // 11:00 local
    ['2026-07-30T16:59:00Z', 'checkin'], // 13:59 local
    ['2026-07-30T17:00:00Z', 'feed'], //   14:00 local
  ];
  for (const [iso, expected] of cases) {
    it(`${iso} → ${expected}`, () => {
      expect(composeModeForDate(new Date(iso))).toBe(expected);
    });
  }
});

// ---------------------------------------------------------------------------
// #51 — the load-bearing half: a stale artifact must not call itself fresh
// ---------------------------------------------------------------------------

describe('briefFreshness — the brief is compared to TODAY, in the instance tz', () => {
  // 00:07 ADT on 2026-08-05 — the screenshot instant. Today is the 5th locally
  // even though it is already the 5th in UTC too; the 03:07Z form is what the
  // browser actually holds.
  const midnightIsh = new Date('2026-08-05T03:07:00Z');

  it("calls yesterday's brief STALE at 00:07 — the reported bug", () => {
    expect(briefFreshness('2026-08-04', midnightIsh)).toBe('stale');
  });

  it("calls today's brief fresh", () => {
    expect(briefFreshness('2026-08-05', midnightIsh)).toBe('today');
  });

  it('distinguishes an empty spool from a stale one', () => {
    expect(briefFreshness(null, midnightIsh)).toBe('none');
    expect(briefFreshness('', midnightIsh)).toBe('none');
    expect(briefFreshness('   ', midnightIsh)).toBe('none');
  });

  it('uses the INSTANCE day, not the UTC day', () => {
    // 23:30 ADT on the 4th is already 02:30Z on the 5th. The brief dated the
    // 4th is TODAY's for the operator; a UTC-day comparison would call it stale.
    const lateEvening = new Date('2026-08-05T02:30:00Z');
    expect(briefFreshness('2026-08-04', lateEvening)).toBe('today');
    expect(briefFreshness('2026-08-05', lateEvening)).toBe('stale');
  });

  it('does NOT route the date string through Date() — the off-by-one trap', () => {
    // `new Date('2026-08-05')` is UTC midnight = 21:00 on the 4th in Halifax, so
    // a Date-mediated comparison reports TODAY's brief as stale every day. Pinned
    // because that is the shape of the nearest available wrong fix (reusing
    // rings.isTodayInstanceTz, which takes a timestamp).
    const noonish = new Date('2026-08-05T15:00:00Z'); // 12:00 local on the 5th
    expect(halifaxDayKey(noonish)).toBe('2026-08-05');
    expect(halifaxDayKey(new Date('2026-08-05'))).toBe('2026-08-04'); // the trap
    expect(briefFreshness('2026-08-05', noonish)).toBe('today');
  });
});

describe('brief card copy — rendered strings, not just a freshness enum', () => {
  it('only the today case claims readiness', () => {
    expect(briefCardHeadline('today')).toBe('Your morning brief is ready');
    expect(briefCardHeadline('stale')).not.toMatch(/ready/i);
    expect(briefCardHeadline('none')).not.toMatch(/ready/i);
  });

  it("names the artifact honestly when it is yesterday's", () => {
    expect(briefCardHeadline('stale')).toBe("Yesterday's brief");
  });

  it('every non-today case says when today’s arrives', () => {
    expect(briefCardDetail('stale', 'Salem')).toMatch(/6:00/);
    expect(briefCardDetail('none', 'Salem')).toMatch(/6:00/);
  });

  it('the today detail still names the instance', () => {
    expect(briefCardDetail('today', 'Salem')).toContain('Salem');
  });
});
