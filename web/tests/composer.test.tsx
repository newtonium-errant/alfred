import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
// #54: stub VoiceCapture so a transcript is deliverable on demand. The stub's
// button fires onTranscript exactly as the real component does in direct mode —
// the same shape playerPage/homePage use. The other tests here don't touch
// voice, so the stub is inert for them.
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ onTranscript, idPrefix }: { onTranscript: (t: string) => void; idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock-use`} onClick={() => onTranscript('why is that yellow?')}>
      mock-stt
    </button>
  ),
}));

import { Composer } from '../components/chat/Composer';
import { MAX_IMAGE_BYTES } from '../lib/algernon/schemas';

// A small in-memory image File (real PNG signature bytes; type drives the mime
// allowlist). jsdom's FileReader.readAsDataURL turns it into a data: URI the
// composer strips to bare base64.
function imageFile(name: string, type: string, size = 32): File {
  const bytes = new Uint8Array(size);
  bytes.set([0x89, 0x50, 0x4e, 0x47], 0); // \x89PNG
  return new File([bytes], name, { type });
}

describe('Composer', () => {
  it('sends on Enter and clears the input; tags a typed turn kind="text"', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    await user.type(input, 'hello world');
    await user.keyboard('{Enter}');

    expect(onSend).toHaveBeenCalledTimes(1);
    // A keyboard-typed turn carries kind:'text' (voice tag is only for transcripts).
    expect(onSend).toHaveBeenCalledWith('hello world', 'text');
    expect(input.value).toBe('');
  });

  it('does NOT send on Shift+Enter (inserts a newline instead)', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    const input = screen.getByTestId('composer-input');
    await user.type(input, 'line one');
    await user.keyboard('{Shift>}{Enter}{/Shift}');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('does not send an empty / whitespace-only message', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    await user.type(screen.getByTestId('composer-input'), '   ');
    await user.keyboard('{Enter}');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('disables the send button while disabled', () => {
    render(<Composer onSend={vi.fn()} disabled />);
    const send = screen.getByTestId('composer-send') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
  });

  it('does not send when disabled even on Enter', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} disabled />);

    // The textarea is disabled; typing is a no-op, and submit is gated.
    await user.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe('Composer image-carry (#29)', () => {
  it('a picked image adds a preview and can be removed', async () => {
    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} />);

    const input = screen.getByTestId('composer-file-input') as HTMLInputElement;
    await user.upload(input, imageFile('shot.png', 'image/png'));

    const preview = await screen.findByTestId('composer-image-preview');
    expect(preview).toBeTruthy();
    // The strip worked: the src is a bare data: URI (no double prefix).
    expect((preview as HTMLImageElement).src.startsWith('data:image/png;base64,')).toBe(true);

    await user.click(screen.getByTestId('composer-image-remove'));
    await waitFor(() => expect(screen.queryByTestId('composer-image-preview')).toBeNull());
  });

  it('an image-only send emits the placeholder caption + the images array', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    await user.upload(screen.getByTestId('composer-file-input'), imageFile('shot.png', 'image/png'));
    await screen.findByTestId('composer-image-preview');
    // No text typed → Send is still enabled because an image is attached.
    const send = screen.getByTestId('composer-send') as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    await user.click(send);

    expect(onSend).toHaveBeenCalledTimes(1);
    const [text, kind, images] = onSend.mock.calls[0];
    expect(text).toBe('(image attached, no caption)');
    expect(kind).toBe('text');
    expect(Array.isArray(images)).toBe(true);
    expect(images).toHaveLength(1);
    expect(images[0].media_type).toBe('image/png');
    expect(typeof images[0].data).toBe('string');
    expect(images[0].data.length).toBeGreaterThan(0);
  });

  it('rejects a non-allowlisted mime client-side (no attachment added)', async () => {
    render(<Composer onSend={vi.fn()} />);

    // A tiff can slip past the picker's `accept` via paste/drop, so the mime
    // gate lives in addFiles. fireEvent.change bypasses user-event's accept
    // filtering to drive that shared gate directly.
    const input = screen.getByTestId('composer-file-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [imageFile('bad.tiff', 'image/tiff')] } });

    expect(await screen.findByTestId('composer-image-error')).toBeTruthy();
    expect(screen.queryByTestId('composer-image-preview')).toBeNull();
  });

  it('rejects an oversized image client-side (> 5 MiB decoded)', async () => {
    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} />);

    const big = imageFile('huge.png', 'image/png', MAX_IMAGE_BYTES + 1);
    await user.upload(screen.getByTestId('composer-file-input'), big);

    expect(await screen.findByTestId('composer-image-error')).toBeTruthy();
    expect(screen.queryByTestId('composer-image-preview')).toBeNull();
  });

  it('caps at 4 images and surfaces a too-many error', async () => {
    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} />);

    const five = [1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`, 'image/png'));
    await user.upload(screen.getByTestId('composer-file-input'), five);

    await waitFor(() => expect(screen.getAllByTestId('composer-image-preview')).toHaveLength(4));
    expect(screen.getByTestId('composer-image-error')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// #54 half 1 — the transcript lands in THIS input, and never clobbers
// ---------------------------------------------------------------------------

describe('Composer voice transcript — direct insert, append not replace', () => {
  it('a transcript lands in the main composer input', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));

    await waitFor(() =>
      expect((screen.getByTestId('composer-input') as HTMLTextAreaElement).value).toContain(
        'why is that yellow?',
      ),
    );
  });

  it('PRE-TYPED text survives — the transcript appends with one space', async () => {
    // The reported clobber: the old handler did setValue(t) and destroyed this.
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: 'about the coop' } });
    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));

    await waitFor(() =>
      expect(input.value).toBe('about the coop why is that yellow?'),
    );
  });

  it('Send carries the COMBINED text and the voice provenance flag', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: 'quick q' } });
    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toContain('why is that yellow?'));
    fireEvent.click(screen.getByTestId('composer-send'));

    expect(onSend).toHaveBeenCalledTimes(1);
    const [text, source] = onSend.mock.calls[0];
    expect(text).toBe('quick q why is that yellow?');
    // The hidden provenance marker half 2's capture side keys on.
    expect(source).toBe('voice');
  });

  it('clearing the input IS discard — nothing is sent', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).not.toBe(''));
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.click(screen.getByTestId('composer-send'));

    expect(onSend).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// #54 — WHAT the voice-seeded send carries: the raw transcript, and only it
// ---------------------------------------------------------------------------

describe('Composer transcript carry (#54)', () => {
  it('carries the RAW transcript beside the EDITED text', async () => {
    // The whole loop rests on this pair being different: the backend learns from
    // exactly the gap between what it heard and what the operator sent.
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('why is that yellow?'));
    fireEvent.change(input, { target: { value: 'why is that yolk?' } });
    fireEvent.click(screen.getByTestId('composer-send'));

    const [text, kind, images, transcript] = onSend.mock.calls[0];
    expect(text).toBe('why is that yolk?');
    expect(kind).toBe('voice');
    expect(images).toBeUndefined();
    expect(transcript).toBe('why is that yellow?');
  });

  it('TYPED text is not part of the transcript — only what was actually spoken', async () => {
    // The STT never heard the typed words, so they cannot have been mis-heard.
    // Folding them in would make the operator's edits to his OWN typing eligible
    // to be learned as vocabulary.
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: 'about the coop' } });
    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('about the coop why is that yellow?'));
    fireEvent.click(screen.getByTestId('composer-send'));

    const [text, , , transcript] = onSend.mock.calls[0];
    expect(text).toBe('about the coop why is that yellow?');
    expect(transcript).toBe('why is that yellow?');
  });

  it('dictating twice accumulates both spoken segments with the SAME join rule', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('why is that yellow?'));
    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('why is that yellow? why is that yellow?'));
    fireEvent.click(screen.getByTestId('composer-send'));

    const [, , , transcript] = onSend.mock.calls[0];
    // Joined the way the textarea joined them, so a multi-pass dictation still
    // diffs as one piece against the sent message.
    expect(transcript).toBe('why is that yellow? why is that yellow?');
  });

  it('clearing the box discards the transcript too — the next typed send carries none', async () => {
    // Nothing spoken survives in the input, so keeping the transcript would diff
    // the next typed message against words the operator already threw away.
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).not.toBe(''));
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.change(input, { target: { value: 'typed from scratch' } });
    fireEvent.click(screen.getByTestId('composer-send'));

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0]).toEqual(['typed from scratch', 'text']);
  });

  it('after a clear, RE-DICTATING starts a fresh transcript — no ghost of the discarded one', async () => {
    // This is the case that makes the clear load-bearing rather than tidy. The
    // send-time `voiceSeeded` guard already suppresses a stale transcript on a
    // TYPED send, so the pin above passes even with the clear removed. It is
    // dictating AGAIN that re-arms the flag: without the lockstep clear the new
    // spoken text appends onto the discarded one, and the operator would ship a
    // transcript containing words that are nowhere in his message — inventing a
    // deletion the STT never made.
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('why is that yellow?'));
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).toBe('why is that yellow?'));
    fireEvent.click(screen.getByTestId('composer-send'));

    const [text, kind, , transcript] = onSend.mock.calls[0];
    expect(kind).toBe('voice');
    // The transcript matches the message exactly — one dictation, not two.
    expect(transcript).toBe('why is that yellow?');
    expect(transcript).toBe(text);
  });

  it('a send RESETS the transcript — the next turn does not re-carry it', async () => {
    // Counts are what decide whether a term crosses the propose threshold, so a
    // stale transcript riding a second turn would let one correction masquerade
    // as a repeated pattern.
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;

    fireEvent.click(screen.getByTestId('composer-voice-mock-use'));
    await waitFor(() => expect(input.value).not.toBe(''));
    fireEvent.click(screen.getByTestId('composer-send'));
    fireEvent.change(input, { target: { value: 'a plain follow-up' } });
    fireEvent.click(screen.getByTestId('composer-send'));

    expect(onSend).toHaveBeenCalledTimes(2);
    expect(onSend.mock.calls[1]).toEqual(['a plain follow-up', 'text']);
  });

  it('a typed send passes exactly TWO arguments (byte-identical to pre-feature)', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'hello world' } });
    fireEvent.click(screen.getByTestId('composer-send'));
    expect(onSend.mock.calls[0]).toHaveLength(2);
  });
});
