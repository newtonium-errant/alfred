import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Pins the RingsHeader render: three tier rings (segments vs empty red circle),
// the tap-to-expand bucket panel, the disabled ✓ placeholder (no mutation path),
// row-tap evidence, the all-empty ILB caption, and the fetch/401 seam.
// Plain DOM assertions only (the suite runs without jest-dom — see vitest.setup).

const { mockList, mockAct } = vi.hoisted(() => ({ mockList: vi.fn(), mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));

import { RingsHeader } from '../components/feed/RingsHeader';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

// A completable (routine-item) lane item — enables the ✓.
function routineSlot(overrides: Partial<FeedItem> = {}): FeedItem {
  return slot({
    id: 'slot_suggestion:routine/Bills.md::Pay',
    title: 'T1: Pay Eastlink',
    attention: 'needs_you',
    evidence: { tier: 1, origin: 'routine_item', routine_record: 'routine/Bills.md', item_text: 'Pay Eastlink' },
    ...overrides,
  });
}

const flushAct = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

function slot(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'slot_suggestion:task/A.md',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Pay rent',
    mode: 'fyi',
    attention: 'fyi',
    evidence: { tier: 1, name: 'Pay rent', surface_reason: 'due today' },
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
  mockList.mockReset();
  mockList.mockResolvedValue({ items: [], count: 0 });
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
});
afterEach(() => vi.restoreAllMocks());

describe('RingsHeader (controlled render)', () => {
  it('renders three rings; empty tiers draw the red empty circle', () => {
    render(<RingsHeader items={[slot({ id: 'a', evidence: { tier: 1 } })]} />);
    expect(screen.queryByTestId('ring-1')).not.toBeNull();
    expect(screen.queryByTestId('ring-2')).not.toBeNull();
    expect(screen.queryByTestId('ring-3')).not.toBeNull();
    // Tier 1 has an item → no empty circle; tiers 2 & 3 are empty → red circle.
    expect(screen.queryByTestId('ring-empty-1')).toBeNull();
    expect(screen.queryByTestId('ring-empty-2')).not.toBeNull();
    expect(screen.queryByTestId('ring-empty-3')).not.toBeNull();
  });

  it('draws one arc segment per item in a bucket', () => {
    const { container } = render(
      <RingsHeader
        items={[
          slot({ id: 'a', evidence: { tier: 1 } }),
          slot({ id: 'b', evidence: { tier: 1 } }),
          slot({ id: 'c', evidence: { tier: 1 } }),
        ]}
      />,
    );
    expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(3);
  });

  it('shows the all-empty ILB caption when nothing is in any ring', () => {
    render(<RingsHeader items={[]} />);
    expect(screen.queryByTestId('rings-empty')).not.toBeNull();
  });

  it('tapping a ring toggles its bucket panel', () => {
    render(<RingsHeader items={[slot({ id: 'a', evidence: { tier: 1 } })]} />);
    expect(screen.queryByTestId('ring-panel-1')).toBeNull();
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-1')).not.toBeNull();
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-1')).toBeNull();
  });

  it('a NON-completable lane (tier-1, no writer) keeps the honest-disabled ✓, and a row-tap shows evidence', () => {
    render(<RingsHeader items={[slot({ id: 'a', evidence: { tier: 1, surface_reason: 'due today' } })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-item')).not.toBeNull();
    const complete = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(complete.disabled).toBe(true);
    // Visually-honest disabled: the STANDALONE opacity-50 muted class is pinned
    // WITH the disabled attr (split-includes, so a live button's `disabled:opacity-50`
    // variant doesn't count) — un-disabling this forces a conscious restyle too.
    expect(complete.className.split(' ')).toContain('opacity-50');
    // Evidence hidden until the row is tapped.
    expect(screen.queryByTestId('ring-item-evidence')).toBeNull();
    fireEvent.click(screen.getByTestId('ring-item-row'));
    expect(screen.queryByTestId('ring-item-evidence')).not.toBeNull();
    expect(screen.getByTestId('ring-item-evidence').textContent).toContain('due today');
  });

  it('per-lane enablement: a task lane stays disabled, a routine lane is LIVE (mutation-verify)', () => {
    // Task-backed → no board writer → disabled + the honesty pin.
    const { unmount } = render(
      <RingsHeader items={[slot({ id: 't', evidence: { tier: 1, origin: 'task', path: 'task/A.md' } })]} />,
    );
    fireEvent.click(screen.getByTestId('ring-1'));
    const taskBtn = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(taskBtn.disabled).toBe(true); // ← reddens if completable(task) ever returns true
    expect(taskBtn.className.split(' ')).toContain('opacity-50');
    unmount();

    // Routine-item → wired writer → an ENABLED live control (no standalone opacity-50).
    render(<RingsHeader items={[routineSlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    const routineBtn = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(routineBtn.disabled).toBe(false);
    expect(routineBtn.className.split(' ')).not.toContain('opacity-50');
  });

  it('completing a routine item goes green + strikethrough and reveals undo', async () => {
    render(<RingsHeader items={[routineSlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-complete'));
    });
    await flushAct();
    // The ✓ is gone (replaced by the done marker + undo), the item marked done.
    expect(screen.queryByTestId('ring-complete')).toBeNull();
    expect(screen.queryByTestId('ring-item-done')).not.toBeNull();
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-done')).toBe('true');
    expect(screen.queryByTestId('ring-undo')).not.toBeNull();
    expect(mockAct).toHaveBeenCalledWith('r', 'done');
  });

  it('undo on a done row returns it to an actionable ✓', async () => {
    // Start already done (server truth) so undo is available immediately.
    render(<RingsHeader items={[routineSlot({ id: 'r', state: 'acted' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-undo')).not.toBeNull();
    mockAct.mockResolvedValue({ ok: true, status: 'undone' });
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-undo'));
    });
    await flushAct();
    expect(mockAct).toHaveBeenLastCalledWith('r', 'undo_done');
    // Back to an actionable ✓.
    expect(screen.queryByTestId('ring-complete')).not.toBeNull();
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-done')).toBe('false');
  });

  it('a failed completion reverts and shows a per-item error', async () => {
    mockAct.mockRejectedValue(new ApiError(409, 'request_failed'));
    render(<RingsHeader items={[routineSlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-complete'));
    });
    await flushAct();
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-done')).toBe('false');
    expect(screen.queryByTestId('ring-item-error')).not.toBeNull();
    expect(screen.queryByTestId('ring-complete')).not.toBeNull(); // still actionable
  });

  it('an empty bucket panel shows its own ILB line', () => {
    render(<RingsHeader items={[]} />);
    fireEvent.click(screen.getByTestId('ring-2'));
    expect(screen.queryByTestId('ring-panel-empty')).not.toBeNull();
  });
});

describe('RingsHeader (fetch seam)', () => {
  it('fetches open slot_suggestion items on mount', async () => {
    mockList.mockResolvedValue({ items: [slot({ id: 'a', evidence: { tier: 1 } })], count: 1 });
    const { container } = render(<RingsHeader />);
    await waitFor(() => expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(1));
    expect(mockList).toHaveBeenCalledWith({ kind: 'slot_suggestion', state: 'open' });
  });

  it('bubbles a 401 to onAuthExpired instead of showing an error', async () => {
    mockList.mockRejectedValue(new ApiError(401, 'invalid_session'));
    const onAuthExpired = vi.fn();
    render(<RingsHeader onAuthExpired={onAuthExpired} />);
    await waitFor(() => expect(onAuthExpired).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('rings-error')).toBeNull();
  });

  it('shows an error banner on a non-401 failure', async () => {
    mockList.mockRejectedValue(new ApiError(502, 'feed_upstream_unavailable'));
    render(<RingsHeader />);
    await waitFor(() => expect(screen.queryByTestId('rings-error')).not.toBeNull());
  });

  it('skips the fetch entirely when items are supplied (controlled)', () => {
    render(<RingsHeader items={[]} />);
    expect(mockList).not.toHaveBeenCalled();
  });
});
