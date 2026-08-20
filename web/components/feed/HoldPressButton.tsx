import { useCallback, useEffect, useRef } from 'react';
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react';
import { GESTURE_HOLD_MS } from '../../lib/algernon/feedConstants';

// The HOLD door for a BUTTON control — the affirm-with-hold-modifier pattern
// on a surface with no drag geometry (backdated completion, 2026-08-20; the
// board's ✓ is the second consumer of the pattern, anchored by VERB rather
// than by swipe direction — see `holdChoicesForVerb`).
//
// SAME CLOCK AS THE FAMILY: `GESTURE_HOLD_MS` (a binding to the #14 hold
// constant — one number, never a second literal). A press held that long
// OPENS the alternatives; a shorter press commits the default on release,
// exactly the band's contract translated to a tap surface: quick semantics
// unchanged, holding is the only new move.
//
// THE CLICK IS SUPPRESSED AFTER A HOLD, and that flag is load-bearing: the
// browser fires `click` after `pointerup` regardless, so without it a hold
// would both open the selector AND fire the quick verb on the card behind it
// — the double-fire the deck's own hold machinery guards against with its
// `gesture already spent itself` flag.
//
// KEYBOARD: an Enter/Space click arrives with no pointerdown, so the quick
// action works from the keyboard unchanged; the hold door is pointer-only,
// like every hold in this family (the selector's choices stay reachable —
// the act itself is never keyboard-gated, only the shortcut into the sheet).
//
// `onHold` null/undefined = a PLAIN button (no timer armed, no behaviour
// change) — the caller passes the family's presence, not a per-kind opinion.

export interface HoldPressButtonProps {
  /** The quick commit — fires on a plain tap/click, never after a hold. */
  onTap: () => void;
  /** Open the alternatives. Absent = this is a plain button. */
  onHold?: (() => void) | null;
  disabled?: boolean;
  className?: string;
  'data-testid'?: string;
  'aria-label'?: string;
  children: ReactNode;
}

export function HoldPressButton({
  onTap,
  onHold,
  disabled,
  className,
  'data-testid': testid,
  'aria-label': ariaLabel,
  children,
}: HoldPressButtonProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const consumedRef = useRef(false);

  const clear = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // A timer armed when the component unmounts mid-press must not fire into a
  // dead closure.
  useEffect(() => clear, [clear]);

  const onPointerDown = useCallback(
    (e: ReactPointerEvent) => {
      if (!onHold || disabled) return;
      // Primary button/touch only — a right-click is the context menu's, not a
      // hold. Checked as "an EXPLICIT non-primary button" rather than
      // `!== 0`: a pointer event without the field (jsdom's generic-event
      // fallback in tests; defensive for exotic UAs) is treated as primary,
      // which is the direction that keeps the control usable.
      if (typeof e.button === 'number' && e.button > 0) return;
      consumedRef.current = false;
      clear();
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        consumedRef.current = true;
        onHold();
      }, GESTURE_HOLD_MS);
    },
    [onHold, disabled, clear],
  );

  const onClick = useCallback(() => {
    if (consumedRef.current) {
      // The press spent itself opening the selector — the trailing click is
      // the same gesture, not a second decision.
      consumedRef.current = false;
      return;
    }
    onTap();
  }, [onTap]);

  return (
    <button
      type="button"
      data-testid={testid}
      aria-label={ariaLabel}
      disabled={disabled}
      className={className}
      onPointerDown={onPointerDown}
      onPointerUp={clear}
      onPointerLeave={clear}
      onPointerCancel={clear}
      onClick={onClick}
      // A touch hold summons the system context menu right where the selector
      // is about to open; suppress it only when a hold is actually wired.
      onContextMenu={onHold ? (e) => e.preventDefault() : undefined}
    >
      {children}
    </button>
  );
}
