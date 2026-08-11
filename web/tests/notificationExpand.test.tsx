import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// #76 — reading the ticket IN the PWA.
//
// Operator ruling (option 2): the notification card expands to show the issue's
// content. The tunnel was REJECTED, so the link stays box-local and #63b's
// label stays the honest fallback — expansion is how the ticket actually gets
// read from a phone.
//
// The content rides the notify payload (composed on KAL-LE, which has both the
// ticket and the forgejo client) rather than being fetched on demand, because
// the instance that renders this tray has no forgejo client at all. Details in
// tests/test_ticket_notify_content.py.

import { NotificationList } from '../components/NotificationList';
import { notificationSchema } from '../lib/algernon/schemas';
import type { NotificationItem } from '../lib/algernon/types';

function item(over: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n1',
    text: 'New ticket [bug] Payroll button — filed as issue #9',
    precedence: 'R',
    source: 'kal-le',
    ticket_uid: 'tkt-1',
    issue_url: 'http://localhost:3001/x/y/issues/9',
    ts: '2026-08-10T09:00:00Z',
    read: false,
    ...over,
  };
}

const BODY = 'Reported by: Ben\nPriority: high\n\n## Repro\n1. Click Submit Payroll';

describe('the card expands to show the ticket', () => {
  it('is collapsed by default — glance first, per the interaction grammar', () => {
    render(<NotificationList notifications={[item({ ticket_body: BODY })]} onAck={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.queryByTestId('notification-body')).toBeNull();
    expect(screen.getByTestId('notification-expand')).toBeTruthy();
  });

  it('tapping expands to reveal the content', () => {
    render(<NotificationList notifications={[item({ ticket_body: BODY })]} onAck={vi.fn()} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByTestId('notification-expand'));
    const body = screen.getByTestId('notification-body');
    expect(body.textContent).toContain('Click Submit Payroll');
    expect(body.textContent).toContain('Reported by: Ben');
  });

  it('tapping again collapses it', () => {
    render(<NotificationList notifications={[item({ ticket_body: BODY })]} onAck={vi.fn()} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByTestId('notification-expand'));
    fireEvent.click(screen.getByTestId('notification-expand'));
    expect(screen.queryByTestId('notification-body')).toBeNull();
  });

  it('expands independently per row — one open card does not open the others', () => {
    render(
      <NotificationList
        notifications={[
          item({ id: 'a', ticket_body: 'AAA content' }),
          item({ id: 'b', ticket_body: 'BBB content' }),
        ]}
        onAck={vi.fn()} onDismiss={vi.fn()}
      />,
    );
    fireEvent.click(screen.getAllByTestId('notification-expand')[0]);
    const bodies = screen.getAllByTestId('notification-body');
    expect(bodies).toHaveLength(1);
    expect(bodies[0].textContent).toContain('AAA');
  });
});

describe('what has no content to show', () => {
  it('a plain (non-ticket) notice offers no expand affordance at all', () => {
    render(<NotificationList notifications={[item({ ticket_uid: '', ticket_body: '' })]} onAck={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.queryByTestId('notification-expand')).toBeNull();
  });

  it('ILB — a TICKET notice whose content is missing says so, rather than\n     opening an empty panel. A ticket that arrived without its body is a\n     real state (a pre-#76 entry, or an intake that could not compose one),\n     and an empty expansion would read as a broken card.', () => {
    render(<NotificationList notifications={[item({ ticket_body: '' })]} onAck={vi.fn()} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByTestId('notification-expand'));
    const body = screen.getByTestId('notification-body');
    expect(body.textContent).toMatch(/content unavailable/i);
  });

  it('a truncated body says there is more, rather than ending mid-sentence\n     with no explanation', () => {
    render(
      <NotificationList
        notifications={[item({ ticket_body: BODY, ticket_body_truncated: true })]}
        onAck={vi.fn()} onDismiss={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-expand'));
    expect(screen.getByTestId('notification-body-truncated')).toBeTruthy();
  });
});

describe('the content is TEXT, never markup', () => {
  it('renders a script-shaped body as literal text', () => {
    // The ticket body is operator/reporter-authored and crosses a peer
    // protocol — the #22 stored-XSS precedent applies exactly as it does to
    // feed evidence. React escapes by default; this pins that nobody
    // "improves" it into dangerouslySetInnerHTML for markdown rendering.
    const hostile = '<img src=x onerror=alert(1)> <script>alert(2)</script>';
    render(<NotificationList notifications={[item({ ticket_body: hostile })]} onAck={vi.fn()} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByTestId('notification-expand'));
    const body = screen.getByTestId('notification-body');
    expect(body.textContent).toContain('<script>');
    expect(body.querySelector('script')).toBeNull();
    expect(body.querySelector('img')).toBeNull();
  });
});

describe('schema tolerance', () => {
  it('accepts the new fields', () => {
    const parsed = notificationSchema.safeParse({
      id: 'n1', text: 't', precedence: 'R', source: 's',
      ts: '2026-08-10T09:00:00Z', read: false,
      ticket_body: BODY, ticket_body_truncated: true, issue_number: 9,
    });
    expect(parsed.success && parsed.data.ticket_body).toBe(BODY);
    expect(parsed.success && parsed.data.issue_number).toBe(9);
  });

  it('accepts an entry that predates them — the tray must not empty itself\n     on rollout', () => {
    const parsed = notificationSchema.safeParse({
      id: 'n1', text: 't', precedence: 'R', source: 's',
      ts: '2026-08-10T09:00:00Z', read: false,
    });
    expect(parsed.success).toBe(true);
    expect(parsed.success && parsed.data.ticket_body).toBeUndefined();
  });
});
