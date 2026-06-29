import { HTMLAttributes } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../../lib/utils';

const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full px-3 py-1 text-sm font-semibold whitespace-nowrap',
  {
    variants: {
      variant: {
        neutral: 'bg-status-todo text-status-todo-fg',
        todo: 'bg-status-todo text-status-todo-fg',
        in_progress: 'bg-status-progress text-status-progress-fg',
        blocked: 'bg-status-blocked text-status-blocked-fg',
        done: 'bg-status-done text-status-done-fg',
        // Membership-role pills.
        owner: 'bg-status-done text-status-done-fg',
        manager: 'bg-status-progress text-status-progress-fg',
        member: 'bg-status-todo text-status-todo-fg',
      },
    },
    defaultVariants: {
      variant: 'neutral',
    },
  }
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { badgeVariants };
