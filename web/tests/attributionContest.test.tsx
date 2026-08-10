import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';

// #63a — the contest door on the demoted attribution card.
//
// The operator ruled attribution confirmations down to the FYI/glance tier
// because they are consistently correct and reviewing them cost him time. The
// door back out has to stay open: tapping "Not right" on the glance card must
// (a) reach the backend as the `contest` action and (b) put the item back under
// things that need you. A demotion with no way back would be the trap the
// ruling explicitly refused.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useFeedBoard } from '../components/feed/useFeedBoard';
import { FeedRow } from '../components/feed/FeedRow';
import { CONTEST_ACTION, contestableItem } from '../lib/algernon/feedConstants';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'attribution:note/A.md|inf-1',
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
  };
}

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'contested' });
});
afterEach(() => vi.restoreAllMocks());

const flush = () => act(async () => { await Promise.resolve(); });

describe('contestableItem — which rows offer the door', () => {
  it('an FYI attribution row is contestable', () => {
    expect(contestableItem(item())).toBe(true);
  });

  it('a needs-you attribution row is NOT (it is already back with the operator)', () => {
    expect(contestableItem(item({ mode: 'decide', attention: 'needs_you' }))).toBe(false);
  });

  it('no other FYI kind offers it — contest is attribution\'s verb alone', () => {
    for (const kind of ['radar', 'health', 'weather', 'peer_digest', 'ticket_notice']) {
      expect(contestableItem(item({ kind }))).toBe(false);
    }
  });

  it('HALF-DEPLOY: against a backend that has not demoted the tier yet, the door\n     never draws — an old server sends attribution as needs-you, and a\n     contest button on a card the operator is already being asked to decide\n     would be a second control meaning the same thing', () => {
    const preDemotion = item({ mode: 'decide', attention: 'needs_you' });
    expect(contestableItem(preDemotion)).toBe(false);
  });
});

describe('useFeedBoard — contest', () => {
  it('POSTs the contest action', async () => {
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'a1' })] }));
    act(() => result.current.contest('a1'));
    expect(mockAct).toHaveBeenCalledWith('a1', CONTEST_ACTION);
    await flush();
  });

  it('moves the row out of FYI and into needs-you', async () => {
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'a1' })] }));
    expect(result.current.fyi.map((i) => i.id)).toEqual(['a1']);
    act(() => result.current.contest('a1'));
    await flush();
    expect(result.current.fyi).toHaveLength(0);
    expect(result.current.needsYou.map((i) => i.id)).toEqual(['a1']);
  });

  it('a FAILED contest returns the row to FYI and says so — the operator must\n     never be left believing a contest landed when it did not', async () => {
    mockAct.mockRejectedValueOnce(new ApiError(500, 'boom'));
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'a1' })] }));
    act(() => result.current.contest('a1'));
    await flush();
    expect(result.current.needsYou).toHaveLength(0);
    expect(result.current.fyi.map((i) => i.id)).toEqual(['a1']);
    expect(result.current.toast?.message).toBeTruthy();
  });

  it('502 routes to the server-side banner, not a logout, and restores', async () => {
    const authExpired = vi.fn();
    mockAct.mockRejectedValueOnce(new ApiError(502, 'feed_upstream_unavailable'));
    const { result } = renderHook(() =>
      useFeedBoard({ items: [item({ id: 'a1' })], onAuthExpired: authExpired }));
    act(() => result.current.contest('a1'));
    await flush();
    expect(result.current.banner).toContain('server-side');
    expect(result.current.fyi.map((i) => i.id)).toEqual(['a1']);
    expect(authExpired).not.toHaveBeenCalled();
  });

  it('401 hands off to the auth-expired caller without restoring mid-nav', async () => {
    const authExpired = vi.fn();
    mockAct.mockRejectedValueOnce(new ApiError(401, 'unauthorized'));
    const { result } = renderHook(() =>
      useFeedBoard({ items: [item({ id: 'a1' })], onAuthExpired: authExpired }));
    act(() => result.current.contest('a1'));
    await flush();
    expect(authExpired).toHaveBeenCalled();
  });

  it('acking a DIFFERENT row is unaffected by a contest in flight', async () => {
    const items = [item({ id: 'a1' }), item({ id: 'a2' })];
    const { result } = renderHook(() => useFeedBoard({ items }));
    act(() => result.current.contest('a1'));
    act(() => result.current.ack('a2'));
    await flush();
    expect(result.current.needsYou.map((i) => i.id)).toEqual(['a1']);
    expect(result.current.fyi).toHaveLength(0);
  });
});

describe('FeedRow — the contest control', () => {
  it('renders the door when onContest is supplied', () => {
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={() => {}} />,
    );
    expect(screen.getByTestId('feed-row-contest')).toBeTruthy();
  });

  it('does not render it on a row that was not given one', () => {
    render(
      <FeedRow item={item({ kind: 'radar' })} expanded={false}
        onToggleEvidence={() => {}} onAck={() => {}} />,
    );
    expect(screen.queryByTestId('feed-row-contest')).toBeNull();
  });

  it('tapping it fires the handler, and the Ack stays available beside it', () => {
    const onContest = vi.fn();
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={onContest} />,
    );
    fireEvent.click(screen.getByTestId('feed-row-contest'));
    expect(onContest).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('feed-row-ack')).toBeTruthy();
  });

  it('carries an accessible label naming the record it contests', () => {
    render(
      <FeedRow item={item()} expanded={false} onToggleEvidence={() => {}}
        onAck={() => {}} onContest={() => {}} />,
    );
    const label = screen.getByTestId('feed-row-contest').getAttribute('aria-label') || '';
    expect(label.toLowerCase()).toContain('note/a.md');
  });
});
