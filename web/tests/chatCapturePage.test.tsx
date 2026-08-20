import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// The capture toggle THROUGH THE REAL PAGE (R1): the bar mounts on /chat, the
// toggle round-trips /api/chat/capture, the indicator appears from SERVER
// truth, a captured send renders the received mark and NO reply bubble, and
// the typing indicator is suppressed while capturing (no reply is coming —
// pretending one is would contradict the capture indicator).

const { mockReplace, session } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  session: {
    user: { name: 'andrew', role: 'owner' } as { name: string; role: string } | null,
    loading: false,
  },
}));

vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: session.user, loading: session.loading }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), query: {} }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn(), me: vi.fn() } }));
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ idPrefix }: { idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock`}>
      mock-stt
    </button>
  ),
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));

import ChatPage from '../pages/chat';

function jsonOk(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as unknown as Response;
}

function capturedStream(): Response {
  const frame = `event: done\ndata: ${JSON.stringify({
    reply: '', captured: true, session_key: 'sess-1', ts: '', user_ts: 'U1',
  })}\n\n`;
  let sent = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (sent) return { value: undefined, done: true };
          sent = true;
          return { value: new TextEncoder().encode(frame), done: false };
        },
      }),
    },
  } as unknown as Response;
}

let fetchSpy: ReturnType<typeof vi.fn>;
let captureOn = false;
let holdStream: (() => void) | null = null;

function installFetch() {
  captureOn = false;
  holdStream = null;
  fetchSpy = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.startsWith('/api/chat/targets')) return jsonOk({ targets: [] });
    if (u.startsWith('/api/chat/open')) return jsonOk({ session_key: 'sess-1' });
    if (u.startsWith('/api/chat/history')) {
      return jsonOk({ turns: [], capture_active: false, capture_spans: [] });
    }
    if (u.startsWith('/api/chat/notifications')) return jsonOk({ notifications: [], unread: 0 });
    if (u.startsWith('/api/chat/capture')) {
      // Server truth: the toggle flips the fake server's state and answers
      // with what IS, exactly like the backend.
      captureOn = !captureOn;
      return jsonOk({
        session_key: 'sess-1',
        capture_active: captureOn,
        spans: captureOn
          ? [{ index: 0, start: 0, end: null, turns: 0, extracted: false }]
          : [{ index: 0, start: 0, end: 1, turns: 1, extracted: false }],
        closed_span: captureOn ? null : { index: 0, turns: 1 },
      });
    }
    if (u.startsWith('/api/chat/stream')) {
      if (holdStream) {
        // A HANGING turn — lets the test assert in-flight rendering.
        return new Promise<Response>((resolve) => {
          holdStream = () => resolve(capturedStream());
        });
      }
      return capturedStream();
    }
    if (u.startsWith('/api/ingest/targets')) return jsonOk({ targets: [] });
    if (u.startsWith('/api/batch/targets')) return jsonOk({ targets: [] });
    return jsonOk({});
  });
  (globalThis as unknown as { fetch: unknown }).fetch = fetchSpy;
}

beforeEach(() => {
  mockReplace.mockReset();
  session.user = { name: 'andrew', role: 'owner' };
  session.loading = false;
  localStorage.clear();
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function mountReady() {
  render(<ChatPage />);
  await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
}

describe('/chat capture toggle (R1)', () => {
  it('mounts the capture bar; indicator absent until the SERVER confirms on', async () => {
    await mountReady();
    expect(screen.getByTestId('capture-toggle')).toBeTruthy();
    expect(screen.queryByTestId('capture-indicator')).toBeNull();

    fireEvent.click(screen.getByTestId('capture-toggle'));
    await waitFor(() =>
      expect(screen.queryByTestId('capture-indicator')).not.toBeNull(),
    );
    expect(screen.getByTestId('capture-indicator').textContent).toContain(
      'receiving, not replying',
    );
  });

  it('a captured send: received mark, NO reply bubble, NO typing indicator', async () => {
    await mountReady();
    fireEvent.click(screen.getByTestId('capture-toggle'));
    await waitFor(() =>
      expect(screen.queryByTestId('capture-indicator')).not.toBeNull(),
    );

    // Hold the stream open so the in-flight state is observable.
    holdStream = () => {};
    fireEvent.change(screen.getByTestId('unified-input'), {
      target: { value: 'dictated capture line' },
    });
    fireEvent.submit(screen.getByTestId('unified-composer-form'));

    // In flight WHILE CAPTURING: the user bubble is up, and the typing
    // indicator is suppressed — no reply is coming.
    await waitFor(() => expect(screen.queryByTestId('msg-user')).not.toBeNull());
    expect(screen.queryByTestId('typing-indicator')).toBeNull();

    // Release the stream — the receipt lands: received mark, no assistant.
    holdStream();
    await waitFor(() =>
      expect(screen.queryByTestId('msg-captured')).not.toBeNull(),
    );
    expect(screen.getByTestId('msg-captured').textContent).toContain('Captured');
    expect(screen.queryByTestId('msg-assistant')).toBeNull();
  });

  it('POSITIVE CONTROL: a normal in-flight send DOES show the typing indicator', async () => {
    // The suppression pin above is an absence assertion; this is its
    // admissible neighbour — same page, same hold, capture OFF.
    await mountReady();
    holdStream = () => {};
    fireEvent.change(screen.getByTestId('unified-input'), {
      target: { value: 'a normal question' },
    });
    fireEvent.submit(screen.getByTestId('unified-composer-form'));
    await waitFor(() =>
      expect(screen.queryByTestId('typing-indicator')).not.toBeNull(),
    );
    holdStream();
  });

  it('toggle off surfaces the quiet extraction offer chip', async () => {
    await mountReady();
    fireEvent.click(screen.getByTestId('capture-toggle'));
    await waitFor(() =>
      expect(screen.queryByTestId('capture-indicator')).not.toBeNull(),
    );
    fireEvent.click(screen.getByTestId('capture-toggle'));
    await waitFor(() =>
      expect(screen.queryByTestId('capture-extract-offer')).not.toBeNull(),
    );
    expect(
      screen.getByTestId('capture-extract-offer-text').textContent,
    ).toContain('Captured 1 message');
    // And the indicator is gone — capture is off.
    expect(screen.queryByTestId('capture-indicator')).toBeNull();
  });
});
