import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

// THE SORT AFFORDANCE, on the board — the operator's 2026-08-19 report:
//
//   "These 'not sorted' items have no way of being sorted. They are in the feed,
//    haven't seen them in the deck. No option to sort."
//
// Four dated tasks sat under "NOT SORTED YET" with DONE as their only control,
// so an item the classifier honestly refused to place stayed refused forever.
//
// What each pin here would catch, and why it is a render test rather than a
// model test: the model (`board.ts`) can be exactly right while the control is
// wired to nothing, and that is the failure mode this whole surface exists to
// end — a button that looks live and does nothing. So every pin below drives a
// real tap through the real hook into a mocked `feedApi.act`, and asserts on
// the VERB that reached the wire.
//
// Plain DOM assertions only (this suite runs without jest-dom).

const { mockList, mockAct } = vi.hoisted(() => ({ mockList: vi.fn(), mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));

import { SlotBoard } from '../components/feed/SlotBoard';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import { SORT_ACTION_BY_SLOT } from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

const NOW = new Date('2026-08-19T15:00:00Z'); // 12:00 Halifax, the day of the report
const TODAY_CREATED = '2026-08-19T13:00:00Z';

/** A curated T2 task with no slot signal — the operator's own shape. */
function unsorted(over: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}): FeedItem {
  return withServedActions({
    id: 'slot:task:task/Call Carfax.md',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T2: Call Carfax',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {
      tier: 2,
      name: 'Call Carfax',
      origin: 'task',
      path: 'task/Call Carfax.md',
      slot: 'unslotted',
      slot_rule: 'no_signal',
      ...evidence,
    },
    actions: [],
    state: 'open',
    created_at: TODAY_CREATED,
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

function Harness({ items, now = NOW }: { items: FeedItem[] | null; now?: Date }) {
  const completion = useRingCompletion({});
  return <SlotBoard items={items} completion={completion} now={now} />;
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [], count: 0 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'sorted', render: { slot: 'duty', sorted: true } });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('the residue row can finally be sorted', () => {
  it('offers a Sort control on an unslotted row', () => {
    render(<Harness items={[unsorted()]} />);
    expect(screen.getByTestId('board-sort-open').textContent).toContain('Sort');
  });

  it('offers NO Sort control on a row that already has a slot', () => {
    // The negative half of the same question. Without it, "the control renders"
    // would pass against a build that put a Sort button on every row on the
    // board — including the ones whose slot the operator never has to think
    // about, which is most of them.
    render(<Harness items={[unsorted({}, { slot: 'duty', slot_rule: 'dated_task' })]} />);
    expect(screen.queryByTestId('board-sort-open')).toBeNull();
  });

  it('opens a three-way picker with no default', () => {
    // CO-EQUALITY, asserted. The three slots are a permission system rather than
    // a priority stack, so the picker offers three peers — not one primary with
    // two alternatives, and nothing pre-selected. A build that shipped a default
    // would be making the taxonomy's choice for the operator.
    render(<Harness items={[unsorted()]} />);
    fireEvent.click(screen.getByTestId('board-sort-open'));

    expect(screen.getByTestId('board-sort-duty').textContent).toBe('Duty');
    expect(screen.getByTestId('board-sort-rhythm').textContent).toBe('Rhythm');
    expect(screen.getByTestId('board-sort-fuel').textContent).toBe('Fuel');
    for (const slot of ['duty', 'rhythm', 'fuel']) {
      const btn = screen.getByTestId(`board-sort-${slot}`);
      expect(btn.getAttribute('disabled')).toBeNull();
      expect(btn.getAttribute('aria-pressed')).toBeNull();
    }
  });

  it('can be closed without choosing', () => {
    render(<Harness items={[unsorted()]} />);
    fireEvent.click(screen.getByTestId('board-sort-open'));
    fireEvent.click(screen.getByTestId('board-sort-cancel'));

    expect(screen.queryByTestId('board-sort-picker')).toBeNull();
    expect(mockAct).not.toHaveBeenCalled();
  });
});

describe('the tap drives the real act wire', () => {
  it.each([
    ['duty', 'sort_duty'],
    ['rhythm', 'sort_rhythm'],
    ['fuel', 'sort_fuel'],
  ])('sorting into %s POSTs %s', async (slot, verb) => {
    // THE WIRE PIN. Mutation that reds it: unwire the picker's onClick (or point
    // `SORT_ACTION_BY_SLOT` at a verb the ceiling does not carry). Asserting the
    // VERB rather than "act was called" is what makes it specific — three
    // buttons that all POSTed `sort_duty` would pass a call-count assertion.
    render(<Harness items={[unsorted()]} />);
    fireEvent.click(screen.getByTestId('board-sort-open'));
    fireEvent.click(screen.getByTestId(`board-sort-${slot}`));
    await flush();

    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith('slot:task:task/Call Carfax.md', verb);
  });

  it('sends the verb the server actually advertises', () => {
    // CROSS-SURFACE DRIFT, client half. `SORT_ACTION_BY_SLOT` is a hand-kept
    // mirror of the Python ceiling (TypeScript cannot import a Python dict), and
    // the tap has to name a verb before any response exists to derive it from.
    // The served fixture is GENERATED from `actions_for()`, so pinning against
    // it proves the client's copy names verbs the server really admits. The
    // Python-side half of this pin reads THIS file's source.
    const served = withServedActions(unsorted()).actions.map((a) => a.verb);
    for (const verb of Object.values(SORT_ACTION_BY_SLOT)) {
      expect(served).toContain(verb);
    }
  });
});

describe('the row moves, and moves honestly', () => {
  it('leaves NOT SORTED and appears in its slot on the same render', async () => {
    // The operator's actual complaint, end to end on the surface. The producer
    // will not re-emit this item until tomorrow morning, so without the
    // optimistic `slotOf` seam the row would sit under "Not sorted yet" for the
    // rest of the day after he told the system where it goes — which reads as
    // the tap having done nothing, i.e. the bug again.
    render(<Harness items={[unsorted()]} />);
    expect(screen.getByTestId('board-stack-unslotted')).toBeTruthy();

    fireEvent.click(screen.getByTestId('board-sort-open'));
    fireEvent.click(screen.getByTestId('board-sort-duty'));
    await flush();

    await waitFor(() => {
      // The residue stack renders ONLY when it holds something, so its absence
      // is the row having left it.
      expect(screen.queryByTestId('board-stack-unslotted')).toBeNull();
    });
    expect(screen.getByTestId('board-today-duty').textContent).toContain('Call Carfax');
  });

  it('does NOT move the row when the server sends no render payload', async () => {
    // THE RENDER-PRESENT GATE, which is what stops the optimistic move from
    // becoming a lie. An older router that does not know these verbs, or any
    // render-absent shape, must leave the row exactly where it was rather than
    // asserting a placement nobody made.
    mockAct.mockResolvedValue({ ok: true, status: 'acted' });
    render(<Harness items={[unsorted()]} />);

    fireEvent.click(screen.getByTestId('board-sort-open'));
    fireEvent.click(screen.getByTestId('board-sort-duty'));
    await flush();

    expect(screen.getByTestId('board-stack-unslotted')).toBeTruthy();
    expect(screen.getByTestId('board-stack-unslotted').textContent).toContain('Call Carfax');
  });

  it('surfaces a refusal on the row instead of moving it', async () => {
    // A free-text item has no record to hold a ruling; the server answers 422
    // with a written sentence. The row must stay put AND say why — a silent
    // failure here is the dead-control failure wearing a success animation.
    const { ApiError } = await import('../lib/algernon/http');
    mockAct.mockRejectedValue(
      new ApiError(422, 'unsupported_item', "this one is a free-text note with no record behind it"),
    );
    render(<Harness items={[unsorted()]} />);

    fireEvent.click(screen.getByTestId('board-sort-open'));
    fireEvent.click(screen.getByTestId('board-sort-fuel'));
    await flush();

    await waitFor(() => {
      expect(screen.getByTestId('board-item-error').textContent).toContain('free-text note');
    });
    expect(screen.getByTestId('board-stack-unslotted').textContent).toContain('Call Carfax');
  });
});

describe('register', () => {
  it('draws the sort controls in the dark console register', () => {
    // The board renders on dark registers, and the census governs LIGHT-
    // background literals on dark-reachable components. These controls carry
    // none — they reuse the same `console-*` function-role tokens the Accept and
    // ✓ Done controls beside them already use, so no `ui-*` marker is owed. This
    // pin states that positively rather than leaving it to the census's silence,
    // because "the census did not complain" is the absence of a red flag and not
    // the presence of proof.
    render(<Harness items={[unsorted()]} />);
    const open = screen.getByTestId('board-sort-open');
    expect(open.className).toContain('border-console-edge');
    expect(open.className).toContain('text-console-ink-dim');
    expect(open.className).not.toMatch(/bg-(white|cream|honeydew|amber|slate-50)/);

    fireEvent.click(open);
    for (const slot of ['duty', 'rhythm', 'fuel']) {
      const btn = screen.getByTestId(`board-sort-${slot}`);
      expect(btn.className).toContain('bg-console-raise');
      expect(btn.className).not.toMatch(/bg-(white|cream|honeydew|amber|slate-50)/);
    }
  });
});

describe('the shell', () => {
  it('renders no sort control with no data — the prerender condition', () => {
    // WHY THIS IS HERE: `/` is a SHELL_ROUTE, so its precached HTML is what
    // `sw.js`'s CACHE_VERSION protects, and the bump's trigger is a change to
    // what the SHELL RENDERS (v6's note is explicit: "the trigger here is the
    // RENDER change, not an edit to this file").
    //
    // This lane owes no bump, and this is the positive observation that says so
    // rather than an argument that it does. At prerender there are no feed items,
    // the residue stack is appended ONLY when it holds something, and the sort
    // control renders ONLY on residue rows — so the painted shell is unchanged.
    // The `slotOf` seam is the same story from the other side: it defaults to
    // `boardSlotOf`, so a board with no overrides groups identically.
    //
    // If this ever goes red, the shell's shape HAS moved and the bump is owed.
    // MEASURED while writing this pin, and worth recording because it is
    // stronger than the argument it was written to support: with `items={null}`
    // the board renders its loading state and NO stacks at all — not three empty
    // ones. So the prerendered `/` cannot contain a stack, let alone a residue
    // row, let alone a sort control.
    render(<Harness items={null} />);
    expect(screen.queryByTestId('board-sort-open')).toBeNull();
    expect(screen.queryByTestId('board-stack-unslotted')).toBeNull();
    // Positive control: the board really rendered, so the absences above are not
    // the absence of a board.
    expect(screen.getByTestId('slot-board')).toBeTruthy();

    // And the loaded-but-empty shape, which is what the shell looks like the
    // instant the first fetch returns nothing: three canonical stacks, no
    // residue, no control.
    cleanup();
    render(<Harness items={[]} />);
    expect(screen.getByTestId('board-stack-duty')).toBeTruthy();
    expect(screen.queryByTestId('board-stack-unslotted')).toBeNull();
    expect(screen.queryByTestId('board-sort-open')).toBeNull();
  });
});
