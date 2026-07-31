import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  DEFAULT_DIRECTIVE_ALIASES,
  loadDirectiveAliases,
  matchLeadingDirective,
} from '../lib/algernon/shortcutDirective';

// Pins the DOM-free directive parser + config loader: the three accepted forms,
// alias/STT tolerance, the fail-safe non-matches (must return null → text intact),
// and the garbage-safe env parse.

const ALIASES = {
  hypatia: ['hypatia', 'high patia', 'hi patia'],
  'kal-le': ['kal-le', 'kalle', 'cal e'],
  vera: ['vera'],
  salem: ['salem'],
};

describe('matchLeadingDirective — accepted forms', () => {
  it('form 1 "message for <name>, …" strips the prefix and resolves the instance', () => {
    const m = matchLeadingDirective('message for hypatia, add to my story idea', ALIASES);
    expect(m?.canonical).toBe('hypatia');
    expect(m?.rest).toBe('add to my story idea');
    expect(m?.spokenForm).toBe('message for hypatia,');
  });
  it('form 1 without a separator (just whitespace) still parses', () => {
    const m = matchLeadingDirective('message for hypatia add to my story', ALIASES);
    expect(m?.canonical).toBe('hypatia');
    expect(m?.rest).toBe('add to my story');
  });
  it('form 1 with a colon separator', () => {
    expect(matchLeadingDirective('message for hypatia: buy milk', ALIASES)?.rest).toBe('buy milk');
  });
  it('form 2 "for <name>: …"', () => {
    const m = matchLeadingDirective('for hypatia: buy milk', ALIASES);
    expect(m?.canonical).toBe('hypatia');
    expect(m?.rest).toBe('buy milk');
  });
  it('form 3 "<name>: …"', () => {
    const m = matchLeadingDirective('hypatia: buy milk', ALIASES);
    expect(m?.canonical).toBe('hypatia');
    expect(m?.rest).toBe('buy milk');
  });
});

describe('matchLeadingDirective — alias + STT tolerance', () => {
  it('resolves a multi-word STT alias ("high patia" → hypatia)', () => {
    expect(matchLeadingDirective('high patia: note this', ALIASES)?.canonical).toBe('hypatia');
    expect(matchLeadingDirective('message for hi patia, note this', ALIASES)?.canonical).toBe('hypatia');
  });
  it('resolves a spaced alias for kal-le ("cal e")', () => {
    expect(matchLeadingDirective('cal e: ship it', ALIASES)?.canonical).toBe('kal-le');
  });
  it('is case-insensitive and whitespace-tolerant', () => {
    expect(matchLeadingDirective('  HYPATIA:   buy milk', ALIASES)?.canonical).toBe('hypatia');
  });
  it('preserves the body verbatim after the delimiter', () => {
    expect(matchLeadingDirective('hypatia: line one\nline two', ALIASES)?.rest).toBe('line one\nline two');
  });
});

describe('matchLeadingDirective — fail-safe non-matches (return null → text kept intact)', () => {
  it('an unknown name is not a directive', () => {
    expect(matchLeadingDirective('message for bob: hello', ALIASES)).toBeNull();
    expect(matchLeadingDirective('bob: hello', ALIASES)).toBeNull();
  });
  it('"for the record …" is not a directive (form 2 needs a KNOWN name + colon)', () => {
    expect(matchLeadingDirective('for the record I did the thing', ALIASES)).toBeNull();
  });
  it('a directive with no remainder is not a match (never strips to empty)', () => {
    expect(matchLeadingDirective('hypatia:', ALIASES)).toBeNull();
    expect(matchLeadingDirective('message for hypatia', ALIASES)).toBeNull();
    expect(matchLeadingDirective('hypatia:   ', ALIASES)).toBeNull();
  });
  it('an alias that is a substring of a longer word does not false-match', () => {
    expect(matchLeadingDirective('veranda: needs paint', ALIASES)).toBeNull();
  });
  it('a name NOT at the start is not a directive', () => {
    expect(matchLeadingDirective('remind hypatia: later', ALIASES)).toBeNull();
  });
  it('empty / blank text → null', () => {
    expect(matchLeadingDirective('', ALIASES)).toBeNull();
    expect(matchLeadingDirective('   ', ALIASES)).toBeNull();
  });
});

describe('loadDirectiveAliases — garbage-safe config', () => {
  beforeEach(() => {
    delete process.env.SHORTCUT_DIRECTIVE_ALIASES;
  });
  afterEach(() => {
    delete process.env.SHORTCUT_DIRECTIVE_ALIASES;
  });

  it('defaults when the env is absent (and canonical is a self-alias)', () => {
    const map = loadDirectiveAliases();
    expect(map.hypatia).toContain('hypatia');
    expect(map.hypatia).toContain('high patia');
    expect(Object.keys(map).sort()).toEqual(Object.keys(DEFAULT_DIRECTIVE_ALIASES).sort());
  });
  it('parses a valid JSON map and always adds the canonical as a self-alias', () => {
    process.env.SHORTCUT_DIRECTIVE_ALIASES = JSON.stringify({ hypatia: ['high patia'] });
    const map = loadDirectiveAliases();
    expect(map.hypatia).toContain('high patia');
    expect(map.hypatia).toContain('hypatia'); // self-alias auto-added
  });
  it('degrades to defaults on malformed JSON (never throws)', () => {
    process.env.SHORTCUT_DIRECTIVE_ALIASES = '{not valid json';
    expect(loadDirectiveAliases().hypatia).toContain('hypatia');
  });
  it('degrades to defaults on a non-object (array / string) payload', () => {
    process.env.SHORTCUT_DIRECTIVE_ALIASES = '["hypatia"]';
    expect(Object.keys(loadDirectiveAliases()).sort()).toEqual(Object.keys(DEFAULT_DIRECTIVE_ALIASES).sort());
    process.env.SHORTCUT_DIRECTIVE_ALIASES = '"hypatia"';
    expect(Object.keys(loadDirectiveAliases()).sort()).toEqual(Object.keys(DEFAULT_DIRECTIVE_ALIASES).sort());
  });
  it('drops invalid entries and keeps valid ones', () => {
    process.env.SHORTCUT_DIRECTIVE_ALIASES = JSON.stringify({ hypatia: ['hy'], bad: 'not-array', empty: [] });
    const map = loadDirectiveAliases();
    expect(map.hypatia).toEqual(expect.arrayContaining(['hy', 'hypatia']));
    expect(map.bad).toBeUndefined();
    expect(map.empty).toBeUndefined();
  });
});
