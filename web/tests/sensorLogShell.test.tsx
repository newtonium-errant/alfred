import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { hasExactToken } from './_exactToken';
import { render, screen, waitFor } from '@testing-library/react';
import { SENSOR_SURFACE } from '../lib/algernon/sensorSurface';

// THE SHELL-UNCHANGED PIN — the adoption's required guard.
//
// Adopting `surface="sensor-log"` moved the attribute from an element inside
// `<main>` up to the LAYOUT ROOT, which means it now covers strictly more DOM:
// the nav, the header, the sign-out control, the bug-report FAB. The claim that
// this is visually a no-op was REFUTED before the adoption was written — a
// selector audit found 3 of sensorLog.css's rules would reach the shell from a
// root-level attribute:
//
//   1. the bare `[data-surface='sensor-log']` rule, which carried
//      `background-color` + `color` and would have repainted the whole page
//   2. `:focus-visible`, which would have retargeted the nav's and the FAB's
//      focus rings to the instrument teal (this file loads after globals, so it
//      would have won)
//   3. the `.bg-danger-bg` / `.text-danger` descendants
//
// The ratified fix is the DEFINITIONS/PAINTING SPLIT: custom properties stay on
// the root, where they are inert because a custom property paints nothing;
// everything that paints is scoped to `.sensor-console`. This file asserts the
// split holds, on both the stylesheet and the rendered page.
//
// WHAT IT CANNOT CLAIM: that the shell's computed colours are unchanged. jsdom
// does not evaluate a stylesheet imported through `_app`, so no test here reads
// a real cascade. It asserts the two things that make the cascade safe — no
// painting rule is reachable from the attribute, and no shell element is inside
// the painted scope — which is where this breaks silently.

const CSS = readFileSync(join(__dirname, '..', 'styles', 'sensorLog.css'), 'utf8')
  // Comments first: the header discusses these selectors in prose, and a text
  // scan that counted those would report findings that are not rules.
  .replace(/\/\*[\s\S]*?\*\//g, '');

/** Selector → declaration block, for every rule in the file. */
function rules(): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  for (const chunk of CSS.split('}')) {
    const brace = chunk.indexOf('{');
    if (brace === -1) continue;
    const selector = chunk.slice(0, brace).replace(/\s+/g, ' ').trim();
    if (!selector || selector.startsWith('@')) continue;
    out.push({ selector, body: chunk.slice(brace + 1) });
  }
  return out;
}

/** Declaration names in a block, custom properties included. */
function declarations(body: string): string[] {
  return body
    .split(';')
    .map((d) => d.split(':')[0].trim())
    .filter(Boolean);
}

const { mockList, mockAct } = vi.hoisted(() => ({ mockList: vi.fn(), mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {}, events: { on: vi.fn(), off: vi.fn() } }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import FeedPage from '../pages/feed';
import type { FeedItem } from '../lib/algernon/feed';

const fyiItem: FeedItem = {
  id: 'radar1', kind: 'radar', instance: 'salem', title: 'A tracked thing',
  mode: 'fyi', attention: 'fyi', evidence: { detail: 'x' }, actions: [],
  state: 'open', created_at: '2026-08-12T00:00:00Z', acted_at: null,
  expires_at: null, source_ref: {},
};

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [fyiItem], count: 1 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
});
afterEach(() => vi.restoreAllMocks());

describe('sensor-log adoption — the attribute covers the shell, the paint does not', () => {
  it('the attribute rule is INERT: custom properties only, nothing that paints', () => {
    // The load-bearing assertion of the whole split. A single `background-color`
    // or `color` here and the nav goes dark the moment Layout emits the
    // attribute — which it now does on every feed render.
    const attrRules = rules().filter((r) => r.selector === `[data-surface='${SENSOR_SURFACE}']`);
    expect(attrRules).toHaveLength(1);

    const decls = declarations(attrRules[0].body);
    // Positive control on the denominator: this block really does define the
    // palette, so "no painting declarations" is a fact about a populated rule
    // rather than about an empty one.
    expect(decls.length).toBeGreaterThan(15);

    const painting = decls.filter((d) => !d.startsWith('--'));
    expect(painting).toEqual([]);
  });

  it('every painting rule is scoped to the console CONTENT, not to the surface', () => {
    // The three audited reachers by name. Each must be reachable only through
    // `.sensor-console`; any of them qualified by the bare attribute would be
    // live on the shell again.
    const reachers = [':focus-visible', '.bg-danger-bg', '.text-danger'];
    const found: string[] = [];
    for (const { selector } of rules()) {
      for (const reacher of reachers) {
        // Exact token: a future `.text-danger-strong` must not be counted as
        // `.text-danger`. Latent today, one sibling away — and sibling-adding
        // is what this arc does. See `_exactToken.ts`.
        if (!hasExactToken(selector, reacher)) continue;
        found.push(selector);
        // A row-scoped spelling is fine — that is inside the content by
        // construction. Otherwise it must be `.sensor-console`-scoped.
        //
        // A MARKER-GATED SPELLING IS ALSO FINE, and this is the third
        // containment argument rather than a hole punched for convenience. A
        // `ui-*` rule can only paint an element that OPTED IN by carrying the
        // marker, so "reachable from the attribute" and "reachable to the
        // shell" come apart: the shell's chrome carries no marker, which is a
        // fact about the markup rather than about this selector — so the test
        // below ASSERTS it by rendering the shell, and this allowance is void
        // the moment that assertion goes red.
        //
        // The distinction the guard still enforces is the one it was built for:
        // a BARE painting rule under the attribute (`[data-surface='…'] {…}`,
        // or a bare-element tail) reaches every matching node whether it asked
        // or not. That is what repainted the nav in the original audit, and it
        // still fails here.
        const contained =
          hasExactToken(selector, '.sensor-console') ||
          selector.includes("[data-testid='feed-row']") ||
          /\.ui-[a-z-]+/.test(selector);
        expect(contained, `"${selector}" can reach the shell from the surface attribute`).toBe(true);
      }
    }
    // Vacuity guard: the reachers must actually still be in the file. If a
    // future edit deleted them, an empty sweep would pass and prove nothing.
    expect(found.length).toBeGreaterThanOrEqual(3);
  });

  it('SHELL elements render OUTSIDE the painted scope, feed content inside it', () => {
    render(<FeedPage />);
    return waitFor(() => {
      expect(document.querySelector('[data-testid="feed-row"]')).not.toBeNull();
    }).then(() => {
      // The shell: a nav link and the bug FAB. Both are Layout's, both sit
      // outside `<main>`, and neither may be inside the painted element.
      const navLink = screen.getByTestId('nav-feed');
      expect(navLink.closest('.sensor-console')).toBeNull();
      const fab = document.querySelector('[data-testid="report-bug-fab"]');
      if (fab) expect(fab.closest('.sensor-console')).toBeNull();

      // …and the positive control that makes the two above meaningful: the feed's
      // own content IS inside it. Without this, "nothing is in the painted
      // scope" would also hold for a page that had lost the class entirely.
      const row = document.querySelector('[data-testid="feed-row"]');
      expect(row!.closest('.sensor-console')).not.toBeNull();

      // Both facts at once: the attribute is above BOTH (it is the surface), the
      // paint is above only one.
      expect(navLink.closest(`[data-surface="${SENSOR_SURFACE}"]`)).not.toBeNull();
    });
  });

  it('the SHELL CHROME carries no `ui-*` marker — what earns the marker allowance', () => {
    // THE PIN THAT PAYS FOR THE THIRD CONTAINMENT ARGUMENT ABOVE.
    //
    // That branch lets a `.ui-*` rule sit under the bare attribute on the
    // grounds that a marker-gated rule can only paint what opted in, and the
    // shell's chrome never does. That is a claim about MARKUP, and the selector
    // scan cannot see markup — so without this it would be an assertion the
    // guard makes about itself. Here it is checked against the real shell.
    //
    // NOT A SWEEP OVER THE WHOLE TREE, deliberately. The bug-report DIALOG is
    // Layout-mounted too and it does carry markers (`ui-panel`, and `ui-btn` /
    // `ui-field` through the Button and Textarea it composes) — that predates
    // this lane and it is correct: a full-surface overlay covering a dark page
    // should be dark, which is exactly what an opt-in marker is for. The claim
    // being earned is narrower and it is the one the original audit was about:
    // the persistent chrome — nav, header, sign-out, hamburger, the FAB itself
    // — takes no marker, so no register can repaint it.
    render(<FeedPage />);
    return waitFor(() => {
      expect(screen.getByTestId('nav-feed')).toBeTruthy();
    }).then(() => {
      const chrome = ['nav-feed', 'nav-chat', 'nav-signout', 'nav-menu-button', 'report-bug-fab'];
      const seen: string[] = [];
      for (const id of chrome) {
        const el = document.querySelector(`[data-testid="${id}"]`);
        if (!el) continue;
        seen.push(id);
        expect(
          /(^|\s)ui-[a-z-]+/.test(el.className),
          `shell chrome "${id}" carries a ui-* marker — a register can now repaint it, ` +
            'and the marker allowance in the selector scan above is no longer true',
        ).toBe(false);
      }
      // Vacuity control: an empty sweep proves nothing, and every id above is
      // one `showNav` change away from vanishing.
      expect(seen.length).toBeGreaterThanOrEqual(3);
    });
  });
});
