import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

// Merge conditional class names and de-duplicate conflicting Tailwind utilities.
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
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
