import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';

// Pins the Awareness feed board: the needs-you / FYI split + the Ack flow
// (optimistic remove, error restore, server-config banner, auth-expired).

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useFeedBoard } from '../components/feed/useFeedBoard';
import { FeedRow } from '../components/feed/FeedRow';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'ticket_notice:trp-1',
    kind: 'ticket_notice',
    instance: 'kalle',
    title: 'A ticket',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  };
}

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acked' });
});
afterEach(() => vi.restoreAllMocks());

const flush = () => act(async () => { await Promise.resolve(); });

describe('useFeedBoard — grouping', () => {
  it('splits needs-you (decide / needs_you) above FYI', () => {
    const items = [
      item({ id: 'd1', kind: 'email_tier', mode: 'decide', attention: 'needs_you' }),
      item({ id: 'f1', kind: 'radar', mode: 'fyi', attention: 'fyi' }),
    ];
    const { result } = renderHook(() => useFeedBoard({ items }));
    expect(result.current.needsYou.map((i) => i.id)).toEqual(['d1']);
    expect(result.current.fyi.map((i) => i.id)).toEqual(['f1']);
  });
});

describe('useFeedBoard — ack', () => {
  it('optimistically removes the row and POSTs action_id "ack"', async () => {
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'f1' })] }));
    act(() => result.current.ack('f1'));
    expect(result.current.fyi).toHaveLength(0); // gone immediately
    expect(mockAct).toHaveBeenCalledWith('f1', 'ack');
    await flush();
    expect(result.current.fyi).toHaveLength(0); // stays gone on success
    expect(result.current.banner).toBeNull();
  });

  it('409 stale keeps the optimistic remove (already gone at source)', async () => {
    mockAct.mockRejectedValueOnce(new ApiError(409, 'stale_item'));
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'f1' })] }));
    act(() => result.current.ack('f1'));
    await flush();
    expect(result.current.fyi).toHaveLength(0);
  });

  it('502 → server-config banner (never a logout) and RESTORES the row', async () => {
    const authExpired = vi.fn();
    mockAct.mockRejectedValueOnce(new ApiError(502, 'feed_upstream_unavailable'));
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'f1' })], onAuthExpired: authExpired }));
    act(() => result.current.ack('f1'));
    await flush();
    expect(result.current.banner).toContain('server-side');
    expect(result.current.fyi.map((i) => i.id)).toEqual(['f1']); // restored
    expect(authExpired).not.toHaveBeenCalled();
  });

  it('a generic failure restores the row + toasts', async () => {
    mockAct.mockRejectedValueOnce(new ApiError(500, 'boom'));
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'f1' })] }));
    act(() => result.current.ack('f1'));
    await flush();
    expect(result.current.fyi.map((i) => i.id)).toEqual(['f1']);
    expect(result.current.toast?.message).toContain('back');
  });

  it('401 → onAuthExpired', async () => {
    const authExpired = vi.fn();
    mockAct.mockRejectedValueOnce(new ApiError(401, 'invalid_session'));
    const { result } = renderHook(() => useFeedBoard({ items: [item({ id: 'f1' })], onAuthExpired: authExpired }));
    act(() => result.current.ack('f1'));
    await flush();
    expect(authExpired).toHaveBeenCalledTimes(1);
  });
});

describe('FeedRow — defensive render', () => {
  it('renders untrusted evidence as escaped text, with an accessible Ack', () => {
    const evil = '<script>alert(1)</script>';
    render(
      <FeedRow
        item={item({ title: 'Heads up', evidence: { note: evil } })}
        expanded
        onToggleEvidence={() => {}}
        onAck={() => {}}
      />,
    );
    expect(screen.getByText(evil)).toBeTruthy();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByTestId('feed-row-ack').getAttribute('aria-label')).toContain('Heads up');
  });
});
