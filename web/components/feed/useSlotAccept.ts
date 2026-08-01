import { useCallback, useRef, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { ApiError } from '../../lib/algernon/http';

// The C2 slot-ACCEPT action + optimistic committed flip. Separate from
// useRingCompletion (done / undo_done) because accept is a distinct verb whose
// optimistic target is candidate → committed/PLANNED, driven by the act response's
// `render` payload. DOM-free + driven by a mocked feedApi so the render-present gate,
// the ActResult error routing, and the optimistic flip are unit-pinned; the
// feed-row / rings-panel surfaces read `accepted(id)` to override a SUGGESTED item's
// stage to planned on success.
//
// RENDER-PRESENT GATE (the load-bearing rule, per team-lead): the optimistic flip
// fires ONLY when the act response carries `render` — never on status alone. An older
// router, a non-accept action, an `already_acted` noop, or an error shape has NO
// render → NO flip; the next feed fetch reconciles (never a lie). This is also the
// forward-compat seam: if the heavy review adjusts the backend, the flip silently
// degrades to a refetch rather than asserting a committed state that didn't happen.

export const SLOT_ACTION_ACCEPT = 'accept';

interface AcceptOverride {
  committed: boolean;
  busy: boolean;
  error?: string;
}

export interface UseSlotAcceptOptions {
  onAuthExpired?: () => void;
}

export interface UseSlotAcceptResult {
  /** Whether this item has been optimistically accepted (committed) this session. */
  accepted: (id: string) => boolean;
  /** Whether an accept is in flight for this item. */
  busy: (id: string) => boolean;
  /** The last accept error for this item, or null. */
  errorFor: (id: string) => string | null;
  /** Accept a suggested item — commit it onto today's plan. */
  accept: (item: FeedItem) => void;
}

// Human message for a failed accept — mirrors the deck / completion taxonomy so the
// board speaks one language. (401 is handled by the caller, not here.)
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
      // The provenance guard: accept on a non-candidate (already committed), or an
      // older router that doesn't know `accept` yet.
      return "That's already on today's plan.";
    }
    // 422 error (writer refusal — invalid_tier / thin_evidence): the resolver's line.
    return e.detail || "That couldn't be added to today's plan.";
  }
  return 'That action failed.';
}

export function useSlotAccept(opts: UseSlotAcceptOptions = {}): UseSlotAcceptResult {
  const { onAuthExpired } = opts;
  const [overrides, setOverridesState] = useState<Record<string, AcceptOverride>>({});
  // A ref mirror so an in-flight accept reverts to the CURRENT committed state on
  // failure (a double-accept whose second POST fails must not un-commit the first).
  const overridesRef = useRef<Record<string, AcceptOverride>>({});
  const setOverrides = useCallback(
    (updater: (prev: Record<string, AcceptOverride>) => Record<string, AcceptOverride>) => {
      setOverridesState((prev) => {
        const next = updater(prev);
        overridesRef.current = next;
        return next;
      });
    },
    [],
  );

  const accept = useCallback(
    (item: FeedItem) => {
      const cur = overridesRef.current[item.id];
      const wasCommitted = cur ? cur.committed : false;
      // Enter busy WITHOUT flipping committed yet — the green/planned flip lands on
      // the render-bearing success response, never on the optimistic tap.
      setOverrides((prev) => ({ ...prev, [item.id]: { committed: wasCommitted, busy: true, error: undefined } }));
      feedApi
        .act(item.id, SLOT_ACTION_ACCEPT)
        .then((res) => {
          // RENDER-PRESENT GATE: flip to committed ONLY when the response carries the
          // render payload (a genuine accept success). already_acted / render-absent
          // shapes → no flip; the next fetch reconciles.
          const committed = res.render != null ? true : wasCommitted;
          setOverrides((prev) => ({ ...prev, [item.id]: { committed, busy: false } }));
        })
        .catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 401) {
            onAuthExpired?.();
            setOverrides((prev) => ({ ...prev, [item.id]: { committed: wasCommitted, busy: false } }));
            return;
          }
          setOverrides((prev) => ({ ...prev, [item.id]: { committed: wasCommitted, busy: false, error: messageFor(e) } }));
        });
    },
    [onAuthExpired, setOverrides],
  );

  const accepted = useCallback((id: string) => overrides[id]?.committed ?? false, [overrides]);
  const busy = useCallback((id: string) => overrides[id]?.busy ?? false, [overrides]);
  const errorFor = useCallback((id: string) => overrides[id]?.error ?? null, [overrides]);

  return { accepted, busy, errorFor, accept };
}
