import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';

// #72 item 4 — WHICH SECTION was wrong.
//
// #63a's contest says an inference is wrong. On its own that answers "how many
// were wrong" and nothing else, and the whole point of the per-section tracking
// is to answer "which section produces bad inferences" — a question the corpus
// row already has a field for and nothing was filling in.
//
// The half that matters here is that the answer rides the SAME act. The server
// writes the corpus row only inside the branch that flips the vault entry, and
// a second contest on an already-contested entry is an idempotent no-op that
// records nothing — so a section named after the contest lands nowhere. That is
// why the picker fires the contest itself rather than annotating one, and why
// the sectionless card-level contest has to stay a first-class one-tap door
// instead of becoming step one of two.

const { mockAct, mockList, modeState } = vi.hoisted(() => ({
  mockAct: vi.fn(),
  mockList: vi.fn(),
  modeState: { current: 'feed' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: mockList } }));
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
import { FeedRow } from '../components/feed/FeedRow';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import {
  CONTEST_ACTION,
  CONTEST_SECTIONS,
  contestSectionSlug,
} from '../lib/algernon/feedConstants';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

const ATTRIBUTION_ID = 'attribution:note/A.md|inf-1';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: ATTRIBUTION_ID,
    kind: 'attribution',
    instance: 'salem',
    title: 'Attribution: note/A.md',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-10T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  });
}

beforeEach(() => {
  modeState.current = 'feed';
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'contested' });
  mockList.mockReset().mockResolvedValue({ items: [item()] });
});
afterEach(() => vi.restoreAllMocks());

const flush = () => act(async () => { await Promise.resolve(); });

describe('CONTEST_SECTIONS — the vocabulary the picker offers', () => {
  it('is the eight rendered summary headings, in render order', () => {
    // Held against the Python renderer's tuple by a drift pin in
    // tests/test_attribution_section_tap.py, which parses THIS array out of the
    // TS source. Restating the list there too would just be a third copy.
    expect([...CONTEST_SECTIONS]).toEqual([
      'Topics',
      'Decisions',
      'Open Questions',
      'Action Items',
      'Key Insights',
      'Raw Contradictions',
      'Discarded Noise',
      'Re-encounters',
    ]);
  });

  it('slugs a multi-word heading into a stable id fragment', () => {
    expect(contestSectionSlug('Open Questions')).toBe('open-questions');
    expect(contestSectionSlug('Topics')).toBe('topics');
  });
});

describe('feedApi.act — carrying the section', () => {
  // The client is mocked in this file, so these two pin the CALL SHAPE the hook
  // produces. The body shape it serialises to is pinned in feedFoundation /
  // feedActRoute against the real client + the real BFF.
  it('sends the tapped section alongside the contest action', async () => {
    const { result } = renderHook(() => useFeedBoard({ items: [item()] }));
    act(() => result.current.contest(ATTRIBUTION_ID, 'Decisions'));
    await flush();
    expect(mockAct).toHaveBeenCalledWith(ATTRIBUTION_ID, CONTEST_ACTION, undefined, 'Decisions');
  });

  it('a card-level contest sends no section — the sectionless door stays open', async () => {
    const { result } = renderHook(() => useFeedBoard({ items: [item()] }));
    act(() => result.current.contest(ATTRIBUTION_ID));
    await flush();
    expect(mockAct).toHaveBeenCalledWith(ATTRIBUTION_ID, CONTEST_ACTION, undefined, undefined);
  });

  it('a sectioned contest that FAILS returns the row to FYI, exactly like a bare\n     one — naming a section must not make a failure look like a success', async () => {
    mockAct.mockRejectedValue(new ApiError(500, 'boom'));
    const { result } = renderHook(() => useFeedBoard({ items: [item()] }));
    act(() => result.current.contest(ATTRIBUTION_ID, 'Topics'));
    await flush();
    expect(result.current.needsYou).toHaveLength(0);
    expect(result.current.fyi.map((i) => i.id)).toEqual([ATTRIBUTION_ID]);
    expect(result.current.toast?.message).toContain("Couldn't flag");
  });
});

describe('FeedRow — the section picker', () => {
  it('offers the picker on a contestable row, closed until asked for', () => {
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={() => {}} />,
    );
    expect(screen.getByTestId('feed-row-contest-which')).toBeTruthy();
    expect(screen.queryByTestId('feed-row-contest-sections')).toBeNull();
  });

  it('does not offer it on a row with no contest door at all', () => {
    render(
      <FeedRow item={item({ kind: 'radar' })} expanded={false}
        onToggleEvidence={() => {}} onAck={() => {}} />,
    );
    expect(screen.queryByTestId('feed-row-contest-which')).toBeNull();
  });

  it('opening it lists every heading in the vocabulary', () => {
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('feed-row-contest-which'));
    for (const section of CONTEST_SECTIONS) {
      expect(screen.getByTestId(`feed-row-contest-section-${contestSectionSlug(section)}`)).toBeTruthy();
    }
  });

  it('tapping a heading CONTESTS with it — one act, not an annotation of a\n     previous one (a second contest records nothing server-side)', () => {
    const onContest = vi.fn();
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={onContest} />,
    );
    fireEvent.click(screen.getByTestId('feed-row-contest-which'));
    fireEvent.click(screen.getByTestId('feed-row-contest-section-action-items'));
    expect(onContest).toHaveBeenCalledTimes(1);
    expect(onContest).toHaveBeenCalledWith('Action Items');
  });

  it('the plain "Not right" door still contests with NO section in ONE tap —\n     the demotion rests on disagreeing being as cheap as agreeing, so the\n     picker must never become step one of two', () => {
    const onContest = vi.fn();
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={onContest} />,
    );
    fireEvent.click(screen.getByTestId('feed-row-contest'));
    expect(onContest).toHaveBeenCalledTimes(1);
    expect(onContest).toHaveBeenCalledWith();
  });

  it('cancel closes the picker without contesting anything', () => {
    const onContest = vi.fn();
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={onContest} />,
    );
    fireEvent.click(screen.getByTestId('feed-row-contest-which'));
    fireEvent.click(screen.getByTestId('feed-row-contest-section-cancel'));
    expect(screen.queryByTestId('feed-row-contest-sections')).toBeNull();
    expect(onContest).not.toHaveBeenCalled();
  });

  it('a section button names the section AND the record it contests', () => {
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('feed-row-contest-which'));
    const label = screen.getByTestId('feed-row-contest-section-topics').getAttribute('aria-label') || '';
    expect(label).toContain('Topics');
    expect(label.toLowerCase()).toContain('note/a.md');
  });
});

// The prop is optional and defaulted at every layer, which is exactly the shape
// that gets pinned by direct invocation and then never threaded at the real call
// site. These drive the actual pages.
describe('through the real pages — the picker is wired, not just built', () => {
  it('FeedPage POSTs the tapped section', async () => {
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-contest-which')).not.toBeNull());
    fireEvent.click(screen.getByTestId('feed-row-contest-which'));
    fireEvent.click(screen.getByTestId('feed-row-contest-section-key-insights'));
    await waitFor(() =>
      expect(mockAct).toHaveBeenCalledWith(ATTRIBUTION_ID, CONTEST_ACTION, undefined, 'Key Insights'),
    );
  });

  it('HomePage composer POSTs the tapped section', async () => {
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-contest-which')).not.toBeNull());
    fireEvent.click(screen.getByTestId('feed-row-contest-which'));
    fireEvent.click(screen.getByTestId('feed-row-contest-section-discarded-noise'));
    await waitFor(() =>
      expect(mockAct).toHaveBeenCalledWith(ATTRIBUTION_ID, CONTEST_ACTION, undefined, 'Discarded Noise'),
    );
  });
});
