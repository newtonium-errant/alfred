import { describe, expect, it } from 'vitest';
import {
  MAX_NOTIFICATION_ACK_IDS,
  notificationSchema,
  notificationsAckBodySchema,
  notificationsResponseSchema,
} from '../lib/algernon/schemas';

// The zod mirrors of the backend notification contract (parity #22). The
// backend (src/alfred/web/notify_state.py + routes_notify.py) is the
// authority; these pins catch FE↔BE drift at test time.

const VALID_ITEM = {
  id: 'abc123def4567890',
  text: 'New ticket [bug] Login broken — filed as issue #7',
  precedence: 'R',
  source: 'kal-le',
  ticket_uid: 'vera-20260719-0001',
  issue_url: 'https://github.com/acme/site/issues/7',
  ts: '2026-07-19T12:00:00+00:00',
  read: false,
};

describe('notificationSchema', () => {
  it('accepts a full backend entry', () => {
    expect(notificationSchema.safeParse(VALID_ITEM).success).toBe(true);
  });

  it('tolerates absent ticket fields (non-ticket notices / forward-compat)', () => {
    const { ticket_uid: _t, issue_url: _u, ...rest } = VALID_ITEM;
    expect(notificationSchema.safeParse(rest).success).toBe(true);
  });

  it('rejects a missing id / non-boolean read', () => {
    expect(
      notificationSchema.safeParse({ ...VALID_ITEM, id: '' }).success,
    ).toBe(false);
    expect(
      notificationSchema.safeParse({ ...VALID_ITEM, read: 'no' }).success,
    ).toBe(false);
  });

  it('sanitizes a non-http(s) issue_url to undefined (#22 XSS defense-in-depth)', () => {
    // The notification survives, but a javascript:/data: url is dropped so it
    // can never reach the <a href> in the operator's session. (Backend
    // _safe_http_url is the authority; this mirrors it.)
    const js = notificationSchema.safeParse({
      ...VALID_ITEM,
      issue_url: 'javascript:alert(document.cookie)',
    });
    expect(js.success).toBe(true);
    expect(js.success && js.data.issue_url).toBeUndefined();

    const data = notificationSchema.safeParse({ ...VALID_ITEM, issue_url: 'data:text/html,x' });
    expect(data.success && data.data.issue_url).toBeUndefined();

    // A valid http(s) url is preserved unchanged.
    const ok = notificationSchema.safeParse({ ...VALID_ITEM });
    expect(ok.success && ok.data.issue_url).toBe(VALID_ITEM.issue_url);
  });
});

describe('notificationsResponseSchema', () => {
  it('accepts the ILB empty tray', () => {
    expect(
      notificationsResponseSchema.safeParse({ notifications: [], unread: 0 })
        .success,
    ).toBe(true);
  });

  it('accepts a populated tray', () => {
    expect(
      notificationsResponseSchema.safeParse({
        notifications: [VALID_ITEM],
        unread: 1,
      }).success,
    ).toBe(true);
  });

  it('rejects a non-numeric unread', () => {
    expect(
      notificationsResponseSchema.safeParse({
        notifications: [],
        unread: 'zero',
      }).success,
    ).toBe(false);
  });
});

describe('notificationsAckBodySchema', () => {
  it('accepts a bounded non-empty id list', () => {
    expect(
      notificationsAckBodySchema.safeParse({ ids: ['abc123'] }).success,
    ).toBe(true);
  });

  it('rejects empty list / empty ids / over-cap lists', () => {
    expect(notificationsAckBodySchema.safeParse({ ids: [] }).success).toBe(false);
    expect(notificationsAckBodySchema.safeParse({ ids: [''] }).success).toBe(false);
    expect(
      notificationsAckBodySchema.safeParse({
        ids: Array.from({ length: MAX_NOTIFICATION_ACK_IDS + 1 }, (_, i) => `id${i}`),
      }).success,
    ).toBe(false);
  });

  it('caps at 200 in lockstep with the backend MAX_ACK_IDS', () => {
    expect(MAX_NOTIFICATION_ACK_IDS).toBe(200);
  });
});
