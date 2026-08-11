/**
 * #82 — the Composer actually ROUTES attachments through the downscaler.
 *
 * This file exists because `imageDownscale.test.ts` cannot prove it. That
 * suite tests the helper by calling it directly, so it stays fully green
 * against a build where `addFiles` never calls it at all — the helper ships,
 * every pin passes, and in the field images go up at full size and wedge the
 * session exactly as before. Under jsdom the downscale is also a no-op (no
 * canvas), so there is no observable size change to assert on either.
 *
 * So the helper is mocked to return a MARKER file, and the assertion is that
 * the marker's bytes are what reach `onSend`. That is only true if the
 * production path calls it.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: () => <button type="button">mock-stt</button>,
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

import { Composer } from '../components/chat/Composer';
import { MAX_IMAGE_BYTES } from '../lib/algernon/schemas';

function imageFile(name = 'scan.png', type = 'image/png', size = 4096): File {
  const bytes = new Uint8Array(size);
  bytes.set([0x89, 0x50, 0x4e, 0x47], 0);
  return new File([bytes], name, { type });
}

describe('Composer → imageDownscale wiring', () => {
  it('passes every attached image through downscaleImage', async () => {
    downscaleImage.mockClear();
    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} />);

    await user.upload(screen.getByTestId('composer-file-input'), imageFile());
    await screen.findByTestId('composer-image-preview');

    expect(downscaleImage).toHaveBeenCalledTimes(1);
    expect((downscaleImage.mock.calls[0][0] as File).name).toBe('scan.png');
  });

  it('sends the DOWNSCALED bytes, not the originally-picked file', async () => {
    // The load-bearing assertion. Remove the downscale call from addFiles and
    // this reddens, because the original file's bytes would arrive instead.
    downscaleImage.mockClear();
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    await user.upload(screen.getByTestId('composer-file-input'), imageFile());
    await screen.findByTestId('composer-image-preview');
    await user.click(screen.getByTestId('composer-send'));

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
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    await user.upload(screen.getByTestId('composer-file-input'), imageFile('scan.png', 'image/png'));
    await screen.findByTestId('composer-image-preview');
    await user.click(screen.getByTestId('composer-send'));

    expect(onSend.mock.calls[0][2][0].media_type).toBe('image/jpeg');
  });
});

describe('Composer comment provenance (#83 NOTE-A)', () => {
  it('does not carry the corrected-away 1568 / dimensions-wedge reasoning', async () => {
    // A source-level pin, in the same style as the routes_voice literal sweep.
    // The #82 gate corrected two claims that had been written into this file's
    // comments: the cap is 2576 (per-tier, not 1568), and the wedge is held by
    // the history TRIM bounding image count — not by downscaling dimensions.
    // Stale reasoning next to live code is what a future reader trusts, so it
    // gets a pin rather than a one-time edit.
    const { readFileSync } = await import('node:fs');
    const { resolve } = await import('node:path');
    // vitest runs with cwd at web/, so a repo-relative path is stable here —
    // import.meta.url is not a file: URL under this transform.
    const src = readFileSync(resolve('components/chat/Composer.tsx'), 'utf8');
    expect(src).not.toContain('1568px');
    expect(src).not.toMatch(/oversized DIMENSIONS.*wedge/s);
    expect(src).not.toContain("20-image threshold");
    // And it states the corrected position instead of merely omitting the old.
    expect(src).toContain('history trim');
  });
});


describe('Composer byte-budget step-down wiring (#83 NOTE-B)', () => {
  it('retries ONCE with the step-down params when the first result is over budget', async () => {
    // The wiring pin. imageDownscale.test.ts proves the step-down works when
    // called; it stays green against a Composer that never calls it — which is
    // exactly what the first attempt at this shipped, and what the probe found.
    downscaleImage.mockClear();
    const over = sized(
      new File([MARKER_BYTES], 'huge.png', { type: 'image/jpeg' }),
      MAX_IMAGE_BYTES + 1,
    );
    const under = new File([MARKER_BYTES], 'huge.png', { type: 'image/jpeg' });
    downscaleImage
      .mockResolvedValueOnce({ file: over, resized: true, source: null })
      .mockResolvedValueOnce({ file: under, resized: true, source: null });

    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    await user.upload(screen.getByTestId('composer-file-input'), imageFile('huge.png'));
    await screen.findByTestId('composer-image-preview');

    expect(downscaleImage).toHaveBeenCalledTimes(2);
    // Second call carries the step-down edge AND quality — passing only the
    // edge would skip the forced re-encode that sheds the bytes.
    expect(downscaleImage.mock.calls[1].slice(1)).toEqual([
      STEP_DOWN_EDGE_PX, STEP_DOWN_QUALITY,
    ]);
    // And the attachment survived: never-blocks-the-user, end to end.
    await user.click(screen.getByTestId('composer-send'));
    expect(onSend.mock.calls[0][2]).toHaveLength(1);
  });

  it('does NOT retry when the first result is already under budget', async () => {
    // One retry, not a loop, and no wasted re-encode on the common path.
    downscaleImage.mockClear();
    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} />);
    await user.upload(screen.getByTestId('composer-file-input'), imageFile('ok.png'));
    await screen.findByTestId('composer-image-preview');
    expect(downscaleImage).toHaveBeenCalledTimes(1);
  });

  it('still refuses when even the step-down cannot get under budget', async () => {
    // The give-up path stays honest: one retry, then an explicit inline error
    // rather than a silent drop.
    downscaleImage.mockClear();
    const over = () => sized(
      new File([MARKER_BYTES], 'huge.png', { type: 'image/jpeg' }),
      MAX_IMAGE_BYTES + 1,
    );
    downscaleImage
      .mockResolvedValueOnce({ file: over(), resized: true, source: null })
      .mockResolvedValueOnce({ file: over(), resized: true, source: null });

    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} />);
    await user.upload(screen.getByTestId('composer-file-input'), imageFile('huge.png'));

    expect(await screen.findByTestId('composer-image-error')).toBeTruthy();
    expect(downscaleImage).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId('composer-image-preview')).toBeNull();
  });
});
