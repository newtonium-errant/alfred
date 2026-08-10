import { useEffect, useRef } from 'react';

// #62 defect (3) — a resumed PWA must not render last night's DOM.
//
// iOS suspends a backgrounded PWA rather than unloading it. Reopening restores
// the JS heap intact: same component tree, same state, same rendered rows, no
// mount and no fetch. The 2026-08-07 incident's most visible symptom was this
// one — at 11:43 the operator was looking at a deck rendered at 23:43 the night
// before, still showing an item as pending twelve hours after it completed.
//
// `visibilitychange` alone is not enough. Safari fires `pageshow` with
// `persisted: true` for a bfcache restore, and that path can skip
// `visibilitychange` entirely, so both are needed to cover a real resume.

/** Minimum gap between refetches. Rapid app-switching fires these events in
 *  bursts; without a floor a few flicks would queue a request each. */
export const RESUME_REFETCH_MIN_INTERVAL_MS = 3_000;

/**
 * Refetch when the page becomes visible again.
 *
 * `refetch` is held in a ref so a caller passing an inline closure does not
 * detach and re-attach the listeners on every render — that churn is invisible
 * until a resume arrives during it and lands on a listener that is being torn
 * down.
 */
export function useResumeRefetch(
  refetch: () => void,
  opts: { minIntervalMs?: number; enabled?: boolean } = {},
): void {
  const { minIntervalMs = RESUME_REFETCH_MIN_INTERVAL_MS, enabled = true } = opts;
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;
  const lastAtRef = useRef(0);

  useEffect(() => {
    if (!enabled || typeof document === 'undefined') return;

    const maybeRefetch = () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      if (now - lastAtRef.current < minIntervalMs) return;
      lastAtRef.current = now;
      refetchRef.current();
    };

    document.addEventListener('visibilitychange', maybeRefetch);
    window.addEventListener('pageshow', maybeRefetch);
    return () => {
      document.removeEventListener('visibilitychange', maybeRefetch);
      window.removeEventListener('pageshow', maybeRefetch);
    };
  }, [enabled, minIntervalMs]);
}
