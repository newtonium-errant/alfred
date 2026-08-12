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
// same rows — is labelled as SOMETHING ELSE. Both rendered for real, not
// asserted from source.
//
// That home-side wording is narrower than it first was, and the narrowing is
// deliberate. The shared Layout now labels every surface it renders, so "home
// has no `data-surface` at all" stopped being true for a reason unrelated to
// leaking. The assertion moved to "home is not labelled sensor-log", paired
// with a control proving the labelling mechanism is live — see the comment on
// the home-side test for why the pair is needed and the narrowed half alone
// would rot.
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

// The scope token, named ONCE. Threaded through all three places this file
// spells it — the feed-side attribute, the home-side exclusion, and the
// stylesheet check — so a rename cannot leave one of them querying a string
// nothing sets any more. That failure would be silent in the worst way: the
// home-side assertion below is an ABSENCE check, and an absence check against
// a stale selector passes forever.
//
// Deliberately test-local rather than exported from production: the scope
// token is the stylesheet's own business, and widening it to a public constant
// is more surface than this guarantee needs.
// Imported from production rather than restated: the page and the tests now
// share one spelling, so a rename cannot half-land.
import { SENSOR_SURFACE } from '../lib/algernon/sensorSurface';

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
    // SYNCHRONISE ON THE ASSERTED ELEMENT, not on a nearby one.
    //
    // This waited on `feed-console`, which is rendered OUTSIDE every `loaded &&`
    // gate and therefore paints on the first frame — before the feed fetch has
    // resolved. The assertions below need `feed-row`, which is behind those
    // gates, so the synchronisation point was satisfied while the asserted
    // element could still be absent: a real 1-in-5 flake under parallel-worker
    // load, and one that would read as "containment broke" rather than "the
    // wait was mistargeted".
    //
    // Waiting on the row (exactly what the home-side sibling below already
    // does) makes the wait cover everything the test touches. Causality is
    // proven, not argued: a 40ms delay on the mocked fetch fails this test
    // deterministically with the old target and passes with this one.
    await waitFor(() => expect(container.querySelector('[data-testid="feed-row"]')).not.toBeNull());

    // POST-ADOPTION: the attribute is emitted by Layout on the surface ROOT, not
    // by this page on its own inner div. The claim being pinned is unchanged —
    // the skin's scope must be an ANCESTOR of the rows it dresses, because a
    // rule reads `[data-surface='sensor-log'] [data-testid='feed-row']` and an
    // attribute in a sibling branch would style nothing. Only WHERE that
    // ancestor sits moved.
    const row = container.querySelector('[data-testid="feed-row"]');
    expect(row).not.toBeNull();
    const scope = row!.closest(`[data-surface="${SENSOR_SURFACE}"]`);
    expect(scope).not.toBeNull();

    // …and the console element (which now carries the PAINTING, not the
    // attribute) sits inside that scope. Both halves of the adoption split are
    // asserted here: the attribute above the content, the paint on the content.
    const console_ = screen.getByTestId('feed-console');
    expect(console_.className).toContain('sensor-console');
    expect(scope!.contains(console_)).toBe(true);
  });

  it('the HOME page renders the SAME row and is NOT labelled as this surface', async () => {
    const { container } = render(<HomePage />);
    // Wait for the feed-driven render, so the assertion is made against a
    // populated page rather than a spinner that trivially has no attributes.
    await waitFor(() => expect(container.querySelector('[data-testid="feed-row"]')).not.toBeNull());

    // The first positive control is the row itself: the brief IS rendering the
    // shared component this skin re-points. If it were not, "not labelled
    // sensor-log here" would be true and meaningless.
    const row = container.querySelector('[data-testid="feed-row"]');
    expect(row).not.toBeNull();

    // THE PAIRED VACUITY CONTROL, and the reason this assertion is narrowed
    // rather than "no [data-surface] at all".
    //
    // The shared Layout now labels EVERY surface it renders (`data-surface` is
    // the containment mechanism the whole arc keys off; the prop is just its
    // ergonomic spelling), so home legitimately carries an attribute — just not
    // this one. Asserting the attribute is absent would now fail for a reason
    // that has nothing to do with leaking.
    //
    // But narrowing alone would rot: in a world where Layout stopped emitting
    // `data-surface` entirely, BOTH directions would go green — home trivially,
    // because nothing matches, and the feed via its own inner console div,
    // whose attribute it sets itself independently of Layout. So the claim is
    // TWO facts and both are pinned: the labelling mechanism is LIVE, and
    // home's label is not ours.
    //
    // Presence form, deliberately not `=== 'warm'`: this file must not pin the
    // deck lane's choice of default name. What matters here is that something
    // labels the surface, not what it chose to call it.
    expect(row!.closest('[data-surface]')).not.toBeNull();
    expect(row!.closest(`[data-surface="${SENSOR_SURFACE}"]`)).toBeNull();
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
    expect(css).toContain(`[data-surface='${SENSOR_SURFACE}']`);
  });
});
