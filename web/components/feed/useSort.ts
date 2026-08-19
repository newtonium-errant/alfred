import { useCallback, useRef, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { ApiError } from '../../lib/algernon/http';
import { ACT_UNCONFIRMED_MESSAGE, isInconclusive } from '../../lib/algernon/actConfirm';
import { SORT_ACTION_BY_SLOT, type BoardSlot } from '../../lib/algernon/feedConstants';

// The SORT action + its optimistic slot flip (2026-08-19). The operator's report:
// four dated tasks under "NOT SORTED YET" with DONE as their only control —
// *"These 'not sorted' items have no way of being sorted."*
//
// Separate from `useSlotAccept` for the same reason `sort_*` is separate from
// `accept` on the server: accept commits an item onto today's TIER list, a sort
// writes the SLOT axis onto its backing record, and the two axes are orthogonal.
// One hook doing both would be the one-control-two-meanings shape the #14 ruling
// forbids.
//
// RENDER-PRESENT GATE, inherited verbatim from `useSlotAccept` and load-bearing
// for the same reason: the optimistic move fires ONLY when the act response
// carries `render`, never on status alone. An older router that does not know
// these verbs, an error shape, or any render-absent response leaves the row
// exactly where it was and the next fetch reconciles. The surface never asserts
// a placement the server did not make.

interface SortOverride {
  /** The slot this row was optimistically moved into, or null. */
  slot: BoardSlot | null;
  busy: boolean;
  error?: string;
}

export interface UseSortOptions {
  onAuthExpired?: () => void;
}

export interface UseSortResult {
  /** The slot this item was optimistically sorted into this session, or null. */
  sortedInto: (id: string) => BoardSlot | null;
  /** Whether a sort is in flight for this item. */
  busy: (id: string) => boolean;
  /** The last sort error for this item, or null. */
  errorFor: (id: string) => string | null;
  /** Record where this item belongs. */
  sort: (item: FeedItem, slot: BoardSlot) => void;
}

// Human message for a failed sort. Mirrors the accept/completion taxonomy so the
// board speaks one language, and every line takes the ITEM as its subject rather
// than the operator — the board's standing voice rule.
function messageFor(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409 || e.code === 'stale_item' || e.code === 'retired') {
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
    if (isInconclusive(e)) {
      return ACT_UNCONFIRMED_MESSAGE;
    }
    if (e.status === 400 || e.code === 'invalid_action') {
      // An older router that does not carry the sort verbs yet.
      return "Sorting isn't wired on this instance yet.";
    }
    // 422 (`unsupported_item`) is the writer's own refusal — a free-text item
    // with no record behind it, a record that has gone. Its detail is a written
    // sentence, so prefer it over anything invented here.
    return e.detail || "That one couldn't be sorted.";
  }
  return 'That action failed.';
}

export function useSort(opts: UseSortOptions = {}): UseSortResult {
  const { onAuthExpired } = opts;
  const [overrides, setOverridesState] = useState<Record<string, SortOverride>>({});
  // A ref mirror so an in-flight sort reverts to the CURRENT slot on failure: a
  // second sort whose POST fails must not undo the first one's placement.
  const overridesRef = useRef<Record<string, SortOverride>>({});
  const setOverrides = useCallback(
    (updater: (prev: Record<string, SortOverride>) => Record<string, SortOverride>) => {
      setOverridesState((prev) => {
        const next = updater(prev);
        overridesRef.current = next;
        return next;
      });
    },
    [],
  );

  const sort = useCallback(
    (item: FeedItem, slot: BoardSlot) => {
      const action = SORT_ACTION_BY_SLOT[slot];
      // Unmapped slot → no POST at all. The picker only offers the three, so
      // this is unreachable through the UI; it is here because a hook that
      // POSTed `undefined` would 400 in the operator's hand for a bug that
      // belongs to the caller.
      if (!action) return;
      const cur = overridesRef.current[item.id];
      const wasSlot = cur ? cur.slot : null;
      setOverrides((prev) => ({ ...prev, [item.id]: { slot: wasSlot, busy: true, error: undefined } }));
      feedApi
        .act(item.id, action)
        .then((res) => {
          // RENDER-PRESENT GATE. The server answers `{slot, sorted: true}`; a
          // response without it leaves the row where it was.
          const placed = res.render != null ? slot : wasSlot;
          setOverrides((prev) => ({ ...prev, [item.id]: { slot: placed, busy: false } }));
        })
        .catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 401) {
            onAuthExpired?.();
            setOverrides((prev) => ({ ...prev, [item.id]: { slot: wasSlot, busy: false } }));
            return;
          }
          setOverrides((prev) => ({ ...prev, [item.id]: { slot: wasSlot, busy: false, error: messageFor(e) } }));
        });
    },
    [onAuthExpired, setOverrides],
  );

  const sortedInto = useCallback((id: string) => overrides[id]?.slot ?? null, [overrides]);
  const busy = useCallback((id: string) => overrides[id]?.busy ?? false, [overrides]);
  const errorFor = useCallback((id: string) => overrides[id]?.error ?? null, [overrides]);

  return { sortedInto, busy, errorFor, sort };
}
