import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

// #95 — the bug-report dialog.
//
// The capture engine is mocked at OUR module (`screenCapture`), never at
// html2canvas: that seam is the reason the reporter can be tested without
// loading a canvas library, and mocking the library instead would couple these
// tests to whichever engine the app happens to use this month.
//
// Two behaviours here are load-bearing rather than cosmetic:
//
//   RETAKE KEEPS THE PRIOR SHOT. A failed recapture that discarded the working
//   screenshot would punish the reporter for trying to improve it — they would
//   press a button labelled "Retake" and lose the picture they already had.
//
//   SEND IS NEVER GATED ON THE CAPTURE. Someone reporting a bug in a broken app
//   must be able to send words while the screenshot is still rendering, or after
//   it has failed outright. The screenshot is a convenience; the words are the
//   report.

const { mockCapture, mockToBase64 } = vi.hoisted(() => ({
  mockCapture: vi.fn(),
  mockToBase64: vi.fn(),
}));

vi.mock('../lib/algernon/screenCapture', () => ({
  captureScreen: mockCapture,
  blobToBase64: mockToBase64,
  CAPTURE_IGNORE_ATTR: 'data-report-ignore',
  CAPTURE_TIMEOUT_MS: 8000,
}));

import { ReportBugModal } from '../components/ReportBugModal';

function shot(tag = 'a'): Blob {
  return new Blob([tag], { type: 'image/png' });
}

function renderModal(over: Partial<React.ComponentProps<typeof ReportBugModal>> = {}) {
  const onClose = vi.fn();
  const onRetake = vi.fn().mockResolvedValue(null);
  const utils = render(
    <ReportBugModal
      route="/chat"
      viewedInstance="Salem"
      initialShot={null}
      capturing={false}
      onRetake={onRetake}
      onClose={onClose}
      {...over}
    />,
  );
  return { ...utils, onClose, onRetake };
}

function body(): HTMLTextAreaElement {
  return screen.getByTestId('report-bug-body') as HTMLTextAreaElement;
}

function submit(): HTMLButtonElement {
  return screen.getByTestId('report-bug-submit') as HTMLButtonElement;
}

beforeEach(() => {
  mockCapture.mockReset();
  mockToBase64.mockReset();
  mockToBase64.mockResolvedValue('QkFTRTY0');
  // jsdom has no object-URL implementation; the preview <img> only needs a string.
  (URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn(() => 'blob:preview');
  (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ReportBugModal — submit', () => {
  it('refuses an empty description instead of sending a blank report', async () => {
    renderModal();
    // Send is disabled at rest, which is the first refusal…
    expect(submit().getAttribute('disabled')).not.toBe(null);

    // …and whitespace does not satisfy it either.
    fireEvent.change(body(), { target: { value: '    ' } });
    expect(submit().getAttribute('disabled')).not.toBe(null);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('sends the description, the context, and the screenshot, then confirms WHERE it landed', async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'filed', report_id: 'r1', instance: 'Salem' }),
    });

    renderModal({ initialShot: shot() });
    fireEvent.change(body(), { target: { value: 'Send did nothing' } });
    await act(async () => {
      fireEvent.click(submit());
    });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/bugreport/submit');
    const sent = JSON.parse((init as RequestInit).body as string);
    expect(sent.description).toBe('Send did nothing');
    expect(sent.context.route).toBe('/chat');
    expect(sent.screenshot_b64).toBe('QkFTRTY0');
    expect(sent.screenshot_media_type).toBe('image/png');

    // ILB: success is VISIBLE and names the destination. "Sent" with no
    // destination leaves the reporter unable to go and check.
    const ok = await screen.findByTestId('report-bug-success');
    expect(ok.textContent).toContain('Report sent');
    expect(ok.textContent).toContain('Salem');
  });

  it('omits the screenshot keys entirely when there is no shot', async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'filed', report_id: 'r1', instance: 'Salem' }),
    });

    renderModal({ initialShot: null });
    fireEvent.change(body(), { target: { value: 'no picture' } });
    await act(async () => {
      fireEvent.click(submit());
    });

    const sent = JSON.parse(
      ((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit).body as string,
    );
    expect('screenshot_b64' in sent).toBe(false);
    expect('screenshot_media_type' in sent).toBe(false);
    expect(mockToBase64).not.toHaveBeenCalled();
  });

  it('maps a backend refusal to ITS OWN sentence rather than a generic failure', async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: async () => ({ error: 'screenshot_too_large' }),
    });

    renderModal({ initialShot: shot() });
    fireEvent.change(body(), { target: { value: 'big picture' } });
    await act(async () => {
      fireEvent.click(submit());
    });

    const err = await screen.findByTestId('report-bug-error');
    expect(err.textContent).toContain('5.0 MB');
    // Still recoverable — the words are not lost and Send comes back.
    expect(submit().getAttribute('disabled')).toBe(null);
    expect(screen.queryByTestId('report-bug-success')).toBe(null);
  });

  it('says the report was NOT saved when the network fails', async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'));

    renderModal();
    fireEvent.change(body(), { target: { value: 'anything' } });
    await act(async () => {
      fireEvent.click(submit());
    });

    const err = await screen.findByTestId('report-bug-error');
    expect(err.textContent).toContain('NOT saved');
  });
});

describe('ReportBugModal — screenshot', () => {
  it('KEEPS the prior shot when a retake fails, and says so', async () => {
    // The regression this pin exists for: onRetake resolving null must not be
    // read as "the user has no screenshot now".
    const onRetake = vi.fn().mockResolvedValue(null);
    renderModal({ initialShot: shot('original'), onRetake });

    expect(screen.getByTestId('report-bug-preview')).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByTestId('report-bug-retake'));
    });

    expect(onRetake).toHaveBeenCalledTimes(1);
    // The preview is still there — the original shot survived the failure.
    expect(screen.getByTestId('report-bug-preview')).toBeTruthy();
    const note = screen.getByTestId('report-bug-retake-note');
    expect(note.textContent).toContain('keeping the previous screenshot');
  });

  it('adopts a successful retake', async () => {
    const onRetake = vi.fn().mockResolvedValue(shot('fresh'));
    renderModal({ initialShot: shot('original'), onRetake });

    await act(async () => {
      fireEvent.click(screen.getByTestId('report-bug-retake'));
    });

    expect(screen.getByTestId('report-bug-preview')).toBeTruthy();
    expect(screen.queryByTestId('report-bug-retake-note')).toBe(null);
  });

  it('explains a failed FIRST capture without pretending one was lost', async () => {
    // Different wording from the retake case: there was never a screenshot to
    // keep, so "keeping the previous" would be a lie.
    const onRetake = vi.fn().mockResolvedValue(null);
    renderModal({ initialShot: null, onRetake });

    await act(async () => {
      fireEvent.click(screen.getByTestId('report-bug-retake'));
    });

    const note = screen.getByTestId('report-bug-retake-note');
    expect(note.textContent).toContain('still send the description');
  });

  it('adopts a capture that lands after the dialog opened', async () => {
    const { rerender, onRetake, onClose } = renderModal({ initialShot: null, capturing: true });
    expect(screen.getByTestId('report-bug-no-shot').textContent).toContain('Capturing');

    rerender(
      <ReportBugModal
        route="/chat"
        viewedInstance="Salem"
        initialShot={shot('late')}
        capturing={false}
        onRetake={onRetake}
        onClose={onClose}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('report-bug-preview')).toBeTruthy());
  });

  it('does NOT resurrect a shot the reporter removed', async () => {
    // The reporter's decision outranks a late-arriving capture. Re-adopting it
    // would silently attach a screenshot they had explicitly taken off.
    const { rerender, onRetake, onClose } = renderModal({ initialShot: shot('one'), capturing: true });
    fireEvent.click(screen.getByTestId('report-bug-remove'));
    expect(screen.queryByTestId('report-bug-preview')).toBe(null);

    rerender(
      <ReportBugModal
        route="/chat"
        viewedInstance="Salem"
        initialShot={shot('two')}
        capturing={false}
        onRetake={onRetake}
        onClose={onClose}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId('report-bug-no-shot').textContent).toContain('No screenshot'),
    );
  });

  // WARN-1 regression. The picker accepts PNG, JPEG and WebP; the submit path
  // used to declare `image/png` unconditionally, so JPEG and WebP bytes were
  // filed under a `.png` name — the exact "file whose bytes disagree with its
  // extension" the route refuses to create and the BFF's zod pairing check
  // exists to prevent. Both server guards were walked around, because a
  // hardcoded value makes the lie internally consistent all the way down.
  //
  // The pin was vacuous before this: every fixture was a PNG, so a constant and
  // a derivation were indistinguishable. Driving all three types is the same
  // positive-control discipline as the retake pin.
  it.each([
    ['image/jpeg', 'shot.jpg'],
    ['image/webp', 'shot.webp'],
    ['image/png', 'shot.png'],
  ])('declares %s when that is what the reporter attached', async (mime, name) => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'filed', report_id: 'r1', instance: 'Salem' }),
    });

    renderModal({ initialShot: null });
    const picked = new File(['bytes'], name, { type: mime });
    fireEvent.change(screen.getByTestId('report-bug-file'), {
      target: { files: [picked] },
    });
    // The file must actually have been accepted — otherwise this test would
    // pass by sending no screenshot at all, which is the vacuity it replaces.
    expect(screen.queryByTestId('report-bug-error')).toBe(null);
    expect(screen.getByTestId('report-bug-preview')).toBeTruthy();

    fireEvent.change(body(), { target: { value: 'wrong colours' } });
    await act(async () => {
      fireEvent.click(submit());
    });

    const sent = JSON.parse(
      ((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit).body as string,
    );
    expect(sent.screenshot_b64).toBeTruthy();
    expect(sent.screenshot_media_type).toBe(mime);
  });

  it('falls back to PNG for an auto-capture with no declared type', async () => {
    // Some encoders hand back a type-less blob. The auto-capture is always PNG
    // (captureScreen encodes it, downscaleImage re-encodes to PNG), and a
    // picked file cannot reach here untyped because handlePickFile refuses
    // anything outside the allowlist — so PNG is a sound default, not a guess.
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'filed', report_id: 'r1', instance: 'Salem' }),
    });

    renderModal({ initialShot: new Blob(['x']) }); // no type
    fireEvent.change(body(), { target: { value: 'typeless' } });
    await act(async () => {
      fireEvent.click(submit());
    });

    const sent = JSON.parse(
      ((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit).body as string,
    );
    expect(sent.screenshot_media_type).toBe('image/png');
  });

  it('refuses an oversize or wrong-typed attachment by naming the limit', async () => {
    renderModal();
    const input = screen.getByTestId('report-bug-file') as HTMLInputElement;

    const huge = new File(['x'], 'huge.png', { type: 'image/png' });
    Object.defineProperty(huge, 'size', { value: 9 * 1024 * 1024 });
    fireEvent.change(input, { target: { files: [huge] } });
    expect(screen.getByTestId('report-bug-error').textContent).toContain('5.0 MB');

    const wrong = new File(['x'], 'doc.pdf', { type: 'application/pdf' });
    fireEvent.change(input, { target: { files: [wrong] } });
    expect(screen.getByTestId('report-bug-error').textContent).toContain('isn’t supported');
    expect(screen.queryByTestId('report-bug-preview')).toBe(null);
  });

  it('names the size cap up front, before anything is refused', () => {
    // #83's discipline: the limit is stated where the reporter chooses a file,
    // not only after they have already picked one that is too big.
    renderModal();
    expect(document.body.textContent).toContain('Up to 5.0 MB');
  });
});

describe('ReportBugModal — send is never gated on the capture', () => {
  it('allows Send while a capture is still running', () => {
    renderModal({ initialShot: null, capturing: true });
    fireEvent.change(body(), { target: { value: 'it broke' } });
    expect(submit().getAttribute('disabled')).toBe(null);
  });

  it('allows Send after the capture failed outright', () => {
    renderModal({ initialShot: null, capturing: false });
    fireEvent.change(body(), { target: { value: 'it broke' } });
    expect(submit().getAttribute('disabled')).toBe(null);
  });
});

describe('ReportBugModal — dismissal', () => {
  it('closes on Escape', () => {
    const { onClose } = renderModal();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Cancel', () => {
    const { onClose } = renderModal();
    fireEvent.click(screen.getByTestId('report-bug-cancel'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('is a labelled modal dialog', () => {
    renderModal();
    const dialog = screen.getByTestId('report-bug-modal');
    expect(dialog.getAttribute('role')).toBe('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('report-bug-title');
  });
});
