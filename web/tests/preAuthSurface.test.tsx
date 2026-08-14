import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';

// THE PRE-AUTH GATE WEARS THE SAME SURFACE AS THE PAGE.
//
// Every authed page renders TWICE: a pre-auth gate while the session probe is in
// flight, then the real page. Both go through `Layout`, so both need the
// `surface` prop — and the gate is the FIRST thing the operator sees on a cold
// open. A gate without it renders warm chrome and then snaps to the console
// hull: a visible flash, on the surface's own doorstep.
//
// `feed.tsx` shipped that way for a while, and the reason it survived is worth
// more than the fix: the AUTHED branch was correct, so the surface was right
// everywhere anyone thought to look. Nothing distinguishes "this page has a
// register" from "this page has a register on every branch" unless something
// checks the branches.
//
// TWO PINS, DELIBERATELY DIFFERENT IN KIND. The first DRIVES one page: feed is
// rendered with the session unresolved and the gate's real chrome inspected.
// The second is a source-level CLASS check over every page that declares a
// surface at all — it cannot be driven without standing up six pages' worth of
// module mocks, and a class pin that exists beats one page's render repeated
// six times badly. The driven pin is what keeps the class pin honest: it proves
// the property the text check is looking for is the property that matters.

vi.mock('../lib/algernon/useSession', () => ({
  // The gate branch: probe in flight, no user yet.
  useSession: () => ({ user: null, loading: true }),
}));
vi.mock('../lib/algernon/feed', () => ({
  feedApi: { list: vi.fn().mockResolvedValue({ items: [], count: 0 }) },
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: vi.fn(), push: vi.fn(), asPath: '/feed', query: {},
    events: { on: vi.fn(), off: vi.fn() },
  }),
}));

import FeedPage from '../pages/feed';
import { SENSOR_SURFACE } from '../lib/algernon/sensorSurface';

const WEB = join(__dirname, '..');

describe('the driven half — the feed gate really wears the register', () => {
  it('renders the gate, not the page', () => {
    // Vacuity control: if this rendered the authed page instead, the assertion
    // below would be testing the branch that was never broken.
    const { container } = render(<FeedPage />);
    expect(container.querySelector('[data-testid="auth-gate"]')).not.toBeNull();
  });

  it('the gate carries the surface AND the hull, not warm chrome', () => {
    const { container } = render(<FeedPage />);
    const root = container.querySelector(`[data-surface="${SENSOR_SURFACE}"]`);
    expect(root).not.toBeNull();
    expect(root?.className).toContain('bg-console-hull');
    expect(root?.className).not.toContain('bg-honeydew-50');
  });
});

describe('the class half — no page declares a surface on only some of its branches', () => {
  // Every page that passes `surface=` to ANY Layout must pass it to EVERY
  // Layout. Pages that never adopt a register are untouched by this rule.
  const PAGES = ['index.tsx', 'chat.tsx', 'feed.tsx', 'deck.tsx', 'ingest.tsx', 'batch.tsx'];

  it('the pin is reading real files with real Layouts — the positive control', () => {
    let withLayout = 0;
    for (const page of PAGES) {
      const src = readFileSync(join(WEB, 'pages', page), 'utf8');
      expect(src.length).toBeGreaterThan(500);
      if (src.includes('<Layout')) withLayout += 1;
    }
    expect(withLayout).toBe(PAGES.length);
  });

  it.each(PAGES)('%s passes surface to every Layout it renders', (page) => {
    const src = readFileSync(join(WEB, 'pages', page), 'utf8');
    if (!src.includes('surface=')) return; // never adopted a register — not this rule's business

    // Each `<Layout` opening tag up to its closing `>`; JSX attributes cannot
    // contain a bare `>` outside braces, so the brace-aware scan is exact
    // enough for the shapes this repo writes.
    const opens: string[] = [];
    for (let i = src.indexOf('<Layout'); i >= 0; i = src.indexOf('<Layout', i + 1)) {
      let depth = 0;
      let j = i;
      for (; j < src.length; j += 1) {
        const c = src[j];
        if (c === '{') depth += 1;
        else if (c === '}') depth -= 1;
        else if (c === '>' && depth === 0) break;
      }
      opens.push(src.slice(i, j + 1));
    }

    expect(opens.length).toBeGreaterThan(0); // vacuity control
    const bare = opens.filter((tag) => !tag.includes('surface='));
    expect(
      bare,
      `${page} renders a <Layout> with no surface prop while other branches have one — ` +
        'the pre-auth gate then flashes warm chrome before the page snaps to its ' +
        'register. Thread the surface through EVERY branch.',
    ).toEqual([]);
  });
});
