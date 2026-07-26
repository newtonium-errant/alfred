import { useCallback, useEffect, useRef, useState } from 'react';
import { chatApi } from './client';
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

export const NOTIFICATIONS_POLL_MS = 60_000;

export interface UseNotifications {
  notifications: NotificationItem[];
  unread: number;
  /** Mark the given ids read (server-side + local mirror). */
  ack: (ids: string[]) => Promise<void>;
  /** Re-fetch the tray now (bootstrap / after-turn hook-in). */
  refresh: () => Promise<void>;
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

  const ack = useCallback(async (ids: string[]) => {
    if (!ids.length) return;
    try {
      const r = await chatApi.ackNotifications(ids);
      if (!alive.current) return;
      setUnread(r.unread);
      setNotifications((prev) =>
        prev.map((n) => (ids.includes(n.id) ? { ...n, read: true } : n)),
      );
    } catch {
      /* best-effort — the entry stays unread and can be re-acked */
    }
  }, []);

  return { notifications, unread, ack, refresh };
}
