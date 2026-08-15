import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FeedRow } from '../components/feed/FeedRow';
import type { FeedItem } from '../lib/algernon/feed';

// THE SKIN COUPLING PIN.
//
// `styles/sensorLog.css` re-skins the SHARED `FeedRow` / `EvidenceBody` under the
// `[data-surface='sensor-log']` ancestor, because those two components are also
// the brief's rows (`pages/index.tsx`) and must not be recoloured at source.
// A skin that targets utility classes it does not own has a real failure mode:
// rename `text-honeydew-700` in the component and the rule silently stops
// matching, leaving dark text on a dark row.
//
// So the coupling is CHECKED rather than trusted. The required class list is
// derived FROM THE STYLESHEET ITSELF at test time — not restated here — so the
// pin cannot drift out of sync with the thing it guards in either direction:
// add a selector to the CSS for a class no component emits and this fails; rename
// a class in the component and this fails.
//
// Each class is checked against the markup ITS OWN SELECTOR REACHES, not against
// every skinned file at once. That distinction is the whole pin: the union
// lookup this replaced could not see an orphaned snoozed-row rule, because every
// class it borrows also appears in FeedRow.tsx.
//
// WHAT IT DOES NOT CLAIM: that the cascade actually applies. jsdom does not
// evaluate an external stylesheet imported through `_app`, so no test here can
// assert a computed colour. This pin guards the SELECTOR TARGETS — the part that
// breaks silently. The paint itself is verified by looking at the page.

const WEB_ROOT = join(__dirname, '..');
const CSS_PATH = join(WEB_ROOT, 'styles', 'sensorLog.css');
// WHICH SOURCE EACH SELECTOR ACTUALLY TARGETS.
//
// Checking every borrowed class against the CONCATENATION of these files was the
// second way this pin watched less than it claimed: every class the snoozed-row
// rules borrow also appears in FeedRow.tsx, so the snoozed row's skin could be
// orphaned outright — its unsnooze button left warm-on-dark in production, the
// exact failure guarded — and the union lookup would still find the string in a
// file that selector cannot match. Per-source is the fix: a class is verified
// against the markup its own selector reaches, and nowhere else.
// `FencedText.tsx` is in this list because the row RENDERS it: EvidenceBody
// passes the evidence body straight through it, so a fenced block inside a feed
// row is markup this surface's selectors genuinely reach. The list models what
// the row renders, not only what it declares directly — a transitive child that
// emits a skinned class is exactly as reachable as one written inline, and
// leaving it out would report a live rule as orphaned (it did, for `.ui-code`).
const ROW_SOURCES = [
  'components/feed/FeedRow.tsx',
  'components/feed/EvidenceBody.tsx',
  'components/markdown/FencedText.tsx',
];
const SNOOZED_ROW_SOURCES = ['pages/feed.tsx'];
// A console-level rule (no testid qualifier) can match anywhere on the surface.
const CONSOLE_SOURCES = [...ROW_SOURCES, ...SNOOZED_ROW_SOURCES];

/** The files a selector can actually reach, by its testid qualifier. */
function sourcesFor(selector: string): string[] {
  if (selector.includes("[data-testid='feed-snoozed-row']")) return SNOOZED_ROW_SOURCES;
  if (selector.includes("[data-testid='feed-row']")) return ROW_SOURCES;
  return CONSOLE_SOURCES;
}

const sourceCache = new Map<string, string>();
function sourceText(rel: string): string {
  let text = sourceCache.get(rel);
  if (text === undefined) {
    text = readFileSync(join(WEB_ROOT, rel), 'utf8');
    sourceCache.set(rel, text);
  }
  return text;
}

const rawCss = readFileSync(CSS_PATH, 'utf8');

// Parse the CSS rather than grepping its text. The first cut of this pin read
// raw lines and duly reported `180deg` and a sentence out of the file header as
// "unscoped selectors" — comments and gradient arguments look exactly like
// selectors to a line matcher. Stripping comments and splitting on real rule
// boundaries is what makes the assertions below about the stylesheet instead of
// about its prose.
const css = rawCss.replace(/\/\*[\s\S]*?\*\//g, '');

/** Every individual selector in the file (multi-line selector lists included). */
function selectors(): string[] {
  return css
    .split('}')
    .map((chunk) => chunk.slice(0, chunk.indexOf('{')))
    .filter((head) => head.trim() && !head.trim().startsWith('@'))
    .flatMap((head) => head.split(','))
    .map((s) => s.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
}

/**
 * One BORROWED class, bound to the selector that re-points it and to the files
 * that selector can actually reach. `.sensor-*` classes are excluded: the
 * stylesheet defines those itself, so they are not a coupling to anyone else's
 * naming. Escaped slashes (`.text-honeydew-600\/80`) are unescaped back to the
 * literal Tailwind class.
 */
interface SkinBinding {
  cls: string;
  selector: string;
  /** The files this class must appear in — its own selector's reach. */
  sources: string[];
}

function skinBindings(): SkinBinding[] {
  const out: SkinBinding[] = [];
  for (const selector of selectors()) {
    for (const m of selector.matchAll(/\.((?:[\w-]|\\\/)+)/g)) {
      const cls = m[1].replace(/\\\//g, '/');
      if (cls.startsWith('sensor-')) continue;
      out.push({ cls, selector, sources: sourcesFor(selector) });
    }
  }
  return out;
}

/** The distinct borrowed classes, for the vacuity guard. */
function skinnedClasses(): string[] {
  return [...new Set(skinBindings().map((b) => b.cls))].sort();
}

function item(over: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'x1',
    kind: 'radar',
    instance: 'salem',
    title: 'A tracked thing',
    mode: 'fyi',
    attention: 'fyi',
    evidence: { detail: 'something' },
    actions: [],
    state: 'open',
    created_at: '2026-08-12T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  };
}

describe('sensor-log skin — the shared row still presents the hooks the CSS targets', () => {
  it('the stylesheet actually targets something (the pin is not vacuous)', () => {
    // Guards the derivation itself: if the regex or the selector shape changed,
    // `skinnedClasses()` would quietly return [] and every assertion below would
    // pass against a skin that targets nothing at all.
    const classes = skinnedClasses();
    expect(classes.length).toBeGreaterThanOrEqual(6);
    expect(classes).toContain('text-honeydew-700');
  });

  it('every class the skin targets is still emitted by the markup THAT selector reaches', () => {
    const orphans = skinBindings()
      .filter(({ cls, sources }) => !sources.some((rel) => sourceText(rel).includes(cls)))
      // The message is the point on failure: it names the selector, so the fix
      // is obvious without re-deriving which rule went stale.
      .map(({ cls, selector }) => `.${cls} — orphaned, targeted by: ${selector}`);
    // A failure here means styles/sensorLog.css and the markup it skins have
    // drifted. Fix BOTH together: re-point the selector at the element's new
    // class, or drop the now-dead rule.
    expect(orphans).toEqual([]);
  });

  it('the per-source check is real — a class present in a file the selector CANNOT reach is still an orphan', () => {
    // The guard on the guard. The union-lookup bug this replaced was invisible
    // precisely because every snoozed-row class also lives in FeedRow.tsx, so a
    // fully-orphaned snoozed-row rule still found its string somewhere. Prove
    // the new lookup is scoped by asking it the same question directly.
    const snoozedOnly = sourcesFor("[data-surface='sensor-log'] [data-testid='feed-snoozed-row'] .bg-cream");
    expect(snoozedOnly).toEqual(['pages/feed.tsx']);
    expect(snoozedOnly).not.toContain('components/feed/FeedRow.tsx');

    // …and that the two qualifiers really do resolve to different file sets.
    const rowOnly = sourcesFor("[data-surface='sensor-log'] [data-testid='feed-row'] .bg-cream");
    expect(rowOnly).not.toContain('pages/feed.tsx');
    expect(rowOnly).toContain('components/feed/FeedRow.tsx');
  });

  it('the row still carries the data-testid the skin keys on', () => {
    render(<FeedRow item={item()} expanded={false} onToggleEvidence={() => {}} />);
    const row = screen.getByTestId('feed-row');
    expect(row).toBeTruthy();
    // …and the root classes the skin re-points, on the element it re-points them on.
    expect(row.className).toContain('bg-cream');
    expect(row.className).toContain('border-honeydew-200');
  });

  it('the skin is scoped so it CANNOT reach the brief', () => {
    // The structural guarantee, asserted on the stylesheet: no rule in this file
    // may target a testid-identified element without the surface attribute in
    // front of it. The brief renders the same `FeedRow` and must keep its warm
    // palette; the attribute is the only thing standing between the two.
    const testidSelectors = selectors().filter((s) => s.includes('[data-testid='));
    expect(testidSelectors.length).toBeGreaterThan(0);
    for (const selector of testidSelectors) {
      expect(selector).toContain("[data-surface='sensor-log']");
    }
  });

  it('every rule in the stylesheet is surface-scoped or a sensor-owned class', () => {
    // The same guarantee widened to the whole file: a bare selector here would
    // be a global style shipped app-wide from a file the feed alone owns, and
    // `_app` imports it on every page.
    const all = selectors();
    expect(all.length).toBeGreaterThan(5);
    const unscoped = all.filter(
      (s) => !s.includes("[data-surface='sensor-log']") && !s.startsWith('.sensor-'),
    );
    expect(unscoped).toEqual([]);
  });
});
