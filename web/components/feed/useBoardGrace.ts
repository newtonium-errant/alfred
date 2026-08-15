import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { UNDO_MS } from '../../lib/algernon/feedConstants';
import { RING_ACTION_DONE } from '../../lib/algernon/rings';
import {
  clearAllBoardUnrecorded,
  clearBoardUnrecorded,
  readBoardUnrecorded,
  recordBoardUnrecorded,
  type UnrecordedCompletion,
} from '../../lib/algernon/boardUnrecorded';
import type { ActOutcomeReport, UseRingCompletionResult } from './useRingCompletion';

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
// ── WHAT THE COPY INHERITED, AND WHAT IT DIDN'T ─────────────────────────────
// It copied the quartet WITHOUT the thing that makes the quartet honest. A
// deferred write has one door with no component behind it — the unmount flush —
// and the POST it fires answers into a dead closure. On the deck that silence
// was the 2026-08-15 incident (five verdicts, recorded nowhere, never reported);
// here it was the same silence on the operator's primary morning surface, where
// a refused ✓ left the row painted done and told nobody, ever.
//
// So the board now keeps the deck's other half too: a definite refusal is
// written to an UNRECORDED-COMPLETION LEDGER (`lib/algernon/boardUnrecorded`) —
// plain storage calls, so they work identically from a live render and from the
// unmount closure — and the next mount of this hook reads it back and hands it
// to `SlotBoard` to name on screen. Its own key, never the deck's; see that
// module for why.
//
// ── WHY THE SHARED HOOK IS (NOW, NARROWLY) TOUCHED ──────────────────────────
// This wraps `useRingCompletion`; it still does not change what that hook DOES.
// Putting the grace delay inside it would silently change the behaviour of the
// feed page and the rings panel, which drive the same hook — so the grace still
// lives at the board layer, where the ruling put it.
//
// What was added there is a per-CALL, optional outcome reporter: the hook is the
// only thing that knows whether the server answered, and a deferred caller is
// the only caller that needs to be told separately (everyone else is looking at
// the row that renders the error). Callers that pass nothing are unaffected —
// which is every caller but the flush below.

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
  /**
   * Every ✓ the server refused and that has not been settled since — oldest
   * first, ACCUMULATED rather than replaced.
   *
   * A burst is a normal shape of this failure (one dead upstream refuses every
   * row tapped against it), so five refusals must read as five. Includes the
   * ones whose answer arrived after the board was gone — that is what the
   * store is for.
   */
  unrecorded: UnrecordedCompletion[];
  /** The ids in `unrecorded` — the rows that carry the not-recorded mark. */
  unrecordedIds: Set<string>;
  /** The operator has read the list: clear it (the rows stay on the board). */
  acknowledgeUnrecorded: () => void;
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

  // The unrecorded-completion ledger, mirrored from `boardUnrecorded` storage.
  // The STORE is the source of truth (it is what survives unmount and tab
  // close); this is the render copy.
  const [unrecorded, setUnrecorded] = useState<UnrecordedCompletion[]>([]);

  // Hydrate the ledger on mount — NOT as lazy `useState(readBoardUnrecorded)`,
  // which would read storage during the first client render and disagree with
  // the server's HTML (no notice) that React is hydrating against. An effect
  // runs after that reconciliation, so the notice appears without a mismatch.
  //
  // This is also the SECOND HALF of the unmount path: a refusal whose answer
  // arrived when there was no board left wrote itself to storage from a dead
  // closure, and this is where it comes back into view.
  useEffect(() => {
    const stored = readBoardUnrecorded();
    if (stored.length > 0) setUnrecorded(stored);
  }, []);

  const pendingRef = useRef<Pending | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The completion hook is read through a ref inside the unmount path: that
  // effect must run exactly once (an unmount flush that re-subscribed on every
  // render would fire on each change instead of on leaving the page).
  const completionRef = useRef(completion);
  completionRef.current = completion;
  // The ledger as of this render, readable from an async outcome handler without
  // making it a dependency (which would re-create the flush on every debt).
  const unrecordedRef = useRef(unrecorded);
  unrecordedRef.current = unrecorded;

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  // ACCOUNT FOR ONE HELD WRITE — the accounting the flush never used to do.
  //
  // The ledger call comes FIRST and is a plain storage call, so it works
  // identically from a live render and from the unmount closure, where the
  // setState below is a no-op and there is no component left to tell. That
  // ordering is the whole fix: state-only bookkeeping cannot survive the door
  // most of these failures leave by.
  const accountFor = useCallback((item: FeedItem, report: ActOutcomeReport) => {
    if (report.outcome === 'refused') {
      setUnrecorded(
        recordBoardUnrecorded({
          id: item.id,
          // The row's own title, because the notice must be able to NAME a row
          // that is no longer in the served batch — which is exactly the row the
          // operator has no other way to find out about.
          title: item.title || item.id,
          actionId: RING_ACTION_DONE,
          reason: report.reason,
          at: Date.now(),
        }),
      );
      return;
    }
    if (report.outcome === 'landed' && unrecordedRef.current.some((u) => u.id === item.id)) {
      // The ✓ was given again and stuck this time — settle the debt this row was
      // carrying. Gated on the ref rather than done inside a state updater:
      // clearing writes to storage, and a state updater is not a place for that.
      setUnrecorded(clearBoardUnrecorded(item.id));
    }
    // 'unknown' deliberately does nothing. It is the ABSENCE of an answer, and
    // ledgering it would tell the operator a completion failed that may well
    // have landed — the #62 overreach, in the one place with no way to correct
    // itself later.
  }, []);

  // Held in a ref for the same reason `completion` is: the unmount effect must
  // stay mount-scoped, so it may not close over anything whose identity a
  // re-render could change.
  const accountForRef = useRef(accountFor);
  accountForRef.current = accountFor;

  const flush = useCallback(() => {
    clearTimer();
    const p = pendingRef.current;
    pendingRef.current = null;
    setPendingId(null);
    setToast(null);
    if (!p) return;
    setCommitting((prev) => new Set(prev).add(p.item.id));
    completionRef.current.complete(p.item, (report) => accountFor(p.item, report));
  }, [accountFor]);

  // Flush on unmount so a held completion is not silently dropped when the
  // operator navigates away from home mid-window. Dropping it would be the
  // done-but-unrecorded failure this whole surface exists to end.
  //
  // The POST fires from here with no component behind it, so its answer lands in
  // a dead closure — which is why the outcome is threaded through `accountFor`
  // rather than left to the completion hook's per-row error line. That line has
  // nowhere to render by now; the ledger write does not care.
  useEffect(() => {
    return () => {
      const p = pendingRef.current;
      clearTimer();
      pendingRef.current = null;
      if (p) completionRef.current.complete(p.item, (report) => accountForRef.current(p.item, report));
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

  // The ONE control on the notice. It clears the list AND the marks on the rows,
  // because a single control that means one thing beats two the operator has to
  // tell apart — and because a "hide" that kept the ledger would strand any entry
  // whose row is no longer on the board, which is exactly the entry with no other
  // way to be settled. The rows themselves stay either way; acknowledging is
  // reading, not deciding.
  const acknowledgeUnrecorded = useCallback(() => {
    setUnrecorded(clearAllBoardUnrecorded());
  }, []);

  return {
    pendingId,
    toast,
    optimisticallyDone,
    tap,
    undo,
    flush,
    reconcile,
    unrecorded,
    unrecordedIds: new Set(unrecorded.map((u) => u.id)),
    acknowledgeUnrecorded,
  };
}
