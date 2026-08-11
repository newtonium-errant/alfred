import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Readable } from 'stream';

// #100 — the batch submit's wire-level idempotency key, on the CLIENT side.
//
// THE FAILURE: a submit lands on the box, the box saves the scans and answers,
// and the ANSWER is lost (tunnel blip, BFF timeout, phone drops to cell). The
// operator sees "couldn't reach the instance" and presses Submit again. Without
// a key on the wire that retry mints a SECOND batch and the drip pays twice.
//
// The #97 chip-level mitigation does NOT cover this shape and never could: it
// clears a chip that finished, and in this failure the chip never finished.
//
// BOTH CLIENTS ARE PINNED THROUGH THEIR COMPONENTS, not through the helper.
// `keyForStagedBatch` returning a stable key is not the property that matters —
// the property is that BatchForm and UnifiedComposer actually put it on the
// request, and a helper-only test stays green against a component that never
// calls it. That is the wiring gap this file exists to close.

import {
  BATCH_IDEMPOTENCY_HEADER,
  isBatchIdempotencyKey,
  keyForStagedBatch,
  stagedBatchSignature,
} from '../lib/algernon/batchSubmit';

function img(name: string, size = 64, type = 'image/jpeg'): File {
  return new File([new Uint8Array(size)], name, { type });
}

/** The key on a fetch call, or undefined when the header was not sent. */
function headerKey(call: unknown[]): string | undefined {
  const init = call[1] as RequestInit | undefined;
  const headers = (init?.headers ?? {}) as Record<string, string>;
  return headers[BATCH_IDEMPOTENCY_HEADER];
}

// ---------------------------------------------------------------------------
// The staged-set signature — what makes the key per-SET rather than per-attempt
// ---------------------------------------------------------------------------

describe('the staged-set signature', () => {
  const base = { target: 'Salem', instruction: 'Read the total.', files: [img('a.jpg')] };

  it('holds the SAME key while the staged set is unchanged', async () => {
    const sig = stagedBatchSignature(base);
    const first = keyForStagedBatch(null, sig);
    const second = keyForStagedBatch(first, sig);
    expect(second.key).toBe(first.key);
  });

  it('mints a NEW key when the files change', async () => {
    const first = keyForStagedBatch(null, stagedBatchSignature(base));
    const changed = stagedBatchSignature({ ...base, files: [img('a.jpg'), img('b.jpg')] });
    expect(keyForStagedBatch(first, changed).key).not.toBe(first.key);
  });

  it('mints a NEW key when the INSTRUCTION changes', async () => {
    // Load-bearing, and the reason the instruction is in the signature at all:
    // if an edited instruction reused the key, the box would replay the first
    // receipt and the operator's correction would be silently discarded while
    // the UI said it had been sent.
    const first = keyForStagedBatch(null, stagedBatchSignature(base));
    const edited = stagedBatchSignature({ ...base, instruction: 'Read the DATE.' });
    expect(keyForStagedBatch(first, edited).key).not.toBe(first.key);
  });

  it('mints a NEW key when the target instance changes', async () => {
    const first = keyForStagedBatch(null, stagedBatchSignature(base));
    const elsewhere = stagedBatchSignature({ ...base, target: 'VERA' });
    expect(keyForStagedBatch(first, elsewhere).key).not.toBe(first.key);
  });

  it('mints keys the wire will accept', async () => {
    const minted = keyForStagedBatch(null, stagedBatchSignature(base)).key;
    expect(isBatchIdempotencyKey(minted)).toBe(true);
  });
});

describe('key well-formedness', () => {
  it.each(['', '   ', 'has space', 'new\nline', 'semi;colon', 'a'.repeat(201)])(
    'rejects %j',
    (bad) => {
      expect(isBatchIdempotencyKey(bad)).toBe(false);
    },
  );

  it.each(['11111111-2222-4333-8444-555555555555', 'batch-abc-def', 'A_b-1'])(
    'accepts %j (the positive control for the table above)',
    (good) => {
      expect(isBatchIdempotencyKey(good)).toBe(true);
    },
  );

  it('rejects a non-string', () => {
    expect(isBatchIdempotencyKey(undefined)).toBe(false);
    expect(isBatchIdempotencyKey(['a'])).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Client 1 — the /batch page
// ---------------------------------------------------------------------------

describe('BatchForm sends the key', () => {
  const originalFetch = global.fetch;
  const TARGETS = { targets: [{ name: 'Salem', label: 'Salem', home: true }] };
  let fetchMock: ReturnType<typeof vi.fn>;

  /** Route by URL; `submits` is consumed one response per submit attempt. */
  function mockFetch(submits: Array<{ ok: boolean; status: number; json: () => Promise<unknown> }>) {
    let n = 0;
    fetchMock = vi.fn(async (url: string) => {
      if (String(url).startsWith('/api/batch/targets')) {
        return { ok: true, status: 200, json: async () => TARGETS };
      }
      const next = submits[Math.min(n, submits.length - 1)];
      n += 1;
      return next;
    });
    global.fetch = fetchMock as unknown as typeof global.fetch;
    return fetchMock;
  }

  function submitCalls() {
    return fetchMock.mock.calls.filter((c) => String(c[0]).startsWith('/api/batch/submit'));
  }

  function pick(input: HTMLInputElement, files: File[]) {
    Object.defineProperty(input, 'files', { value: files, configurable: true });
    fireEvent.change(input);
  }

  // `json` MUST be async: the form does `res.json().catch(...)`, and a
  // synchronous stub makes every response take the network-error path — which
  // looks like a passing refusal test while proving nothing about the response.
  const ok = {
    ok: true,
    status: 200,
    json: async () => ({
      status: 'queued', batch_id: 'b-1', images: 1, bytes: 64,
      path: 'note/b-1.md', instance: 'Salem',
    }),
  };
  const lost = {
    ok: false,
    status: 502,
    json: async () => ({ error: 'transport_unreachable' }),
  };

  async function stageAndSubmit(files = [img('a.jpg')], instruction = 'Read the total.') {
    const { BatchForm } = await import('../components/ingest/BatchForm');
    render(<BatchForm />);
    await waitFor(() => expect(screen.queryByTestId('batch-form')).not.toBeNull());
    pick(screen.getByTestId('batch-files') as HTMLInputElement, files);
    await waitFor(() => expect(screen.queryByTestId('batch-count')).not.toBeNull());
    fireEvent.change(screen.getByTestId('batch-instruction'), { target: { value: instruction } });
    fireEvent.click(screen.getByTestId('batch-submit'));
  }

  afterEach(() => {
    global.fetch = originalFetch;
    vi.clearAllMocks();
  });

  it('puts a well-formed key on the submit request', async () => {
    mockFetch([ok]);
    await stageAndSubmit();
    await waitFor(() => expect(submitCalls()).toHaveLength(1));
    const key = headerKey(submitCalls()[0]);
    expect(isBatchIdempotencyKey(key ?? '')).toBe(true);
  });

  it('RESENDS THE SAME KEY after a lost response — the #100 pin', async () => {
    // The retry is the operator pressing Submit again on an unchanged staged
    // set, which is exactly what they do when the first attempt reported a
    // network failure.
    mockFetch([lost, ok]);
    await stageAndSubmit();
    await waitFor(() => expect(screen.queryByTestId('batch-error')).not.toBeNull());

    fireEvent.click(screen.getByTestId('batch-submit'));
    await waitFor(() => expect(submitCalls()).toHaveLength(2));

    const first = headerKey(submitCalls()[0]);
    expect(first).toBeTruthy();
    expect(headerKey(submitCalls()[1])).toBe(first);
  });

  it('POSITIVE CONTROL — editing the instruction between attempts mints a NEW key', async () => {
    // Without this, "the two keys matched" would pass identically against a
    // build that hardcoded one constant key for every submission ever made —
    // which would dedupe unrelated batches into the first one.
    mockFetch([lost, ok]);
    await stageAndSubmit();
    await waitFor(() => expect(screen.queryByTestId('batch-error')).not.toBeNull());

    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Actually, read the DATE.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));
    await waitFor(() => expect(submitCalls()).toHaveLength(2));

    expect(headerKey(submitCalls()[1])).not.toBe(headerKey(submitCalls()[0]));
  });

  it('RETIRES the key after a success, so a deliberate re-send is a new batch', async () => {
    // An operator who re-picks the same scans with the same instruction is
    // asking for a SECOND batch. Holding the spent key would rebuild the same
    // signature and hand them the first batch's receipt instead.
    mockFetch([ok, ok]);
    await stageAndSubmit();
    await waitFor(() => expect(screen.queryByTestId('batch-success')).not.toBeNull());
    const firstKey = headerKey(submitCalls()[0]);

    fireEvent.click(screen.getByTestId('batch-another'));
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg')]);
    await waitFor(() => expect(screen.queryByTestId('batch-count')).not.toBeNull());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));
    await waitFor(() => expect(submitCalls()).toHaveLength(2));

    expect(headerKey(submitCalls()[1])).not.toBe(firstKey);
  });

  it('does NOT set Content-Type, which would destroy the multipart boundary', async () => {
    // Adding a header to this request is the change that could plausibly have
    // added the wrong one. A hand-set Content-Type produces a body the box
    // cannot parse, and the failure looks like an empty batch.
    mockFetch([ok]);
    await stageAndSubmit();
    await waitFor(() => expect(submitCalls()).toHaveLength(1));
    const init = submitCalls()[0][1] as RequestInit;
    const headers = (init.headers ?? {}) as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain('content-type');
  });
});

// ---------------------------------------------------------------------------
// Client 2 — the unified composer's batch chip
// ---------------------------------------------------------------------------

const { ingestTargets, ingestSubmit } = vi.hoisted(() => ({
  ingestTargets: vi.fn(),
  ingestSubmit: vi.fn(),
}));
vi.mock('../lib/algernon/client', () => ({
  ingestApi: { targets: ingestTargets, submit: ingestSubmit },
  chatApi: {},
}));

describe('UnifiedComposer sends the key', () => {
  let batchSubmit: ReturnType<typeof vi.fn>;

  async function mountComposer() {
    const { UnifiedComposer } = await import('../components/chat/UnifiedComposer');
    return render(
      <UnifiedComposer
        onSend={vi.fn()}
        instance="Salem"
        instanceLabel="Salem"
        submitBatchRequest={batchSubmit as never}
        transcribe={vi.fn() as never}
      />,
    );
  }

  /** Stage five images (which defaults to the BATCH chip) and send. */
  async function stageBatchAndSend(user: ReturnType<typeof userEvent.setup>, instruction: string) {
    await user.upload(
      screen.getByTestId('unified-file-input'),
      [1, 2, 3, 4, 5].map((n) => img(`s${n}.png`, 64, 'image/png')),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed'),
      ).toBe('true'),
    );
    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: instruction } });
    await user.click(screen.getByTestId('unified-send'));
  }

  beforeEach(() => {
    ingestTargets.mockResolvedValue({ targets: [] });
    (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async (url: string) => {
      if (String(url).startsWith('/api/batch/targets')) {
        return { ok: true, json: async () => ({ targets: [{ name: 'Salem', label: 'Salem', home: true }] }) };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  afterEach(() => vi.clearAllMocks());

  it('passes a well-formed key as the third argument', async () => {
    batchSubmit = vi.fn(async () => ({
      status: 'queued', batch_id: 'b-1', images: 5, bytes: 320,
      path: 'note/b-1.md', instance: 'Salem',
    }));
    const user = userEvent.setup();
    await mountComposer();
    await stageBatchAndSend(user, 'read the invoice number');

    await waitFor(() => expect(batchSubmit).toHaveBeenCalledTimes(1));
    const key = batchSubmit.mock.calls[0][2] as string;
    expect(isBatchIdempotencyKey(key)).toBe(true);
  });

  it('RESENDS THE SAME KEY when a failed send is retried', async () => {
    // A failed batch KEEPS its chip and its files (#97), so Send again is one
    // tap — and that tap must not mint a second batch when the first actually
    // landed. This is the composer's half of the #100 pin.
    let attempt = 0;
    batchSubmit = vi.fn(async () => {
      attempt += 1;
      if (attempt === 1) {
        const { ApiError } = await import('../lib/algernon/http');
        throw new ApiError(502, 'transport_unreachable');
      }
      return {
        status: 'queued', batch_id: 'b-1', images: 5, bytes: 320,
        path: 'note/b-1.md', instance: 'Salem',
      };
    });
    const user = userEvent.setup();
    await mountComposer();
    await stageBatchAndSend(user, 'read the invoice number');
    await waitFor(() => expect(batchSubmit).toHaveBeenCalledTimes(1));

    // The instruction is retained on failure, so Send again is the whole retry.
    await user.click(screen.getByTestId('unified-send'));
    await waitFor(() => expect(batchSubmit).toHaveBeenCalledTimes(2));

    const first = batchSubmit.mock.calls[0][2] as string;
    expect(first).toBeTruthy();
    expect(batchSubmit.mock.calls[1][2]).toBe(first);
  });
});

// ---------------------------------------------------------------------------
// The BFF door — allowlist the key, never mint one
// ---------------------------------------------------------------------------

const identity = vi.hoisted(() => ({
  resolveSessionToken: vi.fn(),
  readDisplayIdentity: vi.fn(),
}));
const transport = vi.hoisted(() => ({
  callTransportBatch: vi.fn(),
  listBatchTargets: vi.fn(),
}));
vi.mock('../lib/algernon/identity', () => identity);
vi.mock('../lib/algernon/transport', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../lib/algernon/transport');
  return { ...actual, ...transport };
});

describe('/api/batch/submit relays the key', () => {
  const GOOD = '11111111-2222-4333-8444-555555555555';

  function req(headers: Record<string, string> = {}) {
    const stream = Readable.from([Buffer.from('body')]) as unknown as Record<string, unknown>;
    stream.method = 'POST';
    stream.query = {};
    stream.headers = {
      'content-type': 'multipart/form-data; boundary=----abc',
      ...headers,
    };
    return stream as never;
  }

  function res() {
    const out: { status?: number; body?: unknown } = {};
    const r = {
      status(code: number) { out.status = code; return r; },
      json(payload: unknown) { out.body = payload; return r; },
      setHeader() { /* unused */ },
    };
    return { r: r as never, out };
  }

  async function call(headers: Record<string, string> = {}) {
    const handler = (await import('../pages/api/batch/submit')).default;
    const { r, out } = res();
    await handler(req(headers), r);
    return out;
  }

  beforeEach(() => {
    identity.resolveSessionToken.mockReturnValue('session-token');
    identity.readDisplayIdentity.mockReturnValue({ name: 'Andrew', role: 'owner' });
    transport.listBatchTargets.mockReturnValue([{ name: 'Salem', label: 'Salem', home: true }]);
    transport.callTransportBatch.mockResolvedValue({
      status: 200, body: { status: 'queued', batch_id: 'b1' },
    });
  });

  afterEach(() => vi.clearAllMocks());

  it('relays a well-formed key VERBATIM', async () => {
    await call({ [BATCH_IDEMPOTENCY_HEADER.toLowerCase()]: GOOD });
    expect(transport.callTransportBatch).toHaveBeenCalledTimes(1);
    expect(transport.callTransportBatch.mock.calls[0][1].idempotencyKey).toBe(GOOD);
  });

  it('DROPS a malformed key rather than relaying it', async () => {
    // Fail-open, and deliberately: the key is a double-submit guard, not an
    // authorisation, so a client bug must not cost the operator their scans.
    // It is dropped rather than forwarded because the BFF puts this value into
    // an outbound request header.
    await call({ [BATCH_IDEMPOTENCY_HEADER.toLowerCase()]: 'not a key\r\nX-Evil: 1' });
    expect(transport.callTransportBatch).toHaveBeenCalledTimes(1);
    expect(transport.callTransportBatch.mock.calls[0][1].idempotencyKey).toBeUndefined();
  });

  it('NEVER mints a key of its own when the client sent none', async () => {
    // A BFF-minted key would be per-RELAY: a retry would arrive carrying a
    // different one and mint a second batch, which is the exact failure the
    // key exists to prevent — while looking, on the wire, entirely correct.
    await call();
    expect(transport.callTransportBatch).toHaveBeenCalledTimes(1);
    expect(transport.callTransportBatch.mock.calls[0][1].idempotencyKey).toBeUndefined();
  });

  it('still relays the batch itself when the key is absent', async () => {
    // The positive control for both drops above: a missing or malformed key
    // must leave the submission WORKING, not refuse it.
    const out = await call();
    expect(out.status).toBe(200);
    expect(out.body).toEqual({ status: 'queued', batch_id: 'b1' });
  });
});
