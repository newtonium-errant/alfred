import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { NotificationList } from '../components/NotificationList';
import type { NotificationItem } from '../lib/algernon/types';

// The tray component (parity #22): the ILB explicit empty state, the unread
// "Mark read" affordance (hidden on read entries), and the issue link-out.

const ITEM: NotificationItem = {
  id: 'n1',
  text: 'New ticket [bug] Login broken — filed as issue #7',
  precedence: 'R',
  source: 'kal-le',
  ticket_uid: 'vera-20260719-0001',
  issue_url: 'https://github.com/acme/site/issues/7',
  ts: '2026-07-19T12:00:00+00:00',
  read: false,
};

describe('NotificationList', () => {
  it('renders the explicit "No notifications" empty state (ILB)', () => {
    render(<NotificationList notifications={[]} onAck={() => {}} onDismiss={() => {}} />);
    const empty = screen.getByTestId('notifications-empty');
    expect(empty.textContent).toBe('No notifications');
    expect(screen.queryByTestId('notification-list')).toBeNull();
  });

  it('renders an unread item with a Mark-read button that acks its id', () => {
    const onAck = vi.fn();
    render(<NotificationList notifications={[ITEM]} onAck={onAck} onDismiss={vi.fn()} />);
    expect(screen.getByTestId('notification-item').textContent).toContain(
      'New ticket [bug]',
    );
    fireEvent.click(screen.getByTestId('notification-ack'));
    expect(onAck).toHaveBeenCalledWith(['n1']);
  });

  it('hides the Mark-read button on a read item', () => {
    render(
      <NotificationList
        notifications={[{ ...ITEM, read: true }]}
        onAck={() => {}} onDismiss={() => {}}
      />,
    );
    expect(screen.queryByTestId('notification-ack')).toBeNull();
  });

  it('links out to the GitHub issue when issue_url is present', () => {
    render(<NotificationList notifications={[ITEM]} onAck={() => {}} onDismiss={() => {}} />);
    const link = screen.getByTestId('notification-issue-link');
    expect(link.getAttribute('href')).toBe('https://github.com/acme/site/issues/7');
  });

  it('renders no issue link for a non-ticket notice', () => {
    render(
      <NotificationList
        notifications={[{ ...ITEM, issue_url: '' }]}
        onAck={() => {}} onDismiss={() => {}}
      />,
    );
    expect(screen.queryByTestId('notification-issue-link')).toBeNull();
  });
});
