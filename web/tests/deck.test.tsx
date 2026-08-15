import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';

// Pins the deck state machine (useDeck) + the card's defensive render. The
// intricate parts — DELAYED ACT (fire on timeout / flush-on-next / cancel-on-undo,
// never an un-act), the two-step HEAVY confirm, client-side SNOOZE, and error
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
import { withServedActions } from './helpers/servedActions';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
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
  });
}

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
  // The unrecorded-verdict ledger is PERSISTENT by design (it has to outlive the
  // deck's own unmount), so a test that refuses an act leaves a real entry
  // behind and the next `renderHook` hydrates from it. That is the feature in
  // production and contamination here — same shape as the dispatcher env-var
  // bleed rule. Any test file that can produce a refusal must clear it.
  window.localStorage.clear();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  window.localStorage.clear();
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

describe('useDeck — snooze (client-side defer, no POST)', () => {
  it('snooze never POSTs, counts, persists, and undo un-snoozes', () => {
    const snoozePersist = vi.fn();
    const unsnoozePersist = vi.fn();
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() =>
      useDeck({ items, onSnoozePersist: snoozePersist, onUnsnoozePersist: unsnoozePersist }),
    );
    act(() => result.current.snooze());
    expect(result.current.current?.id).toBe('b');
    expect(result.current.snoozedCount).toBe(1);
    expect(snoozePersist).toHaveBeenCalledWith('a');
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled(); // snooze is a pure client defer

    act(() => result.current.snooze()); // snooze b too
    act(() => result.current.undo()); // un-snooze b
    expect(result.current.snoozedCount).toBe(1);
    expect(unsnoozePersist).toHaveBeenCalledWith('b');
  });
});

describe('useDeck — snoozed retention + deal-now (task #26)', () => {
  it('snooze RETAINS the item (title + kind) for the drill-down; snoozedCount = snoozed.length', () => {
    const items = [item({ id: 'a', title: 'Alpha' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.snooze());
    expect(result.current.snoozedCount).toBe(1);
    expect(result.current.snoozed.map((p) => p.id)).toEqual(['a']);
    expect(result.current.snoozed[0].title).toBe('Alpha');
  });

  it('dealNow un-snoozes (persist) and re-enters the card into the queue immediately', () => {
    const unsnooze = vi.fn();
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items, onUnsnoozePersist: unsnooze }));
    act(() => result.current.snooze());   // snooze a → current b
    act(() => result.current.affirm()); // commit b → deck clears, a snoozed
    expect(result.current.cleared).toBe(true);
    expect(result.current.snoozedCount).toBe(1);

    act(() => result.current.dealNow(result.current.snoozed[0])); // deal a back
    expect(unsnooze).toHaveBeenCalledWith('a');
    expect(result.current.snoozedCount).toBe(0);
    expect(result.current.cleared).toBe(false);
    expect(result.current.current?.id).toBe('a'); // re-dealt, dealable now
  });

  it('dealNow appends at the TAIL mid-swipe — the cursor is undisturbed', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' }), item({ id: 'c' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.snooze()); // snooze a → current b
    expect(result.current.current?.id).toBe('b');
    act(() => result.current.dealNow(result.current.snoozed[0]));
    expect(result.current.current?.id).toBe('b'); // cursor NOT reset
    expect(result.current.snoozedCount).toBe(0);
    // a re-entered at the tail: b → c → a
    act(() => result.current.affirm()); // b
    act(() => result.current.affirm()); // c (flushes b in order)
    expect(result.current.current?.id).toBe('a');
  });

  it('deal → re-snooze → deal-again works (a re-dealt card can be snoozed + dealt again)', () => {
    const items = [item({ id: 'a' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.snooze());                            // snooze a (cleared)
    act(() => result.current.dealNow(result.current.snoozed[0])); // deal a back → current a
    expect(result.current.current?.id).toBe('a');
    act(() => result.current.snooze());                            // re-snooze a
    expect(result.current.snoozedCount).toBe(1);
    act(() => result.current.dealNow(result.current.snoozed[0])); // deal a AGAIN
    expect(result.current.snoozedCount).toBe(0);
    expect(result.current.current?.id).toBe('a'); // reachable again, no stuck state
  });

  it('undo after a snooze removes it from the snoozed LIST (not just the count)', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    const { result } = renderHook(() => useDeck({ items }));
    act(() => result.current.snooze());
    expect(result.current.snoozed.map((p) => p.id)).toEqual(['a']);
    act(() => result.current.undo());
    expect(result.current.snoozed).toEqual([]);
    expect(result.current.current?.id).toBe('a'); // restored to the deck
  });
});

describe('useDeck — C2 slot: Accept POSTs, Skip sets aside client-side (rejectDefers, no POST)', () => {
  const slotCandidate = (id: string) =>
    item({ id, kind: 'slot_suggestion', evidence: { tier: 1, origin: 'routine_item', routine_record: 'r/S.md', name: 'X', candidate: true } });

  it('affirm on a slot candidate DEFERS a POST of "accept"', () => {
    const { result } = renderHook(() => useDeck({ items: [slotCandidate('s')] }));
    act(() => result.current.affirm());
    expect(mockAct).not.toHaveBeenCalled(); // deferred
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('s', 'accept');
  });

  it('reject (LEFT = Skip) SETS THE CANDIDATE ASIDE — no POST, counts + persists', () => {
    const snoozePersist = vi.fn();
    const { result } = renderHook(() => useDeck({ items: [slotCandidate('s'), item({ id: 'b' })], onSnoozePersist: snoozePersist }));
    act(() => result.current.reject()); // Skip
    expect(result.current.current?.id).toBe('b'); // advanced
    expect(result.current.snoozedCount).toBe(1);
    expect(snoozePersist).toHaveBeenCalledWith('s');
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled(); // ← reddens if Skip POSTs a decline
  });

  it('Skip and Snooze produce DIFFERENT toasts — one control never means both', () => {
    // The #14 ruling: Skip is "not this one", Snooze is "yes, but later". They
    // share the session-local set-aside mechanism, so the words are the only
    // thing telling the operator which verb they just used.
    //
    // Mutation: route Skip back through commit('snooze', …) → both toasts read
    // "Snoozed — …" and this fails on the second assert.
    const skipped = renderHook(() => useDeck({ items: [slotCandidate('s')] }));
    act(() => skipped.result.current.reject());
    expect(skipped.result.current.toast?.message).toContain('Skipped');

    const dozed = renderHook(() => useDeck({ items: [slotCandidate('s2')] }));
    act(() => dozed.result.current.snooze());
    expect(dozed.result.current.toast?.message).toContain('Snoozed');
    expect(dozed.result.current.toast?.message).not.toContain('Skipped');
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

  it('409 stale_item → the card COMES BACK, and no toast speaks for it', async () => {
    // This pin used to assert the opposite — a "moved on" toast and nothing else
    // — and that was the 2026-08-15 P1: the deferred act's failure arrived while
    // a LATER card was on screen, so the one line it produced named no card and
    // was replaced by the next. A refusal is an answer; the verdict is
    // definitely unrecorded; the card returns. Full behaviour in
    // `deckActHonesty.test.tsx`; this is the routing half.
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    await commitAndFlush(result, new ApiError(409, 'stale_item'));
    expect(result.current.current?.id).toBe('a');
    expect(result.current.unrecorded.map((u) => u.id)).toEqual(['a']);
    expect(result.current.toast).toBeNull();
    expect(result.current.banner).toBeNull();
  });

  it('the "moved on" toast is still LIVE on the immediate paths (dealNow)', async () => {
    // The copy did not die with the pin above; it moved to where a toast is
    // attributable — a caller whose card is in front of it. dealNow's un-snooze
    // is that caller, and a pin that only deleted the old assertion would have
    // left this branch unguarded.
    const { result } = renderHook(() => useDeck({ items: [slotItem()] }));
    act(() => result.current.snooze('snooze_1d'));
    act(() => vi.advanceTimersByTime(UNDO_MS)); // the snooze POST lands
    mockAct.mockRejectedValueOnce(new ApiError(409, 'stale_item'));
    await act(async () => {
      result.current.dealNow(result.current.snoozed[0]);
      await Promise.resolve();
    });
    expect(result.current.toast?.message).toContain('moved on');
    expect(result.current.unrecorded).toHaveLength(0); // not a swiped verdict
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

  it('badges the heavy DIRECTION, not the card (a proposal: its Confirm)', () => {
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
    // Was "Heavy · writes a record" for the whole kind. The badge now names the
    // verb that is heavy, because on an attribution card only one of the two is.
    expect(screen.getByTestId('deck-heavy-affirm').textContent).toContain('Confirm');
    expect(screen.queryByTestId('deck-heavy-reject')).toBeNull();
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
    // HIGH is drawn in the alert role. (Was `text-danger` under the honeydew
    // palette; the console identity spends the same meaning as `negative`.)
    expect(badge.className).toContain('text-negative');
    // Footer affirm verb is dynamic (no blind confirm).
    expect(screen.getByTestId('deck-card').textContent).toContain('Confirm HIGH');
  });

  it('draws the tiers apart — only HIGH gets the alert role, and SPAM is quietest', () => {
    // The positive/negative control the assertion above needs to mean anything:
    // "HIGH is red" is equally true of a card that painted every tier red. What
    // the operator relies on is the DIFFERENCE between tiers at a glance.
    const classFor = (tier: string) => {
      const { unmount } = renderCard({ evidence: { classifier_priority: tier, sender: 'a@b.com' } });
      const cls = screen.getByTestId('deck-tier-badge').className;
      unmount();
      return cls;
    };
    const high = classFor('high');
    const medium = classFor('medium');
    const low = classFor('low');
    const spam = classFor('spam');

    expect(high).toContain('text-negative');
    // Nothing else claims the alert role.
    for (const other of [medium, low, spam]) {
      expect(other).not.toContain('text-negative');
    }
    // And the four are genuinely four treatments, not one repeated.
    expect(new Set([high, medium, low, spam]).size).toBe(4);
    // The standing ruling, carried across the repalette: spam is junk, not
    // urgency, so it must never be drawn like the urgent tier.
    expect(spam).not.toBe(high);
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
    expect(face).toContain('Skip'); // LEFT is Skip (a set-aside), never a hard reject
  });
});

describe('Deck — snoozed drill-down (task #26 render)', () => {
  it('the Snoozed label is a BUTTON that opens the panel listing snoozed cards', () => {
    render(<Deck items={[item({ id: 'a', title: 'Alpha' }), item({ id: 'b', title: 'Bravo' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze'))); // snooze a → current b
    const label = screen.getByTestId('deck-snoozed');
    expect(label.tagName).toBe('BUTTON'); // never a number you can't tap
    expect(label.textContent).toContain('view'); // advertises its own verb
    act(() => fireEvent.click(label));
    expect(screen.getByTestId('deck-snoozed-panel')).toBeTruthy();
    const rows = screen.getAllByTestId('deck-snoozed-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('Alpha');
  });

  it('deck-clear "View snoozed" opens the panel; Deal now re-deals + shows the empty ILB', () => {
    render(<Deck items={[item({ id: 'a', title: 'Alpha' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze'))); // snooze a → deck clears
    expect(screen.getByTestId('deck-cleared')).toBeTruthy();
    act(() => fireEvent.click(screen.getByTestId('deck-cleared-view')));
    expect(screen.getByTestId('deck-snoozed-panel')).toBeTruthy();

    act(() => fireEvent.click(screen.getByTestId('deck-snoozed-deal'))); // deal a back
    expect(screen.getByTestId('deck-snoozed-empty')).toBeTruthy(); // ILB, not a blank panel
    expect(screen.queryByTestId('deck-cleared')).toBeNull(); // deck un-cleared

    act(() => fireEvent.click(screen.getByTestId('deck-snoozed-close')));
    expect(screen.queryByTestId('deck-snoozed-panel')).toBeNull();
    expect(screen.getByTestId('deck-card')).toBeTruthy(); // a is dealt again
  });

  it('Deal now removes just that card from the list; the others stay snoozed', () => {
    render(
      <Deck
        items={[item({ id: 'a', title: 'Alpha' }), item({ id: 'b', title: 'Bravo' }), item({ id: 'c', title: 'Charlie' })]}
      />,
    );
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze'))); // snooze a → current b
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze'))); // snooze b → current c
    act(() => fireEvent.click(screen.getByTestId('deck-snoozed'))); // open the panel
    expect(screen.getAllByTestId('deck-snoozed-row')).toHaveLength(2);

    act(() => fireEvent.click(screen.getAllByTestId('deck-snoozed-deal')[0])); // deal a
    const rows = screen.getAllByTestId('deck-snoozed-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('Bravo'); // b stays snoozed
  });

  it('panel open → an arrow key does NOT act on the underlying card', () => {
    render(<Deck items={[item({ id: 'a', title: 'Alpha' }), item({ id: 'b', title: 'Bravo' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze'))); // snooze a → current b
    act(() => fireEvent.click(screen.getByTestId('deck-snoozed'))); // open the panel
    // An arrow key while the panel overlays the deck must be inert (the card is hidden).
    act(() => fireEvent.keyDown(screen.getByTestId('deck'), { key: 'ArrowRight' }));
    act(() => fireEvent.click(screen.getByTestId('deck-snoozed-close')));
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

  it('the snoozed panel also opens ABOVE the top card mid-deck (same latent z bug, closed)', () => {
    render(<Deck items={[emailItem({ id: 'a', title: 'Email A' }), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze'))); // snooze a → current b, panel available
    act(() => fireEvent.click(screen.getByTestId('deck-snoozed'))); // open the panel WITH a card present
    const panel = screen.getByTestId('deck-snoozed-panel');
    const topCard = screen.getAllByTestId('deck-card')[0];
    expect(Number(panel.style.zIndex)).toBeGreaterThan(Number(topCard.style.zIndex));
  });
});

describe('useDeck — email_urgent ack wiring (#27)', () => {
  const urgent = (over: Partial<FeedItem> = {}) =>
    item({ id: 'u', kind: 'email_urgent', evidence: { sender: 'a@b.com', subject: 's', classifier_priority: 'high', high_source: 'llm' }, ...over });

  it('affirm on an urgent card DEFERS a POST of "ack" (deals + acks via the generic flow)', () => {
    const { result } = renderHook(() => useDeck({ items: [urgent()] }));
    act(() => result.current.affirm());
    expect(result.current.current).toBeNull(); // advanced off the deck (optimistic)
    expect(mockAct).not.toHaveBeenCalled(); // deferred
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('u', 'ack'); // flip-on-acted rides the standard deck flow
  });

  it('reject is a no-op on urgent (ACK-only — re-tier lives on the calibration card)', () => {
    const { result } = renderHook(() => useDeck({ items: [urgent(), item({ id: 'b' })] }));
    act(() => result.current.reject());
    expect(result.current.current?.id).toBe('u'); // not advanced — no reject door
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled();
  });
});

describe('Deck — email_urgent interrupt card (#27 render)', () => {
  const urgentEvidence = (high_source: string) => ({ sender: 'a@b.com', subject: 'prod down', classifier_priority: 'high', high_source });
  const urgent = (over: Partial<FeedItem> = {}) =>
    item({ kind: 'email_urgent', title: 'a@b.com — prod down', evidence: urgentEvidence('override'), ...over });

  it('shows the interrupt "Needs you" badge, NOT the calibration tier badge or the re-tier verb', () => {
    render(<Deck items={[urgent()]} />);
    expect(screen.getByTestId('deck-urgent-badge')).toBeTruthy();
    expect(screen.queryByTestId('deck-tier-badge')).toBeNull(); // priority badge is email_tier-only
    expect(screen.queryByTestId('deck-retier-open')).toBeNull(); // no re-tier on urgent (two cards, two jobs)
  });

  it('the high_source chip is honest provenance — override → "Priority sender"', () => {
    render(<Deck items={[urgent({ evidence: urgentEvidence('override') })]} />);
    expect(screen.getByTestId('deck-urgent-why').textContent).toContain('Priority sender');
  });

  it('the high_source chip — llm → "Classifier: high"', () => {
    render(<Deck items={[urgent({ evidence: urgentEvidence('llm') })]} />);
    expect(screen.getByTestId('deck-urgent-why').textContent).toContain('Classifier: high');
  });

  it('ACK-only verbs: the ✓ (ack) is enabled, the left/reject door is disabled', () => {
    render(<Deck items={[urgent()]} />);
    expect((screen.getByTestId('deck-btn-affirm') as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByTestId('deck-btn-reject') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('deck-card').textContent).toContain('Got it');
  });

  it('reuses the evidence path — an expanded urgent card renders the "Open in Gmail" anchor', () => {
    render(
      <Deck
        items={[
          urgent({
            evidence: { ...urgentEvidence('llm'), body: 'the email body', truncated: false, gmail_url: 'https://mail.google.com/mail/u/0/#search/rfc822msgid:x' },
          }),
        ]}
      />,
    );
    act(() => fireEvent.click(screen.getByTestId('deck-evidence-toggle')));
    expect(screen.getByTestId('evidence-external-link').getAttribute('href')).toContain('mail.google.com');
  });
});

describe('useDeck — reject-with-correction (#13)', () => {
  const routineItem = (over: Partial<FeedItem> = {}) =>
    item({
      id: 'r',
      kind: 'routine_match',
      title: 'Routine match: clean hammer → Clean house',
      evidence: {
        query: 'clean hammer',
        matched_to: 'Clean house',
        record: 'Weekly',
        candidates: [
          { text: 'Clean house', record: 'Weekly' },
          { text: 'Tidy the workshop', record: 'Weekly' },
          { text: 'Walk the dog', record: 'Daily' },
        ],
      },
      ...over,
    });

  it('a picked item posts `correct` WITH the target and flips on acted', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'r', action_id: 'correct', detail: '' });
    const { result } = renderHook(() => useDeck({ items: [routineItem(), item({ id: 'b' })] }));
    await act(async () => { await result.current.correctRoutine('Tidy the workshop'); });
    // The three-arg shape IS the contract — a two-arg call would reach the server
    // as a targetless `correct` and be refused.
    expect(mockAct).toHaveBeenCalledWith('r', 'correct', 'Tidy the workshop');
    expect(result.current.current?.id).toBe('b');
    expect(result.current.toast?.message).toContain('Tidy the workshop');
  });

  it('the one-off door posts `one_off` with NO target', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'r', action_id: 'one_off', detail: '' });
    const { result } = renderHook(() => useDeck({ items: [routineItem(), item({ id: 'b' })] }));
    await act(async () => { await result.current.correctRoutine(null); });
    expect(mockAct).toHaveBeenCalledWith('r', 'one_off', undefined);
    expect(result.current.current?.id).toBe('b');
    expect(result.current.toast?.message).toContain('one-off');
  });

  it('does NOT advance until acted returns — only the server knows the pick is valid', async () => {
    let resolveAct: (v: unknown) => void = () => undefined;
    mockAct.mockImplementation(() => new Promise((r) => { resolveAct = r; }));
    const { result } = renderHook(() => useDeck({ items: [routineItem(), item({ id: 'b' })] }));
    let pending: Promise<void> = Promise.resolve();
    await act(async () => {
      pending = result.current.correctRoutine('Tidy the workshop');
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.correcting).toBe('Tidy the workshop');
    expect(result.current.current?.id).toBe('r');
    await act(async () => {
      resolveAct({ ok: true, status: 'acted', id: 'r', action_id: 'correct', detail: '' });
      await pending;
    });
    expect(result.current.correcting).toBeNull();
    expect(result.current.current?.id).toBe('b');
  });

  it('a refused correction KEEPS the card and shows the server’s own reason', async () => {
    mockAct.mockResolvedValue({
      ok: false, status: 'error', id: 'r', action_id: 'correct',
      detail: '“Polish the DeLorean” isn’t an item on any active routine',
    });
    const { result } = renderHook(() => useDeck({ items: [routineItem(), item({ id: 'b' })] }));
    await act(async () => { await result.current.correctRoutine('Polish the DeLorean'); });
    expect(result.current.current?.id).toBe('r'); // NOT advanced — nothing was taught
    expect(result.current.toast?.message).toContain('active routine');
  });

  it('flushes a deferred swipe act BEFORE the correction (ordering)', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'x', action_id: 'y', detail: '' });
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' }), routineItem()] }));
    act(() => result.current.affirm()); // defer a's POST
    expect(mockAct).not.toHaveBeenCalled();
    await act(async () => { await result.current.correctRoutine(null); });
    expect(mockAct).toHaveBeenNthCalledWith(1, 'a', 'confirm');
    expect(mockAct).toHaveBeenNthCalledWith(2, 'r', 'one_off', undefined);
  });

  it('is inert on a non-routine card (the verbs belong to one kind)', async () => {
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    await act(async () => { await result.current.correctRoutine('Whatever'); });
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('an empty pick never reaches the server (it would only be refused)', async () => {
    const { result } = renderHook(() => useDeck({ items: [routineItem()] }));
    await act(async () => { await result.current.correctRoutine('   '); });
    expect(mockAct).not.toHaveBeenCalled();
  });
});

describe('Deck — correction picker (task #13 render)', () => {
  const routineItem = (over: Partial<FeedItem> = {}) =>
    item({
      id: 'r',
      kind: 'routine_match',
      title: 'Routine match: clean hammer → Clean house',
      evidence: {
        query: 'clean hammer',
        matched_to: 'Clean house',
        record: 'Weekly',
        candidates: [
          { text: 'Clean house', record: 'Weekly' },
          { text: 'Tidy the workshop', record: 'Weekly' },
          { text: 'Walk the dog', record: 'Daily' },
        ],
      },
      ...over,
    });

  it('"What did this mean?" opens the picker with every item EXCEPT the proposal', () => {
    render(<Deck items={[routineItem()]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    expect(screen.getByTestId('deck-correct-picker')).toBeTruthy();
    const labels = screen.getAllByTestId('deck-correct-choice').map((b) => b.getAttribute('data-item'));
    // "Clean house" is what the card proposed — offering it would be a door that
    // can only fail (the server refuses target === proposal).
    expect(labels).toEqual(['Tidy the workshop', 'Walk the dog']);
    expect(screen.getByTestId('deck-correct-one-off')).toBeTruthy();
  });

  it('picking an item posts correct+target, closes the picker, toasts honestly', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'r', action_id: 'correct', detail: '' });
    render(<Deck items={[routineItem(), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    await act(async () => {
      fireEvent.click(screen.getAllByTestId('deck-correct-choice')[0]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockAct).toHaveBeenCalledWith('r', 'correct', 'Tidy the workshop');
    expect(screen.queryByTestId('deck-correct-picker')).toBeNull();
    expect(screen.getByTestId('deck-toast').textContent).toContain('Tidy the workshop');
  });

  it('the one-off door posts one_off and needs no pick', async () => {
    mockAct.mockResolvedValue({ ok: true, status: 'acted', id: 'r', action_id: 'one_off', detail: '' });
    render(<Deck items={[routineItem(), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    await act(async () => {
      fireEvent.click(screen.getByTestId('deck-correct-one-off'));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockAct).toHaveBeenCalledWith('r', 'one_off', undefined);
    expect(screen.getByTestId('deck-toast').textContent).toContain('one-off');
  });

  it('no candidates → an explicit ILB line, and the one-off door still works', () => {
    render(<Deck items={[routineItem({ evidence: { query: 'q', matched_to: 'Clean house', record: 'Weekly' } })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    // An empty picker with no explanation is indistinguishable from a broken one.
    expect(screen.getByTestId('deck-correct-empty')).toBeTruthy();
    expect(screen.queryAllByTestId('deck-correct-choice')).toHaveLength(0);
    expect(screen.getByTestId('deck-correct-one-off')).toBeTruthy();
  });

  it('the affordance is routine_match only (absent on other kinds)', () => {
    render(<Deck items={[item({ id: 'a', kind: 'email_tier' })]} />);
    expect(screen.queryByTestId('deck-correct-open')).toBeNull();
  });

  it('picker open → an arrow key does NOT act on the hidden card (input gated)', () => {
    render(<Deck items={[routineItem(), item({ id: 'b', title: 'Bee' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    act(() => fireEvent.keyDown(screen.getByTestId('deck'), { key: 'ArrowRight' }));
    act(() => fireEvent.click(screen.getByTestId('deck-correct-cancel')));
    expect(screen.getAllByTestId('deck-card')[0].textContent).toContain('clean hammer');
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('the picker opens ABOVE the top card, not behind it (the #28 dead-tap class)', () => {
    render(<Deck items={[routineItem()]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    const picker = screen.getByTestId('deck-correct-picker');
    const topCard = screen.getAllByTestId('deck-card')[0];
    expect(Number(picker.style.zIndex)).toBeGreaterThan(Number(topCard.style.zIndex));
  });

  it('a malformed candidates payload renders no choices rather than blank buttons', () => {
    render(<Deck items={[routineItem({
      evidence: {
        query: 'q', matched_to: 'Clean house', record: 'Weekly',
        candidates: [{ record: 'Weekly' }, 'nope', null, { text: '   ' }],
      },
    })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-correct-open')));
    expect(screen.queryAllByTestId('deck-correct-choice')).toHaveLength(0);
    expect(screen.getByTestId('deck-correct-empty')).toBeTruthy();
  });
});

// --- #14: one defer verb, with a duration ladder behind it -------------------
// The ruling: ↑ is Snooze; a full swipe is a quick defer recorded as
// "until I say"; a partial ↑ HELD in the stamp band opens 1d / 3d / 7d / until
// I say. Durations only exist where the backend can store them, so the pins
// come in pairs — the capable kind and a kind that has no snooze verb at all.

const slotItem = (over: Partial<FeedItem> = {}) =>
  item({
    id: 'slot_suggestion:task:task/Pay Steph.md',
    kind: 'slot_suggestion',
    title: 'T1: Pay Steph',
    evidence: { tier: 1, origin: 'task', path: 'task/Pay Steph.md', name: 'Pay Steph', candidate: true },
    ...over,
  });

describe('useDeck — snooze POSTs a real act on a snooze-capable kind (#14)', () => {
  it('a bare snooze() records the INDEFINITE rung (the old Park), deferred + undoable', () => {
    const { result } = renderHook(() => useDeck({ items: [slotItem()] }));
    act(() => result.current.snooze());
    expect(mockAct).not.toHaveBeenCalled(); // still inside the undo window
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('slot_suggestion:task:task/Pay Steph.md', 'snooze_until_i_say');
  });

  it('a chosen duration POSTs that rung', () => {
    const { result } = renderHook(() => useDeck({ items: [slotItem()] }));
    act(() => result.current.snooze('snooze_3d'));
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('slot_suggestion:task:task/Pay Steph.md', 'snooze_3d');
  });

  it('undo CANCELS the snooze POST — a defer taken back was never a defer', () => {
    const { result } = renderHook(() => useDeck({ items: [slotItem(), item({ id: 'b' })] }));
    act(() => result.current.snooze('snooze_7d'));
    act(() => result.current.undo());
    act(() => vi.advanceTimersByTime(UNDO_MS * 2));
    expect(mockAct).not.toHaveBeenCalled();
    expect(result.current.snoozedCount).toBe(0);
  });

  it('a kind with NO backend snooze verb still defers — but POSTs nothing', () => {
    // The honest half of the per-kind design: the gesture works everywhere, the
    // PERSISTENCE is only claimed where a store exists.
    //
    // Mutation: make snoozeActionFor return the action unconditionally → this
    // fails, and the field gets a 400 on every email card the operator flicks up.
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'e', kind: 'email_tier' })] }));
    act(() => result.current.snooze('snooze_3d'));
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled();
    expect(result.current.snoozedCount).toBe(1); // deferred all the same
  });
});

describe('useDeck — dealing a snoozed card back un-snoozes it (#14)', () => {
  it('dealNow POSTs unsnooze once the snooze has already gone out', () => {
    // Otherwise the card returns to THIS session's deck while the store still
    // says snoozed, and the next sync hides it again — the divergence the
    // delayed-act design avoids everywhere else.
    const { result } = renderHook(() => useDeck({ items: [slotItem()] }));
    act(() => result.current.snooze('snooze_1d'));
    act(() => vi.advanceTimersByTime(UNDO_MS)); // the snooze POST lands
    expect(mockAct).toHaveBeenCalledTimes(1);

    act(() => result.current.dealNow(result.current.snoozed[0]));
    expect(mockAct).toHaveBeenCalledTimes(2);
    expect(mockAct).toHaveBeenNthCalledWith(2, 'slot_suggestion:task:task/Pay Steph.md', 'unsnooze');
  });

  it('dealNow inside the undo window CANCELS instead of POSTing an unsnooze', () => {
    // Nothing has been written yet, so there is nothing to un-write. POSTing
    // anyway would ask the server to undo a row it has never heard of.
    const { result } = renderHook(() => useDeck({ items: [slotItem()] }));
    act(() => result.current.snooze('snooze_1d'));
    act(() => result.current.dealNow(result.current.snoozed[0]));
    act(() => vi.advanceTimersByTime(UNDO_MS * 2));
    expect(mockAct).not.toHaveBeenCalled();
    expect(result.current.current?.id).toBe('slot_suggestion:task:task/Pay Steph.md');
  });

  it('dealNow on an unbacked kind POSTs nothing (there was no act to reverse)', () => {
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'e', kind: 'email_tier' })] }));
    act(() => result.current.snooze());
    act(() => vi.advanceTimersByTime(UNDO_MS));
    act(() => result.current.dealNow(result.current.snoozed[0]));
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled();
  });
});

describe('Deck — the snooze duration menu (#14 render)', () => {
  it('the ↑ button on a snooze-capable card opens the ladder, all four rungs labelled', () => {
    render(<Deck items={[slotItem()]} />);
    expect(screen.queryByTestId('deck-snooze-menu')).toBeNull();
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));

    const menu = screen.getByTestId('deck-snooze-menu');
    expect(menu.getAttribute('role')).toBe('dialog');
    expect(menu.textContent).toContain('1 day');
    expect(menu.textContent).toContain('3 days');
    expect(menu.textContent).toContain('7 days');
    expect(menu.textContent).toContain('Until I say');
    // The card is still there — opening a menu is not a verdict.
    expect(screen.getByTestId('deck-card')).toBeTruthy();
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('picking a rung closes the menu and commits THAT duration', () => {
    render(<Deck items={[slotItem(), item({ id: 'b', title: 'Bravo' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
    act(() => fireEvent.click(screen.getByTestId('deck-snooze-choice-snooze_7d')));
    expect(screen.queryByTestId('deck-snooze-menu')).toBeNull();
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).toHaveBeenCalledWith('slot_suggestion:task:task/Pay Steph.md', 'snooze_7d');
  });

  it('Cancel closes the menu, keeps the card, and springs the frozen transform back', () => {
    // The card freezes at the held offset while the menu is open (the menu reads
    // as attached to the gesture). Dismissing must RELEASE it — nothing else
    // clears the inline transform, because the drag listeners are gone by then.
    render(<Deck items={[slotItem()]} />);
    const card = screen.getByTestId('deck-card');
    card.style.transform = 'translate(4px, -60px) rotate(0.2deg)'; // as a held drag leaves it
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
    act(() => fireEvent.click(screen.getByTestId('deck-snooze-cancel')));

    expect(screen.queryByTestId('deck-snooze-menu')).toBeNull();
    expect(screen.getByTestId('deck-card')).toBeTruthy();
    expect(card.style.transform).toBe('');
    expect(mockAct).not.toHaveBeenCalled(); // cancelling defers nothing
  });

  it('an open menu BLOCKS the keyboard on the card underneath', () => {
    // Every overlay must gate input or an arrow key acts on a hidden card.
    render(<Deck items={[slotItem(), item({ id: 'b' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
    act(() => fireEvent.keyDown(screen.getByTestId('deck'), { key: 'ArrowRight' }));
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled();
    expect(screen.getByTestId('deck-snooze-menu')).toBeTruthy();
  });

  it('a kind with no backend snooze verb gets NO menu, and POSTs nothing at all', () => {
    // A "3 days" button with no store behind it is the accepted-then-ignored
    // promise this round exists to stop making.
    //
    // The no-POST half is pinned HERE as well as at the hook layer (see "a kind
    // with NO backend snooze verb still defers — but POSTs nothing") because a
    // regression could live in either place: `snoozeActionFor` could start
    // returning an id for every kind, OR Deck's own ↑ wiring could route around
    // it. Both would 400 every email card the operator flicks up, and the field
    // symptom is identical, so both layers carry the assert.
    render(<Deck items={[item({ id: 'e', kind: 'email_tier' }), item({ id: 'b' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
    expect(screen.queryByTestId('deck-snooze-menu')).toBeNull();
    expect(screen.getByTestId('deck-snoozed').textContent).toContain('1'); // it DID defer
    act(() => vi.advanceTimersByTime(UNDO_MS)); // past the window: a real act would fire now
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('the ↑ button advertises which of the two it is', () => {
    const { unmount } = render(<Deck items={[slotItem()]} />);
    const backed = screen.getByTestId('deck-btn-snooze');
    expect(backed.getAttribute('aria-label')).toContain('choose how long');
    expect(backed.getAttribute('aria-haspopup')).toBe('dialog');
    unmount();

    render(<Deck items={[item({ id: 'e', kind: 'email_tier' })]} />);
    const unbacked = screen.getByTestId('deck-btn-snooze');
    expect(unbacked.getAttribute('aria-label')).toBe('Set aside for now');
    expect(unbacked.getAttribute('aria-haspopup')).toBeNull();
  });
});

// --- #14 WARN-1: the ↑ toast must describe what the gesture actually did -----
// One gesture, three outcomes, three different return times. The full swipe —
// the preserved Park muscle memory, and so the most-used path — defaults to the
// indefinite rung, which by design NEVER comes back on its own. Telling that
// operator "it resurfaces at the next sync" is a false promise in the round
// whose whole thesis is copy honesty.
//
// Asserted on the RENDERED toast, not the hook's return value: the string the
// operator reads is the artifact under test.

describe('Deck — the snooze toast names the real outcome (#14 WARN-1)', () => {
  it('a full ↑ swipe (indefinite) promises nothing about a sync', () => {
    // Mutation: collapse snoozeToast to the single old sentence → this fails.
    render(<Deck items={[slotItem(), item({ id: 'b' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
    act(() => fireEvent.click(screen.getByTestId('deck-snooze-choice-snooze_until_i_say')));

    const toast = screen.getByTestId('deck-toast').textContent ?? '';
    expect(toast).toContain('until you say otherwise');
    expect(toast).not.toContain('next sync');
  });

  it('a DATED rung names the duration, and also does not claim the next sync', () => {
    // A 3-day snooze doesn't return at the next sync either — the store is the
    // entire point. So the dated branch gets its own true sentence rather than
    // inheriting the set-aside wording.
    render(<Deck items={[slotItem(), item({ id: 'b' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
    act(() => fireEvent.click(screen.getByTestId('deck-snooze-choice-snooze_3d')));

    const toast = screen.getByTestId('deck-toast').textContent ?? '';
    expect(toast).toContain('3 days');
    expect(toast).not.toContain('next sync');
  });

  it('an UNBACKED kind is the one case where the next-sync wording is true', () => {
    // Nothing was written and the item is still open server-side, so it really
    // does come back — and the word matches the button ("Set aside for now").
    render(<Deck items={[item({ id: 'e', kind: 'email_tier' }), item({ id: 'b' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));

    const toast = screen.getByTestId('deck-toast').textContent ?? '';
    expect(toast).toContain('Set aside');
    expect(toast).toContain('next sync');
  });

  it('all three ↑ outcomes read differently from each other', () => {
    // The point of the branch is DISCRIMINATION. A refactor that made two of
    // them collapse to the same sentence would leave each individual assert
    // above still passing.
    const seen = new Set<string>();
    for (const [items, choice] of [
      [[slotItem()], 'deck-snooze-choice-snooze_until_i_say'],
      [[slotItem()], 'deck-snooze-choice-snooze_3d'],
      [[item({ id: 'e', kind: 'email_tier' })], null],
    ] as Array<[FeedItem[], string | null]>) {
      const { unmount } = render(<Deck items={items} />);
      act(() => fireEvent.click(screen.getByTestId('deck-btn-snooze')));
      if (choice) act(() => fireEvent.click(screen.getByTestId(choice)));
      seen.add(screen.getByTestId('deck-toast').textContent ?? '');
      unmount();
    }
    expect(seen.size).toBe(3);
  });
});
