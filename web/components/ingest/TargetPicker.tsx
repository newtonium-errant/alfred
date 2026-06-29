import { Label } from '../ui/label';
import { cn } from '../../lib/utils';
import type { IngestTarget } from '../../lib/algernon/types';

// The instance + record-type selectors. The record-type options are CONSTRAINED
// by the chosen target (each target advertises its own recordTypes), so the
// picker can never offer a type the target's scope would reject. Native <select>
// styled to match ui/input (no select primitive in the kit).
const selectClass = cn(
  'w-full rounded-xl border border-honeydew-300 bg-white px-3 py-2.5 text-base text-honeydew-900',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-1',
  'disabled:cursor-default disabled:opacity-70',
);

export function TargetPicker({
  targets,
  target,
  recordType,
  onTargetChange,
  onRecordTypeChange,
  disabled = false,
}: {
  targets: IngestTarget[];
  target: string;
  recordType: string;
  onTargetChange: (name: string) => void;
  onRecordTypeChange: (type: string) => void;
  disabled?: boolean;
}) {
  const selected = targets.find((t) => t.name === target);
  const recordTypes = selected?.recordTypes ?? [];

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ingest-target">Target instance</Label>
        <select
          id="ingest-target"
          data-testid="ingest-target"
          className={selectClass}
          value={target}
          disabled={disabled || targets.length === 0}
          onChange={(e) => onTargetChange(e.target.value)}
        >
          {targets.map((t) => (
            <option key={t.name} value={t.name}>
              {t.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ingest-record-type">Record type</Label>
        <select
          id="ingest-record-type"
          data-testid="ingest-record-type"
          className={selectClass}
          value={recordType}
          disabled={disabled || recordTypes.length === 0}
          onChange={(e) => onRecordTypeChange(e.target.value)}
        >
          {recordTypes.map((rt) => (
            <option key={rt} value={rt}>
              {rt}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
