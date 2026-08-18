import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

// THE TRAY NO LONGER SWALLOWS A REFUSED ACT.
//
// `ack` and `dismiss` both ended in a bare `catch {}`. The operator tapped
// "Mark read", the POST failed, and the pill stayed exactly as it was with
// nothing said — indistinguishable from a control that does not work, on the
// one surface built because a notice would not go away.
//
// The source comments even PROMISED the recovery ("the entry stays unread and
// can be re-acked"), which is true and was told only to whoever read the file.
//
// A POLL may still be swallowed and that distinction is pinned below: nobody is
// waiting on a poll, and the next tick fixes it. An ACT is the operator asking,
// once, by tapping.

const { mockNotifications, mockAck, mockDismiss } = vi.hoisted(() => ({
  mockNotifications: vi.fn(),
  mockAck: vi.fn(),
  mockDismiss: vi.fn(),
}));

vi.mock('../lib/algernon/client', () => ({
  chatApi: {
    notifications: mockNotifications,
    ackNotifications: mockAck,
    dismissNotifications: mockDismiss,
  },
}));

import { useNotifications } from '../lib/algernon/useNotifications';
import { ApiError } from '../lib/algernon/http';

function entry(id: string, read = true) {
  return {
    id, text: `notice ${id}`, precedence: 'R', source: 'kal-le',
    ts: '2026-08-10T09:00:00Z', read,
  };
}

beforeEach(() => {
  mockNotifications.mockReset();
  mockAck.mockReset();
  mockDismiss.mockReset();
  mockNotifications.mockResolvedValue({
    notifications: [entry('a'), entry('b'), entry('c', false)],
    unread: 1,
  });
});

async function ready() {
  const h = renderHook(() => useNotifications({ enabled: true }));
  await waitFor(() => expect(h.result.current.notifications).toHaveLength(3));
  return h;
}

describe('a refused ACK is reported, not swallowed', () => {
  it('names the row, quotes the server, and says the pill is still unread', async () => {
    mockAck.mockRejectedValue(new ApiError(503, 'transport_unreachable', 'the home instance is down'));
    const { result } = await ready();

    await act(async () => {
      await result.current.ack(['c']);
    });

    const said = result.current.failures['c'];
    expect(said).toBeTruthy();
    // The SERVER'S OWN WORDS — the shared `refusalReason`, so a refusal reads
    // the same here as on the deck, the board and the batch door.
    expect(said).toContain('the home instance is down');
    // …and what is now true about the row, which is the part the operator acts on.
    expect(said).toContain('still unread');
    // The row itself is untouched: still there, still unread, still tappable.
    expect(result.current.notifications.find((n) => n.id === 'c')?.read).toBe(false);
  });

  it('a later ack that LANDS settles the debt', async () => {
    mockAck.mockRejectedValueOnce(new ApiError(503, 'transport_unreachable', 'down'));
    const { result } = await ready();
    await act(async () => {
      await result.current.ack(['c']);
    });
    expect(result.current.failures['c']).toBeTruthy();

    mockAck.mockResolvedValue({ unread: 0 });
    await act(async () => {
      await result.current.ack(['c']);
    });

    expect(result.current.failures['c']).toBeUndefined();
    expect(result.current.notifications.find((n) => n.id === 'c')?.read).toBe(true);
  });
});

describe('a refused DISMISS is reported, and the row stays', () => {
  it('says it is still here rather than removing it on a failure', async () => {
    mockDismiss.mockRejectedValue(new ApiError(500, 'server_error', 'the tray writer is wedged'));
    const { result } = await ready();

    await act(async () => {
      await result.current.dismiss(['a']);
    });

    expect(result.current.failures['a']).toContain('the tray writer is wedged');
    expect(result.current.failures['a']).toContain('still here');
    // NOT removed — the server still lists it, so removing locally would show a
    // tray that disagrees with the box until the next poll put the row back.
    expect(result.current.notifications.map((n) => n.id)).toContain('a');
  });
});

describe('failures ACCUMULATE — a bulk clear reports every row it lost', () => {
  it('one failed Dismiss-all marks every id in the batch, not one line total', async () => {
    // The shape this keying exists for: `Dismiss all` sends every read id in one
    // call, so a single failure covers many rows. A shared "last error" string
    // would report that as one problem and leave the rest sitting there
    // unexplained — the swallow again, one row louder.
    mockDismiss.mockRejectedValue(new ApiError(500, 'server_error', 'wedged'));
    const { result } = await ready();

    await act(async () => {
      await result.current.dismiss(['a', 'b']);
    });

    expect(Object.keys(result.current.failures).sort()).toEqual(['a', 'b']);
    expect(result.current.notifications.map((n) => n.id)).toEqual(['a', 'b', 'c']);
  });

  it('two separate failed acts on different rows both survive', async () => {
    mockAck.mockRejectedValue(new ApiError(503, 'transport_unreachable', 'down'));
    mockDismiss.mockRejectedValue(new ApiError(500, 'server_error', 'wedged'));
    const { result } = await ready();

    await act(async () => {
      await result.current.ack(['c']);
    });
    await act(async () => {
      await result.current.dismiss(['a']);
    });

    // The second failure must not replace the first — they are two rows the
    // operator asked about and two answers they are owed.
    expect(result.current.failures['c']).toContain('still unread');
    expect(result.current.failures['a']).toContain('still here');
  });
});

describe('the boundary — a POLL is still allowed to be quiet', () => {
  it('a failed refresh records nothing and keeps the last-known tray', async () => {
    // The deliberate asymmetry, pinned so it cannot be "tidied" into symmetry.
    // Nobody is waiting on a poll: it is the system asking on its own
    // initiative, a stale tray is harmless, and the next tick retries. Surfacing
    // it would put a red line on the operator's screen for something they did
    // not do and cannot act on.
    const { result } = await ready();
    mockNotifications.mockRejectedValue(new ApiError(503, 'transport_unreachable', 'down'));

    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.failures).toEqual({});
    expect(result.current.notifications).toHaveLength(3); // last-known, kept
  });
});
