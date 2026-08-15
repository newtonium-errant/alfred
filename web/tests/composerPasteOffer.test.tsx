import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// The long-paste door, driven through the component.
//
// The design names three ways into ingest mode: attach a document, or "paste a
// long body". The first two shipped with #97; this is the third. What makes it
// delicate is WHOSE content it is — a file was chosen to be attached, but a
// paste is the operator's own message, mid-composition. So every pin here is
// about restraint: the offer appears, the text stays put, and nothing moves
// until it is accepted.

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
import { MAX_INLINE_DOC_CHARS, PASTED_TEXT_FILENAME } from '../lib/algernon/composerFanout';

/** A body over the line, and one comfortably under it. */
const LONG = 'L'.repeat(MAX_INLINE_DOC_CHARS + 1);
const SHORT = 'the coop needs a new latch';

let onSend: ReturnType<typeof vi.fn>;

function mount(props: Record<string, unknown> = {}) {
  return render(
    <UnifiedComposer
      onSend={onSend}
      instance="Salem"
      instanceLabel="Salem"
      submitBatchRequest={vi.fn() as never}
      transcribe={vi.fn() as never}
      {...props}
    />,
  );
}

/** Paste into the composer's box, as a browser would. */
async function pasteInto(user: ReturnType<typeof userEvent.setup>, body: string) {
  await user.click(screen.getByTestId('unified-input'));
  await user.paste(body);
}

beforeEach(() => {
  onSend = vi.fn();
  ingestTargets.mockResolvedValue({
    targets: [{ name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] }],
  });
  ingestSubmit.mockResolvedValue({
    status: 'created',
    path: 'document/Pasted text.md',
    record_type: 'document',
    instance: 'Salem',
  });
  (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async () => ({
    ok: true,
    json: async () => ({ targets: [] }),
  }));
});

afterEach(() => vi.clearAllMocks());

describe('a long paste is offered, never taken', () => {
  it('offers the ingest door AND leaves the text in the message', async () => {
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);

    // The offer is on screen, naming the size.
    const offer = await screen.findByTestId('unified-paste-offer');
    expect(offer.textContent).toContain((MAX_INLINE_DOC_CHARS + 1).toLocaleString());
    // …and the paste is STILL the operator's message. Nothing was intercepted,
    // nothing was moved. This half is the whole design decision: the alternative
    // — silently converting a paste into an attachment — takes the operator's
    // words out of their own message without being asked.
    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe(LONG);
    // And no chip was created behind the offer.
    expect(screen.queryByTestId('unified-chip-doc-0')).toBeNull();
  });

  it('an ORDINARY paste is left completely alone — the control', async () => {
    // Without this the pin above passes identically against a build that offers
    // the door to every paste, including a two-word one.
    const user = userEvent.setup();
    mount();

    await pasteInto(user, SHORT);

    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe(SHORT);
    expect(screen.queryByTestId('unified-paste-offer')).toBeNull();
  });

  it('a pasted FILE still routes as a file, not as text', async () => {
    // The two paste paths share one handler; the file branch must keep winning.
    const user = userEvent.setup();
    mount();

    const input = screen.getByTestId('unified-file-input');
    await user.upload(input, [new File(['# notes'], 'notes.md', { type: 'text/markdown' })]);

    await screen.findByTestId('unified-chip-doc-0');
    expect(screen.queryByTestId('unified-paste-offer')).toBeNull();
  });
});

describe('accepting the offer', () => {
  it('moves the pasted chunk into a document chip and out of the message', async () => {
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await user.click(await screen.findByTestId('unified-paste-offer-accept'));

    // The chip exists, wearing the paste's name.
    const chip = await screen.findByTestId('unified-chip-doc-0');
    expect(chip.textContent).toContain(PASTED_TEXT_FILENAME);
    // The box no longer holds it — the body lives in one place, not two.
    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe('');
    // The offer is spent.
    expect(screen.queryByTestId('unified-paste-offer')).toBeNull();
  });

  it('keeps words the operator typed AROUND the paste', async () => {
    // The chunk comes out; the message does not. An accept that cleared the box
    // wholesale would delete a question the operator had already written about
    // the very thing being filed.
    const user = userEvent.setup();
    mount();

    const box = screen.getByTestId('unified-input');
    await user.click(box);
    await user.type(box, 'what does this say about the roof? ');
    await user.paste(LONG);

    await user.click(await screen.findByTestId('unified-paste-offer-accept'));

    await screen.findByTestId('unified-chip-doc-0');
    expect((box as HTMLTextAreaElement).value).toBe('what does this say about the roof? ');
  });

  it('files the pasted body VERBATIM through the production ingest path', async () => {
    // End-to-end through the component's own default wiring rather than by
    // calling the helper: the per-layer pins cannot see a chip that was built
    // but never handed to the ingest route.
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await user.click(await screen.findByTestId('unified-paste-offer-accept'));
    await screen.findByTestId('unified-chip-doc-0');

    await user.click(screen.getByTestId('unified-send'));

    await waitFor(() => expect(ingestSubmit).toHaveBeenCalledTimes(1));
    const payload = ingestSubmit.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.body).toBe(LONG);
    expect(payload.target).toBe('SALEM');
    // The filename's extension is stripped for the title, as any attachment's is.
    expect(payload.title).toBe('Pasted text');
  });
});

describe('removing a paste-derived chip gives the text back', () => {
  // THE INVERSE OF ACCEPT. Every other doc chip is a reference to a file on
  // disk, so Remove drops a reference; a paste-derived chip holds the only
  // copy, and the same button would otherwise destroy the operator's own words.
  // These are the pins for the one path that could lose data.

  it('paste → accept → remove puts the text back VERBATIM', async () => {
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await user.click(await screen.findByTestId('unified-paste-offer-accept'));
    await screen.findByTestId('unified-chip-doc-0');
    // Precondition: the body really did leave the box, or the round trip below
    // would pass against a build that never moved it in the first place.
    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe('');

    await user.click(screen.getByTestId('unified-doc-0-remove'));

    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe(LONG);
    expect(screen.queryByTestId('unified-chip-doc-0')).toBeNull();
  });

  it('says so out loud — a reversible action that looks destructive', async () => {
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await user.click(await screen.findByTestId('unified-paste-offer-accept'));
    // BEFORE: the promise is on the chip while the operator is deciding.
    const promise = await screen.findByTestId('unified-doc-0-paste-promise');
    expect(promise.textContent).toBeTruthy();

    await user.click(screen.getByTestId('unified-doc-0-remove'));

    // AFTER: and the outcome is stated rather than left to be inferred.
    expect(screen.getByTestId('unified-paste-restored').textContent).toBeTruthy();
  });

  it('keeps words typed AFTER accepting, and appends the body', async () => {
    // The merge rule. The operator's own text is never moved or clobbered to
    // make room for a body coming back.
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await user.click(await screen.findByTestId('unified-paste-offer-accept'));
    await screen.findByTestId('unified-chip-doc-0');

    const box = screen.getByTestId('unified-input') as HTMLTextAreaElement;
    await user.type(box, 'what does this say about the roof?');
    await user.click(screen.getByTestId('unified-doc-0-remove'));

    expect(box.value.startsWith('what does this say about the roof?')).toBe(true);
    expect(box.value).toContain(LONG);
  });

  it('a real file NAMED “Pasted text.md” is still file-backed — the console control', async () => {
    // THE COLLISION CASE, and the reason the chip stores its body rather than
    // sniffing its filename. `fileFromPastedText` names its artifact
    // `PASTED_TEXT_FILENAME`, so a filename test would call this upload a
    // paste-derived chip — and Remove would then dump a file the operator
    // picked from disk into their message as text, while promising to.
    //
    // The distinction is PROVENANCE (`pastedBody !== null`), which a real file
    // cannot fake. The control that proves it has to use the exact colliding
    // name; `notes.md` below tests the easy half.
    const user = userEvent.setup();
    mount();

    await user.upload(screen.getByTestId('unified-file-input'), [
      new File(['# genuinely from disk'], PASTED_TEXT_FILENAME, { type: 'text/markdown' }),
    ]);
    const chip = await screen.findByTestId('unified-chip-doc-0');
    // Same name the paste artifact carries — so the chip LOOKS identical.
    expect(chip.textContent).toContain(PASTED_TEXT_FILENAME);
    // …and carries no promise, because it is not the same kind of thing.
    expect(screen.queryByTestId('unified-doc-0-paste-promise')).toBeNull();

    await user.click(screen.getByTestId('unified-doc-0-remove'));

    // Removed freely: nothing restored into the box, nothing claimed.
    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe('');
    expect(screen.queryByTestId('unified-paste-restored')).toBeNull();
    expect(screen.queryByTestId('unified-chip-doc-0')).toBeNull();
  });

  it('a FILE-backed chip removes freely and restores nothing — the control', async () => {
    // Without this the pins above pass identically against a build that dumped
    // every removed attachment into the message, which would be its own bug.
    const user = userEvent.setup();
    mount();

    await user.upload(screen.getByTestId('unified-file-input'), [
      new File(['# notes from disk'], 'notes.md', { type: 'text/markdown' }),
    ]);
    await screen.findByTestId('unified-chip-doc-0');
    // A file-backed chip carries no promise, because Remove means something
    // different on it.
    expect(screen.queryByTestId('unified-doc-0-paste-promise')).toBeNull();

    await user.click(screen.getByTestId('unified-doc-0-remove'));

    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe('');
    expect(screen.queryByTestId('unified-chip-doc-0')).toBeNull();
    expect(screen.queryByTestId('unified-paste-restored')).toBeNull();
  });
});

describe('declining, and going stale', () => {
  it('declining dismisses the offer and keeps the text', async () => {
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await user.click(await screen.findByTestId('unified-paste-offer-decline'));

    expect(screen.queryByTestId('unified-paste-offer')).toBeNull();
    expect((screen.getByTestId('unified-input') as HTMLTextAreaElement).value).toBe(LONG);
    // Declining is not filing: nothing was attached.
    expect(screen.queryByTestId('unified-chip-doc-0')).toBeNull();
  });

  it('the offer withdraws when the paste it describes is deleted', async () => {
    // An offer quoting a character count for text that is no longer in the box
    // is describing something that does not exist.
    const user = userEvent.setup();
    mount();

    await pasteInto(user, LONG);
    await screen.findByTestId('unified-paste-offer');

    await user.clear(screen.getByTestId('unified-input'));

    await waitFor(() => expect(screen.queryByTestId('unified-paste-offer')).toBeNull());
  });
});
