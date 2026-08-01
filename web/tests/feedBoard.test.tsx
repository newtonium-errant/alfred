import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';

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

  it('renders a peer_digest body as prose + a truncated notice (the digest content)', () => {
    render(
      <FeedRow
        item={item({
          kind: 'peer_digest',
          title: 'Peer digest: kalle',
          evidence: { peer: 'kalle', date: '2026-07-31', body: 'First line.\nSecond line.', truncated: true },
        })}
        expanded
        onToggleEvidence={() => {}}
        onAck={() => {}}
      />,
    );
    const body = screen.getByTestId('evidence-body');
    expect(body.textContent).toContain('First line.');
    expect(body.textContent).toContain('Second line.');
    expect(screen.queryByTestId('evidence-truncated')).not.toBeNull();
    // body/truncated are prose/flag — never key:value rows.
    expect(screen.queryByTestId('feed-row-evidence')?.textContent ?? '').not.toContain('First line.');
  });

  it('a body makes the row expandable even with no key:value rows', () => {
    render(
      <FeedRow
        item={item({ kind: 'peer_digest', title: 'Peer digest: kalle', evidence: { body: 'only a body' } })}
        expanded={false}
        onToggleEvidence={() => {}}
        onAck={() => {}}
      />,
    );
    expect(screen.queryByTestId('feed-row-details')).not.toBeNull();
  });
});

// FeedRow drives the SHARED useRingCompletion (never a second implementation).
// A fake completion object pins FeedRow's per-lane RENDER + wiring against that
// interface; the hook's own act/undo/error logic is pinned in useRingCompletion.test.
import type { UseRingCompletionResult } from '../components/feed/useRingCompletion';
import type { UseSlotAcceptResult } from '../components/feed/useSlotAccept';

function fakeCompletion(over: Partial<UseRingCompletionResult> = {}): UseRingCompletionResult {
  return {
    effectiveDone: () => false,
    busy: () => false,
    errorFor: () => null,
    complete: vi.fn(),
    undo: vi.fn(),
    ...over,
  };
}
function fakeAccept(over: Partial<UseSlotAcceptResult> = {}): UseSlotAcceptResult {
  return {
    accepted: () => false,
    busy: () => false,
    errorFor: () => null,
    accept: vi.fn(),
    ...over,
  };
}
function slot(over: Partial<FeedItem> = {}): FeedItem {
  return item({
    id: 'slot_suggestion:routine/Bills.md::Pay',
    kind: 'slot_suggestion',
    title: 'T1: Pay Eastlink',
    attention: 'needs_you',
    mode: 'decide',
    evidence: { tier: 1, routine_record: 'routine/Bills.md', item_text: 'Pay' },
    ...over,
  });
}

describe('FeedRow — per-lane completion (shared hook)', () => {
  it('a completable lane shows a LIVE ✓ that calls complete()', () => {
    const complete = vi.fn();
    render(
      <FeedRow item={slot()} expanded={false} onToggleEvidence={() => {}} completion={fakeCompletion({ complete })} />,
    );
    const btn = screen.getByTestId('feed-row-complete');
    fireEvent.click(btn);
    expect(complete).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('feed-row-ack')).toBeNull();
  });

  it('an unknown-origin lane shows the honest note + NO button', () => {
    // No origin, no routine_record, tier < 3 → no writer → the honest note. (Task is
    // completable now — see the task-lane tests below.)
    render(
      <FeedRow
        item={slot({ evidence: { tier: 1, surface_reason: 'due today' } })}
        expanded={false}
        onToggleEvidence={() => {}}
        completion={fakeCompletion()}
      />,
    );
    expect(screen.getByTestId('feed-row-unavailable').textContent).toContain("Completion isn't available for this item");
    expect(screen.queryByTestId('feed-row-complete')).toBeNull();
  });

  it('a task lane shows a LIVE ✓ (C1b), not the honest note', () => {
    const complete = vi.fn();
    render(
      <FeedRow
        item={slot({ evidence: { tier: 1, origin: 'task', path: 'task/A.md' } })}
        expanded={false}
        onToggleEvidence={() => {}}
        completion={fakeCompletion({ complete })}
      />,
    );
    expect(screen.queryByTestId('feed-row-unavailable')).toBeNull();
    fireEvent.click(screen.getByTestId('feed-row-complete'));
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it('a DONE task row shows ✓ Done but NO undo (done-only; undo is via chat)', () => {
    // ← reddens if the undo gate uses `completable` instead of `undoable`.
    render(
      <FeedRow
        item={slot({ evidence: { tier: 1, origin: 'task', path: 'task/A.md' } })}
        expanded={false}
        onToggleEvidence={() => {}}
        completion={fakeCompletion({ effectiveDone: () => true })}
      />,
    );
    expect(screen.queryByTestId('feed-row-done')).not.toBeNull();
    expect(screen.queryByTestId('feed-row-undo')).toBeNull();
  });

  it('a done row shows ✓ Done + Undo, strikes the title, and calls undo()', () => {
    const undo = vi.fn();
    render(
      <FeedRow
        item={slot()}
        expanded={false}
        onToggleEvidence={() => {}}
        completion={fakeCompletion({ effectiveDone: () => true, undo })}
      />,
    );
    expect(screen.queryByTestId('feed-row-done')).not.toBeNull();
    expect(screen.getByTestId('feed-row').getAttribute('data-done')).toBe('true');
    fireEvent.click(screen.getByTestId('feed-row-undo'));
    expect(undo).toHaveBeenCalledTimes(1);
  });

  it('surfaces a completion error', () => {
    render(
      <FeedRow
        item={slot()}
        expanded={false}
        onToggleEvidence={() => {}}
        completion={fakeCompletion({ errorFor: () => 'That could not be completed.' })}
      />,
    );
    expect(screen.getByTestId('feed-row-completion-error').textContent).toContain('could not be completed');
  });
});

describe('FeedRow — C2 SUGGESTED stage (accept)', () => {
  const suggested = (over: Partial<FeedItem> = {}) =>
    slot({
      id: 'sug',
      evidence: { tier: 1, origin: 'routine_item', routine_record: 'routine/SelfCare.md', item_text: 'Meditate', name: 'Meditate', candidate: true },
      ...over,
    });

  it('a SUGGESTED slot shows Accept (no ✓) and calls accept()', () => {
    const acc = vi.fn();
    render(
      <FeedRow item={suggested()} expanded={false} onToggleEvidence={() => {}} completion={fakeCompletion()} accept={fakeAccept({ accept: acc })} />,
    );
    expect(screen.queryByTestId('feed-row-complete')).toBeNull(); // candidates aren't completable
    fireEvent.click(screen.getByTestId('feed-row-accept'));
    expect(acc).toHaveBeenCalledTimes(1);
  });

  it('an accepted (optimistic-committed) slot flips to PLANNED — the live ✓, not Accept', () => {
    render(
      <FeedRow item={suggested()} expanded={false} onToggleEvidence={() => {}} completion={fakeCompletion()} accept={fakeAccept({ accepted: () => true })} />,
    );
    expect(screen.queryByTestId('feed-row-accept')).toBeNull();
    expect(screen.queryByTestId('feed-row-complete')).not.toBeNull(); // routine lane → live ✓
  });

  it('surfaces an accept error (merged into the row error line)', () => {
    render(
      <FeedRow item={suggested()} expanded={false} onToggleEvidence={() => {}} completion={fakeCompletion()} accept={fakeAccept({ errorFor: () => "That's already on today's plan." })} />,
    );
    expect(screen.getByTestId('feed-row-completion-error').textContent).toContain('already on');
  });
});
