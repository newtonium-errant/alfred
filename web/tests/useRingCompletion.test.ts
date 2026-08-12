import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

// Pins the rings completion state machine: optimistic busy→green on success,
// revert+message on failure, undo cycle, the ActResult status map, and 401 → auth.

const { mockAct, mockList } = vi.hoisted(() => ({ mockAct: vi.fn(), mockList: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: mockList } }));

import { useRingCompletion } from '../components/feed/useRingCompletion';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function slot(overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
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
  });
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mockAct.mockReset();
  mockList.mockReset();
  mockList.mockResolvedValue({ items: [], count: 0 });
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

// ---------------------------------------------------------------------------
// #62 — the incident, pinned
// ---------------------------------------------------------------------------
//
// 2026-08-07 23:43 ADT: the act COMMITTED server-side in 30ms; the phone never
// saw the response; the client reverted the tick and rendered a failure line.
// Twelve hours later the resumed PWA still showed it pending under that line.
//
// The rule these hold: a network failure is not an act failure — it is a
// failure to LEARN THE OUTCOME, and the client must go and find out before it
// tells the operator their action did not happen.

describe('#62 a timeout no longer declares failure without checking', () => {
  it('THE INCIDENT: act times out but the server DID act → done + honest notice', async () => {
    const it0 = slot();
    mockAct.mockRejectedValue(new ApiError(0, 'timeout'));
    // The server's truth: it landed.
    mockList.mockResolvedValue({ items: [{ ...it0, state: 'acted', acted_action: 'done' }], count: 1 });

    const { result } = renderHook(() => useRingCompletion());
    act(() => result.current.complete(it0));
    await flush();
    await flush();

    expect(result.current.effectiveDone(it0)).toBe(true);
    expect(result.current.errorFor(it0.id)).toBeNull();
    expect(result.current.noticeFor(it0.id)).toContain('landed');
    expect(result.current.busy(it0.id)).toBe(false);
  });

  it('a timeout where the act genuinely did NOT land keeps the error line', async () => {
    const it0 = slot();
    mockAct.mockRejectedValue(new ApiError(0, 'timeout'));
    mockList.mockResolvedValue({ items: [{ ...it0, state: 'open' }], count: 1 });

    const { result } = renderHook(() => useRingCompletion());
    act(() => result.current.complete(it0));
    await flush();
    await flush();

    expect(result.current.effectiveDone(it0)).toBe(false);
    expect(result.current.errorFor(it0.id)).toContain('next sync will reconcile');
    expect(result.current.noticeFor(it0.id)).toBeNull();
  });

  it('an UNVERIFIABLE timeout keeps the error line — unknown is not success', async () => {
    // The direction matters: claiming success we did not observe would be the
    // mirror of the original bug, and worse (a false done cannot be retried).
    const it0 = slot();
    mockAct.mockRejectedValue(new ApiError(0, 'network_error'));
    mockList.mockRejectedValue(new ApiError(0, 'network_error'));

    const { result } = renderHook(() => useRingCompletion());
    act(() => result.current.complete(it0));
    await flush();
    await flush();

    expect(result.current.effectiveDone(it0)).toBe(false);
    expect(result.current.errorFor(it0.id)).toContain('next sync will reconcile');
  });

  it('a 4xx does NOT trigger a verify — the server already answered', async () => {
    const it0 = slot();
    mockAct.mockRejectedValue(new ApiError(409, 'stale_item'));

    const { result } = renderHook(() => useRingCompletion());
    act(() => result.current.complete(it0));
    await flush();

    expect(mockList).not.toHaveBeenCalled();
    expect(result.current.errorFor(it0.id)).toContain('moved on');
  });

  it('a 401 still routes to auth without probing', async () => {
    const onAuthExpired = vi.fn();
    const it0 = slot();
    mockAct.mockRejectedValue(new ApiError(401, 'invalid_session'));

    const { result } = renderHook(() => useRingCompletion({ onAuthExpired }));
    act(() => result.current.complete(it0));
    await flush();

    expect(onAuthExpired).toHaveBeenCalled();
    expect(mockList).not.toHaveBeenCalled();
  });
});

describe('#62 an override must not outlive the question it answered', () => {
  it('a fresh render clears a stale failure override', async () => {
    // Defect (2): before this, effectiveDone preferred the override for the
    // page's lifetime, so a refetch showing `acted` could not correct the
    // display. That is what held a 12-hour-old red line on screen.
    const it0 = slot();
    mockAct.mockRejectedValue(new ApiError(409, 'stale_item'));

    const { result } = renderHook(() => useRingCompletion());
    act(() => result.current.complete(it0));
    await flush();
    expect(result.current.errorFor(it0.id)).not.toBeNull();

    const fresh = { ...it0, state: 'acted', acted_action: 'done' };
    act(() => result.current.reconcile([fresh]));

    expect(result.current.errorFor(it0.id)).toBeNull();
    expect(result.current.effectiveDone(fresh)).toBe(true);
  });

  it('a render mid-flight does NOT clear the spinner', async () => {
    const it0 = slot();
    let resolveAct: (v: unknown) => void = () => {};
    mockAct.mockReturnValue(new Promise((r) => { resolveAct = r; }));

    const { result } = renderHook(() => useRingCompletion());
    act(() => result.current.complete(it0));
    expect(result.current.busy(it0.id)).toBe(true);

    act(() => result.current.reconcile([{ ...it0, state: 'acted' }]));
    expect(result.current.busy(it0.id)).toBe(true);

    await act(async () => {
      resolveAct({ ok: true, status: 'acted', detail: '', id: 'x', action_id: 'done' });
      await Promise.resolve();
    });
    expect(result.current.busy(it0.id)).toBe(false);
  });
});
