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
    <div data-testid="push-toggle" className="mt-6 flex items-center justify-between gap-3 rounded-xl border border-honeydew-200 bg-cream px-4 py-3 text-sm shadow-soft">
      <div className="min-w-0">
        <p className="font-semibold text-honeydew-700">Push notifications</p>
        <p className="text-honeydew-600">
          {status === 'on'
            ? 'On — Algernon rings you when something needs a decision.'
            : status === 'denied'
              ? 'Blocked in your browser settings. Re-allow notifications for this site to turn them on.'
              : status === 'error'
                ? 'Something went wrong — try again.'
                : 'Get a nudge when something needs you, even with the app closed.'}
        </p>
      </div>
      {status !== 'denied' && (
        <button
          type="button"
          data-testid="push-toggle-button"
          disabled={busy}
          onClick={() => (status === 'on' ? disable() : enable())}
          className="shrink-0 rounded-lg border border-honeydew-400 px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
        >
          {busy ? '…' : status === 'on' ? 'Turn off' : 'Turn on'}
        </button>
      )}
    </div>
  );
}
