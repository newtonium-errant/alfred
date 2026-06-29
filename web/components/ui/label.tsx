import { forwardRef, LabelHTMLAttributes } from 'react';
import { cn } from '../../lib/utils';

export const Label = forwardRef<HTMLLabelElement, LabelHTMLAttributes<HTMLLabelElement>>(
  ({ className, ...props }, ref) => (
    <label
      ref={ref}
      className={cn('text-sm font-semibold text-honeydew-700', className)}
      {...props}
    />
  )
);
Label.displayName = 'Label';
