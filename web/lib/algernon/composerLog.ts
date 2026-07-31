import { useEffect, useRef } from 'react';
import type { NextRouter } from 'next/router';
import type { ComposeMode } from './composer';

// CLIENT-side home-composer telemetry (B3-3). Fire-and-forget POSTs to the BFF
// /api/feed/composer-log — capture-only; the learning is Phase D. Extracted from
// the page so the mount/navigate-away wiring is unit-testable without a full
// page render.

// The window (ms) after composing within which a navigation-away is attributed
// to the composition (a quick bounce = the composed view didn't hold the user).
export const COMPOSER_NAV_AWAY_WINDOW_MS = 10_000;

/**
 * POST one composer-log event. Swallows EVERY error — telemetry must never break
 * the page. keepalive lets a navigated_away beacon survive the route change/unload.
 */
export function postComposerLog(body: {
  rule: ComposeMode;
  event: 'composed' | 'navigated_away';
  dwell_ms?: number;
  path?: string;
}): void {
  try {
    void fetch('/api/feed/composer-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {
      /* best-effort */
    });
  } catch {
    /* telemetry must never break the page */
  }
}

/**
 * Log 'composed' once when the mode resolves, then 'navigated_away' on the FIRST
 * route change within COMPOSER_NAV_AWAY_WINDOW_MS of composing. mode === null
 * (still resolving) logs nothing.
 */
export function useComposerLog(mode: ComposeMode | null, router: NextRouter): void {
  const composedAt = useRef<number | null>(null);
  const firedAway = useRef(false);

  useEffect(() => {
    if (mode == null || composedAt.current != null) return;
    composedAt.current = Date.now();
    postComposerLog({ rule: mode, event: 'composed' });
  }, [mode]);

  useEffect(() => {
    if (mode == null) return;
    const onRouteChange = (url: string) => {
      const start = composedAt.current;
      if (start == null || firedAway.current) return;
      const dwell = Date.now() - start;
      if (dwell <= COMPOSER_NAV_AWAY_WINDOW_MS) {
        firedAway.current = true;
        postComposerLog({ rule: mode, event: 'navigated_away', dwell_ms: dwell, path: url.slice(0, 200) });
      }
    };
    router.events.on('routeChangeStart', onRouteChange);
    return () => router.events.off('routeChangeStart', onRouteChange);
  }, [mode, router]);
}
