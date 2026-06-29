import { forwardRef, TextareaHTMLAttributes } from 'react';
import { cn } from '../../lib/utils';

// Styled NATIVE <textarea>. Forwards ref + all native props (value, onChange,
// onKeyDown, rows, maxLength, data-testid, etc.).
export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
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
