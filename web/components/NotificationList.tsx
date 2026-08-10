import { useState } from 'react';
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
//
// #76 — a ticket entry EXPANDS to show the issue's content. The operator ruled
// against a public forgejo URL, so the link stays box-local (#63b labels it as
// such) and expansion is how the ticket actually gets read from a phone. Same
// glance-then-tap grammar the feed rows use — no new UI paradigm.

/** Does this entry have an expandable ticket at all? Ticket-shaped entries get
 *  the affordance even when the body is missing, because "this ticket's content
 *  didn't arrive" is worth saying; a plain notice has nothing to expand. */
function isTicketEntry(n: NotificationItem): boolean {
  return Boolean(n.ticket_uid) || Boolean(n.ticket_body);
}

function NotificationRow({
  n,
  onAck,
}: {
  n: NotificationItem;
  onAck: (ids: string[]) => void;
}) {
  // Per-row, because expansion is transient per-row UI — nothing outside this
  // row needs to know, and hoisting it would make one card's tap open another's.
  const [open, setOpen] = useState(false);
  const expandable = isTicketEntry(n);
  const body = (n.ticket_body || '').trim();

  return (
    <li
      data-testid="notification-item"
      className={cn(
        'rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-700',
        !n.read && 'font-semibold',
      )}
    >
      <div className="flex items-start justify-between gap-3">
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
      </div>

      {expandable && (
        <button
          type="button"
          data-testid="notification-expand"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="mt-1 text-xs font-normal text-honeydew-600 underline underline-offset-2"
        >
          {open ? 'Hide ticket' : 'Read ticket'}
        </button>
      )}

      {expandable && open && (
        <div
          data-testid="notification-body"
          className="mt-2 border-t border-dashed border-honeydew-300 pt-2"
        >
          {body ? (
            <>
              {/* Escaped React text children ONLY. This is reporter-authored
                  text that crossed a peer protocol; the #22 stored-XSS
                  precedent applies exactly as it does to feed evidence. Do not
                  "improve" this into markdown-via-innerHTML. Bounded +
                  scrollable so a long ticket can't blow out the tray. */}
              <p className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words text-xs font-normal leading-relaxed text-honeydew-700">
                {body}
              </p>
              {n.ticket_body_truncated && (
                <p
                  data-testid="notification-body-truncated"
                  className="mt-1 text-[11px] font-normal italic text-honeydew-600/80"
                >
                  Truncated — the full ticket is in the tracker.
                </p>
              )}
            </>
          ) : (
            // ILB: a ticket whose content didn't arrive says so. An empty panel
            // would read as a broken card, and the operator would tap it twice
            // before concluding anything.
            <p className="text-xs font-normal italic text-honeydew-600/80">
              Ticket content unavailable — nothing was captured with this notice.
            </p>
          )}
        </div>
      )}
    </li>
  );
}

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
        <NotificationRow key={n.id} n={n} onAck={onAck} />
      ))}
    </ul>
  );
}
