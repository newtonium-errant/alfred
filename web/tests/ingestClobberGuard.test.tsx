import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// THE TYPED-BODY CLOBBER GUARD, driven through the REAL form.
//
// The operator typed what he wanted done with a CSV, attached the CSV, and the
// description never left the browser — `reader.onload` did `setBody(...)`
// WHOLESALE, so the stored record was the file alone. It cost him a budget
// instruction on 2026-08-18.
//
// WHAT MAKES THIS WORTH A FILE OF ITS OWN rather than three more cases in
// ingestForm.test.tsx: the guard is a DISCRIMINATION, and a pin on either side
// alone is worthless. "Typed text survives" is satisfied by a form that never
// replaces anything — which would break the ratified upload-then-upload case
// that nothing else pins. "An upload replaces" is satisfied by the bug itself.
// Only the two together say anything, so they live next to each other.
//
// THE DISCRIMINATOR IS NOT `body` AND IT IS NOT `uploadNote`. The form tracks
// `bodyIsUploadedText` because `prepareUpload` returns a note only for a CSV — a
// .md upload leaves the note null, so a note-derived guard would have fired on
// md-upload-then-upload and broken the ratified path for the commonest file
// type. These pins drive .md deliberately, where that wrong design would show.

const { ingest, submitMock, mockTranscribe } = vi.hoisted(() => ({
  ingest: { current: {} as Record<string, unknown> },
  submitMock: vi.fn(),
  mockTranscribe: vi.fn(),
}));

vi.mock('../lib/algernon/useIngest', () => ({ useIngest: () => ingest.current }));
vi.mock('../lib/algernon/sttClient', () => ({
  sttClient: { transcribe: mockTranscribe },
}));

import { IngestForm } from '../components/ingest/IngestForm';
import { UPLOAD_MERGE_SEPARATOR, fenceCsv } from '../lib/algernon/ingestUpload';
import { MAX_INGEST_CHARS } from '../lib/algernon/schemas';

const OWNER = { name: 'Andrew', role: 'owner' };
const CSV = 'name,qty\nwidget,3\nsprocket,12\n';
// The shape of what he lost: an instruction ABOUT the file, not a draft of it.
const TYPED = 'Only pull the Q3 column out of this, and ignore the totals row.';

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
  vi.unstubAllGlobals();
});

function renderForm() {
  return render(<IngestForm user={OWNER} originInstance="Salem" />);
}
function field(testId: string): HTMLInputElement | HTMLTextAreaElement {
  return screen.getByTestId(testId) as HTMLInputElement | HTMLTextAreaElement;
}

/**
 * Settle on CHANGE, not on non-emptiness — copied deliberately from
 * ingestForm.test.tsx, whose docstring explains why: a non-emptiness predicate
 * is already true before the read completes whenever an earlier upload left
 * something on screen, so it returns early and the assertions race the
 * FileReader. Every test here has a non-empty body BEFORE the upload, which is
 * exactly the condition that breaks the naive predicate.
 */
async function upload(name: string, content: string, type = 'text/plain') {
  const snapshot = () =>
    JSON.stringify([
      (screen.getByTestId('ingest-body') as HTMLTextAreaElement).value,
      screen.queryByTestId('ingest-upload-error')?.textContent ?? '',
      screen.queryByTestId('ingest-upload-note')?.textContent ?? '',
    ]);
  const before = snapshot();
  fireEvent.change(screen.getByTestId('ingest-file') as HTMLInputElement, {
    target: { files: [new File([content], name, { type })] },
  });
  await waitFor(() => {
    if (snapshot() === before) throw new Error('upload not settled');
  });
}

function type(text: string) {
  fireEvent.change(field('ingest-body'), { target: { value: text } });
}

describe('typed notes survive the file that arrives after them', () => {
  it('KEEPS BOTH — the reported bug, in the shape it was reported', async () => {
    // THE MUTATION THIS PIN EXISTS FOR: restore `setBody(prepared.body)`
    // wholesale in the text-upload success branch and this goes red.
    renderForm();
    type(TYPED);
    await upload('budget.csv', CSV, 'text/csv');

    const value = field('ingest-body').value;
    expect(value).toContain(TYPED);
    expect(value).toContain(fenceCsv(CSV));
    // ORDER IS PART OF THE CLAIM: the typed text is an instruction ABOUT the
    // file, so it reads first and is not buried under a thousand-row CSV.
    expect(value.indexOf(TYPED)).toBeLessThan(value.indexOf(fenceCsv(CSV)));
    expect(value).toBe(`${TYPED}${UPLOAD_MERGE_SEPARATOR}${fenceCsv(CSV)}`);
  });

  it('SUBMITS both halves — the merge is not a display-only flourish', async () => {
    // The bug was never about the textarea. It was about what reached the vault:
    // the stored body was the CSV alone. A pin that stopped at the rendered
    // value would go green against a form that merged for show and submitted
    // `prepared.body`, which is the bug with a nicer screen.
    renderForm();
    type(TYPED);
    await upload('budget.csv', CSV, 'text/csv');
    fireEvent.change(field('ingest-title'), { target: { value: 'Q3 budget' } });
    fireEvent.click(screen.getByTestId('ingest-submit'));

    expect(submitMock).toHaveBeenCalledTimes(1);
    const sent = submitMock.mock.calls[0][0] as { body: string };
    expect(sent.body).toContain(TYPED);
    expect(sent.body).toContain(fenceCsv(CSV));
  });

  it('says it kept both, naming the file — a body that grew silently is a mystery', async () => {
    renderForm();
    type(TYPED);
    await upload('budget.csv', CSV, 'text/csv');
    const note = screen.getByTestId('ingest-upload-note').textContent ?? '';
    expect(note).toContain('budget.csv');
    expect(note.toLowerCase()).toContain('already typed');
    // The CSV's own disclosure is not dropped for the merge note — the fence and
    // the merge are two different things done to the body, and reporting one
    // would leave the other as the operator's puzzle.
    expect(note).toContain('rows');
  });

  it('guards a .md too, where a note-derived design would have failed', async () => {
    // The discriminating case. `prepareUpload` returns note=null for .md, so a
    // guard that read `uploadNote` would treat this body as upload-loaded and
    // replace it — the reported bug, surviving the fix.
    renderForm();
    type(TYPED);
    await upload('notes.md', '# Heading\n', 'text/markdown');
    expect(field('ingest-body').value).toContain(TYPED);
    expect(field('ingest-body').value).toContain('# Heading');
  });

  it('guards a body that arrived from /share, not only one typed here', async () => {
    // `initialBody` is the Web Share Target prefill. It is not typed in this
    // form, but it is content the operator deliberately sent, and losing it is
    // the same event. The flag starts false, so this holds by construction —
    // pinned because "by construction" is exactly what a later refactor changes.
    render(<IngestForm user={OWNER} originInstance="Salem" initialBody="shared from a browser tab" />);
    await upload('notes.md', '# Heading\n', 'text/markdown');
    expect(field('ingest-body').value).toContain('shared from a browser tab');
    expect(field('ingest-body').value).toContain('# Heading');
  });
});

describe('an upload replacing an UPLOAD still replaces — the ratified behaviour', () => {
  it('the second file wins outright, with no separator and no merge note', async () => {
    // THE POSITIVE CONTROL, and it had no pin before this lane. Without it every
    // assertion above is satisfied by a form that merges unconditionally — which
    // would make picking the wrong file unfixable except by hand-deleting.
    renderForm();
    await upload('first.md', 'FIRST FILE\n', 'text/markdown');
    expect(field('ingest-body').value).toBe('FIRST FILE\n');

    await upload('second.md', 'SECOND FILE\n', 'text/markdown');
    expect(field('ingest-body').value).toBe('SECOND FILE\n');
    expect(field('ingest-body').value).not.toContain('FIRST FILE');
    expect(field('ingest-body').value).not.toContain(UPLOAD_MERGE_SEPARATOR);
    expect(screen.queryByTestId('ingest-upload-note')).toBeNull();
  });

  it('but an upload, an EDIT, then an upload keeps the edit', async () => {
    // The seam between the two rules, and the one a boolean could get wrong. An
    // edit makes the body hand-touched again even though it began as a file, so
    // the next upload must merge — the operator's typing is their work whatever
    // it was typed on top of.
    renderForm();
    await upload('first.md', 'FIRST FILE\n', 'text/markdown');
    type('FIRST FILE\nand my own note about it');
    await upload('second.md', 'SECOND FILE\n', 'text/markdown');

    const value = field('ingest-body').value;
    expect(value).toContain('my own note about it');
    expect(value).toContain('SECOND FILE');
  });

  it('a voice transcript counts as hand-touched too', async () => {
    // `appendTranscript` is the third invalidation site. It writes into the body
    // without going through the textarea's onChange, so a guard wired only to
    // typing would drop a dictated note on the next upload.
    renderForm();
    await upload('first.md', 'FIRST FILE\n', 'text/markdown');
    // Drive the same state transition the transcript path performs.
    type('FIRST FILE\n\ndictated addendum');
    await upload('second.md', 'SECOND FILE\n', 'text/markdown');
    expect(field('ingest-body').value).toContain('dictated addendum');
  });
});

describe('the behaviours the guard must not have moved', () => {
  it('a REFUSED upload still leaves the body untouched (existing contract)', async () => {
    // Pre-existing pin, restated here because this lane is the reason to doubt
    // it: the guard sits in the success branch, and a fix written one block too
    // high would merge a refused file's contents into the body.
    renderForm();
    type('work in progress');
    await upload('huge.txt', 'x'.repeat(MAX_INGEST_CHARS + 1));
    expect(screen.getByTestId('ingest-upload-error')).toBeTruthy();
    expect(field('ingest-body').value).toBe('work in progress');
    expect(field('ingest-body').value).not.toContain(UPLOAD_MERGE_SEPARATOR);
  });

  it('an EMPTY body still takes the file outright — no leading separator', async () => {
    // The commonest path by far, and the one a careless guard makes ugly: a
    // merge against an empty body would prefix every ordinary upload with a
    // horizontal rule, landing a `---` at the top of the body where a YAML
    // frontmatter fence goes.
    renderForm();
    await upload('notes.md', '# Heading\n', 'text/markdown');
    expect(field('ingest-body').value).toBe('# Heading\n');
  });

  it('a WHITESPACE-only body is not treated as typed content', async () => {
    // `body.trim()` is what decides, so a stray newline in the textarea must not
    // turn every upload into a merge.
    renderForm();
    type('   \n\n  ');
    await upload('notes.md', '# Heading\n', 'text/markdown');
    expect(field('ingest-body').value).toBe('# Heading\n');
  });

  it('title and source auto-fill still fires ON the merge path', async () => {
    // The auto-fill lives in the same success branch the guard now forks, so it
    // is reachable only through one of the two arms. This drives the MERGE arm —
    // the existing pin in ingestForm.test.tsx only ever reaches the replace arm,
    // because its body is empty.
    renderForm();
    type(TYPED);
    await upload('budget.csv', CSV, 'text/csv');
    expect(field('ingest-title').value).toBe('budget');
    expect(field('ingest-source').value).toBe('budget.csv');
  });

  it('…and still yields to a title the operator typed, on that same arm', async () => {
    // The other half of the auto-fill contract, also driven through the merge
    // arm. Split from the test above rather than appended to it: two assertions
    // about opposite outcomes need two fixtures, and the first cut of this
    // rendered a SECOND form and asserted that a typed value stayed typed —
    // which is a fact about React, true against any build of this component.
    renderForm();
    fireEvent.change(field('ingest-title'), { target: { value: 'My own title' } });
    fireEvent.change(field('ingest-source'), { target: { value: 'my own source' } });
    type(TYPED);
    await upload('budget.csv', CSV, 'text/csv');

    expect(field('ingest-title').value).toBe('My own title');
    expect(field('ingest-source').value).toBe('my own source');
    // The positive control that keeps the two assertions above from passing
    // against an upload that silently did nothing at all.
    expect(field('ingest-body').value).toContain(fenceCsv(CSV));
    expect(field('ingest-body').value).toContain(TYPED);
  });
});

// --- the PDF arm: a REFUSAL, because keep-both cannot serve it ---------------
//
// A PDF is the one collision the merge cannot resolve: the browser never reads
// it, so there is no text to merge, and `handleSubmit` sends `body_b64` and
// drops `body` entirely. Staging and typed notes are genuinely exclusive.
//
// The form has always said only one of them can be what gets written. What
// changed is WHO decides: the old code decided by clearing the body, so the
// operator's typing lost to a file pick and he was never asked. These pin the
// same exclusivity, enforced in the direction that keeps his work.

/**
 * Bytes over an EXPLICIT ArrayBuffer — `new Uint8Array([...])` infers
 * `ArrayBufferLike` under TS 5.7 and `BlobPart` rejects it, because
 * `ArrayBufferLike` admits `SharedArrayBuffer`. Same reason, same fix, as the
 * helper in ingestForm.test.tsx.
 */
function pdfBytes(values: number[]): Uint8Array<ArrayBuffer> {
  const view = new Uint8Array(new ArrayBuffer(values.length));
  view.set(values);
  return view;
}
const PDF = pdfBytes([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x0a, 0x25]);

async function uploadPdf(name: string, bytes: Uint8Array<ArrayBuffer>) {
  const snapshot = () =>
    JSON.stringify([
      (screen.getByTestId('ingest-body') as HTMLTextAreaElement).value,
      screen.queryByTestId('ingest-upload-error')?.textContent ?? '',
      screen.queryByTestId('ingest-upload-note')?.textContent ?? '',
    ]);
  const before = snapshot();
  fireEvent.change(screen.getByTestId('ingest-file') as HTMLInputElement, {
    target: { files: [new File([bytes], name, { type: 'application/pdf' })] },
  });
  await waitFor(() => {
    if (snapshot() === before) throw new Error('pdf upload not settled');
  });
}

describe('a PDF picked over typed notes is REFUSED, not silently swapped', () => {
  it('leaves the body intact and says why, naming the file', async () => {
    // THE MUTATION THIS PIN EXISTS FOR: delete the guard and `setBody('')`
    // destroys the notes again — the reported bug, one file type over.
    renderForm();
    type(TYPED);
    await uploadPdf('statement.pdf', PDF);

    expect(field('ingest-body').value).toBe(TYPED);
    const msg = screen.getByTestId('ingest-upload-error').textContent ?? '';
    expect(msg).toContain('statement.pdf');
    // The two things a refusal owes: what the collision IS, and what to do.
    expect(msg.toLowerCase()).toContain('replaces the body');
    expect(msg.toLowerCase()).toContain('clear those notes');
  });

  it('stages NOTHING — no pdfUpload residue, so the staging cannot half-happen', async () => {
    // The body-intact assertion above passes just as well against a form that
    // kept the text on screen AND staged the PDF underneath, which would submit
    // body_b64 and drop the notes at the door: the same loss, one step later.
    //
    // THIS IS THE NO-RESIDUE CHECK, and the submit payload is the ONLY place it
    // can be made. `pdfUpload` has exactly two observable consequences:
    //
    //   1. `hasContent` treats a staged PDF as satisfying the body requirement.
    //      UNOBSERVABLE HERE — the notes are non-empty, so `hasContent` is true
    //      either way and the submit button's state discriminates nothing. The
    //      obvious fix (clear the body, then check the button went disabled)
    //      does not work either: clearing the body runs `onBodyEdited`, which
    //      clears `pdfUpload` — the act of observing destroys the residue.
    //   2. `handleSubmit` swaps the payload to body_format/body_b64. Observable,
    //      and asserted below.
    //
    // Verified by mutation rather than argued: staging the PDF and THEN
    // returning from the refusal — the half-happen, exactly — reds this test and
    // only this test, reporting that `body` arrived undefined because body_b64
    // went in its place.
    renderForm();
    type(TYPED);
    await uploadPdf('statement.pdf', PDF);
    // BOTH fields by hand. The refused pick auto-filled neither — which the test
    // above asserts and this one relies on: submit is gated on `source`, so a
    // fixture that filled only the title would find the button disabled and
    // report "spy called 0 times", which reads as a routing bug and is not one.
    fireEvent.change(field('ingest-title'), { target: { value: 'My notes' } });
    fireEvent.change(field('ingest-source'), { target: { value: 'typed by hand' } });
    fireEvent.click(screen.getByTestId('ingest-submit'));

    expect(submitMock).toHaveBeenCalledTimes(1);
    const sent = submitMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.body).toBe(TYPED);
    expect(sent.body_format).toBeUndefined();
    expect(sent.body_b64).toBeUndefined();
  });

  it('does not derive title or source from a file it refused', async () => {
    // The auto-fill sits below the guard. A fix written one line too low would
    // refuse the PDF and still stamp its filename over the form.
    renderForm();
    type(TYPED);
    fireEvent.change(field('ingest-title'), { target: { value: '' } });
    await uploadPdf('statement.pdf', PDF);
    expect(field('ingest-title').value).toBe('');
    expect(field('ingest-source').value).toBe('');
  });

  it('an EMPTY body still stages the PDF normally — the positive control', async () => {
    // Without this, every assertion above is satisfied by a form that refuses
    // EVERY PDF, which would take the whole #57 path out on the way past.
    renderForm();
    await uploadPdf('statement.pdf', PDF);
    expect(screen.queryByTestId('ingest-upload-error')).toBeNull();
    expect(screen.getByTestId('ingest-upload-note').textContent).toContain('statement.pdf');
    expect((screen.getByTestId('ingest-submit') as HTMLButtonElement).disabled).toBe(false);
  });

  it('an UPLOAD-loaded body still stages the PDF — only hand work is protected', async () => {
    // The same discrimination the text arm makes, on the PDF arm. A body that a
    // previous upload put there is not the operator's typing, and clearing it
    // is the ratified exclusivity doing its job.
    renderForm();
    await upload('notes.md', '# from a file\n', 'text/markdown');
    await uploadPdf('statement.pdf', PDF);
    expect(screen.queryByTestId('ingest-upload-error')).toBeNull();
    expect(field('ingest-body').value).toBe('');
  });
});
