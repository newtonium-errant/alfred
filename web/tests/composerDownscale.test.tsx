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
const downscaleImage = vi.fn(async (file: File) => ({
  file: new File([MARKER_BYTES], file.name, { type: 'image/jpeg' }),
  resized: true,
  source: { width: 3000, height: 2000 },
}));
vi.mock('../lib/algernon/imageDownscale', () => ({
  downscaleImage: (file: File) => downscaleImage(file),
  MAX_IMAGE_EDGE_PX: 1568,
  MANY_IMAGE_DIMENSION_LIMIT_PX: 2000,
}));

import { Composer } from '../components/chat/Composer';

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
