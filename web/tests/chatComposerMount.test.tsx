import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// What /chat MOUNTS, through the REAL page.
//
// THIS FILE WAS THE #97 DEPLOY GATE'S PIN. It asserted that
// NEXT_PUBLIC_UNIFIED_COMPOSER selected in both directions — unset, the legacy
// composer; set, the unified one. The composer-deletion lane removed the flag
// and the legacy component, so every assertion about SELECTION went with them:
// there is nothing left to select between. The file is renamed for what
// survives rather than left carrying the name of a variable that no longer
// exists.
//
// Two things survive, and neither was ever a flag pin:
//
//   * THE BYTE-IDENTICAL WIRE. A plain typed turn must produce exactly the key
//     set the pre-#97 page sent. This was asserted on both sides of the flag;
//     the surviving version is the unified one, carrying the `kind` assertion
//     the flag-OFF version had and the ON version did not. A pin on `message`
//     alone would stay green against a composer that quietly added a field.
//
//   * THE #95 WATCH-ITEM. The bug-report FAB mounts from Layout gated on
//     `showBugReport ?? showNav`, so ANY change to how a page is wrapped
//     silently takes the FAB with it. The two flag-varying mounts collapse to
//     one — they varied an axis the guard never read — and the pre-auth pin,
//     which is the branch that actually exercises the other side of that guard,
//     is untouched.

const { mockReplace, session } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  // Mutable so one test can render the PRE-AUTH branch. Defaults to signed in,
  // so every other test here is unchanged.
  session: { user: { name: 'andrew', role: 'owner' } as { name: string; role: string } | null, loading: false },
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
  session.user = { name: 'andrew', role: 'owner' };
  session.loading = false;
  localStorage.clear();
  installFetch();
});

afterEach(() => {
  // `vi.unstubAllEnvs()` stood here for the flag stubs. Nothing in this file
  // stubs env any more, so it went with them.
  vi.restoreAllMocks();
});

describe('/chat mounts the unified composer', () => {
  it('mounts it, unconditionally — no variable is consulted', async () => {
    // No env is stubbed anywhere in this file any more. A page that still read a
    // flag would render nothing here, because the flag's default was OFF and the
    // component it selected is deleted.
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
    expect(screen.getByTestId('unified-attach')).toBeTruthy();
  });

  it('loads BOTH target families, so a chip can refuse by name rather than guess', async () => {
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
    await waitFor(() => {
      const asked = fetchSpy.mock.calls.map((c) => String(c[0]));
      expect(asked.some((u) => u.startsWith('/api/ingest/targets'))).toBe(true);
      expect(asked.some((u) => u.startsWith('/api/batch/targets'))).toBe(true);
    });
  });

  it('a typed send is byte-identical on the wire to the pre-feature body', async () => {
    // THE SURVIVING WIRE PIN, merged from the two the flag used to need. The
    // unified composer changes what an ATTACHMENT does; a plain typed message
    // must be unchanged, or the deletion silently altered every ordinary turn.
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-input')).not.toBeNull());

    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'just typing' } });
    fireEvent.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/stream')).not.toBeNull());
    const body = lastBodyTo('/api/chat/stream')!;
    // The EXACT key set the pre-#97 page sent for a plain typed turn. A pin on
    // `message` alone would stay green against a composer that quietly added a
    // field, which is the drift this pin exists to make impossible.
    expect(Object.keys(body).sort()).toEqual(
      ['idempotency_key', 'instance', 'kind', 'message', 'session_key'].sort(),
    );
    expect(body.message).toBe('just typing');
    // Carried over from the flag-OFF version, which asserted it where the ON
    // version did not. The merge keeps the stronger of the two.
    expect(body.kind).toBe('text');
  });
});

describe('the bug-report FAB survives this change (#95 watch-item)', () => {
  it('mounts on /chat', async () => {
    // Layout gates it on `showBugReport ?? showNav`, so a page-wrapping change
    // takes it away silently — and this lane rewrote how /chat wraps its
    // composer. Two pins used to stand here, one per flag side; they varied an
    // axis this guard never read, so they were one pin wearing two hats.
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-composer')).not.toBeNull());
    expect(screen.getByTestId('report-bug-fab')).toBeTruthy();
  });

  it('is ABSENT on the pre-auth surface, where showNav is false', async () => {
    // The OTHER branch of `showBugReport ?? showNav`, which the pin above cannot
    // reach: it renders the authed page, so it stays green against
    // `{true && <ReportBugFab />}` — a FAB that renders unconditionally,
    // including on the signed-out screen #95's docstring is explicit about
    // excluding (no verified reporter yet, and the BFF would refuse the report
    // with invalid_session anyway).
    //
    // Driven through /chat's OWN pre-auth branch rather than a hand-rendered
    // Layout: that branch is the production call site that passes showNav={false},
    // and a test that reconstructs the composition instead of driving it is
    // testing its own copy of it.
    session.user = null;
    session.loading = true;
    render(<ChatPage />);

    // The control that makes the absence mean something: the pre-auth branch
    // really did render. Without it this passes just as well against a page that
    // rendered nothing at all.
    expect(await screen.findByTestId('auth-gate')).toBeTruthy();
    expect(screen.queryByTestId('unified-input')).toBeNull();
    expect(screen.queryByTestId('report-bug-fab')).toBeNull();
  });
});
