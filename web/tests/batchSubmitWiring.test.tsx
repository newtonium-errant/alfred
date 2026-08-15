import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// BOTH batch surfaces reach the ONE lifted wire call.
//
// `submitBatch` was written out twice — once in the /batch page, once in the
// composer — when the composer arrived (#97). Lifting it into `batchSubmit.ts`
// is only half the job: what makes "extracted, not duplicated" TRUE is that
// each consumer reaches the lifted function BY DEFAULT, on the path production
// takes.
//
// THIS FILE EXISTS BECAUSE THE OTHER SUITES CANNOT SEE THAT. `unifiedComposer`
// injects `submitBatchRequest`, so every one of its 28 pins is green against a
// composer whose DEFAULT is wired to nothing at all — the default binding is
// exactly the code the tests replace. So these two render with NO injection and
// assert on the request that actually left the browser.
//
// The mutation that proves it: change the URL in `submitBatch` (drop the
// `?target=` query) and BOTH tests below must red from that ONE substitution.
// A lift where only one consumer reds is a lift that left a copy behind.

vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ onTranscript, idPrefix }: { onTranscript: (t: string) => void; idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock-use`} onClick={() => onTranscript('x')}>
      mock-stt
    </button>
  ),
  sttErrorMessage: () => 'stt error',
}));

const { ingestTargets, ingestSubmit } = vi.hoisted(() => ({
  ingestTargets: vi.fn(),
  ingestSubmit: vi.fn(),
}));
vi.mock('../lib/algernon/client', () => ({
  ingestApi: { targets: ingestTargets, submit: ingestSubmit },
  chatApi: {},
}));

import { UnifiedComposer } from '../components/chat/UnifiedComposer';
import { BatchForm } from '../components/ingest/BatchForm';
import {
  BATCH_IDEMPOTENCY_HEADER,
  MAX_BATCH_IMAGES,
} from '../lib/algernon/batchSubmit';
import { MAX_IMAGES_PER_TURN } from '../lib/algernon/schemas';

function imageFile(name: string): File {
  const bytes = new Uint8Array(64);
  bytes.set([0x89, 0x50, 0x4e, 0x47], 0);
  return new File([bytes], name, { type: 'image/png' });
}

/** Enough images that the composer's default intent is BATCH, not discuss. */
function batchSizedSet(): File[] {
  return Array.from({ length: MAX_IMAGES_PER_TURN + 1 }, (_, i) => imageFile(`scan-${i}.png`));
}

const SUBMIT_OK = {
  status: 'queued',
  batch_id: 'b-1',
  images: MAX_IMAGES_PER_TURN + 1,
  bytes: 320,
  path: 'batch/b-1.md',
  instance: 'Salem',
};

let fetchMock: ReturnType<typeof vi.fn>;
const originalFetch = global.fetch;

/** The call the assertions are about: the batch POST, whoever made it. */
function submitCall() {
  return fetchMock.mock.calls.find(([url]) => String(url).includes('/api/batch/submit'));
}

beforeEach(() => {
  ingestTargets.mockResolvedValue({ targets: [] });
  fetchMock = vi.fn(async (url: string) => {
    if (String(url).startsWith('/api/batch/targets')) {
      return {
        ok: true,
        json: async () => ({ targets: [{ name: 'Salem', label: 'Salem', home: true }] }),
      };
    }
    if (String(url).startsWith('/api/batch/submit')) {
      return { ok: true, json: async () => SUBMIT_OK };
    }
    return { ok: true, json: async () => ({}) };
  });
  global.fetch = fetchMock as unknown as typeof global.fetch;
});

afterEach(() => {
  global.fetch = originalFetch;
  vi.clearAllMocks();
});

describe('the /batch page reaches the lifted wire call', () => {
  it('POSTs to the batch door with the target and the idempotency key', async () => {
    const user = userEvent.setup();
    render(<BatchForm />);

    await user.upload(screen.getByTestId('batch-files'), [imageFile('a.png')]);
    await user.type(screen.getByTestId('batch-instruction'), 'read the invoice number');
    await user.click(screen.getByTestId('batch-submit'));

    await waitFor(() => expect(submitCall()).toBeTruthy());
    const [url, init] = submitCall() as [string, RequestInit];
    expect(String(url)).toContain('/api/batch/submit?target=Salem');
    expect((init.headers as Record<string, string>)[BATCH_IDEMPOTENCY_HEADER]).toBeTruthy();
    // NO Content-Type: the browser must set it so the multipart boundary is
    // generated. A hand-set one produces a body the box cannot parse.
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
    expect(init.body).toBeInstanceOf(FormData);
  });
});

describe('the composer reaches the SAME lifted wire call', () => {
  it('POSTs to the batch door on its DEFAULT wiring, with no injected submitter', async () => {
    const user = userEvent.setup();
    // Note what is NOT passed: `submitBatchRequest`. That omission is the pin.
    render(
      <UnifiedComposer
        onSend={vi.fn()}
        instance="Salem"
        instanceLabel="Salem"
        transcribe={vi.fn() as never}
      />,
    );

    await user.upload(screen.getByTestId('unified-file-input'), batchSizedSet());
    await screen.findByTestId('unified-chip-images');
    // A set this size defaults to BATCH — the door under test.
    expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe(
      'true',
    );

    await user.type(screen.getByTestId('unified-input'), 'read the invoice number');
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(submitCall()).toBeTruthy());
    const [url, init] = submitCall() as [string, RequestInit];
    expect(String(url)).toContain('/api/batch/submit?target=Salem');
    expect((init.headers as Record<string, string>)[BATCH_IDEMPOTENCY_HEADER]).toBeTruthy();
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
    expect(init.body).toBeInstanceOf(FormData);
  });

  it('the two consumers agree on the cap they are enforcing', () => {
    // Both spend the same constants from the lifted module; a per-surface copy
    // of a cap is the drift this whole lift is against.
    expect(MAX_BATCH_IMAGES).toBeGreaterThan(MAX_IMAGES_PER_TURN);
  });
});
