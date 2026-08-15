import { describe, expect, it } from 'vitest';
import { exactToken, hasExactToken } from './_exactToken';

// The helper the sweep is built on, pinned against the four costumes it exists
// to end. Every case below is a REAL pairing from this codebase, not a
// hypothetical — `ui-code` / `ui-code-label` shipped one day before this file.

describe('a sibling never satisfies its prefix', () => {
  it('distinguishes ui-code from ui-code-label — the live pair', () => {
    // The whole sweep in two lines. `includes` is shown FAILING beside it,
    // because the point is not that the helper works but that the thing it
    // replaced does not.
    expect('ui-code-label'.includes('ui-code')).toBe(true); // the bug
    expect(hasExactToken('ui-code-label', 'ui-code')).toBe(false); // the fix
  });

  it('the same for selector form', () => {
    expect(hasExactToken('[data-surface=\'comms\'] .ui-code-label', '.ui-code')).toBe(false);
    expect(hasExactToken('[data-surface=\'comms\'] .ui-code', '.ui-code')).toBe(true);
  });

  it('\\b is not enough — the second costume', () => {
    // `\b` sits between a word char and a non-word char, and `-` is non-word,
    // so the "careful" fix for `includes` has exactly the same hole. Pinned so
    // nobody re-derives it as an improvement.
    expect(/\.ui-code\b/.test('.ui-code-label')).toBe(true); // still wrong
    expect(hasExactToken('.ui-code-label', '.ui-code')).toBe(false);
  });

  it('a rename does not preserve the token — the third costume', () => {
    // A reviewer's `.ui-code` -> `.ui-code-GONE` mutation scored a LOWER red
    // count than a deletion, because the renamed selector still contained the
    // original string. Under exact-token the rename reads as the removal it is.
    expect(hasExactToken('.ui-code-GONE', '.ui-code')).toBe(false);
  });
});

describe('what it must still match', () => {
  it.each([
    ['.ui-code {', '.ui-code'],
    ['[data-surface=\'console\'] .ui-code{color:red}', '.ui-code'],
    ['className="ui-code max-h-64"', 'ui-code'],
    ['className="foo ui-code"', 'ui-code'],
    [':focus-visible {', ':focus-visible'],
  ])('%s contains %s', (haystack, token) => {
    // The other direction. A boundary check that never matches would satisfy
    // every "sibling does not match" assertion above while guarding nothing.
    expect(hasExactToken(haystack, token)).toBe(true);
  });

  it('matches a COMPOUND selector — why there is no leading lookbehind', () => {
    // `.foo.ui-code` is a legitimate compound targeting the marker. A
    // symmetric `(?<![\w-])` prefix would see `o` before the dot and refuse,
    // turning a strictness fix into a false negative.
    expect(hasExactToken('.foo.ui-code', '.ui-code')).toBe(true);
  });

  it('escapes regex metacharacters in the token', () => {
    // `.` is a metacharacter; unescaped, `.ui-code` would match `Xui-code`.
    expect(hasExactToken('Xui-code', '.ui-code')).toBe(false);
    expect(exactToken('.ui-code').source.startsWith('\\.')).toBe(true);
  });
});
