import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { UNDO_MS } from '../../lib/algernon/feedConstants';
import type { UseRingCompletionResult } from './useRingCompletion';

// ══════════ BOARD UNDO-GRACE (Phase C / C1, ruled option (a)) ═══════════════
//
// A tap on the board's ✓ does NOT post immediately. It marks the row done on
// screen, opens a toast with a draining bar, and holds the write for UNDO_MS.
// Undo inside that window CANCELS — nothing was ever written, so there is
// nothing to reverse. That is the deck's D8 mechanism, and it is strictly
// stronger than a post-hoc undo.
//
// ── DELIBERATE DUPLICATION, SHARED EXTRACTION PENDING ───────────────────────
// The pendingRef / timerRef / flush-in-order / flush-on-unmount quartet below is
// a knowing second copy of `useDeck`'s (`components/feed/useDeck.ts`, the
// `pendingRef`+`timerRef` pair, `flushPending`, and its unmount effect). It is
// duplicated rather than extracted ON PURPOSE and for exactly one round: the
// deck's copy is entangled with deck-only state (card index, restoreIndex,
// snooze re-add) that a board has no concept of, and the extraction that unifies
// them should be driven by two real consumers rather than guessed from one. The
// second consumer is this file. Whoever needs a third should extract, not paste.
//
// ── WHY THE SHARED HOOK IS NOT TOUCHED ──────────────────────────────────────
// This wraps `useRingCompletion`; it does not modify it. `useRingCompletion` is
// also what the feed page and the rings panel drive, and putting a grace delay
// inside it would silently change the behaviour of surfaces outside this lane's
// gate. The grace lives at the board layer, where the ruling put it.

interface Pending {
  item: FeedItem;
}

export interface BoardGraceToast {
  itemId: string;
  /** The row's own title — the toast names what it is holding, not "an item". */
  title: string;
}

export interface UseBoardGraceResult {
  /** The item inside its grace window right now, or null. */
  pendingId: string | null;
  /** The toast to render (message + undo + draining bar), or null. */
  toast: BoardGraceToast | null;
  /**
   * Whether the board should paint this row DONE — the grace window plus the
   * post-flush flight, so the row never flickers back to open between the
   * window closing and the server answering.
   */
  optimisticallyDone: (item: FeedItem) => boolean;
  /** Tap ✓ — open the grace window (no POST yet). */
  tap: (item: FeedItem) => void;
  /** Undo inside the window — cancels the write entirely. */
  undo: () => void;
  /** Commit the held write now (also runs on unmount and before a new tap). */
  flush: () => void;
  /** Drop optimistic state a fresh render has answered — call with each load. */
  reconcile: (items: FeedItem[]) => void;
}

export function useBoardGrace(completion: UseRingCompletionResult): UseBoardGraceResult {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [toast, setToast] = useState<BoardGraceToast | null>(null);
  // Ids whose held write has been FIRED. Kept so the row stays done across the
  // POST's flight: `completion.complete` enters its busy state with `done` still
  // false (it flips only on the success response), so without this the row would
  // blink open the instant the grace window closed — the operator would watch
  // their own completion appear to undo itself.
  const [committing, setCommitting] = useState<ReadonlySet<string>>(() => new Set());

  const pendingRef = useRef<Pending | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The completion hook is read through a ref inside the unmount path: that
  // effect must run exactly once (an unmount flush that re-subscribed on every
  // render would fire on each change instead of on leaving the page).
  const completionRef = useRef(completion);
  completionRef.current = completion;

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const flush = useCallback(() => {
    clearTimer();
    const p = pendingRef.current;
    pendingRef.current = null;
    setPendingId(null);
    setToast(null);
    if (!p) return;
    setCommitting((prev) => new Set(prev).add(p.item.id));
    completionRef.current.complete(p.item);
  }, []);

  // Flush on unmount so a held completion is not silently dropped when the
  // operator navigates away from home mid-window. Dropping it would be the
  // done-but-unrecorded failure this whole surface exists to end.
  useEffect(() => {
    return () => {
      const p = pendingRef.current;
      clearTimer();
      pendingRef.current = null;
      if (p) completionRef.current.complete(p.item);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tap = useCallback(
    (item: FeedItem) => {
      // Flush the PREVIOUS held write before starting a new one, so two quick
      // taps commit in the order they were made rather than the second one
      // cancelling the first.
      flush();
      pendingRef.current = { item };
      setPendingId(item.id);
      setToast({ itemId: item.id, title: item.title || item.id });
      clearTimer();
      timerRef.current = setTimeout(() => flush(), UNDO_MS);
    },
    [flush],
  );

  const undo = useCallback(() => {
    clearTimer();
    pendingRef.current = null;
    setPendingId(null);
    setToast(null);
  }, []);

  const optimisticallyDone = useCallback(
    (item: FeedItem) => {
      if (pendingId === item.id) return true;
      if (!committing.has(item.id)) return false;
      // A failed write is NOT done. The completion hook owns the error copy;
      // this just stops claiming a green row on top of it.
      return completion.errorFor(item.id) === null;
    },
    [pendingId, committing, completion],
  );

  const reconcile = useCallback((items: FeedItem[]) => {
    // A render that includes the item has answered the question this optimistic
    // flag was standing in for — same supersession discipline `useRingCompletion`
    // applies to its own overrides. Holding it longer would prefer a stale
    // opinion over a fact.
    const seen = new Set(items.map((it) => it.id));
    setCommitting((prev) => {
      const next = new Set([...prev].filter((id) => !seen.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, []);

  return { pendingId, toast, optimisticallyDone, tap, undo, flush, reconcile };
}
