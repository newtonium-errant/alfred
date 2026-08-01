import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';

// Pins the deck state machine (useDeck) + the card's defensive render. The
// intricate parts — DELAYED ACT (fire on timeout / flush-on-next / cancel-on-undo,
// never an un-act), the two-step HEAVY confirm, client-side PARK, and error
// routing (stale toast / server-config banner / no-retry timeout / auth-expired)
// — are driven through the hook with fake timers + a mocked feedApi.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useDeck } from '../components/feed/useDeck';
import { DeckCard } from '../components/feed/DeckCard';
import { UNDO_MS } from '../lib/algernon/feedConstants';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'email_tier:note/A.md',
    kind: 'email_tier',
    instance: 'salem',
    title: 'Email tier: a@b.com — Subject',
    mode: 'decide',
    attention: 'needs_you',
    evidence: { sender: 'a@b.com' },
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
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
  vi.useFakeTimers();
});
afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
});

describe('useDeck — delayed act', () => {
  it('affirm advances immediately but DEFERS the POST until the undo window expires', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items }));

    act(() => result.current.affirm());
    // Card advanced optimistically; POST NOT yet fired.
    expect(result.current.current?.id).toBe('b');
    expect(mockAct).not.toHaveBeenCalled();
    expect(result.current.toast?.canUndo).toBe(true);

    act(() => vi.advanceTimersByTime(UNDO_MS));
    // Timer expiry fires the deferred POST with the item's affirm action.
    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith('a', 'confirm');
  });

  it('a second commit FLUSHES the first POST immediately, in order', () => {
    const items = [item({ id: 'a' }), item({ id: 'b', kind: 'email_tier' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.affirm()); // defer a
    act(() => result.current.reject()); // commits b, flushing a first
    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith('a', 'confirm');
    act(() => vi.advanceTimersByTime(UNDO_MS)); // b's timer fires
    expect(mockAct).toHaveBeenCalledTimes(2);
    expect(mockAct).toHaveBeenNthCalledWith(2, 'b', 'spam');
  });

  it('undo CANCELS the deferred POST (never an un-act) and restores the card', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.affirm());
    expect(result.current.current?.id).toBe('b');
    act(() => result.current.undo());
    expect(result.current.current?.id).toBe('a'); // restored
    expect(result.current.toast).toBeNull();
    act(() => vi.advanceTimersByTime(UNDO_MS * 2));
    expect(mockAct).not.toHaveBeenCalled(); // POST never fired
  });
});

describe('useDeck — park (client-side defer, no POST)', () => {
  it('park never POSTs, counts, persists, and undo un-parks', () => {
    const parkPersist = vi.fn();
    const unparkPersist = vi.fn();
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() =>
      useDeck({ items, onParkPersist: parkPersist, onUnparkPersist: unparkPersist }),
    );
    act(() => result.current.park());
    expect(result.current.current?.id).toBe('b');
    expect(result.current.parkedCount).toBe(1);
    expect(parkPersist).toHaveBeenCalledWith('a');
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled(); // park is a pure client defer

    act(() => result.current.park()); // park b too
    act(() => result.current.undo()); // un-park b
    expect(result.current.parkedCount).toBe(1);
    expect(unparkPersist).toHaveBeenCalledWith('b');
  });
});

describe('useDeck — C2 slot: Accept POSTs, Skip = client park (rejectParks, no POST)', () => {
  const slotCandidate = (id: string) =>
    item({ id, kind: 'slot_suggestion', evidence: { tier: 1, origin: 'routine_item', routine_record: 'r/S.md', name: 'X', candidate: true } });

  it('affirm on a slot candidate DEFERS a POST of "accept"', () => {
    const { result } = renderHook(() => useDeck({ items: [slotCandidate('s')] }));
    act(() => result.current.affirm());
    expect(mockAct).not.toHaveBeenCalled(); // deferred
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('s', 'accept');
  });

  it('reject (LEFT = Skip) PARKS the candidate — no POST, counts + persists', () => {
    const parkPersist = vi.fn();
    const { result } = renderHook(() => useDeck({ items: [slotCandidate('s'), item({ id: 'b' })], onParkPersist: parkPersist }));
    act(() => result.current.reject()); // Skip
    expect(result.current.current?.id).toBe('b'); // advanced
    expect(result.current.parkedCount).toBe(1);
    expect(parkPersist).toHaveBeenCalledWith('s');
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled(); // ← reddens if skip POSTs instead of parking
  });
});

describe('useDeck — heavy two-step confirm', () => {
  it('heavy affirm reveals a confirm stage (no advance, no POST) until confirm-tap', () => {
    const items = [item({ id: 'p1', kind: 'proposal', title: 'New person' })];
    const { result } = renderHook(() => useDeck({ items }));

    act(() => result.current.affirm());
    // First affirm on a heavy card only reveals the confirm stage.
    expect(result.current.confirmingId).toBe('p1');
    expect(result.current.current?.id).toBe('p1'); // not advanced
    expect(mockAct).not.toHaveBeenCalled();

    act(() => result.current.confirmHeavy());
    expect(result.current.confirmingId).toBeNull();
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('p1', 'confirm');
  });

  it('cancelHeavy dismisses the confirm stage without acting', () => {
    const items = [item({ id: 'p1', kind: 'proposal' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.affirm());
    act(() => result.current.cancelHeavy());
    expect(result.current.confirmingId).toBeNull();
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled();
  });
});

describe('useDeck — a kind with no reject (pending) ignores reject', () => {
  it('reject is a no-op for pending (only "noted")', () => {
    const items = [item({ id: 'x', kind: 'pending', title: 'Pending' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.reject());
    expect(result.current.current?.id).toBe('x'); // not advanced
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled();
  });
});

describe('useDeck — error routing (card already dismissed → toast/banner)', () => {
  async function commitAndFlush(result: { current: ReturnType<typeof useDeck> }, err: unknown) {
    mockAct.mockRejectedValueOnce(err);
    act(() => result.current.affirm());
    await act(async () => {
      vi.advanceTimersByTime(UNDO_MS);
      await Promise.resolve();
    });
  }

  it('409 stale_item → a benign "moved on" toast', async () => {
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    await commitAndFlush(result, new ApiError(409, 'stale_item'));
    expect(result.current.toast?.message).toContain('moved on');
    expect(result.current.banner).toBeNull();
  });

  it('502 feed_upstream_unavailable → a fatal server-config banner (never a logout)', async () => {
    const authExpired = vi.fn();
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })], onAuthExpired: authExpired }));
    await commitAndFlush(result, new ApiError(502, 'feed_upstream_unavailable'));
    expect(result.current.banner).toContain('server-side');
    expect(authExpired).not.toHaveBeenCalled();
  });

  it('504 timeout → a "reconcile at next sync" toast, and NO retry', async () => {
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    await commitAndFlush(result, new ApiError(0, 'timeout'));
    expect(result.current.toast?.message).toContain('reconcile');
    expect(mockAct).toHaveBeenCalledTimes(1); // never resent
  });

  it('401 invalid_session → onAuthExpired (the ONLY 401 the feed path can mean)', async () => {
    const authExpired = vi.fn();
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })], onAuthExpired: authExpired }));
    await commitAndFlush(result, new ApiError(401, 'invalid_session'));
    expect(authExpired).toHaveBeenCalledTimes(1);
  });
});

describe('DeckCard — defensive render (untrusted evidence)', () => {
  it('renders a script-ish evidence value as TEXT, never as markup', () => {
    const evil = '<img src=x onerror=alert(1)>';
    render(
      <DeckCard
        item={item({ evidence: { subject: evil } })}
        depth={0}
        expanded
        confirming={false}
        onToggleEvidence={() => {}}
        onConfirmHeavy={() => {}}
        onCancelHeavy={() => {}}
      />,
    );
    // The raw string is present as text (React-escaped) and there is NO injected img.
    expect(screen.getByText(evil)).toBeTruthy();
    expect(document.querySelector('img')).toBeNull();
  });

  it('shows the heavy badge for a proposal', () => {
    render(
      <DeckCard
        item={item({ kind: 'proposal', title: 'New person' })}
        depth={0}
        expanded={false}
        confirming={false}
        onToggleEvidence={() => {}}
        onConfirmHeavy={() => {}}
        onCancelHeavy={() => {}}
      />,
    );
    expect(screen.getByText(/writes a record/i)).toBeTruthy();
  });

  function renderCard(overrides: Partial<Parameters<typeof item>[0]> = {}, expanded = false) {
    return render(
      <DeckCard
        item={item(overrides)}
        depth={0}
        expanded={expanded}
        confirming={false}
        onToggleEvidence={() => {}}
        onConfirmHeavy={() => {}}
        onCancelHeavy={() => {}}
      />,
    );
  }

  it('an email card shows the assigned TIER badge + a dynamic affirm label on the face', () => {
    renderCard({ evidence: { classifier_priority: 'high', sender: 'a@b.com' } });
    const badge = screen.getByTestId('deck-tier-badge');
    expect(badge.textContent?.toLowerCase()).toContain('high');
    expect(badge.className).toContain('text-danger'); // high → danger colour
    // Footer affirm verb is dynamic (no blind confirm).
    expect(screen.getByTestId('deck-card').textContent).toContain('Confirm HIGH');
  });

  it('an email card with NO recognised priority shows no badge and the plain Confirm verb', () => {
    renderCard({ evidence: { sender: 'a@b.com' } });
    expect(screen.queryByTestId('deck-tier-badge')).toBeNull();
    expect(screen.getByTestId('deck-card').textContent).toContain('Confirm →');
  });

  it('a SPAM-classified email shows the badge + "Confirm SPAM" (face honesty, operator ruling)', () => {
    renderCard({ evidence: { classifier_priority: 'spam', sender: 'a@b.com' } });
    expect(screen.getByTestId('deck-tier-badge').textContent?.toLowerCase()).toContain('spam');
    expect(screen.getByTestId('deck-card').textContent).toContain('Confirm SPAM');
  });

  it('renders an evidence.body as escaped prose (generic mechanism, deck too)', () => {
    const evil = '<img src=x onerror=alert(1)>';
    renderCard({ evidence: { body: `digest text\n${evil}`, truncated: true } }, true);
    const body = screen.getByTestId('evidence-body');
    expect(body.textContent).toContain('digest text');
    expect(body.textContent).toContain(evil); // present as TEXT
    expect(document.querySelector('img')).toBeNull(); // never as markup
    expect(screen.queryByTestId('evidence-truncated')).not.toBeNull();
  });

  it('evidence scrolls WITHIN the card — container carries overflow, verbs stay outside it', () => {
    renderCard({ evidence: { classifier_priority: 'low', sender: 'a@b.com', snippet: 'x'.repeat(600) } }, true);
    const evidence = screen.getByTestId('deck-evidence');
    expect(evidence.className).toContain('overflow-y-auto');
    expect(evidence.className).toContain('min-h-0');
    // The verb footer is a sibling OUTSIDE the scroll region (DOM-order containment).
    expect(evidence.textContent).not.toContain('Confirm');
    expect(screen.getByTestId('deck-card').textContent).toContain('Confirm');
  });

  it('a SUGGESTED slot card shows the tier badge + the WHY + a "Take it — T{n}" affirm + Skip', () => {
    renderCard({
      kind: 'slot_suggestion',
      title: 'Meditate',
      evidence: { tier: 3, origin: 'routine_item', routine_record: 'r/S.md', name: 'Meditate', surface_reason: 'self-care', candidate: true },
    });
    expect(screen.getByTestId('deck-slot-tier').textContent).toContain('T3');
    expect(screen.getByTestId('deck-slot-why').textContent).toContain('self-care');
    const face = screen.getByTestId('deck-card').textContent ?? '';
    expect(face).toContain('Take it — T3'); // tier-bearing affirm, not a blind swipe
    expect(face).toContain('Skip'); // LEFT is Skip (park), never a hard reject
  });
});
