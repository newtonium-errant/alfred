import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// #57 CSV half, driven through the REAL form: the picker, the FileReader path,
// and the body that actually reaches `submit`. Unit pins on prepareUpload live in
// ingestUpload.test.ts; what these add is the wiring — a helper that fences
// perfectly is worth nothing if the component never calls it, or calls it and
// submits something else.

const { ingest, submitMock, mockTranscribe } = vi.hoisted(() => ({
  ingest: { current: {} as Record<string, unknown> },
  submitMock: vi.fn(),
  mockTranscribe: vi.fn(),
}));

vi.mock('../lib/algernon/useIngest', () => ({
  useIngest: () => ingest.current,
}));

// VoiceCapture (mounted for decision F) pulls the STT client at import time.
vi.mock('../lib/algernon/sttClient', () => ({
  sttClient: { transcribe: mockTranscribe },
}));

import { IngestForm } from '../components/ingest/IngestForm';
import { MAX_INGEST_CHARS } from '../lib/algernon/schemas';
import { fenceCsv } from '../lib/algernon/ingestUpload';

const OWNER = { name: 'Andrew', role: 'owner' };
const CSV = 'name,qty\nwidget,3\nsprocket,12\n';

beforeEach(() => {
  submitMock.mockReset();
  mockTranscribe.mockReset();
  ingest.current = {
    targets: [{ name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] }],
    status: 'ready',
    error: null,
    result: null,
    unauthenticated: false,
    submit: submitMock,
    reset: vi.fn(),
  };
});

afterEach(() => {
  vi.clearAllMocks();
  // The failed-read tests stub the global FileReader; without this the stub leaks
  // into every later test in the file and every upload silently fails.
  vi.unstubAllGlobals();
});

function renderForm() {
  return render(<IngestForm user={OWNER} originInstance="Salem" />);
}

function field(testId: string): HTMLInputElement | HTMLTextAreaElement {
  return screen.getByTestId(testId) as HTMLInputElement | HTMLTextAreaElement;
}

/**
 * Drive the real file input, then wait for the async FileReader to land.
 *
 * The settle predicate is a CHANGE detector over the three things an upload can
 * move — the body, the refusal, the note — not "the body is non-empty". A
 * non-emptiness predicate is already satisfied BEFORE the read completes whenever
 * a previous upload left a body or a refusal on screen, so it returns early and
 * the assertions race the FileReader.
 */
async function upload(name: string, content: string, type = 'text/plain') {
  const snapshot = () =>
    JSON.stringify([
      (screen.getByTestId('ingest-body') as HTMLTextAreaElement).value,
      screen.queryByTestId('ingest-upload-error')?.textContent ?? '',
      screen.queryByTestId('ingest-upload-note')?.textContent ?? '',
    ]);
  const before = snapshot();
  const input = screen.getByTestId('ingest-file') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File([content], name, { type })] } });
  await waitFor(() => {
    if (snapshot() === before) throw new Error('upload not settled');
  });
}

describe('the picker offers CSV', () => {
  it('accepts .csv and text/csv alongside .md/.txt', () => {
    renderForm();
    const accept = screen.getByTestId('ingest-file').getAttribute('accept') ?? '';
    expect(accept).toContain('.csv');
    expect(accept).toContain('text/csv');
    expect(accept).toContain('.md');
    expect(accept).toContain('.txt');
  });

  it('says so on the label — the affordance is not a secret', () => {
    renderForm();
    expect(screen.getByText('Upload .md / .txt / .csv')).toBeTruthy();
  });
});

describe('a CSV upload lands fenced, with provenance derived from the filename', () => {
  it('puts a ```csv fence in the body the operator can see', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    expect(field('ingest-body').value).toBe('```csv\nname,qty\nwidget,3\nsprocket,12\n```\n');
  });

  it('derives title and source from the filename exactly as .md/.txt do', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    expect(field('ingest-title').value).toBe('sales');
    expect(field('ingest-source').value).toBe('sales.csv');
  });

  it('SUBMITS the fenced body verbatim — the fence is not a display-only flourish', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    fireEvent.click(screen.getByTestId('ingest-submit'));
    expect(submitMock).toHaveBeenCalledTimes(1);
    const payload = submitMock.mock.calls[0][0];
    expect(payload.body).toBe(fenceCsv(CSV));
    expect(payload.body).toContain(CSV);
    expect(payload.title).toBe('sales');
    expect(payload.source).toBe('sales.csv');
  });

  it('shows the row count, saying which count it is', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    const note = screen.getByTestId('ingest-upload-note').textContent ?? '';
    expect(note).toContain('sales.csv');
    expect(note).toContain('3 rows (newline count, header included)');
    expect(note).toContain('fenced in the body');
  });

  it('renders the row count with a thousands separator for a big file', async () => {
    renderForm();
    const rows = ['a,b', ...Array.from({ length: 1242 }, (_, i) => `${i},x`)].join('\n');
    await upload('big.csv', `${rows}\n`, 'text/csv');
    expect(screen.getByTestId('ingest-upload-note').textContent).toContain(
      `${(1243).toLocaleString()} rows`,
    );
  });

  it('leaves the frontmatter rows alone — the note is not smuggled in among them', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    const provenance = screen.getByTestId('ingest-provenance').textContent ?? '';
    expect(provenance).toContain('origin_instance');
    // The note is a sibling of the rows, not a row: no invented frontmatter key.
    expect(provenance).not.toContain('rows:');
  });
});

describe('.md / .txt behave exactly as they did before CSV existed', () => {
  it('loads a .md body raw — no fence, no note', async () => {
    renderForm();
    const md = '# Heading\n\nBody *text* here.\n';
    await upload('notes.md', md, 'text/markdown');
    expect(field('ingest-body').value).toBe(md);
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });

  it('loads a .txt body raw and derives the same title/source as always', async () => {
    renderForm();
    await upload('plain.txt', 'just words\n');
    expect(field('ingest-body').value).toBe('just words\n');
    expect(field('ingest-title').value).toBe('plain');
    expect(field('ingest-source').value).toBe('plain.txt');
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });

  it('derives title and source from ONE normalised filename (reviewer-57 finding 6)', async () => {
    // A padded name is trimmed before CSV detection, so it must be trimmed before
    // title/source derivation too — otherwise the file is treated as a CSV while
    // the source field keeps whitespace the operator never typed.
    renderForm();
    await upload('  sales.csv  ', CSV, 'text/csv');
    expect(field('ingest-source').value).toBe('sales.csv');
    expect(field('ingest-title').value).toBe('sales');
    // Detection agreed with derivation: it was fenced and noted as a CSV.
    expect(field('ingest-body').value).toBe(fenceCsv(CSV));
    expect(screen.getByTestId('ingest-upload-note').textContent).toContain('sales.csv');
  });

  it('still does not overwrite a title or source the operator already typed', async () => {
    renderForm();
    fireEvent.change(field('ingest-title'), { target: { value: 'My own title' } });
    fireEvent.change(field('ingest-source'), { target: { value: 'my own source' } });
    await upload('notes.md', '# body\n', 'text/markdown');
    expect(field('ingest-title').value).toBe('My own title');
    expect(field('ingest-source').value).toBe('my own source');
  });
});

describe('an empty file is said out loud', () => {
  it('refuses a zero-byte CSV with an explicit message instead of an empty submit', async () => {
    renderForm();
    await upload('empty.csv', '', 'text/csv');
    const msg = screen.getByTestId('ingest-upload-error').textContent ?? '';
    expect(msg).toContain('empty.csv');
    expect(msg).toContain('nothing to ingest');
    expect(field('ingest-body').value).toBe('');
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });

  it('keeps submit disabled and never fires it for an empty file', async () => {
    renderForm();
    await upload('empty.csv', '', 'text/csv');
    expect((screen.getByTestId('ingest-submit') as HTMLButtonElement).disabled).toBe(true);
    expect(submitMock).not.toHaveBeenCalled();
  });

  it('gives the refusal the alert role so it is announced, not just drawn', async () => {
    renderForm();
    await upload('empty.csv', '', 'text/csv');
    expect(screen.getByTestId('ingest-upload-error').getAttribute('role')).toBe('alert');
  });
});

describe('the size limit reaches the operator here, not as a server bounce', () => {
  it('names the file, its size and the limit when an upload is too large', async () => {
    renderForm();
    const over = MAX_INGEST_CHARS + 1;
    await upload('huge.txt', 'x'.repeat(over));
    const msg = screen.getByTestId('ingest-upload-error').textContent ?? '';
    expect(msg).toContain('huge.txt');
    expect(msg).toContain(over.toLocaleString());
    expect(msg).toContain(MAX_INGEST_CHARS.toLocaleString());
  });

  it('does NOT load an over-limit file — no unsubmittable body parked in the form', async () => {
    renderForm();
    await upload('huge.txt', 'x'.repeat(MAX_INGEST_CHARS + 1));
    expect(field('ingest-body').value).toBe('');
    expect(submitMock).not.toHaveBeenCalled();
  });

  it('preserves a body the operator already had when it refuses the upload', async () => {
    renderForm();
    fireEvent.change(field('ingest-body'), { target: { value: 'work in progress' } });
    await upload('huge.txt', 'x'.repeat(MAX_INGEST_CHARS + 1));
    expect(screen.getByTestId('ingest-upload-error')).toBeTruthy();
    expect(field('ingest-body').value).toBe('work in progress');
  });

  it('clears a stale refusal once a good file loads', async () => {
    renderForm();
    await upload('empty.csv', '', 'text/csv');
    expect(screen.getByTestId('ingest-upload-error')).toBeTruthy();
    await upload('sales.csv', CSV, 'text/csv');
    expect(screen.queryByTestId('ingest-upload-error')).toBeNull();
    expect(screen.getByTestId('ingest-upload-note')).toBeTruthy();
  });

  it('explains an over-limit PASTED body too, rather than only greying out submit', () => {
    renderForm();
    fireEvent.change(field('ingest-body'), {
      target: { value: 'x'.repeat(MAX_INGEST_CHARS + 25) },
    });
    const msg = screen.getByTestId('ingest-body-over-limit').textContent ?? '';
    expect(msg).toContain('25 characters over');
    expect(msg).toContain(MAX_INGEST_CHARS.toLocaleString());
    expect((screen.getByTestId('ingest-submit') as HTMLButtonElement).disabled).toBe(true);
  });

  it('says nothing about the limit while the body is within it', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    expect(screen.queryByTestId('ingest-body-over-limit')).toBeNull();
  });
});

describe('a failed READ is said out loud too (reviewer-57 finding 2)', () => {
  // FileReader can fail after the picker accepted the file. jsdom will not produce
  // that failure from a well-formed File, so the reader itself is stubbed — the
  // component resolves `new FileReader()` off the global at call time.
  function stubFailingReader(errorName?: string) {
    class FailingReader {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      error = errorName ? { name: errorName } : null;
      result: string | null = null;
      readAsText() {
        setTimeout(() => this.onerror?.(), 0);
      }
    }
    vi.stubGlobal('FileReader', FailingReader);
  }

  it('names the file and the browser reason instead of doing nothing at all', async () => {
    stubFailingReader('NotReadableError');
    renderForm();
    fireEvent.change(screen.getByTestId('ingest-file'), {
      target: { files: [new File(['whatever'], 'sales.csv', { type: 'text/csv' })] },
    });
    const alert = await screen.findByTestId('ingest-upload-error');
    expect(alert.textContent).toContain('sales.csv');
    expect(alert.textContent).toContain('Could not read');
    expect(alert.textContent).toContain('NotReadableError');
    expect(alert.getAttribute('role')).toBe('alert');
  });

  it('leaves the body and the note untouched when the read fails', async () => {
    stubFailingReader();
    renderForm();
    fireEvent.change(field('ingest-body'), { target: { value: 'work in progress' } });
    fireEvent.change(screen.getByTestId('ingest-file'), {
      target: { files: [new File(['whatever'], 'sales.csv', { type: 'text/csv' })] },
    });
    await screen.findByTestId('ingest-upload-error');
    expect(field('ingest-body').value).toBe('work in progress');
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });

  it('drops a stale rows-note when a later read fails', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    expect(screen.getByTestId('ingest-upload-note')).toBeTruthy();
    stubFailingReader();
    fireEvent.change(screen.getByTestId('ingest-file'), {
      target: { files: [new File(['whatever'], 'other.csv', { type: 'text/csv' })] },
    });
    await screen.findByTestId('ingest-upload-error');
    // The note described the PREVIOUS upload; leaving it up would caption the
    // body with a row count belonging to a file that never loaded.
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });
});

describe('editing the body clears upload messages (reviewer-57 finding 4)', () => {
  it('drops a refusal once the operator composes a body by hand', async () => {
    renderForm();
    await upload('empty.csv', '', 'text/csv');
    expect(screen.getByTestId('ingest-upload-error')).toBeTruthy();
    fireEvent.change(field('ingest-body'), { target: { value: 'composed by hand' } });
    // A red alert must not sit beside a body that is now perfectly valid.
    expect(screen.queryByTestId('ingest-upload-error')).toBeNull();
    expect(field('ingest-body').value).toBe('composed by hand');
  });

  it('submits the hand-composed body, so the cleared alert was not cosmetic', async () => {
    renderForm();
    await upload('empty.csv', '', 'text/csv');
    fireEvent.change(field('ingest-title'), { target: { value: 'Typed title' } });
    fireEvent.change(field('ingest-source'), { target: { value: 'typed source' } });
    fireEvent.change(field('ingest-body'), { target: { value: 'composed by hand' } });
    fireEvent.click(screen.getByTestId('ingest-submit'));
    expect(submitMock).toHaveBeenCalledTimes(1);
    expect(submitMock.mock.calls[0][0].body).toBe('composed by hand');
  });

  it('drops the rows-note once the body no longer matches it', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    expect(screen.getByTestId('ingest-upload-note')).toBeTruthy();
    fireEvent.change(field('ingest-body'), { target: { value: 'edited away' } });
    // "3 rows, fenced in the body" is a claim about the uploaded text; the body
    // is no longer that text.
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });

  it('drops the rows-note when a voice transcript is appended to a CSV body', async () => {
    renderForm();
    await upload('sales.csv', CSV, 'text/csv');
    mockTranscribe.mockResolvedValue({ transcript: 'an spoken annotation', low_confidence: false });
    fireEvent.change(screen.getByTestId('ingest-voice-file'), {
      target: { files: [new File(['dummy-audio'], 'note.webm', { type: 'audio/webm' })] },
    });
    await screen.findByTestId('ingest-voice-transcript');
    fireEvent.click(screen.getByTestId('ingest-voice-use'));
    await waitFor(() => expect(field('ingest-body').value).toContain('an spoken annotation'));
    // The transcript is APPENDED, so the fenced CSV is still in there — but the
    // body is no longer only the CSV, and the note claimed it was.
    expect(field('ingest-body').value).toContain('```csv');
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });
});

describe('VoiceCapture stays on the confirm-first flow (#54 default pin)', () => {
  // Driven to a real transcript on purpose. Asserting only that some element is
  // absent at rest would pass against an `insertDirectly` IngestForm too — the
  // confirm box is absent in BOTH modes until a transcript lands. What separates
  // them is what happens WHEN one lands: direct mode writes the body immediately
  // and renders no confirm box; the default holds it for review.
  async function transcribeAudio(text: string) {
    mockTranscribe.mockResolvedValue({ transcript: text, low_confidence: false });
    fireEvent.change(screen.getByTestId('ingest-voice-file'), {
      target: { files: [new File(['dummy-audio'], 'note.webm', { type: 'audio/webm' })] },
    });
    return (await screen.findByTestId('ingest-voice-transcript')) as HTMLTextAreaElement;
  }

  it('holds a transcript for review instead of writing it straight into the body', async () => {
    renderForm();
    const box = await transcribeAudio('spoken into the ingest body');
    expect(box.value).toBe('spoken into the ingest body');
    // The discriminating assertion: direct mode would already have set this.
    expect(field('ingest-body').value).toBe('');
  });

  it('writes the body only on an explicit Use', async () => {
    renderForm();
    await transcribeAudio('spoken into the ingest body');
    fireEvent.click(screen.getByTestId('ingest-voice-use'));
    await waitFor(() =>
      expect(field('ingest-body').value).toBe('spoken into the ingest body'),
    );
    expect(screen.queryByTestId('ingest-voice-transcript')).toBeNull();
  });

  it('drops it on Discard, leaving the body untouched', async () => {
    renderForm();
    await transcribeAudio('never mind');
    fireEvent.click(screen.getByTestId('ingest-voice-discard'));
    await waitFor(() => expect(screen.queryByTestId('ingest-voice-transcript')).toBeNull());
    expect(field('ingest-body').value).toBe('');
  });
});
