import { useMemo } from 'react';
import {
  extentProgress,
  formatTimeExtent,
  readTimeExtent,
  type ExtentFormatOptions,
  type HasExtent,
} from '../../lib/algernon/timeExtent';

// D7 made visible: an interval RENDERS AS ITS EXTENT.
//
// Portable on purpose. The Decide deck is the first surface to mount it, but
// the two producers that stamp extents today (`event`, `weather`) are both FYI
// kinds, so the Awareness feed is where this has real data — it takes the same
// component and the same tokens.
//
// The BAR is the part that earns the ruling. A label saying "11:00 – 15:00"
// is still a timestamp with extra characters; a bar that is two hours into its
// four shows the shape of the thing. Progress is clamped, so a window that has
// not opened reads empty and one that has closed reads full.

export interface TimeExtentProps {
  item: HasExtent | null | undefined;
  /** Injected by tests so assertions don't depend on the runner's clock or zone. */
  format?: ExtentFormatOptions;
  className?: string;
}

export function TimeExtent({ item, format, className }: TimeExtentProps) {
  const read = useMemo(() => readTimeExtent(item), [item]);
  const shown = useMemo(() => formatTimeExtent(read, format), [read, format]);
  const progress = useMemo(() => extentProgress(read, format?.now), [read, format?.now]);

  // No time dimension → no chrome at all. Most feed items have no time, and a
  // row of empty slots would bury the ones that do.
  if (shown === null) return null;

  const unreadable = read.kind === 'unparseable';

  return (
    <div
      data-testid="time-extent"
      data-extent-kind={read.kind}
      className={`flex shrink-0 items-center gap-2 ${className ?? ''}`}
    >
      <span
        data-testid="time-extent-label"
        title={shown.description}
        className={`font-mono text-[11px] tabular-nums ${
          unreadable ? 'text-caution' : 'text-console-ink-dim'
        }`}
      >
        {shown.label}
      </span>

      {progress !== null && (
        // The extent itself. aria-hidden because the label + duration already
        // say everything this shows — a screen reader gets the sentence, not a
        // meterless bar.
        <span
          aria-hidden="true"
          data-testid="time-extent-bar"
          data-progress={progress.toFixed(3)}
          className="relative h-[3px] min-w-[42px] flex-1 overflow-hidden bg-console-edge"
        >
          <span
            className="absolute inset-y-0 left-0 bg-info"
            style={{ width: `${(progress * 100).toFixed(1)}%` }}
          />
        </span>
      )}

      {shown.duration && (
        <span
          data-testid="time-extent-duration"
          className="font-mono text-[10px] font-bold tabular-nums text-console-ink-faint"
        >
          {shown.duration}
        </span>
      )}

      {/* The screen-reader sentence. The visual label uses an en dash, which is
          announced as "to" by some engines and as nothing by others. */}
      <span className="sr-only">{shown.description}</span>
    </div>
  );
}
