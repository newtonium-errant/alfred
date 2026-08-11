import { useCallback, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { ReportBugModal } from './ReportBugModal';
import { CAPTURE_IGNORE_ATTR, captureScreen } from '../lib/algernon/screenCapture';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

/**
 * The discreet floating "Report a bug" button, plus the dialog it opens (#95).
 *
 * Mounted from `Layout`, so it is available on every authed surface without
 * each page remembering to add it.
 *
 * ON OPEN IT SNAPSHOTS THE CURRENT SCREEN BEFORE THE DIALOG RENDERS, so the
 * capture shows the broken thing the operator is looking at rather than the
 * dialog sitting on top of it. Both the button and the dialog carry
 * `data-report-ignore`, so a retake taken while the dialog is open still
 * photographs the page behind it.
 *
 * THE DIALOG OPENS IMMEDIATELY and the shot streams in as a prop when ready.
 * Waiting on the capture would mean the button appears to do nothing for up to
 * eight seconds, which is the worst possible behaviour for the control someone
 * reaches for when the app is already misbehaving.
 */
export function ReportBugFab({
  viewedInstance,
}: {
  /**
   * The instance whose surface is on screen (#99) — the /chat switcher's
   * current selection, threaded down through `Layout`. Omitted on every surface
   * that has no instance concept of its own, where the reporter is looking at
   * the home app and the home name is the truthful answer.
   */
  viewedInstance?: string;
} = {}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  // The route frozen AT OPEN, so navigating afterwards can't change the
  // breadcrumb attached to this report.
  const [openRoute, setOpenRoute] = useState('');
  // The viewed instance is frozen at open for the SAME reason as the route:
  // both describe the screen the reporter was looking at when they reached for
  // the button, and a switcher flipped while the dialog is up must not rewrite
  // the report's account of where they were.
  const [openInstance, setOpenInstance] = useState('');
  const [shot, setShot] = useState<Blob | null>(null);
  const [capturing, setCapturing] = useState(false);
  // Focus returns here when the dialog closes (a11y).
  const fabRef = useRef<HTMLButtonElement>(null);

  const handleOpen = useCallback(async () => {
    if (capturing || open) return;
    setOpenRoute(router.asPath);
    // Falls back to the home name — never to an empty string, which the box
    // would record as "(unset)" and read as a capture failure rather than as
    // "this surface has no instance of its own".
    setOpenInstance(viewedInstance?.trim() || HOME_INSTANCE_NAME);
    setShot(null);
    setCapturing(true);
    setOpen(true);
    const blob = await captureScreen();
    setShot(blob);
    setCapturing(false);
  }, [capturing, open, router.asPath, viewedInstance]);

  /**
   * Retake. Returns the new blob (or `null` on failure/timeout) and lets the
   * MODAL decide what to do with it — it keeps the prior shot on `null` rather
   * than nuking it. The FAB's own `shot` is synced only on SUCCESS, so
   * `initialShot` can never regress to null underneath the dialog.
   */
  const handleRetake = useCallback(async (): Promise<Blob | null> => {
    if (capturing) return null;
    setCapturing(true);
    const blob = await captureScreen();
    if (blob) setShot(blob);
    setCapturing(false);
    return blob;
  }, [capturing]);

  const handleClose = useCallback(() => {
    setOpen(false);
    setShot(null);
    // Reset `capturing` too: closing mid-capture would otherwise leave the
    // button disabled (handleOpen bails on `capturing`) until the 8s timeout.
    // The orphaned capture's late setShot is harmless — the next open resets
    // `shot` to null first.
    setCapturing(false);
    fabRef.current?.focus();
  }, []);

  return (
    <>
      {/* data-report-ignore keeps the button itself out of the snapshot. */}
      <button
        ref={fabRef}
        type="button"
        data-testid="report-bug-fab"
        {...{ [CAPTURE_IGNORE_ATTR]: '' }}
        aria-label="Report a bug"
        title="Report a bug"
        onClick={() => void handleOpen()}
        className="fixed bottom-4 right-4 z-40 flex h-12 w-12 items-center justify-center rounded-full border border-honeydew-300 bg-white text-xl text-honeydew-700 shadow-lg transition-colors hover:bg-honeydew-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2 sm:bottom-5 sm:right-5"
      >
        <span aria-hidden="true">🐛</span>
      </button>

      {open && (
        // data-report-ignore so a retake photographs the page, not the dialog.
        <div {...{ [CAPTURE_IGNORE_ATTR]: '' }}>
          <ReportBugModal
            route={openRoute}
            viewedInstance={openInstance}
            initialShot={shot}
            capturing={capturing}
            onRetake={handleRetake}
            onClose={handleClose}
          />
        </div>
      )}
    </>
  );
}
