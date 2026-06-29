import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

// Merge conditional class names and de-duplicate conflicting Tailwind utilities.
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// Format a chat turn's ISO-8601 stamp for the message bubble. Returns '' for a
// falsy or unparseable stamp (pre-stamp records, optimistic-before-reply) so the
// caller renders NOTHING rather than an "Invalid Date". The backend `_ts` is UTC
// ISO with offset; toLocaleTimeString renders in the VIEWER's local zone. Chat
// convention is time-only (h:mm AM/PM); the full ISO rides in the <time dateTime>
// attribute for hover/a11y (see MessageBubble).
export function formatMessageTime(ts: string): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

// Scroll to + focus the element with the given data-testid. Used by the
// empty-state forward-action buttons ("Start your first one" -> focus the
// already-on-page input) so a major empty state offers the next step rather than
// only hinting at it. No-op if the element isn't found (the action just does
// nothing rather than throwing). Respects prefers-reduced-motion for the scroll.
export function focusByTestId(testId: string): void {
  if (typeof document === 'undefined') return;
  const el = document.querySelector<HTMLElement>(`[data-testid="${testId}"]`);
  if (!el) return;
  const reduceMotion =
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
  el.focus({ preventScroll: true });
}
