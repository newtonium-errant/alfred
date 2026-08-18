import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

// SNOOZE HONESTY — the render half, across all three surfaces that draw a slot
// row from the shared `rings.ts` stage seam.
//
// THE REPORT (2026-08-16, operator, screenshot-verified): he snoozed three
// overdue T1 duties — delay-a-week — and home's DUTY section rendered all three
// struck through with "✓ DONE", a "3/3 done" scoreline, and "All done here
// today." underneath. A delay recorded as a completion, on the surface he reads
// first every morning, on the day he had just decided to do none of it.
//
// The site was one binary in `ringItemStage`:
//     item.acted_action === 'accept' ? 'planned' : 'done'
// which routed EVERY verb that was not `accept` into the done rendering. A slot
// snooze stamps `state=acted` + `acted_action='snooze'` (action_router.py:1164),
// so it took the else-branch and came out DONE.
//
// These pins are cross-SURFACE on purpose. The seam pins in rings.test.ts prove
// the stage is right; only a render pin proves each surface asks. Plain DOM
// assertions (no jest-dom — see vitest.setup).

const { mockList, mockAct } = vi.hoisted(() => ({ mockList: vi.fn(), mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));

import { SlotBoard } from '../components/feed/SlotBoard';
import { RingsHeader } from '../components/feed/RingsHeader';
import { FeedRow } from '../components/feed/FeedRow';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

const NOW = new Date('2026-08-12T15:00:00Z'); // 12:00 Halifax
const ACTED_TODAY = '2026-08-12T14:00:00Z';

// A routine-item lane: completable AND board-undoable, so every control this
// lane cares about suppressing is one the item would otherwise genuinely earn.
// Pinning against a non-completable item would prove nothing — the ✓ would be
// absent for the wrong reason.
function slot(over: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}): FeedItem {
  return withServedActions({
    id: 'slot:x',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'Pay Eastlink',
    mode: 'fyi',
    attention: 'needs_you',
    evidence: {
      tier: 1,
      slot: 'duty',
      name: 'Pay Eastlink',
      origin: 'routine_item',
      routine_record: 'routine/Bills.md',
      item_text: 'Pay Eastlink',
      ...evidence,
    },
    actions: [],
    state: 'open',
    created_at: '2026-08-12T13:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

const snoozedSlot = (over: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}) =>
  slot({ state: 'acted', acted_action: 'snooze', acted_at: ACTED_TODAY, ...over }, evidence);
const doneSlot = (over: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}) =>
  slot({ state: 'acted', acted_action: 'done', acted_at: ACTED_TODAY, ...over }, evidence);

function BoardHarness({ items }: { items: FeedItem[] }) {
  const completion = useRingCompletion({});
  return <SlotBoard items={items} completion={completion} now={NOW} />;
}

function RowHarness({ item }: { item: FeedItem }) {
  const completion = useRingCompletion({});
  return (
    <ul>
      <FeedRow item={item} expanded={false} onToggleEvidence={() => {}} completion={completion} />
    </ul>
  );
}

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [], count: 0 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ── THE DAY BOARD (home's DUTY section — the reported surface) ───────────────
describe('SlotBoard — a snoozed duty is not a finished duty', () => {
  it('renders SNOOZED, not struck through and not "✓ Done"', () => {
    render(<BoardHarness items={[snoozedSlot()]} />);
    // The drill is collapsed by default, so open it to reach the row.
    fireEvent.click(screen.getByTestId('board-show-snoozed-duty'));
    const row = screen.getByTestId('board-item');
    expect(row.getAttribute('data-stage')).toBe('snoozed');
    expect(screen.getByTestId('board-item-snoozed').textContent).toContain('Snoozed');
    expect(screen.queryByTestId('board-item-done')).toBeNull();
    // The strikethrough is gated on `done` alone, so this is the class that
    // actually appeared in the screenshot.
    expect(row.innerHTML).not.toContain('line-through');
  });

  it('offers NO ✓ control on a snoozed row — a completable lane is the control', () => {
    render(<BoardHarness items={[snoozedSlot()]} />);
    fireEvent.click(screen.getByTestId('board-show-snoozed-duty'));
    expect(screen.queryByTestId('board-complete')).toBeNull();
    // CONTROL: the same evidence, still open, DOES earn the ✓. Without this the
    // assertion above would pass against a board that renders no controls at all.
    cleanup();
    render(<BoardHarness items={[slot()]} />);
    expect(screen.getByTestId('board-complete')).toBeTruthy();
  });

  it('does NOT say "All done here today." over a stack emptied by snoozing', () => {
    // THE SENTENCE THIS LANE CAME FOR. Three overdue duties, all snoozed: the
    // stack has no live work left, and the old code read that as a win.
    render(<BoardHarness items={[snoozedSlot({ id: 's1' }), snoozedSlot({ id: 's2' }), snoozedSlot({ id: 's3' })]} />);
    expect(screen.queryByTestId('board-stack-clear-duty')).toBeNull();
    const line = screen.getByTestId('board-stack-snoozed-clear-duty');
    expect(line.textContent).toContain('3 snoozed');
    expect(line.textContent).not.toContain('All done');
  });

  it('DOES still say "All done here today." when the day was genuinely finished', () => {
    // The positive control for the pin above, and the reason it is not simply
    // "delete the sentence": a real win must still be reported as one.
    render(<BoardHarness items={[doneSlot({ id: 'd1' })]} />);
    expect(screen.getByTestId('board-stack-clear-duty').textContent).toContain('All done here today');
    expect(screen.queryByTestId('board-stack-snoozed-clear-duty')).toBeNull();
  });

  it('keeps "All done" off a MIXED stack — some finished, some pushed', () => {
    // Both branches are reachable with `done` non-empty. Finishing one duty and
    // pushing another is not "all done", and this is the case a naive
    // done-vs-empty gate would still get wrong.
    render(<BoardHarness items={[doneSlot({ id: 'd1' }), snoozedSlot({ id: 's1' })]} />);
    expect(screen.queryByTestId('board-stack-clear-duty')).toBeNull();
    expect(screen.getByTestId('board-stack-snoozed-clear-duty').textContent).toContain('1 snoozed');
  });

  it('excludes snoozed from the "N/M done" scoreline, and shows none for an all-snoozed stack', () => {
    render(<BoardHarness items={[snoozedSlot({ id: 's1' }), snoozedSlot({ id: 's2' }), snoozedSlot({ id: 's3' })]} />);
    // "3/3 done" was the reported scoreline. Snoozed is in neither half of the
    // ratio, so an all-snoozed stack has no ratio to show at all.
    expect(document.body.textContent).not.toContain('3/3 done');
    expect(document.body.textContent).not.toContain('0/3 done');
    cleanup();
    // CONTROL: one done + one planned still scores, and the snoozed third does
    // not inflate either half.
    render(<BoardHarness items={[doneSlot({ id: 'd1' }), slot({ id: 'p1' }), snoozedSlot({ id: 's1' })]} />);
    expect(document.body.textContent).toContain('1/2 done');
  });

  it('does NOT render "Nothing owed today." over a stack holding snoozed rows', () => {
    // The opposite failure: erasing the delay by omission has better manners and
    // is the same lie. He must be able to see what he moved.
    render(<BoardHarness items={[snoozedSlot()]} />);
    expect(screen.queryByTestId('board-stack-empty-duty')).toBeNull();
    expect(screen.getByTestId('board-show-snoozed-duty').textContent).toContain('Show snoozed (1)');
  });

  it('keeps the snoozed drill SEPARATE from the done drill', () => {
    // "I finished this" and "I moved this" are opposite facts about the day.
    render(<BoardHarness items={[doneSlot({ id: 'd1' }), snoozedSlot({ id: 's1' })]} />);
    expect(screen.getByTestId('board-show-done-duty').textContent).toContain('Show done (1)');
    expect(screen.getByTestId('board-show-snoozed-duty').textContent).toContain('Show snoozed (1)');
  });
});

// ── THE RINGS PANEL (the tier drill behind the glance rings) ─────────────────
describe('RingsHeader — the tier panel tells the same truth', () => {
  const renderRings = (items: FeedItem[]) => {
    mockList.mockResolvedValue({ items, count: items.length });
    // `now` is NOT optional decoration here. `ringItemVisibleToday` keeps an
    // acted item on the rings only for the day it was acted, so against the real
    // clock every fixture below is silently dropped as "not today" — and the
    // exclusion pins then pass because the row is ABSENT rather than because it
    // is correctly bucketed. Caught exactly that way: the worklist pin was green
    // against a panel that had never seen the item.
    render(<RingsHeader items={items} now={NOW} />);
    fireEvent.click(screen.getByTestId('ring-1'));
  };

  it('renders SNOOZED in the tier panel, not struck through and not "✓ Done"', () => {
    renderRings([snoozedSlot()]);
    // Behind its own drill, beside the done one. The drill is what makes the row
    // REACHABLE at all: without it the panel reports a count and renders no row,
    // so the operator can see that three duties moved but not which three.
    fireEvent.click(screen.getByTestId('ring-show-snoozed'));
    expect(screen.getByTestId('ring-item-snoozed').textContent).toContain('Snoozed');
    const row = screen.getByTestId('ring-panel-item');
    expect(row.getAttribute('data-stage')).toBe('snoozed');
    expect(row.getAttribute('data-done')).toBe('false');
    expect(row.innerHTML).not.toContain('line-through');
    // No ✓ and no Undo on a delayed row, on this surface too.
    expect(screen.queryByTestId('ring-complete')).toBeNull();
    expect(screen.queryByTestId('ring-undo')).toBeNull();
  });

  it('the snoozed drill is SEPARATE from the done drill and reachable in one tap', () => {
    renderRings([doneSlot({ id: 'd1' }), snoozedSlot({ id: 's1' })]);
    expect(screen.getByTestId('ring-show-done').textContent).toContain('Show done (1)');
    expect(screen.getByTestId('ring-show-snoozed').textContent).toContain('Show snoozed (1)');
    // Collapsed by default — the panel reads as remaining work — and the snoozed
    // row is NOT sitting inside the done list.
    expect(screen.queryByTestId('ring-panel-snoozed')).toBeNull();
    fireEvent.click(screen.getByTestId('ring-show-done'));
    expect(screen.getByTestId('ring-panel-done').textContent).not.toContain('Snoozed');
    fireEvent.click(screen.getByTestId('ring-show-snoozed'));
    expect(screen.getByTestId('ring-panel-snoozed').textContent).toContain('Snoozed');
  });

  it('does NOT claim a win for a tier emptied by snoozing', () => {
    renderRings([snoozedSlot({ id: 's1' }), snoozedSlot({ id: 's2' }), snoozedSlot({ id: 's3' })]);
    // "All 0 done ✓" was the tier-level twin of the board's "All done here today."
    expect(screen.queryByTestId('ring-panel-all-done')).toBeNull();
    expect(screen.getByTestId('ring-panel-all-snoozed').textContent).toContain('3 snoozed');
    // …and the header reports the delay rather than relabelling it "3 suggested",
    // which is what the old single-branch uncommitted fallback did.
    const panel = screen.getByTestId('ring-panel-1');
    expect(panel.textContent).toContain('3 snoozed');
    expect(panel.textContent).not.toContain('suggested');
  });

  it('DOES still claim the win when the tier was genuinely finished (control)', () => {
    renderRings([doneSlot({ id: 'd1' })]);
    expect(screen.getByTestId('ring-panel-all-done').textContent).toContain('All 1 done');
    expect(screen.queryByTestId('ring-panel-all-snoozed')).toBeNull();
  });

  it('keeps snoozed rows out of the tier worklist', () => {
    renderRings([snoozedSlot({ id: 's1' }), slot({ id: 'p1', title: 'Still owed' })]);
    // The worklist is what is still live in the tier. A snoozed row is not
    // remaining work — but the planned one beside it still is (the control).
    const worklist = screen.getByTestId('ring-panel-worklist');
    expect(worklist.textContent).toContain('Still owed');
    expect(worklist.textContent).not.toContain('Pay Eastlink');
    expect(screen.getByTestId('ring-panel-1').textContent).toContain('0/1 done');
  });
});

// ── THE FEED ROW (the third surface on the same seam) ────────────────────────
describe('FeedRow — the stage ladder is total over the union', () => {
  // Both pages that mount a completion-bearing row filter to `state === 'open'`
  // first, so a server-snoozed item does not reach this component today. That is
  // a fetch-level gate one filter away from the file, and it is exactly the kind
  // of call-site reasoning that let the original binary survive: the ladder is
  // pinned on its own terms rather than on who currently calls it.
  it('renders SNOOZED — never the completable arm\'s live "✓ Done" button', () => {
    render(<RowHarness item={snoozedSlot()} />);
    expect(screen.getByTestId('feed-row-snoozed').textContent).toContain('Snoozed');
    expect(screen.queryByTestId('feed-row-complete')).toBeNull();
    expect(screen.queryByTestId('feed-row-done')).toBeNull();
    expect(screen.queryByTestId('feed-row-undo')).toBeNull();
    expect(screen.getByTestId('feed-row').getAttribute('data-done')).toBe('false');
  });

  it('CONTROLS: the same lane still completes when open, and still reads done when done', () => {
    // Without these the pin above passes against a row that renders nothing.
    render(<RowHarness item={slot()} />);
    expect(screen.getByTestId('feed-row-complete')).toBeTruthy();
    cleanup();
    render(<RowHarness item={doneSlot()} />);
    expect(screen.getByTestId('feed-row-done').textContent).toContain('Done');
  });
});
