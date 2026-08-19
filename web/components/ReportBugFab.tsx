import { useCallback, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { ReportBugModal } from './ReportBugModal';
import { CAPTURE_IGNORE_ATTR, captureScreen } from '../lib/algernon/screenCapture';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';
import { cn } from '../lib/utils';

/* --- THE FAB'S FOOTPRINT, AND WHY IT IS A NUMBER ---------------------------
 *
 * The operator photographed the FAB sitting ON TOP of /chat's send button at
 * phone width — the page's primary action, covered by the button you press when
 * the page is already misbehaving.
 *
 * It is `fixed`, so it is out of flow and NOTHING below it knows it is there.
 * The fix is therefore not a nudge to the FAB (every viewport would need its own
 * nudge, and the next page with a bottom-right control would be back here) but a
 * RESERVATION: <main> ends far enough above the viewport floor that no in-flow
 * content can scroll under the FAB's square. Layout owns that reservation
 * because Layout is what mounts the FAB, so the two can never be wired apart.
 *
 * jsdom has no layout engine, so this cannot be pinned in pixels — the same wall
 * `tests/deckLayout.test.tsx` hit for the deck's card stack, and the same answer:
 * pin the RELATIONSHIP between the reservation and the footprint that makes it
 * necessary, and reconcile the class literals against the constants so the two
 * cannot drift apart. `tests/fabClearance.test.tsx` is that pin.
 *
 * THE INSET IS THE LARGER OF THE TWO BREAKPOINTS, deliberately. The button sits
 * at `bottom-4` on phones and `bottom-5` from `sm` up; a reserve computed from
 * the phone value would be 4px short on every wider screen. Reserving the larger
 * costs 4px of empty page and is right at both ends.
 */

/** Tailwind `bottom-5` — the LARGER of the FAB's two breakpoint insets. */
export const FAB_INSET_PX = 20;
/** Tailwind `h-12` / `w-12` — the button is square. */
export const FAB_SIZE_PX = 48;
/** How far up from the viewport floor the FAB's square reaches. */
export const FAB_FOOTPRINT_PX = FAB_INSET_PX + FAB_SIZE_PX;

/**
 * Where the FAB sits. Written out rather than built from the constants above —
 * Tailwind's JIT scans SOURCE TEXT, so a class name assembled at runtime emits
 * no CSS at all. That leaves two sources of truth for the same geometry, which
 * is why the pin reconciles this literal against the constants rather than
 * trusting either alone.
 */
export const FAB_POSITION_CLASS = 'fixed bottom-4 right-4 z-40 sm:bottom-5 sm:right-5';

/** The FAB's own square. Same JIT constraint, same reconciliation. */
export const FAB_SIZE_CLASS = 'h-12 w-12';

/**
 * The reservation Layout puts on <main> so the page's last row clears the FAB.
 *
 * `pb-24` is 96px against a 68px footprint. The margin is not slack for its own
 * sake: a composer's focus ring and a button's shadow both paint OUTSIDE the
 * element box, so a reserve equal to the footprint would clear the geometry and
 * still let the FAB sit on the halo. The pin asserts `>`, not `>=`, for exactly
 * that reason.
 *
 * ORDERING IS LOAD-BEARING AND WAS MEASURED, not assumed: this is appended to a
 * `main` class that already carries `py-4`, and both are single-class
 * specificity, so which one wins is decided by emission order alone. Tailwind
 * emits the padding group `p` → `px`/`py` → `pt`/`pr`/`pb`/`pl`, so `pb-24`
 * lands after `py-4` and wins. Verified by building this project's own config
 * and reading the output (`npx tailwindcss -c tailwind.config.cjs`, 2026-08-19):
 * `.px-3` then `.py-4` then `.pb-24`.
 */
export const FAB_SAFE_PAD_CLASS = 'pb-24';

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
  className,
}: {
  /**
   * The instance whose surface is on screen (#99) — the /chat switcher's
   * current selection, threaded down through `Layout`. Omitted on every surface
   * that has no instance concept of its own, where the reporter is looking at
   * the home app and the home name is the truthful answer.
   */
  viewedInstance?: string;
  /**
   * The SKIN, from Layout's chrome table (`s.fab`).
   *
   * Chrome, not content — which is why it arrives this way rather than through
   * a `ui-*` marker like the panels do. The FAB is mounted by Layout OUTSIDE
   * <main>, and every register's stylesheet deliberately keeps its painting
   * rules off the shell, so no register can reach this button. The chrome table
   * is the seam that is allowed to, and it already knows which surfaces wear the
   * dark hull (`isConsole` is an identity test against the shared chrome object,
   * so this follows the hull automatically rather than naming surfaces).
   *
   * Defaulted to the warm skin so a direct mount in a test renders what it
   * always rendered.
   */
  className?: string;
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
        className={cn(
          FAB_POSITION_CLASS,
          FAB_SIZE_CLASS,
          'flex items-center justify-center rounded-full text-xl shadow-lg transition-colors focus-visible:outline-none focus-visible:ring-2',
          // The warm skin, as the UNMARKED DEFAULT — the same grammar `ui-panel`
          // uses. /share is a warm route and renders this button; a dark default
          // would ship a black circle onto a light page.
          className ?? 'border border-honeydew-300 bg-white text-honeydew-700 hover:bg-honeydew-50 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2',
        )}
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
