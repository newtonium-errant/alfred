import { describe, expect, it } from 'vitest';
import { composeMode, composeModeForDate, halifaxHour } from '../lib/algernon/composer';

// Pins the home-composer rule + its exact boundary hours. The time-based cases
// use July (America/Halifax = ADT, UTC-3) UTC instants so the Halifax-local hour
// is deterministic: local = UTC - 3.

describe('composeMode (by hour)', () => {
  it('is brief before 10:00', () => {
    expect(composeMode(0)).toBe('brief');
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
