import { useCallback, useRef, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { UNSNOOZE_ACTION, snoozeIsBacked, type SnoozeAction } from '../../lib/algernon/feedConstants';
import { ApiError } from '../../lib/algernon/http';
import { ACT_UNCONFIRMED_MESSAGE, isInconclusive } from '../../lib/algernon/actConfirm';

// The snooze verb for the INLINE surfaces (the feed's worklist rows), sibling to
// useRingCompletion (done / undo_done) and useSlotAccept (accept). The deck does
// NOT use this: a swipe is optimistic-and-undoable by design and rides useDeck's
// delayed-act machinery, whereas a row here has no undo window and no gesture —
// it has a labelled button, so the honest thing is to wait for the server and
// say what happened.
//
// SUCCESS IS `acted`, NOT 200. A snooze can be refused for real reasons the
// operator needs to hear: the row is already done ("nothing to snooze"), or
// `tier.snooze.path` isn't wired on this instance, in which case the router
// refuses rather than accepting a tap it would silently drop. Flipping the row
// on status alone would re-create exactly the accepted-then-ignored failure the
// backend went out of its way to avoid.
//
// The optimistic flip is therefore gated on `res.ok && res.status === 'acted'`,
// and every other shape leaves the row where it is with the server's own words
// attached — retry is possible from the same screen.

export interface UseSnoozeOptions {
  onAuthExpired?: () => void;
}

export interface UseSnoozeResult {
  /** Whether this item has been snoozed this session (server-confirmed). */
  snoozed: (id: string) => boolean;
  /** Whether a snooze/unsnooze is in flight for this item. */
  busy: (id: string) => boolean;
  /** The last snooze error for this item, or null. */
  errorFor: (id: string) => string | null;
  /** Snooze an item for the chosen duration. Awaitable (the caller closes its menu). */
  snooze: (item: FeedItem, action: SnoozeAction) => Promise<void>;
  /** Bring a snoozed item back. Awaitable. */
  unsnooze: (item: FeedItem) => Promise<void>;
}

interface SnoozeOverride {
  snoozed: boolean;
  busy: boolean;
  error?: string;
}

// Human message for a failed snooze — mirrors the accept / completion taxonomy
// so the board speaks one language. (401 is handled by the caller, not here.)
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
    // #62: ONE owner of "did the server actually answer us". The four hooks
    // each carried their own copy of this predicate, which is precisely why a
    // single incident implicated all four.
    if (isInconclusive(e)) {
      return ACT_UNCONFIRMED_MESSAGE;
    }
    return e.detail || "That couldn't be snoozed.";
  }
  return 'That action failed.';
}

export function useSnooze(opts: UseSnoozeOptions = {}): UseSnoozeResult {
  const { onAuthExpired } = opts;
  const [overrides, setOverridesState] = useState<Record<string, SnoozeOverride>>({});
  // A ref mirror so an in-flight act reverts to the CURRENT state on failure
  // rather than to whatever the closure captured.
  const overridesRef = useRef<Record<string, SnoozeOverride>>({});
  const setOverrides = useCallback(
    (updater: (prev: Record<string, SnoozeOverride>) => Record<string, SnoozeOverride>) => {
      setOverridesState((prev) => {
        const next = updater(prev);
        overridesRef.current = next;
        return next;
      });
    },
    [],
  );

  const run = useCallback(
    async (item: FeedItem, actionId: string, target: boolean) => {
      const cur = overridesRef.current[item.id];
      if (cur?.busy) return;
      const was = cur ? cur.snoozed : false;
      setOverrides((prev) => ({ ...prev, [item.id]: { snoozed: was, busy: true, error: undefined } }));
      try {
        const res = await feedApi.act(item.id, actionId);
        if (res.ok && res.status === 'acted') {
          setOverrides((prev) => ({ ...prev, [item.id]: { snoozed: target, busy: false } }));
          return;
        }
        // Refused, or an idempotent noop. Either way the row does NOT flip on a
        // status we didn't ask for — say what the server said and leave it.
        setOverrides((prev) => ({
          ...prev,
          [item.id]: { snoozed: was, busy: false, error: res.detail || `Couldn't do that (${res.status}).` },
        }));
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 401) {
          onAuthExpired?.();
          setOverrides((prev) => ({ ...prev, [item.id]: { snoozed: was, busy: false } }));
          return;
        }
        setOverrides((prev) => ({ ...prev, [item.id]: { snoozed: was, busy: false, error: messageFor(e) } }));
      }
    },
    [onAuthExpired, setOverrides],
  );

  const snooze = useCallback(
    async (item: FeedItem, action: SnoozeAction) => {
      // Never spend a round trip to be told the kind has no snooze verb. The
      // row shouldn't have offered the control at all in that case, so this is
      // a guard against a caller wiring the hook to the wrong rows — not a
      // path the operator can reach.
      if (!snoozeIsBacked(item)) return;
      await run(item, action, true);
    },
    [run],
  );

  const unsnooze = useCallback(
    async (item: FeedItem) => {
      if (!snoozeIsBacked(item)) return;
      await run(item, UNSNOOZE_ACTION, false);
    },
    [run],
  );

  const snoozed = useCallback((id: string) => overrides[id]?.snoozed ?? false, [overrides]);
  const busy = useCallback((id: string) => overrides[id]?.busy ?? false, [overrides]);
  const errorFor = useCallback((id: string) => overrides[id]?.error ?? null, [overrides]);

  return { snoozed, busy, errorFor, snooze, unsnooze };
}
