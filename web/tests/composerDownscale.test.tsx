/**
 * #82 — the composer actually ROUTES attachments through the downscaler.
 *
 * This file exists because `imageDownscale.test.ts` cannot prove it. That
 * suite tests the helper by calling it directly, so it stays fully green
 * against a build where the pick path never calls it at all — the helper ships,
 * every pin passes, and in the field images go up at full size and wedge the
 * session exactly as before. Under jsdom the downscale is also a no-op (no
 * canvas), so there is no observable size change to assert on either.
 *
 * So the helper is mocked to return a MARKER file, and the assertion is that
 * the marker's bytes are what reach `onSend`. That is only true if the
 * production path calls it.
 *
 * ONE DOOR NOW. This file used to carry the same pins twice — once against the
 * legacy `Composer`, once against `UnifiedComposer` — because the legacy one
 * was the rollback while #97 baked. The composer-deletion lane removed that
 * component, so only the live door remains, and the legacy half went with it.
 * Nothing was weakened in the move: the legacy pins asserted the same three
 * facts about a component that no longer exists.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: () => <button type="button">mock-stt</button>,
  // `UnifiedComposer` imports this alongside the component; without it the
  // mocked module has no such export and the import is `undefined` at call time.
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));

// The marker: a distinct byte pattern and a distinct media type, so both
// "did the bytes come from here" and "did the re-encoded type follow" are
// observable at onSend.
const MARKER_BYTES = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]);
const downscaleImage = vi.fn(async (file: File, ..._rest: unknown[]) => ({
  file: new File([MARKER_BYTES], file.name, { type: 'image/jpeg' }),
  resized: true,
  source: { width: 3000, height: 2000 } as { width: number; height: number } | null,
}));
// vi.mock factories are HOISTED above every const in this file, so the values
// below are inlined here and re-declared for the assertions afterwards. A
// reference to a module-scope const here is a ReferenceError at collect time.
vi.mock('../lib/algernon/imageDownscale', () => ({
  downscaleImage: (file: File, edge?: number, quality?: number) =>
    downscaleImage(file, edge, quality),
  MAX_IMAGE_EDGE_PX: 2576,
  MANY_IMAGE_DIMENSION_LIMIT_PX: 2000,
  STEP_DOWN_EDGE_PX: 1800,
  STEP_DOWN_QUALITY: 0.72,
}));

const STEP_DOWN_EDGE_PX = 1800;
const STEP_DOWN_QUALITY = 0.72;

/** A File whose reported size is forced, without allocating the bytes. */
function sized(file: File, bytes: number): File {
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}

import { UnifiedComposer } from '../components/chat/UnifiedComposer';
import { MAX_BATCH_IMAGE_BYTES } from '../lib/algernon/batchSubmit';

function imageFile(name = 'scan.png', type = 'image/png', size = 4096): File {
  const bytes = new Uint8Array(size);
  bytes.set([0x89, 0x50, 0x4e, 0x47], 0);
  return new File([bytes], name, { type });
}

// The composer asks for its ingest/batch targets on mount. Neither matters to
// these pins — an image kept on the `discuss` chip goes to `onSend`, not to a
// route — but an unmocked fetch would be a real network call in a unit test.
function stubTargets() {
  (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ targets: [] }),
  }));
}

function renderUnified(onSend = vi.fn()) {
  stubTargets();
  render(
    <UnifiedComposer
      onSend={onSend}
      instance="salem"
      instanceLabel="Salem"
      submitIngest={vi.fn()}
      submitBatchRequest={vi.fn()}
    />,
  );
  return onSend;
}

describe('UnifiedComposer → imageDownscale wiring (the live door)', () => {
  it('passes every attached image through downscaleImage', async () => {
    downscaleImage.mockClear();
    const user = userEvent.setup();
    renderUnified();

    await user.upload(screen.getByTestId('unified-file-input'), imageFile());
    await screen.findByTestId('unified-chip-images');

    expect(downscaleImage).toHaveBeenCalledTimes(1);
    expect((downscaleImage.mock.calls[0][0] as File).name).toBe('scan.png');
  });

  it('sends the DOWNSCALED bytes, not the originally-picked file', async () => {
    // The load-bearing assertion. Remove the downscale from the unified pick
    // path and this reddens, because the originally-picked bytes would reach
    // onSend instead.
    downscaleImage.mockClear();
    const user = userEvent.setup();
    const onSend = renderUnified();

    await user.upload(screen.getByTestId('unified-file-input'), imageFile());
    await screen.findByTestId('unified-chip-images');
    // A single image defaults to the `discuss` chip — the conversation route,
    // which is the one that carries bytes to `onSend`.
    expect(screen.getByTestId('unified-images-intent-discuss').getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByTestId('unified-send'));

    const images = onSend.mock.calls[0][2];
    expect(images).toHaveLength(1);
    // btoa of the marker bytes — what the composer's readAsBase64 produces
    // for the file the downscaler returned.
    const expected = btoa(String.fromCharCode(...MARKER_BYTES));
    expect(images[0].data).toBe(expected);
  });

  it('carries the re-encoded media type, not the picked one', async () => {
    // A WebP/PNG that comes back as JPEG must be LABELLED jpeg — a mismatched
    // media_type is what makes the model receive a corrupt image.
    downscaleImage.mockClear();
    const user = userEvent.setup();
    const onSend = renderUnified();

    await user.upload(screen.getByTestId('unified-file-input'), imageFile('scan.png', 'image/png'));
    await screen.findByTestId('unified-chip-images');
    await user.click(screen.getByTestId('unified-send'));

    expect(onSend.mock.calls[0][2][0].media_type).toBe('image/jpeg');
  });
});

// THE STEP-DOWN WIRING, MOVED TO THE LIVE DOOR.
//
// These three pins used to drive the legacy `Composer`, which reached the
// step-down through `collectImageAttachments`. That component is deleted, but
// the property is NOT legacy: the retry itself lives in the shared
// `prepareImageForUpload` (lib/algernon/imagePrepare.ts), and the unified door
// runs it via `prepareBatch(picked, prepareImageForUpload, …)` at the pick.
// So the pin transfers rather than retires — and it was never made on this door
// before, which is precisely the gap it exists to catch.
//
// The bound differs and the promise does not: `prepareBatch` judges each
// prepared file against MAX_BATCH_IMAGE_BYTES, so that is what these force.
describe('byte-budget step-down wiring (#83 NOTE-B), on the live door', () => {
  it('retries ONCE with the step-down params when the first result is over budget', async () => {
    // The wiring pin. imageDownscale.test.ts proves the step-down works when
    // called; it stays green against a composer that never calls it — which is
    // exactly what the first attempt at this shipped, and what the probe found.
    downscaleImage.mockClear();
    const over = sized(
      new File([MARKER_BYTES], 'huge.png', { type: 'image/jpeg' }),
      MAX_BATCH_IMAGE_BYTES + 1,
    );
    const under = new File([MARKER_BYTES], 'huge.png', { type: 'image/jpeg' });
    downscaleImage
      .mockResolvedValueOnce({ file: over, resized: true, source: null })
      .mockResolvedValueOnce({ file: under, resized: true, source: null });

    const user = userEvent.setup();
    const onSend = renderUnified();
    await user.upload(screen.getByTestId('unified-file-input'), imageFile('huge.png'));
    await screen.findByTestId('unified-chip-images');

    expect(downscaleImage).toHaveBeenCalledTimes(2);
    // Second call carries the step-down edge AND quality — passing only the
    // edge would skip the forced re-encode that sheds the bytes.
    expect(downscaleImage.mock.calls[1].slice(1)).toEqual([
      STEP_DOWN_EDGE_PX, STEP_DOWN_QUALITY,
    ]);
    // And the attachment survived: never-blocks-the-user, end to end.
    await user.click(screen.getByTestId('unified-send'));
    expect(onSend.mock.calls[0][2]).toHaveLength(1);
  });

  it('does NOT retry when the first result is already under budget', async () => {
    // One retry, not a loop, and no wasted re-encode on the common path.
    downscaleImage.mockClear();
    const user = userEvent.setup();
    renderUnified();
    await user.upload(screen.getByTestId('unified-file-input'), imageFile('ok.png'));
    await screen.findByTestId('unified-chip-images');
    expect(downscaleImage).toHaveBeenCalledTimes(1);
  });

  it('still refuses when even the step-down cannot get under budget', async () => {
    // The give-up path stays honest: one retry, then the image is named in an
    // explicit refusal rather than silently dropped.
    downscaleImage.mockClear();
    const over = () => sized(
      new File([MARKER_BYTES], 'huge.png', { type: 'image/jpeg' }),
      MAX_BATCH_IMAGE_BYTES + 1,
    );
    downscaleImage
      .mockResolvedValueOnce({ file: over(), resized: true, source: null })
      .mockResolvedValueOnce({ file: over(), resized: true, source: null });

    const user = userEvent.setup();
    renderUnified();
    await user.upload(screen.getByTestId('unified-file-input'), imageFile('huge.png'));

    const refusal = await screen.findByTestId('unified-pick-error');
    expect(refusal.textContent).toContain('huge.png');
    expect(downscaleImage).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId('unified-chip-images')).toBeNull();
  });
});
