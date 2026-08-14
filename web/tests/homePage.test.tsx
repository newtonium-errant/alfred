import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Composer deck-pill honesty: the deck PROMISE counts only DECK-ABLE kinds (a
// wired verb) — never the full needs-you total (mirrors feed.tsx / b1). The
// check-in "N things need you" line stays the feed total (true, and distinct
// from the deck subset).

const { mockList, mockAct, modeState } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockAct: vi.fn(),
  modeState: { current: 'checkin' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {}, events: { on: vi.fn(), off: vi.fn() } }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
// Composition mode is controllable per test (default check-in). The brief-card
// COPY helpers are passed through to the real module on purpose (#51): this mock
// exists to control the MODE, and a hand-written copy stub here would let the
// card's real wording drift without any test noticing.
vi.mock('../lib/algernon/composer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/algernon/composer')>()),
  composeMode: () => modeState.current,
  halifaxHour: () => 12,
  composeModeForDate: () => modeState.current,
}));

// C4 router (folded here with the console-completion lane, which owns this
// merge surface): `HomePage` runs `useContactRouter` on mount, so without this
// mock every test in this file makes a real `/api/day/state` fetch. It is
// caught by the router's own fail-safe and harmless, but an unmocked network
// call in a unit test is a latent flake and a lie about what the test exercises.
// `configured: false` is the router's stay-put state — the page renders exactly
// as it did before C4 existed.
vi.mock('../lib/algernon/dayClient', () => ({
  dayApi: {
    state: vi.fn().mockResolvedValue({ configured: false, armed_rules: [], rule_order: [] }),
    contact: vi.fn().mockResolvedValue({ contact_id: '', recorded: false }),
    override: vi.fn().mockResolvedValue({ recorded: false, patterns_surfaced: 0 }),
  },
}));
// No-op the telemetry hook (avoids a real fetch on mount).
vi.mock('../lib/algernon/composerLog', () => ({ useComposerLog: () => {} }));

import HomePage from '../pages/index';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function item(kind: string, id: string, attention: string, mode: string): FeedItem {
  return withServedActions({
    id,
    kind,
    instance: 'salem',
    title: `${kind} ${id}`,
    mode,
    attention,
    evidence: kind === 'slot_suggestion' ? { tier: 1 } : {},
    actions: [],
    state: 'open',
    created_at: '2026-07-31T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

beforeEach(() => {
  mockList.mockReset();
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
  modeState.current = 'checkin';
  // #51: the brief card looks up /api/brief/latest for the artifact's DATE.
  // Stubbed so no test makes a real request; `date: null` is the honest empty
  // spool, which renders the neutral copy.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ kind: 'brief', date: null, markdown: null }) })),
  );
});
afterEach(() => vi.restoreAllMocks());

describe('HomePage composer — the deck pill counts only deck-able kinds', () => {
  it('mixed needs-you: pill = deck-able (1), needs-you line = feed total (2)', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'), // deck-able (wired verb)
        // Non-deck-able HERE because it carries no `ringItemSuggested` marker —
        // NOT because slot_suggestion is inherently non-deck-able (isDeckDealt
        // deals a SUGGESTED slot). #22: that conflation was the old false claim.
        item('slot_suggestion', 's1', 'needs_you', 'decide'),
      ],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-deck-pill')).not.toBeNull());

    const pill = screen.getByTestId('composer-deck-pill').textContent ?? '';
    expect(pill).toContain('1 decision');
    expect(pill).not.toContain('2 decision'); // never over-promises the deck

    // The needs-you line is the FEED total — true, and distinct from the deck.
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('2 things need you');
  });

  // --- #22 / D5: the check-in says what is COUNTED, and claims nothing more ---
  // The surface used to rest on `total = deck + rings`, false in both directions:
  // a SUGGESTED slot_suggestion IS deck-able, and a kind with no wired verb is
  // non-deck-able without being in the rings. The structural partition is
  // deliberately deferred to the reimagine arc, so the copy must not imply one.

  it('when the deck holds only some of them, it says so WITHOUT placing the rest in the rings', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'), // deck-able
        item('slot_suggestion', 's1', 'needs_you', 'decide'), // not deck-able here
      ],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you-deckable')).not.toBeNull());

    const split = screen.getByTestId('composer-needs-you-deckable').textContent ?? '';
    expect(split).toContain('1 of them can be handled in the deck');
    // The retired claim, in any of its spellings.
    expect(split.toLowerCase()).not.toContain('ring');
  });

  it('the whole check-in section never claims the rings account for the remainder', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'),
        item('slot_suggestion', 's1', 'needs_you', 'decide'),
      ],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());

    // Asserted on the RENDERED section, so it holds however the copy is reworded.
    const section = screen.getByTestId('compose-checkin').textContent ?? '';
    expect(section.toLowerCase()).not.toContain('ring');
  });

  it('when the deck holds ALL of them there is nothing to explain, so no split line', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'),
        item('email_tier', 'e2', 'needs_you', 'decide'),
      ],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-deck-pill')).not.toBeNull());

    expect(screen.getByTestId('composer-needs-you').textContent).toContain('2 things need you');
    expect(screen.getByTestId('composer-deck-pill').textContent).toContain('2 decisions');
    expect(screen.queryByTestId('composer-needs-you-deckable')).toBeNull();
  });

  it('an ACTED email_tier is NOT counted in the deck pill (the open-split is the guard)', async () => {
    // Since the composer now fetches with no state filter, `openItems` (state==='open')
    // is the SOLE guard keeping acted items off the deck-pill count — deckableCount
    // has no state awareness, and isNeedsYouItem(acted email_tier) is true. Break the
    // split → an acted email would count as a "decision waiting".
    const actedEmail: FeedItem = { ...item('email_tier', 'e-acted', 'needs_you', 'decide'), state: 'acted' };
    mockList.mockResolvedValue({
      items: [item('email_tier', 'e-open', 'needs_you', 'decide'), actedEmail],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-deck-pill')).not.toBeNull());
    const pill = screen.getByTestId('composer-deck-pill').textContent ?? '';
    expect(pill).toContain('1 decision'); // ONLY the open one
    expect(pill).not.toContain('2 decision');
  });

  it('all needs-you non-deck-able → NO deck pill (empty deck never promised)', async () => {
    mockList.mockResolvedValue({
      items: [item('slot_suggestion', 's1', 'needs_you', 'decide')],
      count: 1,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());

    expect(screen.queryByTestId('composer-deck-pill')).toBeNull();
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('1 thing needs you');
  });

  it('a DONE slot item is excluded from the needs-you count (Phase C)', async () => {
    const doneSlot: FeedItem = { ...item('slot_suggestion', 's1', 'needs_you', 'decide'), state: 'acted' };
    mockList.mockResolvedValue({
      items: [item('email_tier', 'e1', 'needs_you', 'decide'), doneSlot],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());
    // Two needs-you items, but the done slot no longer needs you → count is 1.
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('1 thing needs you');
  });
});

describe('HomePage composer — the rings are PERSISTENT across every mode', () => {
  for (const mode of ['brief', 'checkin', 'feed'] as const) {
    it(`renders the rings header in ${mode} mode (completion surface always exists)`, async () => {
      modeState.current = mode;
      mockList.mockResolvedValue({
        items: [item('slot_suggestion', 's1', 'needs_you', 'decide')],
        count: 1,
      });
      render(<HomePage />);
      await waitFor(() => expect(screen.queryByTestId(`compose-${mode}`)).not.toBeNull());
      expect(screen.queryByTestId('rings-header')).not.toBeNull();
    });
  }

  it('does NOT double-fetch — RingsHeader shares the composer feed load (controlled)', async () => {
    modeState.current = 'feed';
    mockList.mockResolvedValue({ items: [], count: 0 });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('compose-feed')).not.toBeNull());
    // The composer's SINGLE unfiltered list({}) (open + today's done for the
    // rings) — RingsHeader is controlled, so no extra slot_suggestion fetch.
    expect(mockList).toHaveBeenCalledTimes(1);
    expect(mockList).toHaveBeenCalledWith({});
  });
});

describe('HomePage composer — a rings completion moves needs-you in the SAME render', () => {
  const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });
  // A COMPLETABLE tier-1 slot (routine lane → a live ✓ in the rings panel).
  const routineSlot = (id: string): FeedItem => ({
    ...item('slot_suggestion', id, 'needs_you', 'decide'),
    evidence: { tier: 1, routine_record: 'routine/Bills.md', item_text: 'Pay' },
  });

  it('completing a slot in the rings decrements the needs-you count without a refetch', async () => {
    mockList.mockResolvedValue({
      items: [item('email_tier', 'e1', 'needs_you', 'decide'), routineSlot('s1')],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('2 things need you');

    fireEvent.click(screen.getByTestId('ring-1')); // open the T1 bucket panel
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-complete'));
    });
    await flush();

    // THE LAG THIS PINS: the completion hook used to live INSIDE RingsHeader, so the
    // segment went green while this count — reading the raw stage — stayed at 2 until
    // the next fetch. One hoisted hook = one truth, same render.
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('1 thing needs you');
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  it('the rings themselves still flip green — the hoist did not cost the ring its state', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('rings-header')).not.toBeNull());

    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-complete'));
    });
    await flush();

    fireEvent.click(screen.getByTestId('ring-show-done'));
    expect(screen.queryByTestId('ring-item-done')).not.toBeNull();
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('Nothing needs you');
  });

  it('a FAILED completion leaves the count where it was (tracks success, not the tap)', async () => {
    mockAct.mockReset().mockRejectedValue(new Error('boom'));
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());

    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-complete'));
    });
    await flush();

    expect(screen.getByTestId('composer-needs-you').textContent).toContain('1 thing needs you');
    expect(screen.getByTestId('ring-item-error').textContent).toBe('That action failed.');
  });
});

// ---------------------------------------------------------------------------
// #51 — the brief card's RENDERED copy, date-gated
// ---------------------------------------------------------------------------

describe('HomePage brief card — a stale artifact does not call itself fresh', () => {
  // The expectation INPUT comes from production's own day-key helper, so the
  // fixture dates track whatever "today" is when the suite runs. The assertions
  // are on rendered strings, not on the enum.
  async function renderBriefCardWith(date: string | null) {
    modeState.current = 'brief';
    mockList.mockResolvedValue({ items: [], count: 0 });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ kind: 'brief', date, markdown: 'x' }) })),
    );
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('compose-brief')).not.toBeNull());
    return async () => {
      await waitFor(() =>
        expect(screen.getByTestId('composer-brief-headline').textContent).not.toBe('No brief yet'),
      );
    };
  }

  async function todayKey(): Promise<string> {
    const { halifaxDayKey } = await import('../lib/algernon/composer');
    return halifaxDayKey(new Date());
  }

  it("today's brief: the card says it is ready", async () => {
    const settle = await renderBriefCardWith(await todayKey());
    await settle();
    expect(screen.getByTestId('composer-brief-headline').textContent).toBe(
      'Your morning brief is ready',
    );
  });

  it("yesterday's brief: the card names it as yesterday's and never says ready", async () => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - 1);
    const { halifaxDayKey } = await import('../lib/algernon/composer');
    const settle = await renderBriefCardWith(halifaxDayKey(d));
    await settle();

    const headline = screen.getByTestId('composer-brief-headline').textContent ?? '';
    const detail = screen.getByTestId('composer-brief-detail').textContent ?? '';
    expect(headline).toBe("Yesterday's brief");
    expect(headline).not.toMatch(/ready/i);
    expect(detail).toMatch(/6:00/);
  });

  it('empty spool: says there is no brief yet, and when today’s arrives', async () => {
    modeState.current = 'brief';
    mockList.mockResolvedValue({ items: [], count: 0 });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ kind: 'brief', date: null, markdown: null }) })),
    );
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('compose-brief')).not.toBeNull());
    expect(screen.getByTestId('composer-brief-headline').textContent).toBe('No brief yet');
    expect(screen.getByTestId('composer-brief-detail').textContent ?? '').toMatch(/6:00/);
  });

  it('a FAILED lookup degrades to "no brief yet", never to the fresh claim', async () => {
    modeState.current = 'brief';
    mockList.mockResolvedValue({ items: [], count: 0 });
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('compose-brief')).not.toBeNull());
    // An unreachable API is not evidence that today's brief landed.
    expect(screen.getByTestId('composer-brief-headline').textContent).not.toBe(
      'Your morning brief is ready',
    );
  });
});
