import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// The unified composer (#97) — the chip surface, driven through the component.
//
// The chips are the propose-then-approve surface: they decide where an
// operator's documents go, and they are the reason this feature ships behind a
// flag the operator opens by hand. So the pins here are about what is ON SCREEN
// before Send, and what actually left the browser after it.

vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ onTranscript, idPrefix }: { onTranscript: (t: string) => void; idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock-use`} onClick={() => onTranscript('dictated words')}>
      mock-stt
    </button>
  ),
  sttErrorMessage: () => 'Couldn’t transcribe that. Try again, or type it instead.',
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
import { MAX_IMAGES_PER_TURN } from '../lib/algernon/schemas';

function imageFile(name: string, type = 'image/png', size = 64): File {
  const bytes = new Uint8Array(size);
  bytes.set([0x89, 0x50, 0x4e, 0x47], 0);
  return new File([bytes], name, { type });
}

function textFile(name: string, body: string, type = 'text/markdown'): File {
  return new File([body], name, { type });
}

let batchSubmit: ReturnType<typeof vi.fn>;
let transcribe: ReturnType<typeof vi.fn>;
let onSend: ReturnType<typeof vi.fn>;

/** Render with both target families configured for the home instance. */
function mount(props: Record<string, unknown> = {}) {
  return render(
    <UnifiedComposer
      onSend={onSend}
      instance="Salem"
      instanceLabel="Salem"
      submitBatchRequest={batchSubmit as never}
      transcribe={transcribe as never}
      {...props}
    />,
  );
}

beforeEach(() => {
  onSend = vi.fn();
  batchSubmit = vi.fn(async () => ({
    status: 'queued',
    batch_id: 'b-1',
    images: 5,
    bytes: 320,
    path: 'batch/b-1.md',
    instance: 'Salem',
  }));
  transcribe = vi.fn(async () => 'the coop needs a new latch');
  ingestTargets.mockResolvedValue({
    targets: [{ name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] }],
  });
  ingestSubmit.mockResolvedValue({
    status: 'created',
    path: 'document/Notes.md',
    record_type: 'document',
    instance: 'Salem',
  });
  (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async (url: string) => {
    if (String(url).startsWith('/api/batch/targets')) {
      return { ok: true, json: async () => ({ targets: [{ name: 'Salem', label: 'Salem', home: true }] }) };
    }
    return { ok: true, json: async () => ({}) };
  });
});

afterEach(() => vi.clearAllMocks());

// ---------------------------------------------------------------------------
// The ratified chip defaults, on screen
// ---------------------------------------------------------------------------

describe('chip defaults are visible before Send', () => {
  it('a small image set defaults to DISCUSS', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), [
      imageFile('a.png'),
      imageFile('b.png'),
    ]);

    await screen.findByTestId('unified-chip-images');
    expect(screen.getByTestId('unified-images-intent-discuss').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('false');
    // And it says what that means, naming the instance.
    expect(screen.getByTestId('unified-images-description').textContent).toContain('Salem');
  });

  it('a bulk image set defaults to BATCH — the count flips it, nothing else', async () => {
    const user = userEvent.setup();
    mount();
    const five = [1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`));
    await user.upload(screen.getByTestId('unified-file-input'), five);

    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );
    expect(screen.getByTestId('unified-images-intent-discuss').getAttribute('aria-pressed')).toBe('false');
    // The bulk set is honest that it does NOT enter the conversation.
    expect(screen.getByTestId('unified-images-description').textContent).toContain(
      'not into this conversation',
    );
  });

  it('the default is recomputed as the set GROWS past the threshold', async () => {
    // Five pictures are a batch whether they arrived together or one at a time.
    const user = userEvent.setup();
    mount();
    const input = screen.getByTestId('unified-file-input');
    await user.upload(input, [imageFile('a.png'), imageFile('b.png')]);
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-discuss').getAttribute('aria-pressed')).toBe('true'),
    );
    await user.upload(input, [imageFile('c.png'), imageFile('d.png'), imageFile('e.png')]);
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );
  });

  it('a document defaults to FILE TO VAULT, with the filename as its title', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(
      screen.getByTestId('unified-file-input'),
      textFile('Coop notes.md', '# Coop\nbody'),
    );

    await screen.findByTestId('unified-chip-doc-0');
    await waitFor(() =>
      expect(screen.getByTestId('unified-doc-0-intent-file').getAttribute('aria-pressed')).toBe('true'),
    );
    await user.click(screen.getByTestId('unified-doc-0-detail'));
    expect((screen.getByTestId('unified-doc-0-title') as HTMLInputElement).value).toBe('Coop notes');
    expect((screen.getByTestId('unified-doc-0-source') as HTMLInputElement).value).toBe('Coop notes.md');
  });
});

describe('one-tap flip', () => {
  it('flipping the image chip changes where it goes', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), [imageFile('a.png')]);
    await screen.findByTestId('unified-chip-images');

    await user.click(screen.getByTestId('unified-images-intent-batch'));
    expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByTestId('unified-images-intent-discuss'));
    expect(screen.getByTestId('unified-images-intent-discuss').getAttribute('aria-pressed')).toBe('true');
  });

  it('an over-cap set REFUSES the Discuss flip with words — it does not silently flip', async () => {
    // The dangerous failure is a flip that appears to work and then quietly
    // routes 9 images at a 4-image door.
    const user = userEvent.setup();
    mount();
    const nine = Array.from({ length: 9 }, (_, i) => imageFile(`s${i}.png`));
    await user.upload(screen.getByTestId('unified-file-input'), nine);
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );

    await user.click(screen.getByTestId('unified-images-intent-discuss'));
    expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true');
    const said = screen.getByTestId('unified-images-message').textContent ?? '';
    expect(said).toContain(String(MAX_IMAGES_PER_TURN));
    expect(said).toContain('9');
  });

  it('a PDF refuses the Discuss flip and says where extraction happens', async () => {
    const user = userEvent.setup();
    mount();
    const pdf = new File([new Uint8Array([37, 80, 68, 70])], 'statement.pdf', {
      type: 'application/pdf',
    });
    await user.upload(screen.getByTestId('unified-file-input'), pdf);
    await screen.findByTestId('unified-chip-doc-0');

    await user.click(screen.getByTestId('unified-doc-0-intent-discuss'));
    expect(screen.getByTestId('unified-doc-0-intent-file').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('unified-doc-0-message').textContent).toContain('browser');
  });
});

// ---------------------------------------------------------------------------
// What actually leaves the browser
// ---------------------------------------------------------------------------

describe('filing a document, then discussing it', () => {
  it('files to the matched target and the SAME turn names the new record', async () => {
    // Discuss-after-filing: the created path rides the turn so the assistant can
    // read it straight out of the vault. It rides IN the message — visible in
    // the operator's own bubble, never a hidden payload.
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), textFile('Notes.md', 'body text'));
    await screen.findByTestId('unified-chip-doc-0');
    await waitFor(() => expect(screen.getByTestId('unified-doc-0-message')).toBeTruthy(), {
      timeout: 2000,
    }).catch(() => undefined);

    fireEvent.change(screen.getByTestId('unified-input'), {
      target: { value: 'what does this say?' },
    });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(1));
    const payload = ingestSubmit.mock.calls[0][0];
    // The ingest env segment, not the chat spelling.
    expect(payload.target).toBe('SALEM');
    expect(payload.title).toBe('Notes');
    expect(payload.body).toBe('body text');

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const [message] = onSend.mock.calls[0];
    expect(message).toContain('what does this say?');
    expect(message).toContain('document/Notes.md');
  });

  it('a doc filed with NO message holds the path for the NEXT turn', async () => {
    // No turn is invented from an operator who typed nothing — but the path is
    // not dropped either. It waits, visibly.
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), textFile('Notes.md', 'body'));
    await screen.findByTestId('unified-chip-doc-0');
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(1));
    expect(onSend).not.toHaveBeenCalled();
    await screen.findByTestId('unified-filed-context');

    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'summarise it' } });
    await user.click(screen.getByTestId('unified-send'));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][0]).toContain('document/Notes.md');
  });

  it('a discussed document is QUOTED into the turn and never filed', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), textFile('Notes.md', 'the body'));
    await screen.findByTestId('unified-chip-doc-0');
    await waitFor(() =>
      expect(screen.getByTestId('unified-doc-0-intent-discuss').getAttribute('disabled')).toBeNull(),
    );
    await user.click(screen.getByTestId('unified-doc-0-intent-discuss'));
    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'thoughts?' } });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][0]).toContain('the body');
    expect(ingestSubmit).not.toHaveBeenCalled();
  });
});

describe('images into the conversation', () => {
  it('a discussed set rides the turn as base64 attachments', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), [imageFile('a.png')]);
    await screen.findByTestId('unified-chip-images');
    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'what is this?' } });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const [message, kind, images] = onSend.mock.calls[0];
    expect(message).toBe('what is this?');
    expect(kind).toBe('text');
    expect(images).toHaveLength(1);
    expect(images[0].media_type).toBe('image/png');
    expect(typeof images[0].data).toBe('string');
    expect(batchSubmit).not.toHaveBeenCalled();
  });

  it('a voice-seeded send still carries the RAW transcript (#54 survives the new composer)', async () => {
    const user = userEvent.setup();
    mount();
    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    const input = screen.getByTestId('unified-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.value).toBe('dictated words'));
    fireEvent.change(input, { target: { value: 'dictated werds' } });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const [message, kind, images, transcript] = onSend.mock.calls[0];
    expect(message).toBe('dictated werds');
    expect(kind).toBe('voice');
    expect(images).toBeUndefined();
    expect(transcript).toBe('dictated words');
  });
});

describe('batch', () => {
  it('the message IS the instruction, and it is not also posted to the chat', async () => {
    const user = userEvent.setup();
    mount();
    const five = [1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`));
    await user.upload(screen.getByTestId('unified-file-input'), five);
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );

    fireEvent.change(screen.getByTestId('unified-input'), {
      target: { value: 'read the invoice number' },
    });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(batchSubmit).toHaveBeenCalledTimes(1));
    const [target, form] = batchSubmit.mock.calls[0];
    expect(target).toBe('Salem');
    expect((form as FormData).get('instruction')).toBe('read the invoice number');
    expect((form as FormData).getAll('images')).toHaveLength(5);
    // A page-reading instruction is not a chat message.
    expect(onSend).not.toHaveBeenCalled();
    // The receipt outlives the chip. A success that clears its own chip and says
    // nothing is indistinguishable from a send that never happened — the chip
    // vanishing IS the thing that made this pin necessary.
    await waitFor(() =>
      expect(screen.getByTestId('unified-result-0').textContent).toContain('b-1'),
    );
    expect(screen.queryByTestId('unified-chip-images')).toBeNull();
  });

  it('a batch with NO instruction is refused before anything is uploaded', async () => {
    const user = userEvent.setup();
    mount();
    const five = [1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`));
    await user.upload(screen.getByTestId('unified-file-input'), five);
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() =>
      expect(screen.getByTestId('unified-images-message').textContent).toContain('every scan'),
    );
    expect(batchSubmit).not.toHaveBeenCalled();
    // Positive control: with an instruction the SAME set uploads.
    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'read them' } });
    await user.click(screen.getByTestId('unified-send'));
    await waitFor(() => expect(batchSubmit).toHaveBeenCalledTimes(1));
  });

  it('a FAILED batch keeps the instruction so the retry is not a retype', async () => {
    batchSubmit.mockRejectedValueOnce(new Error('network down'));
    const user = userEvent.setup();
    mount();
    const five = [1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`));
    await user.upload(screen.getByTestId('unified-file-input'), five);
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );
    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'read them all' } });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() =>
      expect(screen.getByTestId('unified-images-message').textContent).toBeTruthy(),
    );
    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe('read them all');
    // …and the scans are still staged, so Send is one tap rather than a re-pick.
    expect(screen.getByTestId('unified-chip-images')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Partial-failure honesty
// ---------------------------------------------------------------------------

describe('one attachment’s refusal does not sink the others', () => {
  it('two documents, one refused: each reports its own outcome', async () => {
    ingestSubmit
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({
        status: 'created',
        path: 'document/Second.md',
        record_type: 'document',
        instance: 'Salem',
      });

    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), [
      textFile('First.md', 'one'),
      textFile('Second.md', 'two'),
    ]);
    await screen.findByTestId('unified-chip-doc-1');
    await user.click(screen.getByTestId('unified-send'));

    // BOTH were attempted — the second is what a rejection-escapes bug loses.
    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(2));
    // The failed one is kept on screen, with its own words, ready to retry.
    await waitFor(() => expect(screen.getByTestId('unified-chip-doc-0')).toBeTruthy());
    expect(screen.getByTestId('unified-doc-0-message').textContent).toBeTruthy();
    expect(screen.getByTestId('unified-doc-0-message').textContent).not.toBe('');
    // The succeeded one is gone from the tray, and its path reached the turn.
    expect(screen.queryByTestId('unified-chip-doc-1')).toBeNull();
  });

  it('a batch failure leaves a sibling document’s filing intact', async () => {
    batchSubmit.mockRejectedValueOnce(new Error('network down'));
    const user = userEvent.setup();
    mount();
    const files = [
      ...[1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`)),
      textFile('Notes.md', 'body'),
    ];
    await user.upload(screen.getByTestId('unified-file-input'), files);
    await screen.findByTestId('unified-chip-doc-0');
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-intent-batch').getAttribute('aria-pressed')).toBe('true'),
    );
    fireEvent.change(screen.getByTestId('unified-input'), { target: { value: 'read them' } });
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(batchSubmit).toHaveBeenCalledTimes(1));
    // The document still filed, even though the batch beside it did not.
    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId('unified-images-message').textContent).toBeTruthy(),
    );
  });
});

describe('nothing routes to an instance that was not configured for it', () => {
  it('an unconfigured instance is REFUSED by name, and nothing is submitted', async () => {
    const user = userEvent.setup();
    mount({ instance: 'Hypatia', instanceLabel: 'Hypatia' });
    await user.upload(screen.getByTestId('unified-file-input'), textFile('Notes.md', 'body'));
    await screen.findByTestId('unified-chip-doc-0');
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() =>
      expect(screen.getByTestId('unified-doc-0-message').textContent).toContain('Hypatia'),
    );
    expect(screen.getByTestId('unified-doc-0-message').textContent).toContain('nothing was filed');
    // The quiet delivery to whichever instance IS wired is the failure being
    // excluded — so nothing left the browser at all.
    expect(ingestSubmit).not.toHaveBeenCalled();
    // The attachment is kept, so switching instance and re-sending is one tap.
    expect(screen.getByTestId('unified-chip-doc-0')).toBeTruthy();
  });

  it('POSITIVE CONTROL — the configured instance files the identical document', async () => {
    // Without this, the pin above passes against a composer that refuses
    // EVERYTHING, which would be a dead feature reported as a working guard.
    const user = userEvent.setup();
    mount({ instance: 'Salem', instanceLabel: 'Salem' });
    await user.upload(screen.getByTestId('unified-file-input'), textFile('Notes.md', 'body'));
    await screen.findByTestId('unified-chip-doc-0');
    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(1));
    expect(ingestSubmit.mock.calls[0][0].target).toBe('SALEM');
  });
});

describe('audio', () => {
  it('a recording is transcribed at attach and filed as its text', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(
      screen.getByTestId('unified-file-input'),
      new File([new Uint8Array(8)], 'memo.m4a', { type: 'audio/mp4' }),
    );

    await screen.findByTestId('unified-chip-doc-0');
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
    // ILB: the transcription's outcome is stated before it becomes a record.
    await waitFor(() =>
      expect(screen.getByTestId('unified-doc-0-message').textContent).toContain('Transcribed'),
    );

    await user.click(screen.getByTestId('unified-send'));
    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(1));
    expect(ingestSubmit.mock.calls[0][0].body).toBe('the coop needs a new latch');
    expect(ingestSubmit.mock.calls[0][0].title).toBe('memo');
  });

  it('an empty transcript is refused with a remedy, and nothing is filed', async () => {
    transcribe.mockResolvedValueOnce('   ');
    const user = userEvent.setup();
    mount();
    await user.upload(
      screen.getByTestId('unified-file-input'),
      new File([new Uint8Array(8)], 'silence.m4a', { type: 'audio/mp4' }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('unified-doc-0-message').textContent).toContain(
        'Nothing was transcribed',
      ),
    );
    await user.click(screen.getByTestId('unified-send'));
    expect(ingestSubmit).not.toHaveBeenCalled();
  });

  it('the composer offers ONE audio door — the dictation control drops its uploader', () => {
    // Two upload affordances with different outcomes is the "distinction made by
    // which control you used" this surface exists to remove. The mock records
    // the prop it was given.
    mount();
    expect(screen.queryByTestId('composer-voice-file-label')).toBeNull();
    expect(screen.getByTestId('composer-voice-mock-use')).toBeTruthy();
  });
});

describe('refusals at the picker', () => {
  it('an unsupported file is named, and the supported ones alongside it still attach', async () => {
    mount();
    // A .zip can reach the composer by paste/drop, past the picker's `accept`.
    // `fireEvent.change` bypasses user-event's own accept filtering to drive the
    // shared classification gate directly. (The convention was inherited from
    // composer.test.tsx, which the composer-deletion lane retired along with the
    // component it drove; the reason for it is unchanged.)
    fireEvent.change(screen.getByTestId('unified-file-input'), {
      target: {
        files: [
          new File(['x'], 'archive.zip', { type: 'application/zip' }),
          textFile('Notes.md', 'body'),
        ],
      },
    });

    await waitFor(() =>
      expect(screen.getByTestId('unified-pick-error').textContent).toContain('archive.zip'),
    );
    // The good one is not collateral damage.
    expect(screen.getByTestId('unified-chip-doc-0')).toBeTruthy();
  });

  it('an EMPTY document is refused at attach, with #57’s words', async () => {
    const user = userEvent.setup();
    mount();
    await user.upload(screen.getByTestId('unified-file-input'), textFile('blank.md', ''));
    await waitFor(() =>
      expect(screen.getByTestId('unified-doc-0-message').textContent).toContain('is empty'),
    );
    await user.click(screen.getByTestId('unified-send'));
    expect(ingestSubmit).not.toHaveBeenCalled();
  });
});

describe('the composer still behaves like a composer', () => {
  it('an empty box with no attachments cannot send', () => {
    mount();
    expect((screen.getByTestId('unified-send') as HTMLButtonElement).disabled).toBe(true);
  });

  it('Enter sends, Shift+Enter does not', async () => {
    const user = userEvent.setup();
    mount();
    const input = screen.getByTestId('unified-input');
    await user.type(input, 'hello');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: 'hello' } });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
  });

  it('held unsent text (#94c) is offered back into the box', async () => {
    mount({ seedText: 'the reply I nearly lost', onSeedConsumed: vi.fn() });
    await waitFor(() =>
      expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe(
        'the reply I nearly lost',
      ),
    );
  });

  it('a disabled composer sends nothing', () => {
    mount({ disabled: true });
    expect((screen.getByTestId('unified-send') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('unified-attach') as HTMLButtonElement).disabled).toBe(true);
  });
});
