import type { ReactNode } from 'react';
import { cn } from '../lib/utils';

// The empty-state system (roadmap step 3; docs/design/design-language.md Part 2
// "Empty states" + Part 3 principle 4 "warmth is structural"). One reusable,
// warm, non-punitive empty: never a zero, a deficit, or a "you haven't" — a
// warmth surface, not a blank. No red anywhere; honeydew tokens; rounded.
//
// Two tiers:
//   * RICH (default): a centered column with generous padding — a friendly icon
//     in a soft honeydew circle, an optional title, the message, and an optional
//     forward ACTION button. Used on the major surfaces where a clear next step
//     exists (projects, supplies, tools, brainstorm).
//   * COMPACT: a single warm inline line (small leading icon + message), for
//     minor sub-list empties (purchases, time logs, parts, draft sub-lists) —
//     matches today's inline look, just normalized.
//
// THE ICON SLOT IS THE MASCOT SLOT. Today callers pass an emoji (🌱 🧰 🕘 …),
// but `icon` is a ReactNode, so the future Mel mascot component (engagement idea
// 8a, rename-gated) drops straight in here with ZERO restructure at the call
// sites — they already hand this slot a node.
//
// `testId` is forwarded onto the element the e2e already targets (the message
// element), so every existing data-testid is preserved exactly when a bare <p>
// empty state is replaced by <EmptyState>.
export function EmptyState({
  icon,
  title,
  message,
  action,
  testId,
  compact = false,
  className,
}: {
  icon?: ReactNode;
  title?: string;
  message: ReactNode;
  action?: ReactNode;
  testId?: string;
  compact?: boolean;
  className?: string;
}) {
  if (compact) {
    // A single warm inline line. The testid stays on the text element (the e2e
    // target). The icon is decorative (aria-hidden by convention at the call
    // site, or just an emoji span here).
    return (
      <p
        data-testid={testId}
        className={cn('flex items-center gap-2 text-sm text-honeydew-600', className)}
      >
        {icon != null && (
          <span aria-hidden="true" className="shrink-0">
            {icon}
          </span>
        )}
        <span>{message}</span>
      </p>
    );
  }

  return (
    <div
      className={cn(
        'flex flex-col items-center gap-3 px-6 py-8 text-center',
        className
      )}
    >
      {icon != null && (
        // The soft honeydew circle — the mascot's future home. ~44px.
        <span
          aria-hidden="true"
          className="flex h-11 w-11 items-center justify-center rounded-full bg-honeydew-100 text-2xl"
        >
          {icon}
        </span>
      )}
      {title && <p className="text-lg font-bold text-honeydew-700">{title}</p>}
      {/* The message carries the e2e testid (the element existing specs target). */}
      <p data-testid={testId} className="max-w-sm text-sm text-honeydew-600">
        {message}
      </p>
      {action != null && <div className="mt-1">{action}</div>}
    </div>
  );
}
