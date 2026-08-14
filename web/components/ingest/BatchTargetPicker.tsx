import { Label } from '../ui/label';
import { cn } from '../../lib/utils';
import type { BatchTarget } from '../../lib/algernon/types';

// The instance switcher for bulk scan upload (#90) — a deliberate copy of
// ChatTargetPicker's shape so the two selectors read as the same control in two
// places rather than two controls that happen to look alike.
//
// Renders NOTHING when only one target is configured: a single-instance deploy
// needs no picker, and a select with one option is a control that cannot be
// used. The page still states WHERE the batch is going in that case — the
// absence of a picker must not mean the destination goes unsaid.
const selectClass = cn(
  // Same register seam as ui/input — a native <select> with no kit primitive
  // still opts in by marker, so the crt/comms shells reach it too.
  'ui-field',
  'rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-900',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-1',
  'disabled:cursor-default disabled:opacity-70',
);

export function BatchTargetPicker({
  targets,
  target,
  onTargetChange,
  disabled = false,
}: {
  targets: BatchTarget[];
  target: string;
  onTargetChange: (name: string) => void;
  disabled?: boolean;
}) {
  if (targets.length <= 1) return null;

  return (
    <div className="flex flex-col gap-1.5" data-testid="batch-target-picker">
      <Label htmlFor="batch-target">Send to</Label>
      <select
        id="batch-target"
        data-testid="batch-target"
        className={selectClass}
        value={target}
        disabled={disabled}
        onChange={(e) => onTargetChange(e.target.value)}
      >
        {targets.map((t) => (
          <option key={t.name} value={t.name}>
            {t.label}
          </option>
        ))}
      </select>
    </div>
  );
}
