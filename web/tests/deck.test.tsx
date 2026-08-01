import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';

// Pins the deck state machine (useDeck) + the card's defensive render. The
// intricate parts — DELAYED ACT (fire on timeout / flush-on-next / cancel-on-undo,
// never an un-act), the two-step HEAVY confirm, client-side PARK, and error
// routing (stale toast / server-config banner / no-retry timeout / auth-expired)
// — are driven through the hook with fake timers + a mocked feedApi.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useDeck } from '../components/feed/useDeck';
import { Deck } from '../components/feed/Deck';
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

describe('useDeck — parked retention + deal-now (task #26)', () => {
  it('park RETAINS the item (title + kind) for the drill-down; parkedCount = parked.length', () => {
    const items = [item({ id: 'a', title: 'Alpha' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.park());
    expect(result.current.parkedCount).toBe(1);
    expect(result.current.parked.map((p) => p.id)).toEqual(['a']);
    expect(result.current.parked[0].title).toBe('Alpha');
  });

  it('dealNow un-parks (persist) and re-enters the card into the queue immediately', () => {
    const unpark = vi.fn();
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items, onUnparkPersist: unpark }));
    act(() => result.current.park());   // park a → current b
    act(() => result.current.affirm()); // commit b → deck clears, a parked
    expect(result.current.cleared).toBe(true);
    expect(result.current.parkedCount).toBe(1);

    act(() => result.current.dealNow(result.current.parked[0])); // deal a back
    expect(unpark).toHaveBeenCalledWith('a');
    expect(result.current.parkedCount).toBe(0);
    expect(result.current.cleared).toBe(false);
    expect(result.current.current?.id).toBe('a'); // re-dealt, dealable now
  });

  it('dealNow appends at the TAIL mid-swipe — the cursor is undisturbed', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' }), item({ id: 'c' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.park()); // park a → current b
    expect(result.current.current?.id).toBe('b');
    act(() => result.current.dealNow(result.current.parked[0]));
    expect(result.current.current?.id).toBe('b'); // cursor NOT reset
    expect(result.current.parkedCount).toBe(0);
    // a re-entered at the tail: b → c → a
    act(() => result.current.affirm()); // b
    act(() => result.current.affirm()); // c (flushes b in order)
    expect(result.current.current?.id).toBe('a');
  });

  it('deal → re-park → deal-again works (a re-dealt card can be parked + dealt again)', () => {
    const items = [item({ id: 'a' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.park());                            // park a (cleared)
    act(() => result.current.dealNow(result.current.parked[0])); // deal a back → current a
    expect(result.current.current?.id).toBe('a');
    act(() => result.current.park());                            // re-park a
    expect(result.current.parkedCount).toBe(1);
    act(() => result.current.dealNow(result.current.parked[0])); // deal a AGAIN
    expect(result.current.parkedCount).toBe(0);
    expect(result.current.current?.id).toBe('a'); // reachable again, no stuck state
  });

  it('undo after a park removes it from the parked LIST (not just the count)', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.park());
    expect(result.current.parked.map((p) => p.id)).toEqual(['a']);
    act(() => result.current.undo());
    expect(result.current.parked).toEqual([]);
    expect(result.current.current?.id).toBe('a'); // restored to the deck
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

describe('Deck — parked drill-down (task #26 render)', () => {
  it('the Parked label is a BUTTON that opens the panel listing parked cards', () => {
    render(<Deck items={[item({ id: 'a', title: 'Alpha' }), item({ id: 'b', title: 'Bravo' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-park'))); // park a → current b
    const label = screen.getByTestId('deck-parked');
    expect(label.tagName).toBe('BUTTON'); // never a number you can't tap
    expect(label.textContent).toContain('view'); // advertises its own verb
    act(() => fireEvent.click(label));
    expect(screen.getByTestId('deck-parked-panel')).toBeTruthy();
    const rows = screen.getAllByTestId('deck-parked-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('Alpha');
  });

  it('deck-clear "View parked" opens the panel; Deal now re-deals + shows the empty ILB', () => {
    render(<Deck items={[item({ id: 'a', title: 'Alpha' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-park'))); // park a → deck clears
    expect(screen.getByTestId('deck-cleared')).toBeTruthy();
    act(() => fireEvent.click(screen.getByTestId('deck-cleared-view')));
    expect(screen.getByTestId('deck-parked-panel')).toBeTruthy();

    act(() => fireEvent.click(screen.getByTestId('deck-parked-deal'))); // deal a back
    expect(screen.getByTestId('deck-parked-empty')).toBeTruthy(); // ILB, not a blank panel
    expect(screen.queryByTestId('deck-cleared')).toBeNull(); // deck un-cleared

    act(() => fireEvent.click(screen.getByTestId('deck-parked-close')));
    expect(screen.queryByTestId('deck-parked-panel')).toBeNull();
    expect(screen.getByTestId('deck-card')).toBeTruthy(); // a is dealt again
  });

  it('Deal now removes just that card from the list; the others stay parked', () => {
    render(
      <Deck
        items={[item({ id: 'a', title: 'Alpha' }), item({ id: 'b', title: 'Bravo' }), item({ id: 'c', title: 'Charlie' })]}
      />,
    );
    act(() => fireEvent.click(screen.getByTestId('deck-btn-park'))); // park a → current b
    act(() => fireEvent.click(screen.getByTestId('deck-btn-park'))); // park b → current c
    act(() => fireEvent.click(screen.getByTestId('deck-parked'))); // open the panel
    expect(screen.getAllByTestId('deck-parked-row')).toHaveLength(2);

    act(() => fireEvent.click(screen.getAllByTestId('deck-parked-deal')[0])); // deal a
    const rows = screen.getAllByTestId('deck-parked-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('Bravo'); // b stays parked
  });

  it('panel open → an arrow key does NOT act on the underlying card', () => {
    render(<Deck items={[item({ id: 'a', title: 'Alpha' }), item({ id: 'b', title: 'Bravo' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-park'))); // park a → current b
    act(() => fireEvent.click(screen.getByTestId('deck-parked'))); // open the panel
    // An arrow key while the panel overlays the deck must be inert (the card is hidden).
    act(() => fireEvent.keyDown(screen.getByTestId('deck'), { key: 'ArrowRight' }));
    act(() => fireEvent.click(screen.getByTestId('deck-parked-close')));
    // b was NOT affirmed/advanced — still the current card, deck not cleared.
    expect(screen.queryByTestId('deck-cleared')).toBeNull();
    expect(screen.getByTestId('deck-card').textContent).toContain('Bravo');
  });
});

describe('useDeck — re-tier the current email card (#28)', () => {
  const emailItem = (over: Partial<FeedItem> = {}) =>
    item({ kind: 'email_tier', evidence: { classifier_priority: 'low', sender: 'a@b.com', subject: 'x' }, ...over });

  it('reTier posts the EXACT tier action_id and flips the card on acted', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'a', action_id: 'high', detail: '' });
    const { result } = renderHook(() => useDeck({ items: [emailItem({ id: 'a' }), item({ id: 'b' })] }));
    await act(async () => { await result.current.reTier('high'); });
    expect(mockAct).toHaveBeenCalledWith('a', 'high'); // the id VOCAB is the contract
    expect(result.current.current?.id).toBe('b'); // flip-on-acted: leaves the deck
  });

  it('does NOT advance until acted returns (no optimistic lie)', async () => {
    let resolveAct: (v: unknown) => void = () => undefined;
    mockAct.mockImplementation(() => new Promise((r) => { resolveAct = r; }));
    const { result } = renderHook(() => useDeck({ items: [emailItem({ id: 'a' }), item({ id: 'b' })] }));
    let pending: Promise<void> = Promise.resolve();
    await act(async () => {
      pending = result.current.reTier('high');
      await Promise.resolve();
      await Promise.resolve();
    });
    // In flight: pending signal set, card NOT advanced (nothing greens yet).
    expect(result.current.reTiering).toBe('high');
    expect(result.current.current?.id).toBe('a');
    await act(async () => {
      resolveAct({ ok: true, status: 'acted', id: 'a', action_id: 'high', detail: '' });
      await pending;
    });
    expect(result.current.reTiering).toBeNull();
    expect(result.current.current?.id).toBe('b'); // only NOW flipped
  });

  it('a non-acted status keeps the card (honest, retry possible)', async () => {
    mockAct.mockResolvedValue({ ok: false, status: 'invalid_action', id: 'a', action_id: 'high', detail: 'nope' });
    const { result } = renderHook(() => useDeck({ items: [emailItem({ id: 'a' }), item({ id: 'b' })] }));
    await act(async () => { await result.current.reTier('high'); });
    expect(result.current.current?.id).toBe('a'); // NOT advanced
    expect(result.current.toast?.message).toContain('nope');
  });

  it('an ApiError routes to the error handler and keeps the card', async () => {
    mockAct.mockRejectedValue(new ApiError(502, 'feed_upstream_unavailable'));
    const { result } = renderHook(() => useDeck({ items: [emailItem({ id: 'a' })] }));
    await act(async () => { await result.current.reTier('high'); });
    expect(result.current.current?.id).toBe('a'); // stays
    expect(result.current.banner).toContain('server-side');
  });
});

describe('Deck — re-tier picker (task #28 render)', () => {
  const emailItem = (over: Partial<FeedItem> = {}) =>
    item({ kind: 'email_tier', evidence: { classifier_priority: 'low', sender: 'a@b.com', subject: 'x' }, ...over });

  it('"Adjust tier…" opens the picker with the tiers OTHER than the assigned one', () => {
    render(<Deck items={[emailItem({ id: 'a', title: 'Email A' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-retier-open')));
    expect(screen.getByTestId('deck-retier-picker')).toBeTruthy();
    // Assigned LOW → offers high / medium / spam, NOT low (spam included: two honest doors).
    expect(screen.queryByTestId('deck-retier-choice-high')).not.toBeNull();
    expect(screen.queryByTestId('deck-retier-choice-medium')).not.toBeNull();
    expect(screen.queryByTestId('deck-retier-choice-spam')).not.toBeNull();
    expect(screen.queryByTestId('deck-retier-choice-low')).toBeNull(); // assigned excluded
  });

  it('picking a tier posts its exact action_id, closes the picker + shows an honest toast', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'a', action_id: 'high', detail: '' });
    render(<Deck items={[emailItem({ id: 'a', title: 'Email A' }), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-retier-open')));
    await act(async () => {
      fireEvent.click(screen.getByTestId('deck-retier-choice-high'));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockAct).toHaveBeenCalledWith('a', 'high');
    expect(screen.queryByTestId('deck-retier-picker')).toBeNull(); // closed once the act resolved
    expect(screen.getByTestId('deck-toast').textContent).toContain('Re-tiered to HIGH');
  });

  it('the "Adjust tier…" affordance is email_tier only (absent on other kinds)', () => {
    render(<Deck items={[item({ id: 'p', kind: 'proposal', title: 'New person' })]} />);
    expect(screen.queryByTestId('deck-retier-open')).toBeNull();
  });

  it('picker open → an arrow key does NOT act on the hidden card (input gated)', () => {
    render(<Deck items={[emailItem({ id: 'a', title: 'Email A' }), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-retier-open')));
    act(() => fireEvent.keyDown(screen.getByTestId('deck'), { key: 'ArrowRight' }));
    act(() => fireEvent.click(screen.getByTestId('deck-retier-cancel')));
    // a was NOT affirmed/advanced — still the top card.
    expect(screen.getAllByTestId('deck-card')[0].textContent).toContain('Email A');
    expect(mockAct).not.toHaveBeenCalled(); // no act fired from the gated arrow
  });

  // The iOS dead-tap fix: the tap DID fire (picker opened), but the overlay opened at
  // z-20 BEHIND the z-100 top card → invisible → read as unresponsive. Pin the fix in the
  // closest jsdom-testable form: the picker's z-index sits ABOVE the top card's.
  it('the picker opens ABOVE the top card, not hidden behind it (#28 tap fix)', () => {
    render(<Deck items={[emailItem({ id: 'a', title: 'Email A' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-retier-open')));
    const picker = screen.getByTestId('deck-retier-picker');
    const topCard = screen.getAllByTestId('deck-card')[0];
    expect(Number(picker.style.zIndex)).toBeGreaterThan(Number(topCard.style.zIndex));
  });

  it('the parked panel also opens ABOVE the top card mid-deck (same latent z bug, closed)', () => {
    render(<Deck items={[emailItem({ id: 'a', title: 'Email A' }), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-park'))); // park a → current b, panel available
    act(() => fireEvent.click(screen.getByTestId('deck-parked'))); // open the panel WITH a card present
    const panel = screen.getByTestId('deck-parked-panel');
    const topCard = screen.getAllByTestId('deck-card')[0];
    expect(Number(panel.style.zIndex)).toBeGreaterThan(Number(topCard.style.zIndex));
  });
});
