import type { NotificationItem } from '../lib/algernon/types';
import { subtle } from '../lib/typography';
import { cn } from '../lib/utils';

// The notification tray (parity #22, POLL slice). Reuses the chat notice-pill
// styling (rounded honeydew-100 wash — a calm, non-error surface; danger-red
// stays reserved for true system errors). One pill per notification, newest
// first (the backend orders); unread pills read bolder + carry a "Mark read"
// affordance; a ticket-shaped entry links out to its GitHub issue.
//
// ILB: an empty tray renders an EXPLICIT "No notifications" line — never a
// silently absent section, so "nothing to show" is distinguishable from
// "tray broken / not rendered".
export function NotificationList({
  notifications,
  onAck,
}: {
  notifications: NotificationItem[];
  onAck: (ids: string[]) => void;
}) {
  if (notifications.length === 0) {
    return (
      <p data-testid="notifications-empty" className={subtle}>
        No notifications
      </p>
    );
  }

  return (
    <ul data-testid="notification-list" className="flex flex-col gap-2">
      {notifications.map((n) => (
        <li
          key={n.id}
          data-testid="notification-item"
          className={cn(
            'flex items-start justify-between gap-3 rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-700',
            !n.read && 'font-semibold',
          )}
        >
          <span className="min-w-0">
            {n.text}
            {n.issue_url ? (
              <>
                {' '}
                <a
                  href={n.issue_url}
                  target="_blank"
                  rel="noreferrer"
                  data-testid="notification-issue-link"
                  className="underline decoration-honeydew-300 underline-offset-2 hover:text-honeydew-900"
                >
                  View issue
                </a>
              </>
            ) : null}
          </span>
          {!n.read && (
            <button
              type="button"
              data-testid="notification-ack"
              onClick={() => onAck([n.id])}
              className="shrink-0 rounded-lg border border-honeydew-300 bg-white px-2 py-1 text-xs font-semibold text-honeydew-700 hover:bg-honeydew-50"
            >
              Mark read
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
