import { forwardRef, LabelHTMLAttributes } from 'react';
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

export const Label = forwardRef<HTMLLabelElement, LabelHTMLAttributes<HTMLLabelElement>>(
  ({ className, ...props }, ref) => (
    <label
      ref={ref}
      className={cn('ui-label', 'text-sm font-semibold text-honeydew-700', className)}
      {...props}
    />
  )
);
Label.displayName = 'Label';
