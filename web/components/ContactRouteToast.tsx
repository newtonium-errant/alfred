import { useEffect } from 'react';
import { useRouter } from 'next/router';
import { CONSOLE_LABEL, roleChipClass } from '../lib/algernon/consoleTokens';
import { SURFACE_PATHS, type ContactSurface } from '../lib/algernon/contactRouter';
import {
  OVERRIDE_WINDOW_MS,
  overrideChoices,
  useContactRouteToast,
} from '../lib/algernon/useContactRouter';

// The contact-surface router's override layer (C4). The spec: *"Every open, the
// operator can override the rule's surface choice with one tap. Overrides are
// logged (with the triggering state) but do not silently adjust the ruleset."*
//
// ONE TAP PER ALTERNATIVE, not one tap total. A single "wrong" button could only
// record that the router erred, never where the operator actually wanted to be —
// and "you overrode me" is not a pattern anyone can act on. The chips carry the
// destination, so the correction signal names a surface and the pattern card can
// say "you have gone to the deck instead, three times".
//
// MOUNTED IN `_app.tsx`, so it survives the client-side navigation that created
// it (the decision is made on `/`, the toast renders on the destination).
// Renders NOTHING until a routed open publishes one — every other page, and
// every manually-opened session, is byte-unchanged.
//
// It self-dismisses after OVERRIDE_WINDOW_MS: an affordance that sits there all
// session stops reading as "about this open" and starts reading as chrome.

export function ContactRouteToast() {
  const router = useRouter();
  const { toast, dismiss, override } = useContactRouteToast();

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(dismiss, OVERRIDE_WINDOW_MS);
    return () => clearTimeout(timer);
  }, [toast, dismiss]);

  if (!toast) return null;

  const { decision } = toast;
  const choices = overrideChoices(decision.surface);

  const go = (surface: ContactSurface) => {
    // Navigate FIRST, record after: the operator's correction must feel
    // immediate, and `override` is best-effort by construction.
    void router.replace(SURFACE_PATHS[surface]);
    void override(surface);
  };

  return (
    <div
      data-testid="contact-route-toast"
      role="status"
      className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit max-w-[92vw] flex-col gap-2 rounded-xl bg-honeydew-700 px-3.5 py-2.5 text-sm text-cream shadow-card"
    >
      <div className="flex items-center gap-3">
        <span className={CONSOLE_LABEL} data-testid="contact-route-reason">
          {decision.adopted ? 'YOUR DEFAULT' : `RULE · ${decision.rule}`}
        </span>
        <button
          type="button"
          onClick={dismiss}
          data-testid="contact-route-dismiss"
          className="ml-auto font-bold uppercase tracking-wider underline"
        >
          Dismiss
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span>Somewhere else?</span>
        {choices.map((surface) => (
          <button
            key={surface}
            type="button"
            data-testid={`contact-route-override-${surface}`}
            onClick={() => go(surface)}
            className={`rounded-md px-2 py-1 text-xs font-semibold capitalize ${roleChipClass('info')}`}
          >
            {surface}
          </button>
        ))}
      </div>
    </div>
  );
}
