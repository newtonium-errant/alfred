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

// Completion is a STAGE, not a disappearance: an acted item stays on the ring for
// TODAY (green) but a prior-day acted item is gone. 26h back is safely a prior
// calendar day in the instance tz regardless of the DST offset.
const TODAY_ISO = new Date().toISOString();
const YESTERDAY_ISO = new Date(Date.now() - 26 * 60 * 60 * 1000).toISOString();
const doneTodaySlot = (over: Partial<FeedItem> = {}) =>
  routineSlot({ id: 'done-today', state: 'acted', acted_at: TODAY_ISO, ...over });

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

  it('per-lane enablement: a task lane is now LIVE (C1b), an unknown-origin lane stays disabled (mutation-verify)', () => {
    // Task-backed → C1b writer wired → an ENABLED live control (no standalone opacity-50).
    const { unmount } = render(
      <RingsHeader items={[slot({ id: 't', evidence: { tier: 1, origin: 'task', path: 'task/A.md' } })]} />,
    );
    fireEvent.click(screen.getByTestId('ring-1'));
    const taskBtn = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(taskBtn.disabled).toBe(false); // ← reddens if completable(task) regresses to false
    expect(taskBtn.className.split(' ')).not.toContain('opacity-50');
    unmount();

    // Unknown / unstamped origin (no origin, no routine_record, tier < 3) → no writer
    // → honestly disabled + the visual-honesty pin.
    const { unmount: unmount2 } = render(
      <RingsHeader items={[slot({ id: 'u', evidence: { tier: 1, surface_reason: 'due today' } })]} />,
    );
    fireEvent.click(screen.getByTestId('ring-1'));
    const unknownBtn = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(unknownBtn.disabled).toBe(true); // ← reddens if completable(unknown) ever returns true
    expect(unknownBtn.className.split(' ')).toContain('opacity-50');
    unmount2();

    // Routine-item → wired writer → an ENABLED live control (no standalone opacity-50).
    render(<RingsHeader items={[routineSlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    const routineBtn = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(routineBtn.disabled).toBe(false);
    expect(routineBtn.className.split(' ')).not.toContain('opacity-50');
  });

  it('completing a routine item leaves the worklist for a WIN + Show-done reveals undo', async () => {
    render(<RingsHeader items={[routineSlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-complete'));
    });
    await flushAct();
    // Done → out of the worklist → the WIN state, and the ✓ is gone.
    expect(screen.queryByTestId('ring-complete')).toBeNull();
    expect(screen.queryByTestId('ring-panel-all-done')).not.toBeNull();
    expect(mockAct).toHaveBeenCalledWith('r', 'done');
    // Completed rows live behind "Show done (1)" — reveal shows the marker + undo.
    fireEvent.click(screen.getByTestId('ring-show-done'));
    expect(screen.queryByTestId('ring-item-done')).not.toBeNull();
    expect(screen.queryByTestId('ring-undo')).not.toBeNull();
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-done')).toBe('true');
  });

  it('undo (from the Show-done drill-down) returns the item to the worklist', async () => {
    // A board-done-today item (state=acted, acted_at today) — stays on the ring.
    render(<RingsHeader items={[doneTodaySlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-all-done')).not.toBeNull();
    expect(screen.queryByTestId('ring-complete')).toBeNull(); // done → not in the worklist
    fireEvent.click(screen.getByTestId('ring-show-done')); // reveal the done row
    expect(screen.queryByTestId('ring-undo')).not.toBeNull();
    mockAct.mockResolvedValue({ ok: true, status: 'undone' });
    await act(async () => {
      fireEvent.click(screen.getByTestId('ring-undo'));
    });
    await flushAct();
    expect(mockAct).toHaveBeenLastCalledWith('r', 'undo_done');
    // Back in the worklist with a live ✓.
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
  it('fetches slot_suggestion items on mount (no state filter — needs today\'s done)', async () => {
    mockList.mockResolvedValue({ items: [slot({ id: 'a', evidence: { tier: 1 } })], count: 1 });
    const { container } = render(<RingsHeader />);
    await waitFor(() => expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(1));
    expect(mockList).toHaveBeenCalledWith({ kind: 'slot_suggestion' });
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

describe('RingsHeader — completion is a STAGE, not a disappearance', () => {
  it("the operator's case: a done-today item is a GREEN ring segment + 'All N done', never red-empty", () => {
    const { container } = render(<RingsHeader items={[doneTodaySlot()]} />);
    // The done item is STILL on the ring — one green segment, not the red-empty circle.
    expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(1);
    expect(screen.queryByTestId('ring-empty-1')).toBeNull();
    expect(container.querySelector('[data-testid="ring-1"] path')?.getAttribute('class')).toContain('text-status-done-fg');
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.getByTestId('ring-panel-1').textContent).toContain('1/1'); // honest ratio
    expect(screen.getByTestId('ring-panel-all-done').textContent).toContain('All 1 done');
    expect(screen.queryByTestId('ring-panel-empty')).toBeNull(); // NOT the red-empty language
  });

  it("yesterday's acted item is EXCLUDED — only TODAY's done counts (stable keys persist)", () => {
    const { container } = render(<RingsHeader items={[routineSlot({ id: 'y', state: 'acted', acted_at: YESTERDAY_ISO })]} />);
    expect(screen.queryByTestId('ring-empty-1')).not.toBeNull(); // nothing today → red-empty
    expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(0);
  });

  it('mixed planned + done-today: 2 segments, "1/2" ratio, worklist shows only the planned one', () => {
    const { container } = render(
      <RingsHeader items={[routineSlot({ id: 'plan' }), doneTodaySlot({ id: 'done' })]} />,
    );
    expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(2);
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.getByTestId('ring-panel-1').textContent).toContain('1/2');
    // Worklist default = the not-done planned item; the done one hides behind Show done.
    expect(screen.getByTestId('ring-panel-worklist').querySelectorAll('[data-testid="ring-panel-item"]')).toHaveLength(1);
    expect(screen.getByTestId('ring-show-done').textContent).toContain('Show done (1)');
    expect(screen.queryByTestId('ring-panel-all-done')).toBeNull(); // not all done
  });
});

describe('RingsHeader — task lane (C1b: completable, done-only)', () => {
  it('an OPEN task row is a LIVE ✓ (task lane is board-completable now)', () => {
    render(<RingsHeader items={[slot({ id: 't', evidence: { tier: 1, origin: 'task', path: 'task/A.md' } })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    const btn = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-done')).toBe('false');
  });

  it('a DONE task row shows ✓ Done but NO undo control (done-only: undo is via chat)', () => {
    // Task done today → stays on the ring (green), all-done WIN; the done row shows the
    // ✓ Done marker but no Undo — a task is completable, NOT board-undoable (undo_done
    // → 422). Reveal the done row via Show-done to inspect it.
    render(
      <RingsHeader
        items={[slot({ id: 't', state: 'acted', acted_at: TODAY_ISO, evidence: { tier: 1, origin: 'task', path: 'task/A.md' } })]}
      />,
    );
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-all-done')).not.toBeNull();
    fireEvent.click(screen.getByTestId('ring-show-done'));
    expect(screen.queryByTestId('ring-item-done')).not.toBeNull(); // ✓ Done marker present
    expect(screen.queryByTestId('ring-undo')).toBeNull(); // ← reddens if the undo gate uses `completable` not `undoable`
  });

  it('by contrast a DONE routine row DOES show Undo (routine is board-undoable)', () => {
    render(<RingsHeader items={[doneTodaySlot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    fireEvent.click(screen.getByTestId('ring-show-done'));
    expect(screen.queryByTestId('ring-undo')).not.toBeNull();
  });
});

describe('RingsHeader — C2 SUGGESTED stage (candidate cards)', () => {
  // A completable routine candidate (so an accept→planned flip shows the live ✓).
  const suggested = (over: Partial<FeedItem> = {}) =>
    slot({
      id: 'sug',
      attention: 'needs_you',
      evidence: { tier: 1, origin: 'routine_item', routine_record: 'routine/SelfCare.md', item_text: 'Meditate', name: 'Meditate', candidate: true },
      ...over,
    });

  it('a SUGGESTED item shows Accept (no ✓), a MUTED segment, and "N suggested" — excluded from the count', () => {
    const { container } = render(<RingsHeader items={[suggested()]} />);
    // Muted (suggested) ring segment, not amber/green.
    expect(container.querySelector('[data-testid="ring-1"] path')?.getAttribute('class')).toContain('text-honeydew-400');
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-accept')).not.toBeNull();
    expect(screen.queryByTestId('ring-complete')).toBeNull(); // candidates aren't completable
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-stage')).toBe('suggested');
    expect(screen.getByTestId('ring-panel-1').textContent).toContain('1 suggested'); // committed count is 0
  });

  it('mixed suggested + planned: 2 segments, count "0/1 done" (candidate excluded from the denominator)', () => {
    const { container } = render(<RingsHeader items={[suggested({ id: 'sug' }), routineSlot({ id: 'plan' })]} />);
    expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(2);
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.getByTestId('ring-panel-1').textContent).toContain('0/1'); // committed=1 (the planned one)
    expect(screen.queryByTestId('ring-accept')).not.toBeNull();
    expect(screen.queryByTestId('ring-complete')).not.toBeNull();
  });

  it('accepting a suggested item POSTs "accept" and optimistically flips it to PLANNED (render-present)', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'sug', action_id: 'accept', render: { tier: 1, name: 'Meditate', committed: true } });
    render(<RingsHeader items={[suggested({ id: 'sug' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => { fireEvent.click(screen.getByTestId('ring-accept')); });
    await flushAct();
    expect(mockAct).toHaveBeenCalledWith('sug', 'accept');
    // Flipped to planned: Accept gone, a live ✓ (routine lane) shown.
    expect(screen.queryByTestId('ring-accept')).toBeNull();
    expect(screen.queryByTestId('ring-complete')).not.toBeNull();
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-stage')).toBe('planned');
  });

  it('an accept that returns NO render does not flip (reconciles) — the item stays suggested', async () => {
    // ← the render-present gate at the surface: already_acted / render-absent → no flip.
    mockAct.mockResolvedValue({ ok: true, status: 'already_acted', id: 'sug', action_id: 'accept' });
    render(<RingsHeader items={[suggested({ id: 'sug' })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    await act(async () => { fireEvent.click(screen.getByTestId('ring-accept')); });
    await flushAct();
    expect(screen.getByTestId('ring-panel-item').getAttribute('data-stage')).toBe('suggested');
    expect(screen.queryByTestId('ring-accept')).not.toBeNull();
  });
});
