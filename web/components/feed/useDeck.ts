import { useCallback, useEffect, useRef, useState } from 'react';
import { feedApi, type FeedActResult, type FeedItem } from '../../lib/algernon/feed';
import {
  CORRECT_ACTION,
  HEAVY_KINDS,
  ONE_OFF_ACTION,
  ROUTINE_MATCH_KIND,
  UNDO_MS,
  deckVerbsFor,
  type Verdict,
} from '../../lib/algernon/feedConstants';
import { ApiError } from '../../lib/algernon/http';

// The deck state machine — deliberately DOM-free so the intricate parts (the
// delayed act, the two-step heavy confirm, park, undo, and error routing) are
// unit-testable with fake timers + a mocked feedApi, while Deck.tsx supplies the
// pointer-drag + rendering on top.
//
// DELAYED ACT (light kinds + heavy reject + heavy confirm-tap): a commit advances
// the card immediately (optimistic) but the POST is DEFERRED. It fires when the
// 3.5s undo window expires OR the next commit lands (flush-in-order) — and UNDO
// cancels it before it ever fires (never an un-act). PARK never POSTs (client
// defer). HEAVY AFFIRM's FIRST swipe does not commit — it reveals a confirm-tap.
//
// Because the card is already dismissed by the time a deferred POST resolves,
// outcomes surface as a TOAST (benign: stale / timeout — next list poll
// reconciles truth) or a fatal BANNER (server-config 502/503 — never a logout),
// not a card flip. A genuine 401 (the BFF's own invalid_session — the transport's
// wrong-peer 401 is mapped to 502 by the BFF) routes to onAuthExpired.

export interface DeckToast {
  message: string;
  canUndo: boolean;
}

export interface UseDeckOptions {
  items: FeedItem[];
  onAuthExpired?: () => void;
  onParkPersist?: (id: string) => void;
  onUnparkPersist?: (id: string) => void;
}

export interface UseDeckResult {
  current: FeedItem | null;
  upcoming: FeedItem[];
  remaining: number;
  /** The this-session parked cards, retained for the parked drill-down. */
  parked: FeedItem[];
  parkedCount: number;
  confirmingId: string | null;
  toast: DeckToast | null;
  banner: string | null;
  cleared: boolean;
  affirm: () => void;
  reject: () => void;
  park: () => void;
  confirmHeavy: () => void;
  cancelHeavy: () => void;
  undo: () => void;
  /** Deal a parked card back into the deck queue (un-park + re-enter immediately). */
  dealNow: (item: FeedItem) => void;
  /** The re-tier action_id in flight for the current email card (#28), or null. */
  reTiering: string | null;
  /** Re-tier the current email card: await the act, flip only on `acted`. Awaitable. */
  reTier: (tierId: string) => Promise<void>;
  /** The #13 correction in flight for the current routine_match card — the chosen
   *  item text, or the ONE_OFF_ACTION sentinel, or null when idle. */
  correcting: string | null;
  /** Reject the routine match AND teach the right answer. `target` = the item the
   *  operator picked; `null` = "nothing, this was a one-off". Awaitable. */
  correctRoutine: (target: string | null) => Promise<void>;
  dismissToast: () => void;
}

interface Pending {
  item: FeedItem;
  actionId: string | null; // null = park (no POST)
  verdict: Verdict;
  restoreIndex: number;
  restorePark: boolean;
}

export function useDeck(opts: UseDeckOptions): UseDeckResult {
  const { items, onAuthExpired, onParkPersist, onUnparkPersist } = opts;

  const [index, setIndex] = useState(0);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [toast, setToast] = useState<DeckToast | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  // The this-session parked cards, RETAINED (not just counted) so the parked
  // drill-down can list them (title + kind) + deal them back. undo un-parks the last.
  const [parked, setParked] = useState<FeedItem[]>([]);
  // Cards dealt back from the parked view — re-appended to the tail of the deck queue
  // so an un-parked card re-enters the deck immediately without disturbing the index.
  const [readded, setReadded] = useState<FeedItem[]>([]);
  // The re-tier action_id in flight for the current email card (#28), or null. Unlike a
  // swipe (optimistic advance + deferred POST), a re-tier AWAITS the act and only flips
  // on `acted` — a calibration CORRECTION must be honestly confirmed, not optimistic.
  const [reTiering, setReTiering] = useState<string | null>(null);
  // The #13 correction in flight (chosen item text, or the one-off sentinel).
  // Like re-tier and for the same reason: a taught answer must be confirmed by
  // the server before the card leaves, because the server is the only thing that
  // knows whether the pick was a real routine item.
  const [correcting, setCorrecting] = useState<string | null>(null);

  const pendingRef = useRef<Pending | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const readdSeqRef = useRef(0);

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const routeError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError) {
        if (e.status === 401) {
          onAuthExpired?.();
          return;
        }
        if (e.status === 409 || e.code === 'stale_item') {
          setToast({ message: "That one had already moved on — it'll resurface at the next sync.", canUndo: false });
          return;
        }
        if (e.status === 502 || e.status === 503 || e.code === 'feed_upstream_unavailable' || e.code === 'transport_unreachable' || e.code === 'not_configured') {
          setBanner("The deck can't reach Algernon right now — this is a server-side issue, not your session.");
          return;
        }
        if (e.status === 504 || e.code === 'timeout' || e.code === 'gateway_timeout' || e.code === 'network_error') {
          // NO retry — the act may have landed; the next list poll reconciles.
          setToast({ message: "That didn't confirm in time — the next sync will reconcile it.", canUndo: false });
          return;
        }
        setToast({ message: e.detail || e.code || 'That action failed.', canUndo: false });
        return;
      }
      setToast({ message: 'That action failed.', canUndo: false });
    },
    [onAuthExpired],
  );

  // Fire the deferred POST for the current pending commit (if any), in order.
  const flushPending = useCallback(() => {
    clearTimer();
    const p = pendingRef.current;
    pendingRef.current = null;
    if (!p || p.actionId === null) return; // park (or nothing) → no POST
    feedApi.act(p.item.id, p.actionId).catch(routeError);
  }, [routeError]);

  // Flush on unmount so a pending act isn't silently dropped when leaving /deck.
  useEffect(() => {
    return () => {
      const p = pendingRef.current;
      clearTimer();
      pendingRef.current = null;
      if (p && p.actionId !== null) {
        feedApi.act(p.item.id, p.actionId).catch(() => {});
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const commit = useCallback(
    (verdict: Verdict, actionId: string | null, item: FeedItem) => {
      flushPending(); // fire the previous deferred act BEFORE starting a new one
      const restoreIndex = index;
      let restorePark = false;
      if (verdict === 'park') {
        restorePark = true;
        setParked((prev) => [...prev, item]);
        onParkPersist?.(item.id);
      }
      setConfirmingId(null);
      setIndex((i) => i + 1);
      pendingRef.current = { item, actionId, verdict, restoreIndex, restorePark };
      setToast({
        message:
          verdict === 'park'
            ? 'Parked — it resurfaces at the next sync.'
            : verdict === 'reject'
              ? 'Rejected.'
              : 'Confirmed.',
        canUndo: true,
      });
      clearTimer();
      timerRef.current = setTimeout(() => {
        flushPending();
        setToast(null);
      }, UNDO_MS);
    },
    [flushPending, index, onParkPersist],
  );

  // The effective deck queue: the base items plus any cards dealt back from the parked
  // view (appended at the tail). The index walks this combined queue, so a re-dealt card
  // becomes reachable without shifting the cards already behind the cursor.
  const queue = readded.length > 0 ? items.concat(readded) : items;
  const current = index < queue.length ? queue[index] : null;

  const affirm = useCallback(() => {
    if (!current) return;
    const verbs = deckVerbsFor(current.kind);
    if (!verbs || verbs.affirm === null) return; // no affirm action for this kind
    // Heavy affirm's FIRST swipe reveals the confirm stage (does not commit).
    if (HEAVY_KINDS.has(current.kind) && confirmingId !== current.id) {
      setConfirmingId(current.id);
      return;
    }
    commit('affirm', verbs.affirm, current);
  }, [current, confirmingId, commit]);

  const confirmHeavy = useCallback(() => {
    if (!current || confirmingId !== current.id) return;
    const verbs = deckVerbsFor(current.kind);
    if (!verbs || verbs.affirm === null) return;
    commit('affirm', verbs.affirm, current);
  }, [current, confirmingId, commit]);

  const cancelHeavy = useCallback(() => setConfirmingId(null), []);

  const reject = useCallback(() => {
    if (!current) return;
    const verbs = deckVerbsFor(current.kind);
    if (!verbs) return;
    // C2 skip=park: a rejectParks lane (slot candidate) routes the LEFT gesture to a
    // client-side PARK (no POST) — a skip, not a decline; it may resurface at the
    // next sync (there's no backend decline path for slots v1).
    if (verbs.rejectParks) {
      commit('park', null, current);
      return;
    }
    if (verbs.reject === null) return; // no reject action (e.g. pending)
    commit('reject', verbs.reject, current);
  }, [current, commit]);

  const park = useCallback(() => {
    if (!current) return;
    commit('park', null, current);
  }, [current, commit]);

  const undo = useCallback(() => {
    const p = pendingRef.current;
    if (!p) return;
    clearTimer();
    pendingRef.current = null; // cancel the deferred POST — never an un-act
    if (p.restorePark) {
      setParked((prev) => prev.filter((it) => it.id !== p.item.id));
      onUnparkPersist?.(p.item.id);
    }
    setIndex(p.restoreIndex);
    setConfirmingId(null);
    setToast(null);
  }, [onUnparkPersist]);

  // Deal a parked card back into the deck: drop it from the park set (client + persist)
  // and re-append it to the queue tail so it's dealable immediately. The clone carries a
  // fresh render-key (__deckKey) so a re-dealt id can never collide in the visible stack
  // (deal → re-park → deal-again is legal); id is preserved so POST/persist stay correct.
  const dealNow = useCallback(
    (target: FeedItem) => {
      setParked((prev) => prev.filter((it) => it.id !== target.id));
      onUnparkPersist?.(target.id);
      const seq = readdSeqRef.current++;
      setReadded((prev) => [...prev, { ...target, __deckKey: `readd-${seq}` } as FeedItem]);
    },
    [onUnparkPersist],
  );

  // Re-tier the current email card (#28): POST the chosen tier action_id, AWAIT it, and
  // flip the card off the deck ONLY on `acted` — no optimistic advance, nothing "greens"
  // before the backend confirms the correction. A pending swipe act flushes first
  // (ordering). `acted` → advance + honest no-undo toast; already-moved-on → advance
  // (stale); ApiError → routeError; any other status → keep the card + honest detail.
  // Awaitable so the caller can close the picker once the act resolves.
  const reTier = useCallback(
    async (tierId: string) => {
      const item = current;
      if (!item || reTiering) return;
      flushPending(); // fire any deferred swipe act BEFORE the re-tier (in order)
      setReTiering(tierId);
      let res: FeedActResult;
      try {
        res = await feedApi.act(item.id, tierId);
      } catch (e) {
        setReTiering(null);
        routeError(e);
        return;
      }
      setReTiering(null);
      if (res.ok && res.status === 'acted') {
        setIndex((i) => i + 1); // flip-on-acted: leaves the deck like any other act
        setToast({ message: `Re-tiered to ${tierId.toUpperCase()}.`, canUndo: false });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else if (res.status === 'already_acted' || res.status === 'stale_item') {
        setIndex((i) => i + 1); // already moved on server-side → remove the stale card
        setToast({ message: "That one had already moved on — it'll resurface at the next sync.", canUndo: false });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else {
        // invalid_action / error / unknown → keep the card, honest toast (retry possible).
        setToast({ message: res.detail || `Couldn't re-tier (${res.status}).`, canUndo: false });
      }
    },
    [current, reTiering, flushPending, routeError],
  );

  // Reject the current routine_match card AND say what it meant (#13). `target` is
  // the routine item the operator picked; `null` is the one-off door ("nothing").
  //
  // AWAITED, never optimistic — deliberately unlike a swipe. A swipe's outcome is
  // knowable client-side; a correction's is not: only the server can say whether
  // the pick is still a live routine item, and it refuses the WHOLE verdict if it
  // isn't. Advancing first would show the operator a card that left the deck
  // having taught nothing. On refusal the card stays and the toast carries the
  // server's own words, so a retry is possible from the same screen.
  const correctRoutine = useCallback(
    async (target: string | null) => {
      const item = current;
      if (!item || correcting) return;
      if (item.kind !== ROUTINE_MATCH_KIND) return;
      const chosen = (target || '').trim();
      // A `correct` with an empty pick is refused server-side; don't spend a
      // round trip to be told so. The one-off door is the explicit null.
      if (target !== null && !chosen) return;
      flushPending(); // fire any deferred swipe act BEFORE this one (in order)
      setCorrecting(target === null ? ONE_OFF_ACTION : chosen);
      let res: FeedActResult;
      try {
        res = await feedApi.act(
          item.id,
          target === null ? ONE_OFF_ACTION : CORRECT_ACTION,
          target === null ? undefined : chosen,
        );
      } catch (e) {
        setCorrecting(null);
        routeError(e);
        return;
      }
      setCorrecting(null);
      if (res.ok && res.status === 'acted') {
        setIndex((i) => i + 1);
        setToast({
          message: target === null ? 'Noted — a one-off.' : `Noted — it means “${chosen}”.`,
          canUndo: false,
        });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else if (res.status === 'already_acted' || res.status === 'stale_item') {
        setIndex((i) => i + 1);
        setToast({ message: "That one had already moved on — it'll resurface at the next sync.", canUndo: false });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else {
        // Refused (an unpickable item, a vault the server can't read) → KEEP the
        // card and show what the server said. Never a cheerful green over a
        // verdict that didn't land.
        setToast({ message: res.detail || `Couldn't record that (${res.status}).`, canUndo: false });
      }
    },
    [current, correcting, flushPending, routeError],
  );

  const dismissToast = useCallback(() => setToast(null), []);

  const remaining = Math.max(0, queue.length - index);
  const upcoming = queue.slice(index + 1, index + 3);
  const cleared = index >= queue.length;

  return {
    current,
    upcoming,
    remaining,
    parked,
    parkedCount: parked.length,
    confirmingId,
    toast,
    banner,
    cleared,
    affirm,
    reject,
    park,
    confirmHeavy,
    cancelHeavy,
    undo,
    dealNow,
    reTiering,
    reTier,
    correcting,
    correctRoutine,
    dismissToast,
  };
}
