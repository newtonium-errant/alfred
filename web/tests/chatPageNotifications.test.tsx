import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// THE THREADING PIN for the tray's failure line, through the REAL /chat page.
//
// `useNotifications` now records a per-row failure and `NotificationList` now
// renders one — and BOTH could be perfectly correct while the operator still
// sees nothing, because the prop carrying them between the two is optional and
// defaults to `{}`. That default exists so the component's own test mounts keep
// compiling; it is also a standing trap. Every unit pin on either side stays
// green against a page that never wires them together, which is the exact
// failure this project has shipped before and the reason this file drives the
// page rather than the component.
//
// So: fail the real ack POST on the real page and assert the operator sees a
// line. Nothing below reaches into the hook or mounts the list directly.

const { mockReplace } = vi.hoisted(() => ({ mockReplace: vi.fn() }));

vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), query: {} }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn(), me: vi.fn() } }));
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ idPrefix }: { idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock`}>mock-stt</button>
  ),
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));

import ChatPage from '../pages/chat';

const UNREAD = {
  id: 'n1',
  text: 'KAL-LE finished the radar sweep',
  precedence: 'R',
  source: 'kal-le',
  ts: '2026-08-15T09:00:00Z',
  read: false,
};

function jsonOk(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as unknown as Response;
}

/** The ack door answers `ackStatus`; everything else the page needs succeeds. */
function installFetch(ackStatus: number) {
  const spy = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.startsWith('/api/chat/notifications/ack')) {
      if (ackStatus === 200) return jsonOk({ unread: 0 });
      return {
        ok: false,
        status: ackStatus,
        json: async () => ({ error: 'transport_unreachable', detail: 'the home instance is down' }),
      } as unknown as Response;
    }
    if (u.startsWith('/api/chat/notifications')) {
      return jsonOk({ notifications: [UNREAD], unread: 1 });
    }
    if (u.startsWith('/api/chat/targets')) return jsonOk({ targets: [] });
    if (u.startsWith('/api/chat/open')) return jsonOk({ session_key: 'sess-1' });
    if (u.startsWith('/api/chat/history')) return jsonOk({ turns: [] });
    if (u.startsWith('/api/ingest/targets')) return jsonOk({ targets: [] });
    if (u.startsWith('/api/batch/targets')) return jsonOk({ targets: [] });
    return jsonOk({});
  });
  (globalThis as unknown as { fetch: unknown }).fetch = spy;
  return spy;
}

beforeEach(() => {
  mockReplace.mockReset();
  localStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

async function readyWithTray() {
  render(<ChatPage />);
  await waitFor(() => expect(screen.queryByTestId('notification-item')).not.toBeNull());
}

describe('/chat — a refused Mark-read reaches the operator', () => {
  it('renders the failure line on the row, in the server’s own words', async () => {
    installFetch(503);
    await readyWithTray();

    fireEvent.click(screen.getByTestId('notification-ack'));

    const line = await screen.findByTestId('notification-failure');
    expect(line.getAttribute('role')).toBe('alert');
    expect(line.textContent).toContain('the home instance is down');
    expect(line.textContent).toContain('still unread');
    // The row is still there and still offers the control — the operator can
    // simply tap again, which is what the line tells them to do.
    expect(screen.getByTestId('notification-ack')).toBeTruthy();
  });

  it('draws NO line when the ack lands (the control)', async () => {
    // Without this, the pin above would pass identically against a page that
    // rendered the failure line unconditionally.
    installFetch(200);
    await readyWithTray();

    fireEvent.click(screen.getByTestId('notification-ack'));

    await waitFor(() => expect(screen.queryByTestId('notification-ack')).toBeNull());
    expect(screen.queryByTestId('notification-failure')).toBeNull();
  });
});
