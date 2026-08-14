import { forwardRef, ButtonHTMLAttributes } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../../lib/utils';

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 rounded-xl font-semibold transition-colors ' +
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2 ' +
    'disabled:cursor-default disabled:opacity-70',
  {
    variants: {
      variant: {
        // The three NEUTRAL variants carry `ui-btn` and take their register's
        // chrome. `destructive` deliberately does NOT: it is a role-coloured
        // control, and a register restyles chrome, never a verdict. That
        // omission is asserted, not assumed — see consoleRegisters' role pin.
        primary: 'ui-btn bg-honeydew-500 text-white hover:bg-honeydew-600 disabled:bg-honeydew-400 disabled:hover:bg-honeydew-400',
        outline:
          'ui-btn border border-honeydew-300 bg-white text-honeydew-700 hover:bg-honeydew-50',
        ghost: 'ui-btn bg-transparent text-honeydew-700 hover:bg-honeydew-100',
        destructive:
          'border border-honeydew-300 bg-white text-danger hover:bg-danger-bg',
      },
      size: {
        sm: 'px-3 py-1.5 text-sm',
        md: 'px-4 py-2.5 text-base',
      },
    },
    defaultVariants: {
      variant: 'primary',
      size: 'md',
    },
  }
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

// Native <button> styled with cva variants. Forwards ref + all native props
// (incl. data-testid, type, disabled, onClick) so test/behavior contracts hold.
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, type = 'button', ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  )
);
Button.displayName = 'Button';

export { buttonVariants };
