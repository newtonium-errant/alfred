import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

import { useComposerText } from '../lib/algernon/useComposerText';
import { MAX_TRANSCRIPT_CHARS } from '../lib/algernon/schemas';

/**
 * The four rules of the composer's text half, pinned at the rule's own home.
 *
 * WHY THIS FILE EXISTS NOW. These rules were pinned only through the LEGACY
 * `Composer` component, in `composer.test.tsx`, which the composer-deletion
 * lane retired along with the component. The rules did not go with it: the hook
 * is shared, and `UnifiedComposer.tsx:190` calls it with the identical shape
 * the deleted `Composer.tsx:71` did. Deleting the component's test file without
 * this one would have dropped six incident pins in silence — the hook had no
 * test of its own, and the live door covers exactly one of them (the raw
 * transcript riding a voice-seeded send, `unifiedComposer.test.tsx`).
 *
 * The hook's docstring says each rule encodes an incident, so each pin below
 * names the incident rather than the mechanism:
 *
 *   * APPEND, never replace — the original `setValue(t)` destroyed whatever the
 *     operator had already typed. That is the clobber that was reported.
 *   * The transcript mirrors the append with the SAME join rule, so a message
 *     dictated in several passes diffs as ONE piece against what was sent.
 *   * Clearing the box clears the transcript in LOCKSTEP — otherwise dictating
 *     again appends onto a discarded transcript and the operator ships one
 *     containing words that are nowhere in his message, inventing a deletion
 *     the STT never made.
 *   * The seed lands only in an EMPTY box: a visible re-compose wins.
 *
 * THE DOOR IS PINNED SEPARATELY, and has to be. These are unit pins on the
 * rule; that the composer an operator actually types into still ROUTES through
 * this hook is a different claim, made e2e through /chat in
 * `chatPageTranscript.test.tsx` and at the component in `unifiedComposer.test.tsx`.
 * A hook that behaved perfectly while nothing called it would satisfy this file
 * completely.
 */

afterEach(() => {
  vi.restoreAllMocks();
});

describe('append, never replace', () => {
  it('PRE-TYPED text survives — the transcript appends with one space', () => {
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.setValue('about the coop'));
    act(() => result.current.appendTranscript('why is that yellow?'));

    expect(result.current.value).toBe('about the coop why is that yellow?');
  });

  it('a voice-only message carries no leading whitespace', () => {
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('why is that yellow?'));

    expect(result.current.value).toBe('why is that yellow?');
  });

  it('the kind flips to voice once a transcript lands, and starts at text', () => {
    const { result } = renderHook(() => useComposerText());
    expect(result.current.kind).toBe('text');

    act(() => result.current.appendTranscript('spoken'));

    expect(result.current.kind).toBe('voice');
    expect(result.current.voiceSeeded).toBe(true);
  });
});

describe('what the transcript carries — only what was spoken', () => {
  it('TYPED text is not part of the transcript', () => {
    // The STT never heard the typed words, so they cannot have been mis-heard.
    // Folding them in would make the operator's edits to his OWN typing
    // eligible to be learned as vocabulary.
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.setValue('about the coop'));
    act(() => result.current.appendTranscript('why is that yellow?'));

    expect(result.current.value).toBe('about the coop why is that yellow?');
    expect(result.current.takeTranscript()).toBe('why is that yellow?');
  });

  it('dictating twice accumulates both segments with the SAME join rule', () => {
    // Joined the way the textarea joined them, so a multi-pass dictation still
    // diffs as one piece against the sent message.
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('why is that'));
    act(() => result.current.appendTranscript('yellow?'));

    expect(result.current.value).toBe('why is that yellow?');
    expect(result.current.takeTranscript()).toBe('why is that yellow?');
  });

  it('the RAW transcript survives an edit to the box — the pair is the point', () => {
    // The backend learns from exactly the gap between what it heard and what
    // the operator sent, so the two must be able to differ.
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('why is that yellow?'));
    act(() => result.current.setValue('why is that yolk?'));

    expect(result.current.value).toBe('why is that yolk?');
    expect(result.current.takeTranscript()).toBe('why is that yellow?');
  });
});

describe('clearing the box discards the transcript in LOCKSTEP', () => {
  it('a cleared box carries no transcript, and the next send is text again', () => {
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('why is that yellow?'));
    act(() => result.current.setValue(''));
    act(() => result.current.setValue('typed from scratch'));

    expect(result.current.kind).toBe('text');
    expect(result.current.takeTranscript()).toBeUndefined();
  });

  it('after a clear, RE-DICTATING starts a fresh transcript — no ghost', () => {
    // THE PIN THAT MAKES THE CLEAR LOAD-BEARING rather than tidy. The
    // `voiceSeeded` guard in `takeTranscript` already suppresses a stale
    // transcript on a TYPED send, so the pin above passes even with the
    // lockstep clear removed. It is dictating AGAIN that re-arms the flag:
    // without the clear the new spoken text appends onto the discarded one and
    // the operator ships a transcript containing words that are nowhere in his
    // message.
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('why is that yellow?'));
    act(() => result.current.setValue(''));
    act(() => result.current.appendTranscript('why is that yellow?'));

    // The transcript matches the message exactly — one dictation, not two.
    expect(result.current.takeTranscript()).toBe('why is that yellow?');
    expect(result.current.takeTranscript()).toBe(result.current.value);
  });

  it('whitespace-only counts as cleared', () => {
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('spoken words'));
    act(() => result.current.setValue('   '));

    expect(result.current.kind).toBe('text');
    expect(result.current.takeTranscript()).toBeUndefined();
  });
});

describe('a send RESETS the transcript', () => {
  it('the next turn does not re-carry it', () => {
    // Counts are what decide whether a term crosses the propose threshold, so a
    // stale transcript riding a second turn would let one correction masquerade
    // as a repeated pattern.
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript('why is that yellow?'));
    expect(result.current.takeTranscript()).toBe('why is that yellow?');

    act(() => result.current.reset());

    expect(result.current.value).toBe('');
    expect(result.current.kind).toBe('text');
    expect(result.current.takeTranscript()).toBeUndefined();
  });
});

describe('an over-long transcript is DROPPED, and says so', () => {
  it('returns undefined and warns with lengths — never the words', () => {
    // Capture is telemetry. Letting an oversized transcript ride would fail the
    // BFF's zod bound and 400 the whole turn: the operator would lose a message
    // he spoke, to a learning feature.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const spoken = 'x'.repeat(MAX_TRANSCRIPT_CHARS + 1);
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript(spoken));

    expect(result.current.takeTranscript()).toBeUndefined();
    // ILB: the drop is announced, never silent — a missing transcript otherwise
    // looks identical to a broken capture.
    expect(warn).toHaveBeenCalledTimes(1);
    const said = String(warn.mock.calls[0][0]);
    expect(said).toContain('transcript dropped');
    expect(said).toContain(String(MAX_TRANSCRIPT_CHARS));
    // The operator's own words never belong in a console.
    expect(said).not.toContain(spoken);
  });

  it('POSITIVE CONTROL — a transcript AT the bound is kept and warns nothing', () => {
    // Without this, the pin above passes identically against a hook that
    // dropped every transcript it was ever given.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const spoken = 'x'.repeat(MAX_TRANSCRIPT_CHARS);
    const { result } = renderHook(() => useComposerText());

    act(() => result.current.appendTranscript(spoken));

    expect(result.current.takeTranscript()).toBe(spoken);
    expect(warn).not.toHaveBeenCalled();
  });
});

describe('the held seed (#94c) lands only in an EMPTY box', () => {
  it('seeds the box with the held text and reports it consumed', async () => {
    const onSeedConsumed = vi.fn();
    const { result } = renderHook(() =>
      useComposerText({ seedText: 'the reply I had composed', onSeedConsumed }),
    );

    await waitFor(() => expect(result.current.value).toBe('the reply I had composed'));
    expect(onSeedConsumed).toHaveBeenCalled();
  });

  it('does NOT clobber text the operator has already started rewriting', async () => {
    // Re-composing is a visible choice; overwriting it would be the same loss
    // pointed the other way.
    const { result, rerender } = renderHook(
      ({ seed }: { seed: string | null }) => useComposerText({ seedText: seed }),
      { initialProps: { seed: null as string | null } },
    );

    act(() => result.current.setValue('already rewriting this'));
    rerender({ seed: 'the old held text' });

    await waitFor(() => expect(result.current.value).toBe('already rewriting this'));
  });

  it('a null seed leaves the box empty', async () => {
    const { result } = renderHook(() => useComposerText({ seedText: null }));

    await waitFor(() => expect(result.current.value).toBe(''));
  });
});
