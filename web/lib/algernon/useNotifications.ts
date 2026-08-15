import { useCallback, useEffect, useRef, useState } from 'react';
import { refusalReason } from './actConfirm';
import { chatApi } from './client';
import { ApiError } from './http';
import type { NotificationItem } from './types';

// Notification tray hook (parity #22, POLL / READ-ON-REQUEST slice).
//
// Refresh cadence — poll-only, NO push channel (deferred):
//   * on bootstrap (once `enabled` flips true),
//   * whenever the caller invokes `refresh()` (index.tsx calls it after each
//     completed turn),
//   * a FOCUSED-ONLY interval poll (~60s) — skipped while the tab is hidden.
//
// LIMITATION (documented by design): this covers the OPEN app only. A closed
// or backgrounded PWA receives nothing until reopened/refocused — the Telegram
// relay remains the closed-app channel; a web push channel is a later slice.
//
// Poll failures are swallowed (the tray is an enhancement — a flaky poll must
// never surface as a chat error); the next tick retries. Ack is optimistic on
// success only (state updates from the server's authoritative unread count).
//
// ── A POLL MAY BE SWALLOWED. AN ACT MAY NOT. ────────────────────────────────
// The two are opposite cases and the distinction is the whole of the fix below.
// A poll is the system asking on its own initiative: no one is waiting on the
// answer, a stale tray is harmless, and the next tick fixes it. An ACK or a
// DISMISS is the OPERATOR asking, once, by tapping — and both used to end in a
// bare `catch {}`. The pill stayed unread, the row stayed on screen, and
// nothing said why: indistinguishable from a control that does not work, which
// is how an operator learns to stop trusting one.
//
// So a failed act now records a per-id line the tray renders in place. Same
// honesty as the deck's ledger and the board's notice, at the scale this
// surface needs — and DELIBERATELY smaller. Those two write to storage because
// their write is DEFERRED and its answer can arrive after the component is
// gone; nothing here is deferred (the operator is looking at the row they
// tapped when the answer lands), so hook state is the honest scale and a store
// would be machinery for a failure mode this surface does not have.

export const NOTIFICATIONS_POLL_MS = 60_000;

/**
 * WHY an act did not stick, in the server's own words where it had any.
 *
 * Shared with the deck, the board and the batch door (`refusalReason`), so a
 * refusal reads the same wherever the operator meets it.
 */
function why(e: unknown): string {
  return e instanceof ApiError ? refusalReason(e) : 'the request did not get through';
}

export interface UseNotifications {
  notifications: NotificationItem[];
  unread: number;
  /** Mark the given ids read (server-side + local mirror). */
  ack: (ids: string[]) => Promise<void>;
  /** Clear the given ids from the tray (#86) — server-side + local removal. */
  dismiss: (ids: string[]) => Promise<void>;
  /** Re-fetch the tray now (bootstrap / after-turn hook-in). */
  refresh: () => Promise<void>;
  /**
   * The acts that failed, by notification id — ACCUMULATED, not replaced.
   *
   * Keyed per id rather than held as one "last error" because the tray offers
   * a bulk Dismiss-all: one tap can fail for twenty rows, and a single shared
   * line would report that as one problem while nineteen rows sat there
   * unexplained. An entry clears when the same id is acted on again and lands.
   */
  failures: Record<string, string>;
}

export function useNotifications({ enabled }: { enabled: boolean }): UseNotifications {
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  // The mounted guard: a poll resolving after unmount must not setState.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    try {
      const r = await chatApi.notifications();
      if (!alive.current) return;
      setNotifications(r.notifications);
      setUnread(r.unread);
    } catch {
      /* best-effort poll — keep the last-known tray, retry next tick */
    }
  }, [enabled]);

  // Bootstrap fetch + the focused-only interval poll.
  useEffect(() => {
    if (!enabled) return;
    void refresh();
    const timer = setInterval(() => {
      // Focused-only: don't burn polls (or battery) in a hidden tab. A
      // hidden→visible transition is caught by the NEXT tick at worst.
      if (typeof document === 'undefined' || document.visibilityState === 'visible') {
        void refresh();
      }
    }, NOTIFICATIONS_POLL_MS);
    return () => clearInterval(timer);
  }, [enabled, refresh]);

  const [failures, setFailures] = useState<Record<string, string>>({});

  // One sentence, applied to every id the failed call covered.
  const noteFailure = useCallback((ids: string[], sentence: string) => {
    setFailures((prev) => {
      const next = { ...prev };
      for (const id of ids) next[id] = sentence;
      return next;
    });
  }, []);

  // The act landed this time — the debt those rows were carrying is settled.
  // Written as a no-op-preserving update so a success on rows that never failed
  // does not churn state on every tap.
  const clearFailures = useCallback((ids: string[]) => {
    setFailures((prev) => {
      if (!ids.some((id) => id in prev)) return prev;
      const next = { ...prev };
      for (const id of ids) delete next[id];
      return next;
    });
  }, []);

  const ack = useCallback(
    async (ids: string[]) => {
      if (!ids.length) return;
      try {
        const r = await chatApi.ackNotifications(ids);
        if (!alive.current) return;
        setUnread(r.unread);
        setNotifications((prev) =>
          prev.map((n) => (ids.includes(n.id) ? { ...n, read: true } : n)),
        );
        clearFailures(ids);
      } catch (e) {
        if (!alive.current) return;
        // The row is untouched — it is still unread and still tappable — and
        // now it SAYS so. The old comment here promised exactly this ("can be
        // re-acked") to a reader of the source; the operator was told nothing.
        noteFailure(ids, `Couldn’t mark that read: ${why(e)}. It’s still unread — try again.`);
      }
    },
    [clearFailures, noteFailure],
  );

  const dismiss = useCallback(
    async (ids: string[]) => {
      if (!ids.length) return;
      try {
        const r = await chatApi.dismissNotifications(ids);
        if (!alive.current) return;
        setUnread(r.unread);
        // REMOVED locally, not flagged. The server has already stopped listing
        // these, so leaving them on screen until the next poll would show the
        // operator a row that no longer exists — and this whole feature is about
        // a row that would not go away.
        setNotifications((prev) => prev.filter((n) => !ids.includes(n.id)));
        clearFailures(ids);
      } catch (e) {
        if (!alive.current) return;
        // NOT removed, and that is the point: the row stays because the server
        // still lists it. On this surface — the one built because a row would
        // not go away — a dismiss that silently fails is the original complaint
        // wearing the fix's clothes.
        noteFailure(ids, `Couldn’t clear that: ${why(e)}. It’s still here — try again.`);
      }
    },
    [clearFailures, noteFailure],
  );

  return { notifications, unread, ack, dismiss, refresh, failures };
}
