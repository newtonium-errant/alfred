import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

// The C2 slot-accept hook: the accept action + the RENDER-PRESENT-gated optimistic
// committed flip, and the ActResult error routing. DOM-free, mocked feedApi.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useSlotAccept, SLOT_ACTION_ACCEPT } from '../components/feed/useSlotAccept';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function candidate(over: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: 'slot_suggestion:task:task/Interview.md',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Interview',
    mode: 'decide',
    attention: 'needs_you',
    evidence: { tier: 1, origin: 'task', name: 'Interview', candidate: true },
    actions: [],
    state: 'open',
    created_at: '2026-07-22T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

// A verbatim accept-success response (from builder-c2's T1_task fixture).
const acceptOk = {
  ok: true,
  status: 'acted',
  detail: "added to today's T1: Interview",
  id: 'slot_suggestion:task:task/Interview.md',
  action_id: 'accept',
  render: { tier: 1, name: 'Interview', committed: true },
};

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue(acceptOk);
});
afterEach(() => vi.restoreAllMocks());

describe('useSlotAccept', () => {
  it('POSTs the "accept" action for the item', async () => {
    const { result } = renderHook(() => useSlotAccept());
    act(() => result.current.accept(candidate()));
    expect(mockAct).toHaveBeenCalledWith('slot_suggestion:task:task/Interview.md', SLOT_ACTION_ACCEPT);
    await flush();
  });

  it('flips to committed ONLY when the response carries render (the render-present gate)', async () => {
    const { result } = renderHook(() => useSlotAccept());
    const item = candidate();
    expect(result.current.accepted(item.id)).toBe(false);
    act(() => result.current.accept(item));
    await flush();
    // ← reddens if the flip keys off status instead of render presence.
    expect(result.current.accepted(item.id)).toBe(true);
    expect(result.current.errorFor(item.id)).toBeNull();
  });

  it('does NOT flip when the response is render-ABSENT (already_acted / older router) — reconciles instead', async () => {
    // A 200 success WITHOUT render (already_acted noop, or a legacy backend) must not
    // assert committed — the next fetch reconciles. This is the gate's negative half.
    mockAct.mockResolvedValue({ ok: true, status: 'already_acted', detail: 'already on the plan', id: candidate().id, action_id: 'accept' });
    const { result } = renderHook(() => useSlotAccept());
    const item = candidate();
    act(() => result.current.accept(item));
    await flush();
    expect(result.current.accepted(item.id)).toBe(false);
    expect(result.current.errorFor(item.id)).toBeNull();
  });

  it('is busy during the flight, clear after', async () => {
    const { result } = renderHook(() => useSlotAccept());
    const item = candidate();
    act(() => result.current.accept(item));
    expect(result.current.busy(item.id)).toBe(true);
    await flush();
    expect(result.current.busy(item.id)).toBe(false);
  });

  it('routes a 401 to onAuthExpired and does not flip', async () => {
    const onAuthExpired = vi.fn();
    mockAct.mockRejectedValue(new ApiError(401, 'invalid_session'));
    const { result } = renderHook(() => useSlotAccept({ onAuthExpired }));
    const item = candidate();
    act(() => result.current.accept(item));
    await flush();
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(result.current.accepted(item.id)).toBe(false);
    expect(result.current.errorFor(item.id)).toBeNull();
  });

  it('surfaces a 422 writer refusal detail and does not flip', async () => {
    mockAct.mockRejectedValue(new ApiError(422, 'error', 'this suggestion has an unrecognized tier — refusing to guess'));
    const { result } = renderHook(() => useSlotAccept());
    const item = candidate();
    act(() => result.current.accept(item));
    await flush();
    expect(result.current.accepted(item.id)).toBe(false);
    expect(result.current.errorFor(item.id)).toContain('unrecognized tier');
  });

  it('maps a 400 provenance guard / old router to the honest already-on-plan line', async () => {
    mockAct.mockRejectedValue(new ApiError(400, 'invalid_action', "this item is already on today's plan — nothing to accept"));
    const { result } = renderHook(() => useSlotAccept());
    const item = candidate();
    act(() => result.current.accept(item));
    await flush();
    expect(result.current.accepted(item.id)).toBe(false);
    expect(result.current.errorFor(item.id)).toContain('already on');
  });

  it('a failed re-accept keeps a prior committed flip (revert reads the current override, not the item)', async () => {
    const { result } = renderHook(() => useSlotAccept());
    const item = candidate();
    // First accept succeeds (render present) → committed.
    act(() => result.current.accept(item));
    await flush();
    expect(result.current.accepted(item.id)).toBe(true);
    // A second accept fails → must NOT un-commit the first.
    mockAct.mockRejectedValue(new ApiError(409, 'stale_item'));
    act(() => result.current.accept(item));
    await flush();
    expect(result.current.accepted(item.id)).toBe(true);
    expect(result.current.errorFor(item.id)).toContain('moved on');
  });
});
