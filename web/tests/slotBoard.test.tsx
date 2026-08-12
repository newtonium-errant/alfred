import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

// The day board's RENDER + its undo-grace, and the WIRING pins that drive the
// real home page rather than the component in isolation. Plain DOM assertions
// only (the suite runs without jest-dom — see vitest.setup).

const { mockList, mockAct, modeState } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockAct: vi.fn(),
  modeState: { current: 'checkin' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
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

import HomePage from '../pages/index';
import { SlotBoard } from '../components/feed/SlotBoard';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import { UNDO_MS } from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

const NOW = new Date('2026-08-12T15:00:00Z'); // 12:00 Halifax
const TODAY_CREATED = '2026-08-12T13:00:00Z';
const YESTERDAY_CREATED = '2026-08-11T13:00:00Z';

function slot(over: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}): FeedItem {
  return withServedActions({
    id: 'slot:x',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'A thing',
    mode: 'fyi',
    attention: 'fyi',
    // A routine-item lane: completable AND board-undoable.
    evidence: {
      tier: 1,
      name: 'A thing',
      origin: 'routine_item',
      routine_record: 'routine/Bills.md',
      item_text: 'A thing',
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

// A harness that supplies the ONE completion instance the page normally owns, so
// the board under test behaves exactly as it does on home.
function Harness({ items, now = NOW }: { items: FeedItem[] | null; now?: Date }) {
  const completion = useRingCompletion({});
  return <SlotBoard items={items} completion={completion} now={now} />;
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [], count: 0 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
  modeState.current = 'checkin';
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ kind: 'brief', date: null, markdown: null }) })),
  );
});
afterEach(() => {
  // Unmount FIRST, while the mocks are still alive. The board's unmount flush is
  // real behaviour (a held write must not be dropped when the operator navigates
  // away), so it fires during teardown and calls feedApi.act — and vitest runs
  // this hook BEFORE testing-library's auto-cleanup, so restoring mocks here
  // would leave that flush calling a stubbed-out act and crashing the teardown.
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('SlotBoard — the three stacks speak SLOTS', () => {
  it('labels the stacks Duty / Rhythm / Fuel', () => {
    render(<Harness items={[]} />);
    expect(screen.getByTestId('board-stack-label-duty').textContent).toBe('Duty');
    expect(screen.getByTestId('board-stack-label-rhythm').textContent).toBe('Rhythm');
    expect(screen.getByTestId('board-stack-label-fuel').textContent).toBe('Fuel');
  });

  // The render-level twin of board.test.ts's axis pin: a T1 item stamped rhythm
  // must appear under Rhythm on screen, with Duty empty. Positive control below.
  it('renders a tier-1 rhythm item in the RHYTHM stack, not duty', () => {
    render(<Harness items={[slot({ id: 'guitar', title: 'Guitar' }, { tier: 1, slot: 'rhythm' })]} />);
    expect(screen.getByTestId('board-today-rhythm').textContent).toContain('Guitar');
    expect(screen.queryByTestId('board-today-duty')).toBeNull();
    expect(screen.getByTestId('board-stack-empty-duty')).toBeTruthy();
  });

  it('renders a tier-1 duty item in the DUTY stack (positive control)', () => {
    render(<Harness items={[slot({ id: 'rent', title: 'Rent' }, { tier: 1, slot: 'duty' })]} />);
    expect(screen.getByTestId('board-today-duty').textContent).toContain('Rent');
    expect(screen.queryByTestId('board-today-rhythm')).toBeNull();
  });

  // The residue signal drives BOTH directions plus the honesty carve-out, because
  // an absent stack alone cannot distinguish "the classifier answered everything"
  // from "the residue feature isn't wired". Coverage is 100% on the box today, so
  // the LINE is what the operator actually sees every morning — the stack is the
  // rarer branch, and neither may be silent.
  it('unslotted=0 with items on the board: the line renders, no stack', () => {
    render(<Harness items={[slot({ id: 'a' }, { slot: 'duty' })]} />);
    expect(screen.getByTestId('board-residue-clear').textContent).toBe('Everything on the board found a slot.');
    expect(screen.queryByTestId('board-stack-unslotted')).toBeNull();
  });

  it('unslotted>0: the full stack renders, no line', () => {
    render(<Harness items={[slot({ id: 'a' }, { slot: 'duty' }), slot({ id: 'b', title: 'Mystery' }, {})]} />);
    expect(screen.getByTestId('board-stack-unslotted').textContent).toContain('Mystery');
    expect(screen.queryByTestId('board-residue-clear')).toBeNull();
  });

  it('an EMPTY board claims neither — nothing was sorted, so it must not say so', () => {
    render(<Harness items={[]} />);
    expect(screen.queryByTestId('board-residue-clear')).toBeNull();
    expect(screen.queryByTestId('board-stack-unslotted')).toBeNull();
    expect(screen.getByTestId('board-balance').textContent).toContain('Nothing on the board yet today');
  });

  // The coverage floor at the RENDER. Ships dormant (the box is at 100% today),
  // so the pins are the only thing that will ever have exercised it before the
  // morning it first fires.
  it('below 80%: one warning line, above the rings, naming the balance', () => {
    render(
      <Harness
        items={[
          slot({ id: 'a' }, { slot: 'duty' }),
          slot({ id: 'b' }, { slot: 'rhythm' }),
          slot({ id: 'c' }, { slot: 'fuel' }),
          slot({ id: 'x' }, {}), // 3/4 = 75%
        ]}
      />,
    );
    const warn = screen.getByTestId('board-coverage-warning');
    expect(warn.textContent).toContain('1 of 4 items');
    expect(warn.textContent).toContain('the balance below counts only the rest');
    // It must NOT blame the rings — they show unslotted items in their tier
    // bucket, so a "the rings are missing part of your day" claim would be false.
    expect(warn.textContent).not.toContain('rings');
    // Above the rings in document order.
    const rings = screen.getByTestId('rings-header');
    expect(warn.compareDocumentPosition(rings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('at or above 80%: silent (the dormant case that ships today)', () => {
    render(
      <Harness
        items={[
          slot({ id: 'a' }, { slot: 'duty' }),
          slot({ id: 'b' }, { slot: 'rhythm' }),
          slot({ id: 'c' }, { slot: 'fuel' }),
          slot({ id: 'd' }, { slot: 'duty' }),
          slot({ id: 'x' }, {}), // 4/5 = exactly 80%
        ]}
      />,
    );
    expect(screen.queryByTestId('board-coverage-warning')).toBeNull();
  });

  it('an empty board makes no coverage claim at all', () => {
    render(<Harness items={[]} />);
    expect(screen.queryByTestId('board-coverage-warning')).toBeNull();
  });

  it('shows the unslotted residue with its own honest note, and only when present', () => {
    const { unmount } = render(<Harness items={[slot({ id: 'a' }, { slot: 'duty' })]} />);
    expect(screen.queryByTestId(`board-stack-unslotted`)).toBeNull();
    unmount();
    render(<Harness items={[slot({ id: 'b', title: 'Mystery' }, {})]} />);
    expect(screen.getByTestId('board-stack-unslotted').textContent).toContain('Mystery');
    expect(screen.getByTestId('board-residue-note').textContent).toContain('don’t count');
  });
});

describe('SlotBoard — intentionally-left-blank states', () => {
  it('says it is loading rather than rendering a blank board', () => {
    render(<Harness items={null} />);
    expect(screen.getByTestId('board-loading')).toBeTruthy();
    expect(screen.queryByTestId('board-stacks')).toBeNull();
  });

  it('says the board ran and found nothing, rather than showing three silent boxes', () => {
    render(<Harness items={[]} />);
    expect(screen.getByTestId('board-balance').textContent).toContain('Nothing on the board yet today');
    expect(screen.getByTestId('board-stack-empty-duty')).toBeTruthy();
  });

  it('reports the balanced-day scoreline once anything is on the board', () => {
    render(<Harness items={[slot({ id: 'a' }, { slot: 'duty' })]} />);
    expect(screen.getByTestId('board-balance').textContent).toBe('0 of 3 slots have something done — no slot outranks another.');
  });
});

describe('SlotBoard — carryover, candidates, browse-on-swap', () => {
  it('separates carried items from today’s and explains why each is back', () => {
    render(
      <Harness
        items={[
          slot({ id: 'fresh', title: 'Fresh', created_at: TODAY_CREATED }, { slot: 'duty' }),
          slot(
            { id: 'late', title: 'Late', created_at: YESTERDAY_CREATED },
            { slot: 'duty', due_iso: '2026-08-11' },
          ),
        ]}
      />,
    );
    expect(screen.getByTestId('board-today-duty').textContent).toContain('Fresh');
    const carried = screen.getByTestId('board-carryover-duty');
    expect(carried.textContent).toContain('Late');
    expect(carried.textContent).toContain('Past its due date');
  });

  it('offers candidates with Accept, capped at three, the rest behind browse', () => {
    const cands = Array.from({ length: 5 }, (_, i) =>
      slot({ id: `s${i}`, title: `Cand ${i}` }, { slot: 'fuel', candidate: true }),
    );
    render(<Harness items={cands} />);
    expect(screen.getByTestId('board-candidates-fuel').querySelectorAll('[data-testid="board-item"]')).toHaveLength(3);
    // Demoted, not dropped — and reachable in one tap.
    const browse = screen.getByTestId('board-browse-fuel');
    expect(browse.textContent).toContain('Browse the rest (2)');
    expect(screen.queryByTestId('board-overflow-fuel')).toBeNull();
    fireEvent.click(browse);
    expect(screen.getByTestId('board-overflow-fuel').querySelectorAll('[data-testid="board-item"]')).toHaveLength(2);
  });

  // The Accept control has to be PRESSED by a pin, not merely counted: a rendered
  // button nobody clicks passes identically against one wired to nothing.
  it('pressing Accept commits the candidate through the accept verb', async () => {
    // The RENDER-PRESENT GATE is `useSlotAccept`'s, not the board's: the flip lands
    // on a response that carries a render payload, never on the optimistic tap.
    mockAct.mockResolvedValue({
      ok: true,
      status: 'acted',
      id: 'cand',
      action_id: 'accept',
      render: { tier: 1, name: 'Walk', committed: true },
    });
    render(<Harness items={[slot({ id: 'cand', title: 'Walk' }, { slot: 'fuel', candidate: true })]} />);
    await act(async () => { fireEvent.click(screen.getByTestId('board-accept')); });
    await flush();
    expect(mockAct).toHaveBeenCalledWith('cand', 'accept');
    // …and an accepted candidate becomes a COMMITMENT, not a completion — it moves
    // out of "Worth considering" and onto the plan with a live ✓.
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).toBe('planned');
    expect(screen.queryByTestId('board-accept')).toBeNull();
    expect(screen.getByTestId('board-complete')).toBeTruthy();
  });

  it('an accept with NO render payload does not flip — it stays a candidate', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'already_acted', id: 'cand', action_id: 'accept' });
    render(<Harness items={[slot({ id: 'cand', title: 'Walk' }, { slot: 'fuel', candidate: true })]} />);
    await act(async () => { fireEvent.click(screen.getByTestId('board-accept')); });
    await flush();
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).toBe('suggested');
    expect(screen.getByTestId('board-accept')).toBeTruthy();
  });

  it('an unknown-origin lane gets an honestly DISABLED ✓, not a dead control', () => {
    render(
      <Harness
        items={[slot({ id: 'u', title: 'Unknown' }, { slot: 'duty', tier: 1, origin: '', routine_record: null })]}
      />,
    );
    const btn = screen.getByTestId('board-complete');
    expect(btn.getAttribute('disabled')).not.toBeNull();
    expect(btn.getAttribute('aria-disabled')).toBe('true');
    expect(btn.className).toContain('opacity-50');
  });
});

describe('SlotBoard — undo-grace holds the write (option (a), board layer)', () => {
  it('a tap paints the row done and posts NOTHING until the window closes', async () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));

    expect(screen.getByTestId('board-item').getAttribute('data-stage')).toBe('done');
    expect(screen.getByTestId('board-toast').textContent).toContain('Pay Eastlink');
    // The whole point of the mechanism: nothing has been written yet.
    expect(mockAct).not.toHaveBeenCalled();

    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith('r', 'done');
  });

  it('undo inside the window CANCELS — the act is never sent at all', async () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    fireEvent.click(screen.getByTestId('board-toast-undo'));

    expect(screen.getByTestId('board-item').getAttribute('data-stage')).not.toBe('done');
    expect(screen.queryByTestId('board-toast')).toBeNull();
    // Run well past the window: a cancelled write must never arrive late.
    await act(async () => { vi.advanceTimersByTime(UNDO_MS * 3); });
    expect(mockAct).not.toHaveBeenCalled();
  });

  // Caught by the render pins during the build: without this the row jumped
  // straight into the collapsed "Show done" drill on tap — the operator's
  // commitment appeared to be ERASED rather than satisfied, and the row they
  // might want to take back was off screen for the whole grace window.
  it('the completed row stays visible in its own stack, not hidden behind Show done', async () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));

    const duty = screen.getByTestId('board-today-duty');
    expect(duty.textContent).toContain('Pay Eastlink');
    expect(screen.getByTestId('board-item-done').textContent).toContain('Done');
    // Still there after the write actually commits.
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    expect(screen.getByTestId('board-today-duty').textContent).toContain('Pay Eastlink');
  });

  it('the scoreline counts the completion immediately, wherever the row renders', () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    expect(screen.getByTestId('board-balance').textContent).toBe('0 of 3 slots have something done — no slot outranks another.');
    fireEvent.click(screen.getByTestId('board-complete'));
    expect(screen.getByTestId('board-balance').textContent).toBe('1 of 3 slots have something done — no slot outranks another.');
    expect(screen.getByTestId('board-stack-score-duty').textContent).toBe('1/1 done');
  });

  it('the toast carries a draining bar whose duration IS UNDO_MS', () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    const bar = screen.getByTestId('board-toast-bar');
    expect(bar.getAttribute('style')).toContain(`${UNDO_MS}ms`);
  });

  it('no row-level Undo while the write is still held — only the toast can cancel', () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    // The row reads done, but its Undo would POST undo_done for a write that
    // was never made. The toast's Undo is the only correct control here.
    expect(screen.queryByTestId('board-undo')).toBeNull();
    expect(screen.getByTestId('board-toast-undo')).toBeTruthy();
  });

  it('a second tap flushes the first write IN ORDER rather than cancelling it', async () => {
    vi.useFakeTimers();
    render(
      <Harness
        items={[
          slot({ id: 'a', title: 'A' }, { slot: 'duty' }),
          slot({ id: 'b', title: 'B' }, { slot: 'rhythm' }),
        ]}
      />,
    );
    const [first, second] = screen.getAllByTestId('board-complete');
    fireEvent.click(first);
    fireEvent.click(second);
    // The first is committed the moment the second is started.
    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenNthCalledWith(1, 'a', 'done');
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    expect(mockAct).toHaveBeenCalledTimes(2);
    expect(mockAct).toHaveBeenNthCalledWith(2, 'b', 'done');
  });

  it('unmounting mid-window commits the held write rather than dropping it', async () => {
    vi.useFakeTimers();
    const { unmount } = render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    expect(mockAct).not.toHaveBeenCalled();
    unmount();
    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith('r', 'done');
  });

  it('the row stays done across the POST flight — it never blinks back open', async () => {
    vi.useFakeTimers();
    let resolveAct: (v: unknown) => void = () => {};
    mockAct.mockImplementation(() => new Promise((res) => { resolveAct = res; }));
    render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    // Window closed, POST in flight, server has not answered.
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).toBe('done');
    await act(async () => { resolveAct({ ok: true, status: 'acted' }); });
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).toBe('done');
  });

  it('a FAILED write stops claiming done and shows the error', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValue(new Error('boom'));
    render(<Harness items={[slot({ id: 'r' }, { slot: 'duty' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).not.toBe('done');
    expect(screen.getByTestId('board-item-error')).toBeTruthy();
  });
});

describe('SlotBoard — a settled completion lives behind the done drill', () => {
  // A server-done item: not completed on this board this session, so it does NOT
  // stay in place. This is the positive control for the stay-in-place pin AND for
  // the row-level Undo, which the grace tests only ever assert ABSENT.
  const settled = () =>
    slot(
      { id: 'r', title: 'Pay Eastlink', state: 'acted', acted_at: '2026-08-12T14:00:00Z' },
      { slot: 'duty', done: true },
    );

  it('hides it behind Show done, and reveals it on tap', () => {
    render(<Harness items={[settled()]} />);
    expect(screen.queryByTestId('board-done-duty')).toBeNull();
    const disclosure = screen.getByTestId('board-show-done-duty');
    expect(disclosure.textContent).toBe('Show done (1)');
    fireEvent.click(disclosure);
    expect(screen.getByTestId('board-done-duty').textContent).toContain('Pay Eastlink');
  });

  it('renders a working row-level Undo there (the control the grace pins assert absent)', async () => {
    render(<Harness items={[settled()]} />);
    fireEvent.click(screen.getByTestId('board-show-done-duty'));
    const undo = screen.getByTestId('board-undo');
    expect(undo).toBeTruthy();
    fireEvent.click(undo);
    await flush();
    expect(mockAct).toHaveBeenCalledWith('r', 'undo_done');
  });

  it('says the slot is clear rather than leaving a box whose only content is a button', () => {
    render(<Harness items={[settled()]} />);
    expect(screen.getByTestId('board-stack-clear-duty').textContent).toContain('All done here today');
  });
});

describe('WIRING — the board is home’s top module, driven through the real page', () => {
  it('renders the board above the mode content on home, with the rings inside it', async () => {
    mockList.mockResolvedValue({ items: [slot({ id: 'r', title: 'Pay Eastlink' }, { slot: 'duty' })], count: 1 });
    render(<HomePage />);
    await waitFor(() => expect(screen.getByTestId('slot-board')).toBeTruthy());

    const board = screen.getByTestId('slot-board');
    // The rings headline lives INSIDE the board module (one composed top module).
    expect(board.querySelector('[data-testid="rings-header"]')).toBeTruthy();
    expect(board.querySelector('[data-testid="board-stacks"]')).toBeTruthy();
    // …and the board precedes the mode's own content in document order.
    const checkin = screen.getByTestId('compose-checkin');
    expect(board.compareDocumentPosition(checkin) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('the board shares the page’s ONE feed load — no second slot fetch', async () => {
    mockList.mockResolvedValue({ items: [slot({ id: 'r' }, { slot: 'duty' })], count: 1 });
    render(<HomePage />);
    await waitFor(() => expect(screen.getByTestId('slot-board')).toBeTruthy());
    await flush();
    // The rings' internal fetch is skipped because `items` is controlled; the
    // board adds no fetch of its own. One load drives counts, rings and stacks.
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  // THE END-TO-END PIN: a tap on the real page's board must reach feedApi.act
  // with the item's OWN id. Per-layer unit pins cannot catch a board wired to a
  // completion hook the page never threads.
  it('a tap on the real page completes THAT item after the grace window', async () => {
    mockList.mockResolvedValue({ items: [slot({ id: 'slot:routine/Bills.md::Pay', title: 'Pay' }, { slot: 'duty' })], count: 1 });
    render(<HomePage />);
    await waitFor(() => expect(screen.getByTestId('board-complete')).toBeTruthy());

    vi.useFakeTimers();
    fireEvent.click(screen.getByTestId('board-complete'));
    expect(mockAct).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    expect(mockAct).toHaveBeenCalledWith('slot:routine/Bills.md::Pay', 'done');
  });
});
