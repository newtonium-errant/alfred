import { Button } from '../ui/button';
import type { ExtractionOffer } from '../../lib/algernon/useChat';

// The unobtrusive capture toggle (R1, 2026-08-20). The operator's words:
// "an unobtrusive capture button. It was useful when I didn't want to be
// interrupted. Perhaps even a way to toggle it on or off mid conversation
// when I do want a response."
//
// PLACEMENT: near the composer but OUT of its primary action path — this row
// sits directly above the composer, right-aligned, never inside the
// [Attach|input|Send] form row. Deliberately NOT a floating bottom-right
// control: that square belongs to the bug-report FAB, and the FAB/Send
// overlap incident (fixed 0c2015e0) is why no second fixed control goes
// there.
//
// ILB IS LOAD-BEARING HERE: while capture is ON the assistant's silence is
// the FEATURE, and the persistent indicator strip is what makes that silence
// legible — the operator must never wonder whether the assistant is broken
// or capturing. The strip renders whenever capture is on and never
// otherwise (a structural pin holds both directions).
//
// The extraction offer is a QUIET chip, never a modal: it appears after a
// toggle-off closes a non-empty span (and re-appears on refresh while the
// span stays unextracted), and dismissing it is safe by design — the
// backend finalizes unextracted spans when the conversation closes.
export function CaptureBar({
  active,
  busy,
  disabled,
  instanceLabel,
  offer,
  extracting,
  onToggle,
  onExtract,
  onDismissOffer,
}: {
  /** Server-truth capture state (useChat.captureActive). */
  active: boolean;
  /** A toggle round-trip is in flight — the button disables. */
  busy: boolean;
  /** Page-level disable (booting). */
  disabled?: boolean;
  /** The active assistant's display label (the indicator names who is quiet). */
  instanceLabel: string;
  offer: ExtractionOffer | null;
  extracting: boolean;
  onToggle: () => void;
  onExtract: () => void;
  onDismissOffer: () => void;
}) {
  return (
    <div className="flex flex-col gap-2" data-testid="capture-bar">
      <div className="flex items-center justify-end gap-2">
        {active && (
          // The persistent capture-on indicator — quiet, but unambiguous
          // and always visible while capture is on. role="status" so the
          // state change is announced once without interrupting.
          <p
            role="status"
            data-testid="capture-indicator"
            className="flex-1 rounded-xl bg-console-raise px-3 py-2 text-sm text-console-ink"
          >
            <span aria-hidden="true">● </span>
            Capturing — {instanceLabel} is receiving, not replying. Toggle
            off when you want a response.
          </p>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="capture-toggle"
          aria-pressed={active}
          aria-label={
            active
              ? 'Stop capturing and resume replies'
              : 'Start capture — send material without getting replies'
          }
          disabled={disabled || busy}
          onClick={onToggle}
        >
          {active ? 'Stop capturing' : 'Capture'}
        </Button>
      </div>

      {offer && (
        // The quiet extraction offer chip (never a modal interrupt).
        <div
          role="status"
          data-testid="capture-extract-offer"
          className="flex items-center justify-between gap-3 rounded-xl bg-console-raise px-3 py-2 text-sm text-console-ink"
        >
          <span data-testid="capture-extract-offer-text">
            {extracting
              ? 'Extracting…'
              : `Captured ${offer.turns} message${offer.turns === 1 ? '' : 's'}. Extract notes now?`}
          </span>
          <span className="flex shrink-0 items-center gap-2">
            <Button
              type="button"
              size="sm"
              data-testid="capture-extract-accept"
              disabled={extracting}
              onClick={onExtract}
            >
              {extracting ? 'Extracting…' : 'Extract'}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              data-testid="capture-extract-dismiss"
              disabled={extracting}
              onClick={onDismissOffer}
            >
              Not now
            </Button>
          </span>
        </div>
      )}

    </div>
  );
}
