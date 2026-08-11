import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// #99 — a bug report must record the instance the reporter was LOOKING AT.
//
// The defect: `ReportBugModal` sent `NEXT_PUBLIC_INSTANCE_NAME`, which Next
// inlines at BUILD time and is therefore always the home instance. A report
// filed while reading Hypatia through the switcher said "Salem", sending
// whoever picked it up to the wrong instance's logs.
//
// THESE DRIVE THE REAL /chat PAGE, not the modal. That is the whole point: the
// modal taking a prop is trivially testable and proves nothing, because the bug
// this closes is a WIRING bug — the value has to travel page → Layout → FAB →
// modal → wire, and a pin that hands the modal its prop directly stays green
// against a page that never passes one. The FAB mounts from Layout, so a report
// is filed here exactly the way an operator files one.
//
// Delivery is NOT under test and deliberately unchanged: every report still
// lands on the home instance. Only the recorded metadata becomes truthful.

const { mockReplace, session, mockCapture } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  session: { user: { name: 'andrew', role: 'owner' } as { name: string; role: string } | null, loading: false },
  mockCapture: vi.fn(),
}));

vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: session.user, loading: session.loading }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), query: {}, asPath: '/chat' }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn(), me: vi.fn() } }));
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ idPrefix }: { idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock`}>mock-stt</button>
  ),
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));
// The capture engine is mocked at OUR seam, never at html2canvas. `null` (a
// failed/absent capture) keeps these tests about the CONTEXT block — the words
// are always sendable without a screenshot, which is its own #95 contract.
vi.mock('../lib/algernon/screenCapture', () => ({
  captureScreen: mockCapture,
  blobToBase64: vi.fn(async () => 'QkFTRTY0'),
  CAPTURE_IGNORE_ATTR: 'data-report-ignore',
  CAPTURE_TIMEOUT_MS: 8000,
}));

import ChatPage from '../pages/chat';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

// The cross-instance target. Named via a constant so the pin reads as "some
// instance that is not home" rather than depending on a particular deploy.
const AWAY_INSTANCE = 'Hypatia';

function jsonOk(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as unknown as Response;
}

let fetchSpy: ReturnType<typeof vi.fn>;

function installFetch() {
  fetchSpy = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.startsWith('/api/chat/targets')) {
      return jsonOk({
        targets: [
          { name: HOME_INSTANCE_NAME, label: HOME_INSTANCE_NAME, home: true },
          { name: AWAY_INSTANCE, label: AWAY_INSTANCE, home: false },
        ],
      });
    }
    if (u.startsWith('/api/chat/open')) return jsonOk({ session_key: 'sess-1' });
    if (u.startsWith('/api/chat/history')) return jsonOk({ turns: [] });
    if (u.startsWith('/api/chat/notifications')) return jsonOk({ notifications: [], unread: 0 });
    // The box answers with the instance that STORED the report — home, always.
    if (u.startsWith('/api/bugreport/submit')) {
      return jsonOk({ ok: true, report_id: 'r-1', instance: HOME_INSTANCE_NAME });
    }
    return jsonOk({});
  });
  (globalThis as unknown as { fetch: unknown }).fetch = fetchSpy;
}

/** The context block of the report that was actually filed. */
function filedContext(): Record<string, unknown> {
  const calls = fetchSpy.mock.calls.filter((c) =>
    String(c[0]).startsWith('/api/bugreport/submit'),
  );
  expect(calls.length).toBe(1);
  const init = calls[0][1] as RequestInit;
  const body = JSON.parse(String(init.body)) as { context: Record<string, unknown> };
  return body.context;
}

/** Open the FAB, type a report, send it. */
async function fileAReport() {
  fireEvent.click(screen.getByTestId('report-bug-fab'));
  await waitFor(() => expect(screen.queryByTestId('report-bug-modal')).not.toBeNull());
  fireEvent.change(screen.getByTestId('report-bug-body'), {
    target: { value: 'The send button did nothing.' },
  });
  fireEvent.click(screen.getByTestId('report-bug-submit'));
  await waitFor(() => expect(screen.queryByTestId('report-bug-success')).not.toBeNull());
}

/** Wait for the page to be signed in and the switcher to have loaded. */
async function readyWithSwitcher() {
  render(<ChatPage />);
  await waitFor(() => expect(screen.queryByTestId('chat-target')).not.toBeNull());
}

beforeEach(() => {
  mockReplace.mockReset();
  mockCapture.mockReset();
  mockCapture.mockResolvedValue(null);
  session.user = { name: 'andrew', role: 'owner' };
  session.loading = false;
  localStorage.clear();
  installFetch();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('#99 — the recorded instance is the one being viewed', () => {
  it('records the SWITCHED-TO instance, not the build-time home name', async () => {
    // THE PIN. Before the fix this recorded HOME_INSTANCE_NAME regardless.
    await readyWithSwitcher();
    fireEvent.change(screen.getByTestId('chat-target'), {
      target: { value: AWAY_INSTANCE },
    });
    await waitFor(() =>
      expect((screen.getByTestId('chat-target') as HTMLSelectElement).value).toBe(AWAY_INSTANCE),
    );

    await fileAReport();

    expect(filedContext().instance).toBe(AWAY_INSTANCE);
    // Not equal to home is asserted SEPARATELY: if a future deploy ever named
    // the away target the same as home, the assertion above would pass while
    // proving nothing about the defect.
    expect(filedContext().instance).not.toBe(HOME_INSTANCE_NAME);
  });

  it('POSITIVE CONTROL — the home context still records home', async () => {
    // Without this, the pin above would pass just as well against a build that
    // recorded some constant that merely happened to equal the away name.
    await readyWithSwitcher();
    await fileAReport();
    expect(filedContext().instance).toBe(HOME_INSTANCE_NAME);
  });

  it('still tells the reporter it was filed HOME, not to the instance viewed', async () => {
    // The two facts must not be conflated. `instance` in the context describes
    // the SCREEN; the confirmation line describes DELIVERY, which is home-only
    // in v1. A reporter told "filed to Hypatia" would go looking somewhere the
    // report has never been.
    await readyWithSwitcher();
    fireEvent.change(screen.getByTestId('chat-target'), {
      target: { value: AWAY_INSTANCE },
    });
    await fileAReport();

    expect(filedContext().instance).toBe(AWAY_INSTANCE);
    expect(screen.getByTestId('report-bug-success').textContent).toContain(HOME_INSTANCE_NAME);
  });

  it('freezes the viewed instance AT OPEN, like the route', async () => {
    // A switcher flipped while the dialog is up must not rewrite the report's
    // account of where the reporter was when they reached for the button —
    // the same reason `route` is frozen at open.
    await readyWithSwitcher();
    fireEvent.click(screen.getByTestId('report-bug-fab'));
    await waitFor(() => expect(screen.queryByTestId('report-bug-modal')).not.toBeNull());

    fireEvent.change(screen.getByTestId('chat-target'), {
      target: { value: AWAY_INSTANCE },
    });

    fireEvent.change(screen.getByTestId('report-bug-body'), {
      target: { value: 'Filed before I switched.' },
    });
    fireEvent.click(screen.getByTestId('report-bug-submit'));
    await waitFor(() => expect(screen.queryByTestId('report-bug-success')).not.toBeNull());

    expect(filedContext().instance).toBe(HOME_INSTANCE_NAME);
  });

  it('records the route as well, so the screen is identified twice over', async () => {
    // Guards the threading from breaking the sibling breadcrumb: `route` and
    // `instance` are captured at the same moment by the same handler, and a
    // change that dropped one would plausibly drop the other.
    await readyWithSwitcher();
    await fileAReport();
    expect(filedContext().route).toBe('/chat');
  });
});
