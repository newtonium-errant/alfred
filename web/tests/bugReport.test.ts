import { describe, expect, it } from 'vitest';

// #95 — the pure helpers behind the in-app bug reporter.
//
// These bound what reaches the wire and phrase every refusal. The box
// re-validates all of it, so nothing here is a security boundary; what these
// tests protect is HONESTY — that the character counter matches the real cap,
// that a refused report explains which limit it hit, and that a context block
// full of nonsense degrades to a shape the box can read rather than to a
// plausible lie (a NaN viewport becoming 0, not `NaN`).

import {
  ALLOWED_SHOT_MIME,
  MAX_BUGREPORT_DESCRIPTION_CHARS,
  MAX_BUGREPORT_SCREENSHOT_BYTES,
  boundDescription,
  bugReportErrorMessage,
  buildBugReportContext,
  formatBytes,
  isAllowedShotType,
} from '../lib/algernon/bugReport';

describe('boundDescription', () => {
  it('returns null for anything empty after trimming', () => {
    // null is what keeps Send disabled rather than round-tripping a value the
    // box refuses with empty_description.
    expect(boundDescription('')).toBe(null);
    expect(boundDescription('   ')).toBe(null);
    expect(boundDescription('\n\t  \r')).toBe(null);
  });

  it('trims surrounding whitespace but keeps the words', () => {
    expect(boundDescription('  the save button did nothing  ')).toBe(
      'the save button did nothing',
    );
  });

  it('truncates rather than rejecting an over-long description', () => {
    // A longer value can only arrive from a paste that outran the textarea's
    // maxLength. Keeping the first 5000 characters is kinder than refusing the
    // whole report.
    const long = 'x'.repeat(MAX_BUGREPORT_DESCRIPTION_CHARS + 500);
    const bounded = boundDescription(long);
    expect(bounded).not.toBe(null);
    expect(bounded!.length).toBe(MAX_BUGREPORT_DESCRIPTION_CHARS);
  });

  it('keeps a description that is exactly at the cap', () => {
    const exact = 'y'.repeat(MAX_BUGREPORT_DESCRIPTION_CHARS);
    expect(boundDescription(exact)!.length).toBe(MAX_BUGREPORT_DESCRIPTION_CHARS);
  });
});

describe('buildBugReportContext', () => {
  it('carries the captured breadcrumbs through', () => {
    const ctx = buildBugReportContext({
      route: '/chat?thread=7',
      instance: 'Salem',
      userAgent: 'Mozilla/5.0 (iPhone)',
      viewportW: 390,
      viewportH: 844,
      appVersion: 'abc1234',
      now: new Date('2026-08-11T12:00:00.000Z'),
    });
    expect(ctx.route).toBe('/chat?thread=7');
    expect(ctx.instance).toBe('Salem');
    expect(ctx.viewport_w).toBe(390);
    expect(ctx.viewport_h).toBe(844);
    expect(ctx.app_version).toBe('abc1234');
    expect(ctx.ts).toBe('2026-08-11T12:00:00.000Z');
  });

  it('floors a non-finite viewport to 0 rather than sending NaN', () => {
    // window.innerWidth is NaN in a detached environment. The box reads 0 as
    // "not captured"; it would read NaN as invalid JSON-ish garbage.
    const ctx = buildBugReportContext({
      route: '/',
      instance: 'Salem',
      userAgent: '',
      viewportW: Number.NaN,
      viewportH: Number.POSITIVE_INFINITY,
      appVersion: 'dev',
    });
    expect(ctx.viewport_w).toBe(0);
    expect(ctx.viewport_h).toBe(0);
  });

  it('floors fractional and negative viewports', () => {
    const ctx = buildBugReportContext({
      route: '/',
      instance: 'Salem',
      userAgent: '',
      viewportW: 390.7,
      viewportH: -12,
      appVersion: 'dev',
    });
    expect(ctx.viewport_w).toBe(390);
    expect(ctx.viewport_h).toBe(0);
  });

  it('caps a pathological user agent and route', () => {
    const ctx = buildBugReportContext({
      route: '/x'.repeat(2000),
      instance: 'i'.repeat(500),
      userAgent: 'u'.repeat(5000),
      viewportW: 1,
      viewportH: 1,
      appVersion: 'v'.repeat(500),
    });
    expect(ctx.route.length).toBe(512);
    expect(ctx.user_agent.length).toBe(512);
    expect(ctx.instance.length).toBe(120);
    expect(ctx.app_version.length).toBe(120);
  });
});

describe('isAllowedShotType', () => {
  it('accepts exactly the box’s allowlist', () => {
    for (const mime of ALLOWED_SHOT_MIME) {
      expect(isAllowedShotType(mime)).toBe(true);
    }
  });

  it('refuses everything else, including empty and null', () => {
    // Unlike the reference implementation, an EMPTY type is refused rather than
    // waved through: this route stores the file under an extension derived from
    // the media type, and a guessed extension on unknown bytes is a file whose
    // name disagrees with its content.
    expect(isAllowedShotType('image/heic')).toBe(false);
    expect(isAllowedShotType('application/pdf')).toBe(false);
    expect(isAllowedShotType('')).toBe(false);
    expect(isAllowedShotType(null)).toBe(false);
    expect(isAllowedShotType(undefined)).toBe(false);
  });
});

describe('formatBytes', () => {
  it('names the screenshot cap the way the refusal copy does', () => {
    expect(formatBytes(MAX_BUGREPORT_SCREENSHOT_BYTES)).toBe('5.0 MB');
  });

  it('degrades to KB below a tenth of a megabyte', () => {
    expect(formatBytes(50 * 1024)).toBe('50 KB');
  });

  it('does not render a negative or non-finite size', () => {
    expect(formatBytes(-1)).toBe('0 MB');
    expect(formatBytes(Number.NaN)).toBe('0 MB');
  });
});

describe('bugReportErrorMessage', () => {
  it('gives each refusal its OWN sentence', () => {
    // The whole point: "too big" and "too long" need different actions from the
    // reporter, so one shared message tells them neither.
    const codes = [
      'empty_description',
      'description_too_long',
      'screenshot_too_large',
      'unsupported_media_type',
      'invalid_base64',
      'bugreport_not_configured',
      'wrong_peer',
      'forbidden',
      'invalid_session',
      'gateway_timeout',
    ];
    const messages = codes.map((c) => bugReportErrorMessage(c));
    expect(new Set(messages).size).toBe(messages.length);
    for (const m of messages) expect(m.length).toBeGreaterThan(10);
  });

  it('names the actual limit in the size and length refusals', () => {
    expect(bugReportErrorMessage('screenshot_too_large')).toContain('5.0 MB');
    expect(bugReportErrorMessage('description_too_long')).toContain('5,000');
  });

  it('does not blame the reporter for a deploy state', () => {
    const notConfigured = bugReportErrorMessage('bugreport_not_configured');
    expect(notConfigured).toContain('isn’t switched on');
    // A misconfigured server must not read as "your report was fine but we
    // dropped it silently" — it says nothing was sent.
    expect(bugReportErrorMessage('wrong_peer')).toContain('nothing was sent');
  });

  it('is honest that a timeout did NOT save the report', () => {
    // The dangerous ambiguity: a reporter who thinks it might have gone through
    // does not resend, and the bug is never filed.
    expect(bugReportErrorMessage('gateway_timeout')).toContain('NOT saved');
  });

  it('falls back without trailing punctuation garbage on an unknown code', () => {
    expect(bugReportErrorMessage('who_knows')).toBe(
      'We couldn’t send your report. Please try again.',
    );
    expect(bugReportErrorMessage('who_knows', 'disk on fire')).toContain('disk on fire');
  });
});
