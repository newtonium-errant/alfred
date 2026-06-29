import { forwardRef, InputHTMLAttributes } from 'react';
import { cn } from '../../lib/utils';

// Styled NATIVE <input>. Forwards ref + all native props (type, value, onChange,
// data-testid, min/step, maxLength, etc.) so behavior/test contracts are intact.
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        'w-full rounded-xl border border-honeydew-300 bg-white px-3 py-2.5 text-base text-honeydew-900',
        'placeholder:text-honeydew-600/50',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-1',
        'disabled:cursor-default disabled:opacity-70',
        className
      )}
      {...props}
    />
  )
);
Input.displayName = 'Input';
