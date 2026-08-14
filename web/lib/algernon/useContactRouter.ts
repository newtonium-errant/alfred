import { useCallback, useEffect, useRef, useState } from 'react';
import type { NextRouter } from 'next/router';
import {
  evaluateRoute,
  isContactSurface,
  type ContactSurface,
  type RouteDecision,
} from './contactRouter';
import { dayApi } from './dayClient';
import { markRouterNavigation } from './composerLog';

// The contact-surface router's EFFECTS half (C4). The decision is pure and lives
// in `contactRouter.ts`; this fetches the state, performs the navigation, logs
// the contact, and publishes the override affordance.
//
// WHY IT HOOKS ON `/` AND NOWHERE ELSE. `/` is the PWA's `start_url`
// (public/manifest.webmanifest), so landing there IS app-open. Every other entry
// is already a deliberate destination — a push tap deep-links through the
// service worker's `notificationclick`, a system share lands on `/share` with a
// payload parked in sessionStorage, a bookmark names its surface. A router
// mounted in `_app.tsx` would fire on all of them and stomp an intent the
// operator had already expressed. Firing only on the landing leaves every deep
// link alone, and needs no coordination with the nine per-page login redirects.
//
// FAIL-SAFE IS "STAY PUT". Every failure path — unconfigured instance, a state
// fetch that 401s or times out, an unroutable decision — results in NO
// navigation. The operator is already somewhere reasonable; moving them on a
// guess is the one outcome with no undo.

/** How long the override affordance stays offered after a routed open. */
export const OVERRIDE_WINDOW_MS = 12_000;

export interface ContactRouteToast {
  decision: RouteDecision;
  /** Empty when the contact could not be logged — override is then unrecordable. */
  contactId: string;
}

type Listener = (toast: ContactRouteToast | null) => void;

// A module-level store rather than React context. `router.replace` is a
// client-side navigation: `_app.tsx` never remounts, so the toast component
// mounted there outlives the page that made the decision. A provider would work
// too, but it would mean wrapping the whole app to carry one nullable value.
let currentToast: ContactRouteToast | null = null;
const listeners = new Set<Listener>();

function publish(toast: ContactRouteToast | null): void {
  currentToast = toast;
  listeners.forEach((fn) => fn(toast));
}

/** Subscribe to the routed-open affordance. Used by `ContactRouteToast`. */
export function useContactRouteToast(): {
  toast: ContactRouteToast | null;
  dismiss: () => void;
  override: (surface: ContactSurface) => Promise<void>;
} {
  const [toast, setToast] = useState<ContactRouteToast | null>(currentToast);

  useEffect(() => {
    const listener: Listener = (next) => setToast(next);
    listeners.add(listener);
    // Re-sync on mount: the decision can be published before this component
    // subscribes (the hook runs on the landing page, this mounts in _app).
    setToast(currentToast);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  const dismiss = useCallback(() => publish(null), []);

  const override = useCallback(
    async (surface: ContactSurface) => {
      const active = currentToast;
      publish(null);
      if (!active?.contactId) return;
      try {
        await dayApi.override(active.contactId, surface);
      } catch {
        // Best-effort, like every other correction-capture path here: a failed
        // log must never block the operator's own navigation. The nav has
        // already been performed by the caller.
      }
    },
    [],
  );

  return { toast, dismiss, override };
}

export interface UseContactRouterOptions {
  /** Gate — the caller passes its own `authed` predicate. */
  enabled: boolean;
  router: NextRouter;
}

/**
 * Evaluate the rule set once per app-open and open the resulting surface.
 *
 * ONCE PER MOUNT, by a ref rather than a dependency list: this must not re-fire
 * when the session resolves, when a refetch lands, or when the operator returns
 * to `/` from elsewhere in the same session. A router that re-ran on resume
 * would yank the operator off whatever they had deliberately opened.
 */
export function useContactRouter(opts: UseContactRouterOptions): void {
  const { enabled, router } = opts;
  const fired = useRef(false);
  // The router is held in a REF and kept OUT of the dependency list — the same
  // technique `useResumeRefetch` uses for its callback, and for a sharper
  // reason here. A caller whose `useRouter()` does not return a stable object
  // would re-run this effect on every render; `fired` stops the second run from
  // doing anything, but the FIRST run's cleanup has already set `cancelled`, so
  // the decision is thrown away mid-flight and the app-open silently never
  // routes. Next's own router is stable today, which is precisely what makes
  // that failure invisible until the day something wraps it.
  const routerRef = useRef(router);
  routerRef.current = router;

  useEffect(() => {
    if (!enabled || fired.current) return;
    fired.current = true;

    let cancelled = false;
    void (async () => {
      let decision: RouteDecision | null = null;
      try {
        const state = await dayApi.state();
        decision = evaluateRoute(state);
      } catch {
        // Fail-safe: no state, no routing. Deliberately silent to the operator
        // (the server logs the read); a toast about a failed router would be
        // noise about a feature that is meant to be invisible when it works.
        return;
      }
      if (cancelled || !decision) return;

      // Record BEFORE navigating: the contact id is what an override attaches
      // to, and the navigation can unmount this effect's caller. Failure here
      // is not fatal — the open still happens, and the toast simply carries no
      // id, so it offers navigation without the (unrecordable) learning.
      let contactId = '';
      try {
        const res = await dayApi.contact(decision.rule, decision.surface);
        contactId = res.contact_id;
      } catch {
        contactId = '';
      }
      if (cancelled) return;

      publish({ decision, contactId });

      // An adopted default (or a rule) may resolve to the surface the operator
      // is already on. Publishing the affordance without navigating is correct:
      // the router DID decide, and the operator can still say it chose wrong.
      const active = routerRef.current;
      if (active.asPath !== decision.path) {
        // Tell the composer telemetry this was us. Without it, every routed
        // open is logged as the operator abandoning the home composer within
        // milliseconds — a false signal in the data that exists to tune the
        // composer's own rule.
        markRouterNavigation();
        void active.replace(decision.path);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [enabled]);
}

/** Test seam: drop any published affordance between cases. */
export function __resetContactRouteToast(): void {
  publish(null);
}

/** The surfaces offered as one-tap overrides, in order. */
export const OVERRIDE_CHOICES: readonly ContactSurface[] = [
  'home',
  'chat',
  'feed',
  'brief',
  'deck',
] as const;

/** The override chips for a decision — every primary surface but the current one. */
export function overrideChoices(surface: string): ContactSurface[] {
  return OVERRIDE_CHOICES.filter(
    (s) => s !== surface && isContactSurface(s),
  );
}
