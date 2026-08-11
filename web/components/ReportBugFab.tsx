import { useCallback, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { ReportBugModal } from './ReportBugModal';
import { CAPTURE_IGNORE_ATTR, captureScreen } from '../lib/algernon/screenCapture';

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
export function ReportBugFab() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  // The route frozen AT OPEN, so navigating afterwards can't change the
  // breadcrumb attached to this report.
  const [openRoute, setOpenRoute] = useState('');
  const [shot, setShot] = useState<Blob | null>(null);
  const [capturing, setCapturing] = useState(false);
  // Focus returns here when the dialog closes (a11y).
  const fabRef = useRef<HTMLButtonElement>(null);

  const handleOpen = useCallback(async () => {
    if (capturing || open) return;
    setOpenRoute(router.asPath);
    setShot(null);
    setCapturing(true);
    setOpen(true);
    const blob = await captureScreen();
    setShot(blob);
    setCapturing(false);
  }, [capturing, open, router.asPath]);

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
