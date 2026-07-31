import { describe, expect, it } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';
import { sanitizeDeepLink } from '../lib/algernon/pushLink';

// Same-origin deep-link guard. The dangerous cases are the protocol-relative
// (`//host`) and backslash (`/\host`) forms — a browser resolves both OFF-origin,
// so a bare startsWith('/') would let a hostile push open an arbitrary site.

const CASES: Array<[unknown, string]> = [
  ['/deck', '/deck'],
  ['/feed', '/feed'],
  ['/deck?x=1', '/deck?x=1'],
  ['//evil.com', '/feed'], // protocol-relative → off-origin
  ['/\\evil.com', '/feed'], // backslash → off-origin
  ['https://evil.com', '/feed'],
  ['http://evil.com', '/feed'],
  ['javascript:alert(1)', '/feed'],
  ['', '/feed'],
  ['deck', '/feed'], // not absolute
  [null, '/feed'],
  [undefined, '/feed'],
  [42, '/feed'],
];

describe('sanitizeDeepLink (lib/algernon/pushLink.ts)', () => {
  for (const [input, expected] of CASES) {
    it(`${JSON.stringify(input)} → ${expected}`, () => {
      expect(sanitizeDeepLink(input)).toBe(expected);
    });
  }
});

// PARITY: public/sw.js is a static file vitest can't import, so extract its
// sanitizeDeepLink source and run the SAME cases against it. This reddens if the
// SW copy ever drifts (e.g. back to a bare startsWith('/')).
describe('sanitizeDeepLink parity with public/sw.js', () => {
  const swSource = readFileSync(join(process.cwd(), 'public/sw.js'), 'utf8');
  const match = swSource.match(/function sanitizeDeepLink\(url\)\s*\{([\s\S]*?)\n\}/);

  it('public/sw.js defines a sanitizeDeepLink function', () => {
    expect(match).not.toBeNull();
  });

  // eslint-disable-next-line no-new-func -- deliberately run the extracted SW source for parity
  const swFn = match ? (new Function('url', match[1]) as (u: unknown) => string) : () => 'NO_MATCH';

  for (const [input, expected] of CASES) {
    it(`sw.js: ${JSON.stringify(input)} → ${expected}`, () => {
      expect(swFn(input)).toBe(expected);
    });
  }
});
