import { describe, expect, it } from 'vitest';
import { formatMessageTime } from '../lib/utils';

// formatMessageTime must NEVER render "Invalid Date" — falsy/unparseable stamps
// return '' so MessageBubble renders nothing. A valid ISO stamp returns a
// non-empty, time-only string (the exact value is locale/tz-dependent, so we
// assert shape, not an exact literal).

describe('formatMessageTime', () => {
  it('returns "" for an empty string', () => {
    expect(formatMessageTime('')).toBe('');
  });

  it('returns "" for an unparseable stamp (never "Invalid Date")', () => {
    expect(formatMessageTime('not-a-date')).toBe('');
    expect(formatMessageTime('2026-13-99T99:99:99Z')).toBe('');
  });

  it('returns a non-empty time-only string for a valid ISO-8601 stamp', () => {
    const out = formatMessageTime('2026-06-29T14:05:00Z');
    expect(out).not.toBe('');
    expect(out).not.toContain('Invalid');
    // hour:minute with an optional AM/PM, no seconds, no date component.
    expect(out).toMatch(/\d{1,2}:\d{2}/);
    expect(out).not.toMatch(/2026/);
  });

  it('parses an ISO stamp with a timezone offset', () => {
    expect(formatMessageTime('2026-06-29T14:05:00+00:00')).not.toBe('');
  });
});
