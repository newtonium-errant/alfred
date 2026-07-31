import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

// Pins the rings completion state machine: optimistic busy→green on success,
// revert+message on failure, undo cycle, the ActResult status map, and 401 → auth.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useRingCompletion } from '../components/feed/useRingCompletion';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

function slot(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'slot_suggestion:routine/Bills.md::Pay',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Pay Eastlink',
    mode: 'fyi',
    attention: 'needs_you',
    evidence: { tier: 1, routine_record: 'routine/Bills.md', item_text: 'Pay Eastlink' },
    actions: [],
    state: 'open',
    created_at: '2026-07-31T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  };
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted', detail: '', id: 'x', action_id: 'done' });
});
afterEach(() => vi.restoreAllMocks());

describe('useRingCompletion — complete', () => {
  it('goes busy in flight, then green on an "acted" success', async () => {
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    expect(result.current.busy(item.id)).toBe(true);
    expect(result.current.effectiveDone(item)).toBe(false); // not green until success
    await flush();
    expect(result.current.busy(item.id)).toBe(false);
    expect(result.current.effectiveDone(item)).toBe(true);
    expect(mockAct).toHaveBeenCalledWith(item.id, 'done');
  });

  it('treats "already_acted" as done (idempotent double-tap)', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'already_acted' });
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(true);
  });

  it('reverts + surfaces the resolver detail on a 422 refusal', async () => {
    mockAct.mockRejectedValue(new ApiError(422, 'request_failed', 'that item is no longer on record'));
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(false);
    expect(result.current.errorFor(item.id)).toContain('no longer on record');
  });

  it('reverts with the stale message on 409', async () => {
    mockAct.mockRejectedValue(new ApiError(409, 'request_failed'));
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(false);
    expect(result.current.errorFor(item.id)).toContain('resurface');
  });

  it('reverts with the not-available message on 400 (old-router degradation)', async () => {
    mockAct.mockRejectedValue(new ApiError(400, 'request_failed'));
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(false);
    expect(result.current.errorFor(item.id)).toMatch(/isn.t available yet/);
  });

  it('routes a 401 to onAuthExpired, no item error', async () => {
    mockAct.mockRejectedValue(new ApiError(401, 'invalid_session'));
    const onAuthExpired = vi.fn();
    const { result } = renderHook(() => useRingCompletion({ onAuthExpired }));
    const item = slot();
    act(() => result.current.complete(item));
    await flush();
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(result.current.errorFor(item.id)).toBeNull();
  });
});

describe('useRingCompletion — undo', () => {
  it('completes then undoes back to open (undone)', async () => {
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(true);

    mockAct.mockResolvedValue({ ok: true, status: 'undone' });
    act(() => result.current.undo(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(false);
    expect(mockAct).toHaveBeenLastCalledWith(item.id, 'undo_done');
  });

  it('reverts a failed undo back to done', async () => {
    const { result } = renderHook(() => useRingCompletion());
    const item = slot();
    act(() => result.current.complete(item));
    await flush();

    mockAct.mockRejectedValue(new ApiError(400, 'request_failed', 'not currently done'));
    act(() => result.current.undo(item));
    await flush();
    expect(result.current.effectiveDone(item)).toBe(true); // stayed done
    expect(result.current.errorFor(item.id)).toBeTruthy();
  });
});
