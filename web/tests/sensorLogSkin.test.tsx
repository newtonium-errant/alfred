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
// WHAT IT DOES NOT CLAIM: that the cascade actually applies. jsdom does not
// evaluate an external stylesheet imported through `_app`, so no test here can
// assert a computed colour. This pin guards the SELECTOR TARGETS — the part that
// breaks silently. The paint itself is verified by looking at the page.

const WEB_ROOT = join(__dirname, '..');
const CSS_PATH = join(WEB_ROOT, 'styles', 'sensorLog.css');
// Everything the skin is allowed to re-point lives in one of these. `feed.tsx`
// is in the list because the console skins its OWN markup too — the snoozed row
// is built there, not in FeedRow, and an earlier cut of this pin only looked
// inside `[data-testid='feed-row']` selectors and so watched half the skin.
const SKINNED_SOURCES = [
  'components/feed/FeedRow.tsx',
  'components/feed/EvidenceBody.tsx',
  'pages/feed.tsx',
];

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
    .filter((head, i, all) => i < all.length && head.trim() && !head.trim().startsWith('@'))
    .flatMap((head) => head.split(','))
    .map((s) => s.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
}

/**
 * Every BORROWED class the skin re-points — each `.foo` in any selector that the
 * stylesheet does not own. `.sensor-*` classes are excluded: those are defined
 * here and consumed by the feed's own markup, so they are not a coupling to
 * anyone else's naming. Escaped slashes (`.text-honeydew-600\/80`) are unescaped
 * back to the literal Tailwind class.
 */
function skinnedClasses(): string[] {
  const found = new Set<string>();
  for (const selector of selectors()) {
    for (const m of selector.matchAll(/\.((?:[\w-]|\\\/)+)/g)) {
      const cls = m[1].replace(/\\\//g, '/');
      if (!cls.startsWith('sensor-')) found.add(cls);
    }
  }
  return [...found].sort();
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

  it('every class the skin targets is still emitted by a skinned component', () => {
    const sources = SKINNED_SOURCES.map((rel) => readFileSync(join(WEB_ROOT, rel), 'utf8')).join('\n');
    const orphans = skinnedClasses().filter((cls) => !sources.includes(cls));
    // A failure here means styles/sensorLog.css and the shared row have drifted.
    // Fix BOTH together: update the selector to the component's new class, or
    // drop the now-dead rule.
    expect(orphans).toEqual([]);
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
