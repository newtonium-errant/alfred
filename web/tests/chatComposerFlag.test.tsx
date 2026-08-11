import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// The #97 deploy gate, through the REAL /chat page.
//
// The promise being pinned is "INERT until the operator turns it on": with
// NEXT_PUBLIC_UNIFIED_COMPOSER unset, /chat renders the composer that has been
// live all along and a send is byte-identical on the wire. That is not a claim a
// unit test of the flag helper can make — the flag is read in the page, and a
// page that reads it and then mounts both, or neither, would pass a helper test
// perfectly.
//
// It also carries the #95 watch-item: the bug-report FAB mounts from Layout
// gated on `showBugReport ?? showNav`, so ANY change to how a page is wrapped
// silently takes the FAB with it. There was no pin on that at all, in either
// direction — so both are asserted here, on both sides of the flag.

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

function streamOk(): Response {
  const frame = `event: done\ndata: ${JSON.stringify({
    reply: 'ok', session_key: 'sess-1', ts: 'T', user_ts: 'U',
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
function installFetch() {
  fetchSpy = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.startsWith('/api/chat/targets')) return jsonOk({ targets: [] });
    if (u.startsWith('/api/chat/open')) return jsonOk({ session_key: 'sess-1' });
    if (u.startsWith('/api/chat/history')) return jsonOk({ turns: [] });
    if (u.startsWith('/api/chat/notifications')) return jsonOk({ notifications: [], unread: 0 });
    if (u.startsWith('/api/chat/stream')) return streamOk();
    if (u.startsWith('/api/ingest/targets')) return jsonOk({ targets: [] });
    if (u.startsWith('/api/batch/targets')) return jsonOk({ targets: [] });
    return jsonOk({});
  });
  (globalThis as unknown as { fetch: unknown }).fetch = fetchSpy;
}

function lastBodyTo(url: string): Record<string, unknown> | null {
  const calls = fetchSpy.mock.calls.filter((c) => String(c[0]).startsWith(url));
  if (calls.length === 0) return null;
  const init = calls[calls.length - 1][1] as RequestInit | undefined;
  return JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
}

beforeEach(() => {
  mockReplace.mockReset();
  localStorage.clear();
  installFetch();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('/chat with the unified composer OFF (the shipped default)', () => {
  it('mounts the ORIGINAL composer and not the unified one', async () => {
    // Unset, not "0": the default an operator who has done nothing gets.
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('composer-input')).not.toBeNull());
    expect(screen.getByTestId('composer')).toBeTruthy();
    // Alternatives, never layered — "flag off" is the old component itself,
    // not the new one in a quiet mode.
    expect(screen.queryByTestId('unified-composer')).toBeNull();
    expect(screen.queryByTestId('unified-attach')).toBeNull();
  });

  it('a typed send is byte-identical on the wire to the pre-feature body', async () => {
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('composer-input')).not.toBeNull());

    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'just typing' } });
    fireEvent.click(screen.getByTestId('composer-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/stream')).not.toBeNull());
    const body = lastBodyTo('/api/chat/stream')!;
    // The EXACT key set the pre-#97 page sent for a plain typed turn. A pin on
    // `message` alone would stay green against a composer that quietly added a
    // field, which is the drift an inert deploy is supposed to make impossible.
    expect(Object.keys(body).sort()).toEqual(
      ['idempotency_key', 'instance', 'kind', 'message', 'session_key'].sort(),
    );
    expect(body.message).toBe('just typing');
    expect(body.kind).toBe('text');
  });

  it('makes NO request to the ingest or batch target routes', async () => {
    // The unified composer loads both target lists on mount. With the flag off
    // nothing should ask — a page that fetches them anyway is a page where the
    // component mounted invisibly.
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('composer-input')).not.toBeNull());
    const asked = fetchSpy.mock.calls.map((c) => String(c[0]));
    expect(asked.some((u) => u.startsWith('/api/ingest/targets'))).toBe(false);
    expect(asked.some((u) => u.startsWith('/api/batch/targets'))).toBe(false);
  });
});

describe('/chat with the unified composer ON', () => {
  it('mounts the unified composer and retires the original', async () => {
    vi.stubEnv('NEXT_PUBLIC_UNIFIED_COMPOSER', '1');
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
    expect(screen.getByTestId('unified-attach')).toBeTruthy();
    expect(screen.queryByTestId('composer')).toBeNull();
  });

  it('loads BOTH target families, so a chip can refuse by name rather than guess', async () => {
    vi.stubEnv('NEXT_PUBLIC_UNIFIED_COMPOSER', '1');
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
    await waitFor(() => {
      const asked = fetchSpy.mock.calls.map((c) => String(c[0]));
      expect(asked.some((u) => u.startsWith('/api/ingest/targets'))).toBe(true);
      expect(asked.some((u) => u.startsWith('/api/batch/targets'))).toBe(true);
    });
  });

  it('a typed send STILL produces the pre-feature wire body', async () => {
    // The unified composer changes what an ATTACHMENT does. A plain typed
    // message must be unchanged either way, or turning the flag on silently
    // alters every ordinary turn.
    vi.stubEnv('NEXT_PUBLIC_UNIFIED_COMPOSER', '1');
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-input')).not.toBeNull());

    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'just typing' } });
    fireEvent.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/stream')).not.toBeNull());
    const body = lastBodyTo('/api/chat/stream')!;
    expect(Object.keys(body).sort()).toEqual(
      ['idempotency_key', 'instance', 'kind', 'message', 'session_key'].sort(),
    );
    expect(body.message).toBe('just typing');
  });
});

describe('the flag is OFF unless it is explicitly on (#97 inert deploy)', () => {
  it.each([undefined, '', '0', 'false', 'off', 'no', 'maybe', ' '])(
    'stays off for %j',
    async (value) => {
      if (value !== undefined) vi.stubEnv('NEXT_PUBLIC_UNIFIED_COMPOSER', value);
      render(<ChatPage />);
      await waitFor(() => expect(screen.queryByTestId('composer-input')).not.toBeNull());
      expect(screen.queryByTestId('unified-composer')).toBeNull();
    },
  );

  it.each(['1', 'true', 'on', 'yes', 'TRUE', ' 1 '])('turns on for %j', async (value) => {
    // The positive control for the table above: a gate that refused every value
    // would satisfy every "stays off" row and ship a feature nobody can enable.
    vi.stubEnv('NEXT_PUBLIC_UNIFIED_COMPOSER', value);
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
  });
});

describe('the bug-report FAB survives this change (#95 watch-item)', () => {
  it('mounts on /chat with the flag OFF', async () => {
    // Layout gates it on `showBugReport ?? showNav`, so a page-wrapping change
    // takes it away silently. There was no pin on it in either direction.
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('composer-input')).not.toBeNull());
    expect(screen.getByTestId('report-bug-fab')).toBeTruthy();
  });

  it('mounts on /chat with the flag ON', async () => {
    vi.stubEnv('NEXT_PUBLIC_UNIFIED_COMPOSER', '1');
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
    expect(screen.getByTestId('report-bug-fab')).toBeTruthy();
  });
});
