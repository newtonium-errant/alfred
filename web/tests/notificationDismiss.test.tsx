import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// #86 — the UI half of "read is not gone".
//
// Operator report: a notice they had already read sat on screen for four days
// with no way to clear it. Two changes answer it, and the split follows the
// glance-then-tap grammar the rest of this surface uses:
//
//   GLANCE — the top level shows only what still wants attention (unread), so
//            marking read clears the surface with NO new tap. That is the fix
//            for the four-days complaint.
//   TAP    — "Show N read" opens history; Dismiss lives in there, because
//            permanently clearing is deliberate and does not belong on a
//            surface meant to be read at a glance.
//
// Plain DOM assertions (no jest-dom in this suite).

import { NotificationList } from '../components/NotificationList';
import type { NotificationItem } from '../lib/algernon/types';

function item(over: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n1',
    text: 'New ticket — filed as issue #9',
    precedence: 'R',
    source: 'kal-le',
    ts: '2026-08-10T09:00:00Z',
    read: false,
    ...over,
  };
}

describe('the glance surface shows only unread', () => {
  it('a read notice is NOT in the main list', () => {
    // The complaint, directly: marking read must clear the surface.
    render(
      <NotificationList
        notifications={[item({ id: 'a', read: true, text: 'already seen' })]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('notification-list')).toBeNull();
    expect(screen.getByTestId('notifications-none-new')).toBeTruthy();
  });

  it('says "Nothing new" — NOT "No notifications" — when only read ones remain', () => {
    // THE ILB subtlety. An empty GLANCE list is not an empty TRAY. Claiming
    // "No notifications" while holding three would be false, and a surface
    // that says a false thing once stops being trusted for the true ones.
    render(
      <NotificationList
        notifications={[
          item({ id: 'a', read: true }),
          item({ id: 'b', read: true }),
          item({ id: 'c', read: true }),
        ]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('notifications-empty')).toBeNull();
    expect(screen.getByTestId('notifications-none-new')).toBeTruthy();
    expect(screen.getByTestId('notification-history-toggle').textContent)
      .toContain('3 read');
  });

  it('an genuinely empty tray still says "No notifications"', () => {
    render(
      <NotificationList notifications={[]} onAck={vi.fn()} onDismiss={vi.fn()} />,
    );
    expect(screen.getByTestId('notifications-empty')).toBeTruthy();
    expect(screen.queryByTestId('notification-history')).toBeNull();
  });

  it('unread notices stay on the glance surface', () => {
    render(
      <NotificationList
        notifications={[item({ id: 'a' }), item({ id: 'b', read: true })]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    const list = screen.getByTestId('notification-list');
    expect(list.querySelectorAll('[data-testid="notification-item"]')).toHaveLength(1);
  });

  it('no history disclosure appears when nothing has been read', () => {
    render(
      <NotificationList notifications={[item()]} onAck={vi.fn()} onDismiss={vi.fn()} />,
    );
    expect(screen.queryByTestId('notification-history')).toBeNull();
  });
});

describe('the read-history disclosure', () => {
  it('is collapsed by default and opens on tap', () => {
    render(
      <NotificationList
        notifications={[item({ id: 'a', read: true })]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('notification-read-list')).toBeNull();
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    expect(screen.getByTestId('notification-read-list')).toBeTruthy();
  });

  it('a read notice inside it can still be EXPANDED to read the ticket', () => {
    // Coverage that MOVED rather than disappeared: #76's expansion pins used a
    // read fixture, and read entries now live one level down. Expanding an
    // archived ticket is a real thing to want, so it keeps a pin at its new
    // location instead of being quietly dropped when the fixtures flipped.
    render(
      <NotificationList
        notifications={[item({
          id: 'a', read: true, ticket_uid: 'tkt-1', ticket_body: 'Repro steps',
        })]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    fireEvent.click(screen.getByTestId('notification-expand'));
    expect(screen.getByTestId('notification-body').textContent).toContain('Repro steps');
  });
});

describe('the controls follow the entry state', () => {
  it('an unread notice offers Mark read and NOT Dismiss', () => {
    // One control per row: showing both would ask the operator to choose
    // between two words for "deal with this".
    render(
      <NotificationList notifications={[item()]} onAck={vi.fn()} onDismiss={vi.fn()} />,
    );
    expect(screen.getByTestId('notification-ack')).toBeTruthy();
    expect(screen.queryByTestId('notification-dismiss')).toBeNull();
  });

  it('a read notice offers Dismiss and NOT Mark read', () => {
    render(
      <NotificationList
        notifications={[item({ read: true })]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    expect(screen.getByTestId('notification-dismiss')).toBeTruthy();
    expect(screen.queryByTestId('notification-ack')).toBeNull();
  });
});

describe('dismissing', () => {
  it('calls onDismiss with just that id', () => {
    const onDismiss = vi.fn();
    render(
      <NotificationList
        notifications={[item({ id: 'a', read: true }), item({ id: 'b', read: true })]}
        onAck={vi.fn()}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    fireEvent.click(screen.getAllByTestId('notification-dismiss')[0]);
    expect(onDismiss).toHaveBeenCalledWith(['a']);
  });

  it('Dismiss all sends every READ id and no unread one', () => {
    // Without a bulk clear, twenty read notices cost twenty taps — the
    // original complaint again, only slower. And it must never sweep up
    // something unread: that would clear work never seen.
    const onDismiss = vi.fn();
    render(
      <NotificationList
        notifications={[
          item({ id: 'unread1' }),
          item({ id: 'a', read: true }),
          item({ id: 'b', read: true }),
        ]}
        onAck={vi.fn()}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    fireEvent.click(screen.getByTestId('notification-dismiss-all'));
    expect(onDismiss).toHaveBeenCalledWith(['a', 'b']);
  });

  it('names the notice in the Dismiss control for screen readers', () => {
    // A row of identical "Dismiss" buttons is unusable without eyes on the
    // adjacent text.
    render(
      <NotificationList
        notifications={[item({ read: true, text: 'Payroll button ticket' })]}
        onAck={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    expect(
      screen.getByTestId('notification-dismiss').getAttribute('aria-label'),
    ).toContain('Payroll button ticket');
  });
});
