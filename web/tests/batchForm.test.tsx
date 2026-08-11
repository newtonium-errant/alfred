import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BatchForm } from '../components/ingest/BatchForm';

// #83 item 6 — the form. Plain DOM assertions (this suite has no jest-dom).
//
// The load-bearing pins are the honest ones: caps stated BEFORE the operator
// scans thirty pages, the Nth over-cap image named with WHICH cap it hit, and a
// success surface that does not promise processing when nothing is configured
// to process the batch.
//
// `createImageBitmap` is absent under jsdom, which makes the real
// `downscaleImage` return every file unchanged — the module's documented
// never-blocks-the-user fallback. So the form is exercised with real
// preparation logic and no canvas, which is exactly the path a browser without
// canvas support would take.

function pick(input: HTMLInputElement, files: File[]) {
  Object.defineProperty(input, 'files', { value: files, configurable: true });
  fireEvent.change(input);
}

function img(name: string, size: number, type = 'image/jpeg'): File {
  return new File([new Uint8Array(size)], name, { type });
}

const originalFetch = global.fetch;

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  global.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('BatchForm', () => {
  it('states the caps before anything is picked', () => {
    // An operator about to scan thirty pages should learn the limit first.
    render(<BatchForm />);
    const limits = screen.getByTestId('batch-limits').textContent ?? '';
    expect(limits).toContain('60 scans');
    expect(limits).toContain('5 MiB');
    expect(limits).toContain('128 MiB');
  });

  it('says the selection is empty on purpose', () => {
    // Intentionally-left-blank: a blank area is indistinguishable from broken.
    render(<BatchForm />);
    expect(screen.getByTestId('batch-empty').textContent).toContain('No scans selected');
  });

  it('submit stays disabled until there are scans AND an instruction', async () => {
    render(<BatchForm />);
    const submit = screen.getByTestId('batch-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    expect((screen.getByTestId('batch-submit') as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    await waitFor(() =>
      expect((screen.getByTestId('batch-submit') as HTMLButtonElement).disabled).toBe(false),
    );
  });

  it('an instruction alone is not submittable', async () => {
    // The mirror of the case above — one gate passing must not open the door.
    render(<BatchForm />);
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    expect((screen.getByTestId('batch-submit') as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows the running count and total against their caps', async () => {
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [
      img('a.jpg', 1024 * 1024),
      img('b.jpg', 1024 * 1024),
    ]);
    await waitFor(() => {
      const count = screen.getByTestId('batch-count').textContent ?? '';
      expect(count).toContain('2 of 60 scans');
      expect(count).toContain('2.0 of 128 MiB');
    });
  });

  it('picking is additive across separate selections', async () => {
    // The operator scans in passes; a second pick must not replace the first.
    render(<BatchForm />);
    const input = screen.getByTestId('batch-files') as HTMLInputElement;
    pick(input, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count').textContent).toContain('1 of 60'));
    pick(input, [img('b.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count').textContent).toContain('2 of 60'));
  });

  it('names the refused file and which cap it hit', async () => {
    // "Some images could not be added" is the operator-facing form of silence.
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [
      img('good.jpg', 10),
      img('report.pdf', 10, 'application/pdf'),
    ]);
    await waitFor(() => {
      const err = screen.getByTestId('batch-pick-error').textContent ?? '';
      expect(err).toContain('report.pdf');
      expect(err).toContain('PNG, JPEG, GIF and WebP');
    });
    // And the acceptable file was still staged.
    expect(screen.getByTestId('batch-count').textContent).toContain('1 of 60');
  });

  it('refuses an over-cap image with the per-image remedy', async () => {
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [
      img('huge.jpg', 6 * 1024 * 1024),
    ]);
    await waitFor(() => {
      const err = screen.getByTestId('batch-pick-error').textContent ?? '';
      expect(err).toContain('huge.jpg');
      expect(err).toContain('5 MiB');
      expect(err).toContain('lower-resolution');
    });
    expect(screen.queryByTestId('batch-count')).toBeNull();
  });

  it('removes one staged scan and re-totals', async () => {
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [
      img('a.jpg', 1024 * 1024),
      img('b.jpg', 1024 * 1024),
    ]);
    await waitFor(() => expect(screen.getByTestId('batch-count').textContent).toContain('2 of 60'));
    fireEvent.click(screen.getByTestId('batch-remove-0'));
    await waitFor(() => {
      const count = screen.getByTestId('batch-count').textContent ?? '';
      expect(count).toContain('1 of 60');
      expect(count).toContain('1.0 of 128 MiB');
    });
  });

  it('flags an over-long instruction with the overage', async () => {
    render(<BatchForm />);
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'x'.repeat(4010) },
    });
    await waitFor(() =>
      expect(screen.getByTestId('batch-instruction-over-limit').textContent).toContain('10'),
    );
  });

  it('posts multipart WITHOUT a hand-set Content-Type', async () => {
    // The browser must set it so the multipart boundary is generated; setting
    // it by hand produces a body the box cannot parse, and the failure looks
    // like an empty batch rather than a malformed request.
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: 'queued', batch_id: 'b1', images: 1, bytes: 10,
        path: 'note/B.md', instance: 'VERA',
      }),
    });
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/batch/submit');
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.headers).toBeUndefined();
    expect((init.body as FormData).get('instruction')).toBe('Read the total.');
  });

  it('a queued submit promises processing and shows the record path', async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: 'queued', batch_id: '20260811-aaaa1111', images: 3, bytes: 30,
        path: 'note/Batch.md', instance: 'VERA',
      }),
    });
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));

    await waitFor(() => expect(screen.getByTestId('batch-success')).toBeTruthy());
    const text = screen.getByTestId('batch-success').textContent ?? '';
    expect(text).toContain('20260811-aaaa1111');
    expect(text).toContain('processing 3 scans');
    expect(text).toContain('note/Batch.md');
  });

  it('a SAVED submit does not promise processing', async () => {
    // THE honesty pin at the UI layer. With no campaign enabled the record sits
    // at 0 of N forever; telling the operator it is being processed would send
    // them away to wait for results that cannot arrive.
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: 'saved', batch_id: 'b2', images: 3, bytes: 30,
        path: 'note/Batch.md', instance: 'VERA',
      }),
    });
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));

    await waitFor(() => expect(screen.getByTestId('batch-success')).toBeTruthy());
    const text = screen.getByTestId('batch-success').textContent ?? '';
    expect(text).toContain('nothing is set up to process it');
    expect(text).not.toContain('processing 3 scans');
  });

  it('a refused submit says why and KEEPS the staged scans', async () => {
    // The operator must not have to re-pick thirty files because of one 413.
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 413,
      json: async () => ({ error: 'batch_too_large' }),
    });
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));

    await waitFor(() => expect(screen.getByTestId('batch-error')).toBeTruthy());
    expect(screen.getByTestId('batch-error').textContent).toContain('128 MiB');
    expect(screen.getByTestId('batch-count')).toBeTruthy();
    expect(screen.queryByTestId('batch-success')).toBeNull();
  });

  it('a 401 bubbles up so the page can redirect to login', async () => {
    const onUnauthenticated = vi.fn();
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ error: 'invalid_session' }),
    });
    render(<BatchForm onUnauthenticated={onUnauthenticated} />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));

    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalled());
  });

  it('a network failure is reported, not swallowed', async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'));
    render(<BatchForm />);
    pick(screen.getByTestId('batch-files') as HTMLInputElement, [img('a.jpg', 10)]);
    await waitFor(() => expect(screen.getByTestId('batch-count')).toBeTruthy());
    fireEvent.change(screen.getByTestId('batch-instruction'), {
      target: { value: 'Read the total.' },
    });
    fireEvent.click(screen.getByTestId('batch-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('batch-error').textContent).toContain('were not submitted'),
    );
  });
});
