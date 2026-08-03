import { afterEach, describe, expect, it, vi } from 'vitest';
import { emailDomain, logAuthOutcome, statusClass } from '../lib/algernon/authLog';

// The BFF auth-outcome logger. Its redaction contract is security-load-bearing:
// no passcode, no session token, no full email address may ever reach a log line.
// Each malformed-input shape gets its own fixture so a regression in the parsing
// can't fall through as a raw echo of whatever the caller submitted.

afterEach(() => {
  vi.restoreAllMocks();
});

describe('statusClass', () => {
  it.each([
    [200, '2xx'],
    [204, '2xx'],
    [302, '3xx'],
    [401, '4xx'],
    [404, '4xx'],
    [429, '4xx'],
    [500, '5xx'],
    [504, '5xx'],
  ])('buckets %i as %s', (status, expected) => {
    expect(statusClass(status)).toBe(expected);
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['NaN', NaN],
    ['Infinity', Infinity],
  ])('reports no relay as "none" (%s)', (_label, value) => {
    expect(statusClass(value as number | null | undefined)).toBe('none');
  });

  it('buckets an out-of-range status as "other"', () => {
    expect(statusClass(99)).toBe('other');
    expect(statusClass(600)).toBe('other');
  });
});

describe('emailDomain', () => {
  it('returns the domain only — NEVER the local part', () => {
    expect(emailDomain('andrew@example.com')).toBe('example.com');
  });

  it('lowercases the domain so a grep matches one spelling', () => {
    expect(emailDomain('Andrew@Example.COM')).toBe('example.com');
  });

  it('takes the LAST @ (a local part may legally contain one)', () => {
    expect(emailDomain('a@b@example.com')).toBe('example.com');
  });

  it.each([
    ['no @ at all', 'notanemail'],
    ['empty local part', '@example.com'],
    ['empty domain', 'andrew@'],
    ['empty string', ''],
    ['whitespace-only domain', 'andrew@   '],
  ])('returns (none) for a malformed address — %s', (_label, raw) => {
    expect(emailDomain(raw)).toBe('(none)');
  });

  it.each([
    ['undefined', undefined],
    ['null', null],
    ['a number', 12345],
    ['an object', { email: 'andrew@example.com' }],
  ])('returns (none) for a non-string input — %s', (_label, raw) => {
    expect(emailDomain(raw)).toBe('(none)');
  });
});

describe('logAuthOutcome', () => {
  it('emits the [bff:<route>] convention with the four standing fields', () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    logAuthOutcome('auth/otp/verify', {
      outcome: 'ok',
      upstream: 200,
      email: 'andrew@example.com',
    });
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0]).toBe(
      '[bff:auth/otp/verify] outcome=ok upstream=200 status_class=2xx email_domain=example.com',
    );
  });

  it('renders a no-relay path as upstream=none status_class=none', () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    logAuthOutcome('auth/otp/request', { outcome: 'bad_request' });
    expect(spy.mock.calls[0][0]).toBe(
      '[bff:auth/otp/request] outcome=bad_request upstream=none status_class=none email_domain=(none)',
    );
  });

  it('appends extra scalars after the standing fields', () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    logAuthOutcome('auth/otp/verify', {
      outcome: 'ok',
      upstream: 200,
      email: 'andrew@example.com',
      extra: { cookie_set: true },
    });
    expect(spy.mock.calls[0][0]).toContain('cookie_set=true');
  });

  it('routes level:warn to console.warn so an operator-visible fault stands out', () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    logAuthOutcome('auth/otp/verify', {
      outcome: 'upstream_ok_no_token',
      upstream: 200,
      level: 'warn',
    });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(log).not.toHaveBeenCalled();
  });

  it('never writes the full email even when handed one', () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    logAuthOutcome('auth/otp/verify', {
      outcome: 'ok',
      upstream: 200,
      email: 'andrew@example.com',
    });
    expect(spy.mock.calls[0][0]).not.toContain('andrew@example.com');
    expect(spy.mock.calls[0][0]).not.toContain('andrew');
  });
});
