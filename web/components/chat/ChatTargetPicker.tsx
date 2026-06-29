import { Label } from '../ui/label';
import { cn } from '../../lib/utils';
import type { ChatTarget } from '../../lib/algernon/types';

// The instance switcher — single-select over the configured chat instances (home
// + cross-instance relay targets). Clearly shows which assistant is active.
// Renders NOTHING when only the home instance is configured (a single-instance
// deploy needs no picker). Native <select> styled to match ui/input (no select
// primitive in the kit), mirroring ingest/TargetPicker.
const selectClass = cn(
  'rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-900',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-1',
  'disabled:cursor-default disabled:opacity-70',
);

export function ChatTargetPicker({
  targets,
  instance,
  onInstanceChange,
  disabled = false,
}: {
  targets: ChatTarget[];
  instance: string;
  onInstanceChange: (name: string) => void;
  disabled?: boolean;
}) {
  // Single-instance deploy: no switcher to show.
  if (targets.length <= 1) return null;

  return (
    <div className="flex flex-col gap-1.5" data-testid="chat-target-picker">
      <Label htmlFor="chat-target">Assistant</Label>
      <select
        id="chat-target"
        data-testid="chat-target"
        className={selectClass}
        value={instance}
        disabled={disabled}
        onChange={(e) => onInstanceChange(e.target.value)}
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
