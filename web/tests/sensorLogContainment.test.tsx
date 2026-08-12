import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// CONTAINMENT, MADE FALSIFIABLE (team-lead's ruling: structurally-cannot-leak
// beats convention, and the guarantee must be checkable in BOTH directions).
//
// `styles/sensorLog.css` re-skins `FeedRow` / `EvidenceBody`, which the BRIEF's
// home surface also renders. Nothing stops that skin reaching the brief except
// the `data-surface="sensor-log"` ancestor — so the claim "it cannot leak" is
// really two claims, and a test that only checks the feed proves half of it. A
// skin scoped to an attribute NOBODY sets would pass a feed-only pin just as
// happily as a correct one.
//
// So: the feed page HAS the attribute, and the home page — same components,
// same rows — does NOT. Both rendered for real, not asserted from source.
//
// `sensorLogSkin.test.tsx` is the sibling half: it guards the SELECTORS (which
// classes the skin borrows). This file guards the SCOPE (who is inside it).

const { mockList, mockAct, modeState } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockAct: vi.fn(),
  modeState: { current: 'feed' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {}, events: { on: vi.fn(), off: vi.fn() } }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
vi.mock('../lib/algernon/composer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/algernon/composer')>()),
  composeMode: () => modeState.current,
  halifaxHour: () => 12,
  composeModeForDate: () => modeState.current,
}));
vi.mock('../lib/algernon/composerLog', () => ({ useComposerLog: () => {} }));

import FeedPage from '../pages/feed';
import HomePage from '../pages/index';
import type { FeedItem } from '../lib/algernon/feed';

// An FYI item, so BOTH surfaces render a real `FeedRow` from it — the shared
// component whose colours are the thing at stake.
const fyiItem: FeedItem = {
  id: 'radar1',
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
};

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [fyiItem], count: 1 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
  modeState.current = 'feed';
});
afterEach(() => vi.restoreAllMocks());

describe('sensor-log containment — the attribute is set on the feed and nowhere else', () => {
  it('the FEED page declares the surface, on an ancestor of its rows', async () => {
    const { container } = render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-console')).not.toBeNull());

    const console_ = screen.getByTestId('feed-console');
    expect(console_.getAttribute('data-surface')).toBe('sensor-log');

    // ANCESTOR, not merely present somewhere on the page: a skin rule reads
    // `[data-surface='sensor-log'] [data-testid='feed-row']`, so an attribute
    // sitting in a sibling branch would style nothing at all.
    const row = container.querySelector('[data-testid="feed-row"]');
    expect(row).not.toBeNull();
    expect(row!.closest('[data-surface="sensor-log"]')).toBe(console_);
  });

  it('the HOME page renders the SAME row and declares no surface at all', async () => {
    const { container } = render(<HomePage />);
    // Wait for the feed-driven render, so the assertion is made against a
    // populated page rather than a spinner that trivially has no attributes.
    await waitFor(() => expect(container.querySelector('[data-testid="feed-row"]')).not.toBeNull());

    // The positive control is the row itself: the brief IS rendering the shared
    // component this skin re-points. If it were not, "no data-surface here"
    // would be true and meaningless.
    const row = container.querySelector('[data-testid="feed-row"]');
    expect(row).not.toBeNull();

    // …and there is no surface declaration anywhere on it.
    expect(container.querySelectorAll('[data-surface]')).toHaveLength(0);
    expect(row!.closest('[data-surface]')).toBeNull();
  });

  it('the two directions are asserted against the same attribute VALUE', () => {
    // Guards a way this pair could rot into agreement: if the feed page's
    // attribute value were changed without the stylesheet's, the pin above
    // would still pass (it would just be checking a different string). The
    // stylesheet's own scope token is the authority for both.
    const css = require('node:fs').readFileSync(
      require('node:path').join(__dirname, '..', 'styles', 'sensorLog.css'),
      'utf8',
    );
    expect(css).toContain("[data-surface='sensor-log']");
  });
});
