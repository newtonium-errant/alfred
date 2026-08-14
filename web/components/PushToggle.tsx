import { usePush } from '../lib/algernon/usePush';

// Minimal push opt-in control (B4). Renders nothing when push is unavailable
// (unsupported browser, or the server has no VAPID keys — the feature is inert),
// so it never shows a dead control. When available it's a single enable/disable
// toggle; a blocked-permissions state explains how to recover.
export function PushToggle() {
  const { status, busy, enable, disable } = usePush();

  // Inert / not-yet-known states render nothing — no dead control.
  if (status === 'checking' || status === 'unsupported' || status === 'unconfigured') {
    return null;
  }

  return (
    <div data-testid="push-toggle" className="mt-6 flex items-center justify-between gap-3 rounded-xl border border-console-edge bg-console-panel px-4 py-3 text-sm shadow-soft">
      <div className="min-w-0">
        <p className="font-semibold text-console-ink">Push notifications</p>
        <p className="text-console-ink-dim">
          {status === 'on'
            // #62 rider (operator-ruled 2026-08-07). The old line —
            // "rings you when something needs a decision" — promised far more
            // than the push policy delivers. Under the strict policy ONLY
            // override-list sender highs ring, and that policy STAYS strict per
            // the operator philosophy memo. So the copy moves to meet the
            // behaviour, not the other way round: a toggle that overstates its
            // reach teaches the operator to expect rings that will never come,
            // and then to distrust the ones that do.
            ? 'On — rings only for urgent email from senders on your override list.'
            : status === 'denied'
              ? 'Blocked in your browser settings. Re-allow notifications for this site to turn them on.'
              : status === 'error'
                ? 'Something went wrong — try again.'
                : 'Ring for urgent email from senders on your override list, even with the app closed.'}
        </p>
      </div>
      {status !== 'denied' && (
        <button
          type="button"
          data-testid="push-toggle-button"
          disabled={busy}
          onClick={() => (status === 'on' ? disable() : enable())}
          className="shrink-0 rounded-lg border border-console-edge-bright px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-console-ink disabled:opacity-50"
        >
          {busy ? '…' : status === 'on' ? 'Turn off' : 'Turn on'}
        </button>
      )}
    </div>
  );
}
