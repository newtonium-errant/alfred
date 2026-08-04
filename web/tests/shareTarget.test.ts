import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';
import {
  SHARE_MAX_TITLE_CHARS,
  SHARE_RESTORE_PATH,
  SHARE_SOURCE_FALLBACK,
  SHARE_STASH_KEY,
  clearStashedCapture,
  deriveShareTitle,
  parseSharedCapture,
  readStashedCapture,
  stashSharedCapture,
} from '../lib/algernon/shareTarget';
import { safeNextPath } from '../lib/algernon/safeNextPath';
import { ingestBodySchema } from '../lib/algernon/schemas';

// Web Share Target payload normalisation (G2). The share sheet hands us a loose,
// app-dependent {title?, text?, url?} and the ingest contract wants a strict
// {title, body, source} — everything below is that gap.

// Local-time constructor on purpose: the stamp is device-local by design, so a
// local-time fixture is deterministic under any TZ the suite runs in.
const NOW = new Date(2026, 7, 4, 14, 5);

describe('parseSharedCapture — the three shapes a share actually arrives in', () => {
  it('a page share (title + selection + url) maps all three fields', () => {
    const c = parseSharedCapture(
      { title: 'Tide tables', text: 'high water at 14:20', url: 'https://example.com/tides' },
      NOW,
    );
    expect(c.title).toBe('Tide tables');
    expect(c.body).toBe('high water at 14:20');
    expect(c.source).toBe('https://example.com/tides');
    expect(c.empty).toBe(false);
  });

  it('a bare LINK share (url only, no text) falls back to the url as the body', () => {
    // Some apps send only `url`. Treating that as empty would drop a real share.
    const c = parseSharedCapture({ url: 'https://example.com/a' }, NOW);
    expect(c.body).toBe('https://example.com/a');
    expect(c.source).toBe('https://example.com/a');
    expect(c.empty).toBe(false);
  });

  it('a plain TEXT share (no title, no url) derives a title and a neutral source', () => {
    const c = parseSharedCapture({ text: 'pick up the trailer hitch on Thursday' }, NOW);
    expect(c.body).toBe('pick up the trailer hitch on Thursday');
    expect(c.source).toBe(SHARE_SOURCE_FALLBACK);
    expect(c.title).toBe('pick up the trailer hitch on Thursday — shared 2026-08-04 1405');
    expect(c.empty).toBe(false);
  });

  it('an empty share is flagged empty rather than filed as a blank record', () => {
    expect(parseSharedCapture({}, NOW).empty).toBe(true);
    expect(parseSharedCapture({ text: '   ' }, NOW).empty).toBe(true);
  });

  it('a repeated query param (string[]) takes the first value, not "[object Array]"', () => {
    const c = parseSharedCapture({ text: ['first', 'second'], url: ['https://a.test'] }, NOW);
    expect(c.body).toBe('first');
    expect(c.source).toBe('https://a.test');
  });

  it('the body relays VERBATIM — inner whitespace and newlines are not normalised', () => {
    const body = '  line one\n\n    indented two  ';
    expect(parseSharedCapture({ text: body }, NOW).body).toBe(body);
  });
});

describe('deriveShareTitle — the stamp exists to dodge 409 title_collision', () => {
  it('takes the first ~8 words and stamps device-local time', () => {
    expect(deriveShareTitle('one two three four five six seven eight nine ten', NOW)).toBe(
      'one two three four five six seven eight — shared 2026-08-04 1405',
    );
  });

  it('two shares of the SAME text a minute apart derive DIFFERENT titles', () => {
    // Without this the second share of one article 409s against the first.
    const a = deriveShareTitle('same words entirely', NOW);
    const b = deriveShareTitle('same words entirely', new Date(2026, 7, 4, 14, 6));
    expect(a).not.toBe(b);
  });

  it('an enormous single-word lead is truncated but the stamp SURVIVES', () => {
    // Truncating the whole string would cut the stamp off and reintroduce
    // collisions, so the lead is trimmed to leave room for it.
    const title = deriveShareTitle('x'.repeat(5000), NOW);
    expect(title.length).toBeLessThanOrEqual(SHARE_MAX_TITLE_CHARS);
    expect(title.endsWith('— shared 2026-08-04 1405')).toBe(true);
  });

  it('a whitespace-only body still yields a usable, stamped title', () => {
    const title = deriveShareTitle('   \n  ', NOW);
    expect(title).toBe('Shared capture — shared 2026-08-04 1405');
  });
});

describe('every derived capture satisfies the REAL ingest schema', () => {
  // Cross-check against ingestBodySchema itself rather than trusting the local
  // SHARE_MAX_TITLE_CHARS literal to stay in step with it.
  const cases: Array<[string, Record<string, string | string[] | undefined>]> = [
    ['plain text', { text: 'buy milk tomorrow' }],
    ['bare link', { url: 'https://example.com/x' }],
    ['overlong shared title', { title: 'T'.repeat(4000), text: 'body here' }],
    ['overlong shared text', { text: 'w'.repeat(9000) }],
    ['title with surrounding whitespace', { title: '   spaced   ', text: 'body' }],
  ];

  for (const [name, query] of cases) {
    it(`${name} → passes ingestBodySchema`, () => {
      const c = parseSharedCapture(query, NOW);
      const parsed = ingestBodySchema.safeParse({
        target: 'SALEM',
        record_type: 'note',
        title: c.title,
        body: c.body,
        source: c.source,
      });
      expect(parsed.success).toBe(true);
    });
  }
});

describe('SHARE_RESTORE_PATH survives safeNextPath — the reason the stash exists', () => {
  it('the restore path passes the open-redirect guard unchanged', () => {
    expect(safeNextPath(SHARE_RESTORE_PATH)).toBe(SHARE_RESTORE_PATH);
  });

  it('a next= carrying REAL shared text is downgraded to / (so it must not carry it)', () => {
    // safeNextPath rejects any codepoint <= 0x20, and shared text essentially
    // always contains a space. This is the trap the sessionStorage stash avoids;
    // if this ever stops being true the stash can be simplified away.
    expect(safeNextPath('/share?text=buy milk tomorrow')).toBe('/');
  });
});

describe('sign-in round-trip stash', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('round-trips a capture through sessionStorage', () => {
    const c = parseSharedCapture({ text: 'held across sign-in', url: 'https://a.test' }, NOW);
    stashSharedCapture(c);
    expect(readStashedCapture()).toEqual(c);
  });

  it('reads null when nothing is parked', () => {
    expect(readStashedCapture()).toBeNull();
  });

  it('clearing removes it (a later bare visit cannot resurrect a stale share)', () => {
    stashSharedCapture(parseSharedCapture({ text: 'old news' }, NOW));
    clearStashedCapture();
    expect(readStashedCapture()).toBeNull();
  });

  it('malformed or bodyless parked JSON reads as null, never as a blank capture', () => {
    for (const junk of ['{not json', '{}', 'null', '[]', '{"body":"   "}', '{"body":42}']) {
      sessionStorage.setItem(SHARE_STASH_KEY, junk);
      expect(readStashedCapture()).toBeNull();
    }
  });

  it('a parked capture missing its source degrades to the neutral fallback', () => {
    sessionStorage.setItem(SHARE_STASH_KEY, JSON.stringify({ body: 'words', title: 't' }));
    expect(readStashedCapture()?.source).toBe(SHARE_SOURCE_FALLBACK);
  });
});

describe('manifest.webmanifest declares the share target', () => {
  const manifest = JSON.parse(
    readFileSync(join(process.cwd(), 'public/manifest.webmanifest'), 'utf8'),
  );

  it('registers /share as a GET share target with the three text params', () => {
    expect(manifest.share_target).toEqual({
      action: '/share',
      method: 'GET',
      params: { title: 'title', text: 'text', url: 'url' },
    });
  });

  it('the action stays inside the manifest scope (or the OS will not route it)', () => {
    expect(manifest.share_target.action.startsWith(manifest.scope)).toBe(true);
  });
});

describe('public/sw.js keeps /share in the offline shell', () => {
  const swSource = readFileSync(join(process.cwd(), 'public/sw.js'), 'utf8');

  it('SHELL_ROUTES includes /share', () => {
    const match = swSource.match(/const SHELL_ROUTES = (\[[^\]]*\]);/);
    expect(match).not.toBeNull();
    // eslint-disable-next-line no-new-func -- evaluate the REAL declaration, not a copy
    const routes = new Function(`return ${match![1]}`)() as string[];
    expect(routes).toContain('/share');
  });

  it('CACHE_VERSION was bumped off v1 (v1 shipped before /share was in the shell)', () => {
    const match = swSource.match(/const CACHE_VERSION = '([^']+)';/);
    expect(match).not.toBeNull();
    expect(match![1]).not.toBe('v1');
  });
});
