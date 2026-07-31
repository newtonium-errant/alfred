import { useCallback, useRef, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { ApiError } from '../../lib/algernon/http';
import { RING_ACTION_DONE, RING_ACTION_UNDO, ringItemDone } from '../../lib/algernon/rings';

// The rings-panel completion state machine (Phase C DONE path). DOM-free + driven
// by a mocked feedApi so the per-lane optimistic flip, the ActResult status map,
// undo, and error routing are unit-pinned; RingsHeader renders on top.
//
// Flow: tap ✓ → the item's button goes BUSY (spinner) while POST /feed/act
// {done} fires. On success (acted / already_acted) the segment + dot flip GREEN
// and the row strikes through — held client-side until the next feed render
// reconciles (done items re-emit done=true / stay acted, so it STAYS green). On
// error the flip reverts and a per-item message shows. Undo is the inverse
// (undo_done), allowed only on a done item. A 401 bubbles to onAuthExpired.

interface Override {
  done: boolean;
  busy: boolean;
  error?: string;
}

export interface UseRingCompletionOptions {
  onAuthExpired?: () => void;
}

export interface UseRingCompletionResult {
  /** Effective done-ness (optimistic override, else server truth). */
  effectiveDone: (item: FeedItem) => boolean;
  /** Whether an act is in flight for this item. */
  busy: (id: string) => boolean;
  /** The last error message for this item, or null. */
  errorFor: (id: string) => string | null;
  /** Complete an item (✓). */
  complete: (item: FeedItem) => void;
  /** Undo a completed item. */
  undo: (item: FeedItem) => void;
}

// Human message for a failed act — mirrors the deck's routeError taxonomy so the
// board speaks the same language. (401 is handled by the caller, not here.)
function messageFor(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409 || e.code === 'stale_item') {
      return "That one moved on — it'll resurface at the next sync.";
    }
    if (
      e.status === 502 ||
      e.status === 503 ||
      e.code === 'feed_upstream_unavailable' ||
      e.code === 'transport_unreachable' ||
      e.code === 'not_configured'
    ) {
      return "Can't reach Algernon right now — this is server-side, not your session.";
    }
    if (e.status === 504 || e.code === 'timeout' || e.code === 'gateway_timeout' || e.code === 'network_error') {
      return "That didn't confirm in time — the next sync will reconcile it.";
    }
    if (e.status === 400 || e.code === 'invalid_action') {
      // Includes graceful degradation: an OLD router that doesn't know `done` yet
      // answers invalid_action → the completion just isn't available until deploy.
      return 'Completing that isn’t available yet — try again after the next sync.';
    }
    // 422 unsupported_item / error (writer domain refusal) — the resolver's own
    // human line where it has one.
    return e.detail || 'That could not be completed.';
  }
  return 'That action failed.';
}

export function useRingCompletion(opts: UseRingCompletionOptions = {}): UseRingCompletionResult {
  const { onAuthExpired } = opts;
  const [overrides, setOverridesState] = useState<Record<string, Override>>({});
  // A ref mirror so an in-flight act can read the CURRENT effective done to revert
  // to (an undo of an optimistically-completed item must revert to done, not to the
  // item's still-open server state).
  const overridesRef = useRef<Record<string, Override>>({});
  const setOverrides = useCallback((updater: (prev: Record<string, Override>) => Record<string, Override>) => {
    setOverridesState((prev) => {
      const next = updater(prev);
      overridesRef.current = next;
      return next;
    });
  }, []);

  const run = useCallback(
    (item: FeedItem, action: string, optimisticDone: boolean) => {
      const cur = overridesRef.current[item.id];
      const revertDone = cur ? cur.done : ringItemDone(item);
      // Enter busy WITHOUT changing done yet (spinner during flight); the green /
      // amber flip lands on the success response.
      setOverrides((prev) => ({ ...prev, [item.id]: { done: revertDone, busy: true, error: undefined } }));
      feedApi
        .act(item.id, action)
        .then((res) => {
          // Success statuses: acted / already_acted (done) or undone (open).
          const nowDone =
            res.status === 'acted' || res.status === 'already_acted'
              ? true
              : res.status === 'undone'
                ? false
                : optimisticDone;
          setOverrides((prev) => ({ ...prev, [item.id]: { done: nowDone, busy: false } }));
        })
        .catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 401) {
            onAuthExpired?.();
            // Clear busy but keep the pre-act state (the page redirects on 401).
            setOverrides((prev) => ({ ...prev, [item.id]: { done: revertDone, busy: false } }));
            return;
          }
          setOverrides((prev) => ({ ...prev, [item.id]: { done: revertDone, busy: false, error: messageFor(e) } }));
        });
    },
    [onAuthExpired, setOverrides],
  );

  const complete = useCallback((item: FeedItem) => run(item, RING_ACTION_DONE, true), [run]);
  const undo = useCallback((item: FeedItem) => run(item, RING_ACTION_UNDO, false), [run]);

  const effectiveDone = useCallback(
    (item: FeedItem) => {
      const o = overrides[item.id];
      return o ? o.done : ringItemDone(item);
    },
    [overrides],
  );
  const busy = useCallback((id: string) => overrides[id]?.busy ?? false, [overrides]);
  const errorFor = useCallback((id: string) => overrides[id]?.error ?? null, [overrides]);

  return { effectiveDone, busy, errorFor, complete, undo };
}
