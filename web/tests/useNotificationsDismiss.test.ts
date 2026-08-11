import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

// #86 — the hook's half of "read is not gone".
//
// The load-bearing behaviour is that dismiss REMOVES the entry from local
// state rather than flagging it. The server has already stopped listing it, so
// leaving it on screen until the next 60s poll would show the operator a row
// that no longer exists — and a row that would not go away is the entire bug
// being fixed. Waiting for the poll would reproduce it in miniature.

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

describe('useNotifications.dismiss', () => {
  it('removes the dismissed entries from local state immediately', async () => {
    mockDismiss.mockResolvedValue({ dismissed: 1, unread: 1 });
    const { result } = renderHook(() => useNotifications({ enabled: true }));
    await waitFor(() => expect(result.current.notifications).toHaveLength(3));

    await act(async () => {
      await result.current.dismiss(['a']);
    });

    expect(mockDismiss).toHaveBeenCalledWith(['a']);
    expect(result.current.notifications.map((n) => n.id)).toEqual(['b', 'c']);
  });

  it('takes the unread count from the SERVER, not a local guess', async () => {
    // The server is the authority on the count; recomputing it here would be a
    // second implementation free to disagree with the badge's source of truth.
    mockDismiss.mockResolvedValue({ dismissed: 2, unread: 7 });
    const { result } = renderHook(() => useNotifications({ enabled: true }));
    await waitFor(() => expect(result.current.notifications).toHaveLength(3));

    await act(async () => {
      await result.current.dismiss(['a', 'b']);
    });
    expect(result.current.unread).toBe(7);
  });

  it('removes several at once (the Dismiss-all path)', async () => {
    mockDismiss.mockResolvedValue({ dismissed: 2, unread: 1 });
    const { result } = renderHook(() => useNotifications({ enabled: true }));
    await waitFor(() => expect(result.current.notifications).toHaveLength(3));

    await act(async () => {
      await result.current.dismiss(['a', 'b']);
    });
    expect(result.current.notifications.map((n) => n.id)).toEqual(['c']);
  });

  it('KEEPS the entry when the request fails', async () => {
    // Best-effort, like ack: a failed dismiss must not remove the row locally,
    // or the operator would believe it was cleared and it would reappear on
    // the next poll — worse than never having gone.
    mockDismiss.mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => useNotifications({ enabled: true }));
    await waitFor(() => expect(result.current.notifications).toHaveLength(3));

    await act(async () => {
      await result.current.dismiss(['a']);
    });
    expect(result.current.notifications.map((n) => n.id)).toEqual(['a', 'b', 'c']);
  });

  it('does not call the server for an empty id list', async () => {
    const { result } = renderHook(() => useNotifications({ enabled: true }));
    await waitFor(() => expect(result.current.notifications).toHaveLength(3));

    await act(async () => {
      await result.current.dismiss([]);
    });
    expect(mockDismiss).not.toHaveBeenCalled();
  });

  it('ack still only FLAGS — it does not remove', async () => {
    // The two must stay distinct at this layer too: ack keeps the entry (so it
    // can collapse into history and still be re-read), dismiss removes it.
    mockAck.mockResolvedValue({ acked: 1, unread: 0 });
    const { result } = renderHook(() => useNotifications({ enabled: true }));
    await waitFor(() => expect(result.current.notifications).toHaveLength(3));

    await act(async () => {
      await result.current.ack(['c']);
    });
    expect(result.current.notifications).toHaveLength(3);
    expect(result.current.notifications.find((n) => n.id === 'c')?.read).toBe(true);
  });
});
