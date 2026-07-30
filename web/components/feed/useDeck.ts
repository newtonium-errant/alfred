import { useCallback, useEffect, useRef, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { UNDO_MS, deckVerbsFor, HEAVY_KINDS, type Verdict } from '../../lib/algernon/feedConstants';
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
  const [parkedCount, setParkedCount] = useState(0);

  const pendingRef = useRef<Pending | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
        setParkedCount((c) => c + 1);
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

  const current = index < items.length ? items[index] : null;

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
    if (!verbs || verbs.reject === null) return; // no reject action (e.g. pending)
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
      setParkedCount((c) => Math.max(0, c - 1));
      onUnparkPersist?.(p.item.id);
    }
    setIndex(p.restoreIndex);
    setConfirmingId(null);
    setToast(null);
  }, [onUnparkPersist]);

  const dismissToast = useCallback(() => setToast(null), []);

  const remaining = Math.max(0, items.length - index);
  const upcoming = items.slice(index + 1, index + 3);
  const cleared = index >= items.length;

  return {
    current,
    upcoming,
    remaining,
    parkedCount,
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
    dismissToast,
  };
}
