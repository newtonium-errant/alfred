import { forwardRef, TextareaHTMLAttributes } from 'react';
import { cn } from '../../lib/utils';

// REGISTER SEAM: `ui-field` is the opt-in marker a surface register styles.
//
// The registers cannot simply select `input` / `button` under their
// `[data-surface]` scope. `[data-surface='crt'] button` has specificity (0,1,1)
// and a Tailwind role class like `.text-danger` has (0,1,0) — so an element
// selector silently OUTRANKS the verdict colours, and repainting a verdict is
// the one thing a register may never do. There are 22 role-coloured usages on
// the crt/comms pages today; an element rule would have caught every one.
//
// So a control opts IN by carrying this class, and the register scopes on
// `[data-surface='crt'] .ui-field` at (0,2,0): high enough to beat the warm
// utilities on the element, narrow enough that it can only ever reach controls
// that asked for it. Role-coloured markup never carries the marker, so it is
// unreachable by construction rather than by careful selector-writing.

// Styled NATIVE <textarea>. Forwards ref + all native props (value, onChange,
// onKeyDown, rows, maxLength, data-testid, etc.).
export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      'ui-field',
      'w-full resize-y rounded-xl border border-honeydew-300 bg-white px-3 py-2.5 text-base text-honeydew-900',
      'placeholder:text-honeydew-600/50',
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-1',
      'disabled:cursor-default disabled:opacity-70',
      className
    )}
    {...props}
  />
));
Textarea.displayName = 'Textarea';
