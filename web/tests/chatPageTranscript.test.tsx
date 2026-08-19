import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// #54 — the FE thread, END TO END through the real page.
//
// WHY THIS TEST EXISTS AND WHY IT IS NOT A UNIT TEST. `transcript` is an
// optional parameter threaded through four layers (UnifiedComposer → chat.tsx →
// useChat → chatApi) before it reaches the wire. Every one of those layers can
// be pinned in isolation and still drop the value at the join: a call site that
// forgets the argument type-checks fine (passing fewer arguments is legal TS),
// so nothing would go red while the feature is dead in the field. That is the
// exact failure this project has shipped before — a gate parameter tested only
// by direct invocation, green everywhere, never threaded in production.
//
// So this renders the REAL /chat page with the REAL hook and the REAL client,
// mocking only what the browser itself provides (fetch, router, session) plus
// VoiceCapture (MediaRecorder does not exist in jsdom). The assertion is on the
// JSON body that actually reaches `fetch` — the wire, not an intermediate call.

const { mockReplace } = vi.hoisted(() => ({ mockReplace: vi.fn() }));

vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), query: {} }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn(), me: vi.fn() } }));

// The dictated text this run delivers. Mutable so one test can dictate twice and
// another can overflow the bound; reset to the default before every test.
const { spoken } = vi.hoisted(() => ({ spoken: { text: 'clean the chicken tracker' } }));
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ onTranscript, idPrefix }: { onTranscript: (t: string) => void; idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock-use`} onClick={() => onTranscript(spoken.text)}>
      mock-stt
    </button>
  ),
  // `UnifiedComposer` imports this beside the component; without it the mocked
  // module has no such export and the import is `undefined` at call time.
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));

import ChatPage from '../pages/chat';
import { MAX_TRANSCRIPT_CHARS } from '../lib/algernon/schemas';

// A 200 SSE response carrying one terminal `done` frame, shaped the way useChat
// consumes it (res.body.getReader()).
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

function jsonOk(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as unknown as Response;
}

// Every fetch the page makes, routed by URL. `streamable` false makes
// /api/chat/stream return a 200 with NO readable body, which is exactly how
// useChat falls back to the buffered /api/chat/turn path — that is how the same
// operator gesture is driven down BOTH production entry points.
let fetchSpy: ReturnType<typeof vi.fn>;
function installFetch(opts: { streamable?: boolean } = {}) {
  const streamable = opts.streamable ?? true;
  fetchSpy = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.startsWith('/api/chat/targets')) return jsonOk({ targets: [] });
    // The unified door asks for these on mount. Empty is the honest answer for
    // this fixture — nothing here files or batches — and an unrouted fetch would
    // otherwise fall through to the catch-all below.
    if (url.startsWith('/api/ingest/targets')) return jsonOk({ targets: [] });
    if (url.startsWith('/api/batch/targets')) return jsonOk({ targets: [] });
    if (url.startsWith('/api/chat/open')) return jsonOk({ session_key: 'sess-1' });
    if (url.startsWith('/api/chat/history')) return jsonOk({ turns: [] });
    if (url.startsWith('/api/chat/notifications')) return jsonOk({ notifications: [], unread: 0 });
    if (url.startsWith('/api/chat/stream')) {
      return streamable ? streamOk() : ({ ok: true, status: 200, body: null } as unknown as Response);
    }
    if (url.startsWith('/api/chat/turn')) {
      return jsonOk({ reply: 'ok', session_key: 'sess-1', ts: 'T', user_ts: 'U' });
    }
    void init;
    return jsonOk({});
  });
  (globalThis as unknown as { fetch: unknown }).fetch = fetchSpy;
}

// The parsed JSON body of the last POST to `url`, or null if it was never called.
function lastBodyTo(url: string): Record<string, unknown> | null {
  const calls = fetchSpy.mock.calls.filter((c) => String(c[0]).startsWith(url));
  if (calls.length === 0) return null;
  const init = calls[calls.length - 1][1] as RequestInit | undefined;
  return JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
}

beforeEach(() => {
  spoken.text = 'clean the chicken tracker';
  mockReplace.mockReset();
  localStorage.clear();
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ONE DOOR. This file used to make every assertion below TWICE — once against
// the legacy composer /chat rendered with NEXT_PUBLIC_UNIFIED_COMPOSER unset,
// once against the unified one it rendered with the flag on. The
// composer-deletion lane removed the flag and the legacy component, so the
// duplicate half went with them.
//
// Only one pin was unique to that half and it is kept, ported below: the
// over-long transcript drop. Everything else the legacy block asserted is
// asserted here about the door that actually serves /chat.
describe('/chat — a voice-seeded send carries the transcript to the wire', () => {
  async function readyUnifiedPage() {
    render(<ChatPage />);
    await waitFor(() => expect(screen.queryByTestId('unified-input')).not.toBeNull());
  }

  it('the STREAM body carries the raw transcript alongside the edited message', async () => {
    await readyUnifiedPage();
    const input = screen.getByTestId('unified-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('clean the chicken tracker'));
    fireEvent.change(input, { target: { value: 'clean the chicken tractor' } });
    fireEvent.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/stream')).not.toBeNull());
    const body = lastBodyTo('/api/chat/stream')!;
    expect(body.message).toBe('clean the chicken tractor');
    expect(body.transcript).toBe('clean the chicken tracker');
    expect(body.kind).toBe('voice');
  });

  it('the buffered TURN body carries it too (the stream-fallback entry point)', async () => {
    installFetch({ streamable: false });
    await readyUnifiedPage();
    const input = screen.getByTestId('unified-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('clean the chicken tracker'));
    fireEvent.change(input, { target: { value: 'clean the chicken tractor' } });
    fireEvent.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/turn')).not.toBeNull());
    const body = lastBodyTo('/api/chat/turn')!;
    expect(body.message).toBe('clean the chicken tractor');
    expect(body.transcript).toBe('clean the chicken tracker');
    expect(body.kind).toBe('voice');
  });

  it('a TYPED send is byte-identical to the pre-feature body — absent, not null', async () => {
    // The control the two pins above need. Without it "transcript is present"
    // proves nothing about the threading: a composer that hard-coded the field
    // on every send would satisfy them both and be wrong in the commonest case.
    //
    // ABSENT, not `transcript: null` / `transcript: ""` — a present-but-empty
    // key is a different wire shape, and the backend's "an older client" branch
    // keys on the field simply not being there.
    await readyUnifiedPage();
    const input = screen.getByTestId('unified-input') as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: 'just typing' } });
    fireEvent.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/stream')).not.toBeNull());
    const body = lastBodyTo('/api/chat/stream')!;
    expect('transcript' in body).toBe(false);
    expect(body.kind).toBe('text');
  });

  it('an over-long transcript is dropped, and the turn still sends', async () => {
    // PORTED FROM THE DELETED LEGACY BLOCK — the one pin that half made and this
    // one did not. Capture is telemetry. If an oversized transcript rode along
    // it would fail the BFF's zod bound and 400 the whole turn: the operator
    // would lose a message he spoke, to a learning feature. Dropping it is the
    // honest trade, and it is made END TO END here rather than at the hook
    // (`useComposerText.test.tsx` pins the drop itself) because what this asks
    // is whether the TURN still reaches the wire without it.
    spoken.text = 'x'.repeat(MAX_TRANSCRIPT_CHARS + 1);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    await readyUnifiedPage();

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    const input = screen.getByTestId('unified-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.value.length).toBe(MAX_TRANSCRIPT_CHARS + 1));
    fireEvent.change(input, { target: { value: 'short after all' } });
    fireEvent.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(lastBodyTo('/api/chat/stream')).not.toBeNull());
    const body = lastBodyTo('/api/chat/stream')!;
    expect(body.message).toBe('short after all');
    expect('transcript' in body).toBe(false);
    // ILB: the drop is announced, never silent — a missing transcript otherwise
    // looks identical to a broken capture. Lengths only, never the words.
    expect(warn).toHaveBeenCalledTimes(1);
    const said = String(warn.mock.calls[0][0]);
    expect(said).toContain('transcript dropped');
    expect(said).toContain(String(MAX_TRANSCRIPT_CHARS));
  });
});
