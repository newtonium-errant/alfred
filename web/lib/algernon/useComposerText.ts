import { useCallback, useEffect, useRef, useState } from 'react';
import { MAX_TRANSCRIPT_CHARS } from './schemas';
import type { ChatKind } from './types';

/**
 * The text half of a composer: the typed value, the held-back unsent text
 * (#94c), and the raw-STT transcript a voice-seeded send carries (#54).
 *
 * Extracted from the legacy `Composer.tsx` when the unified composer (#97)
 * became a second composer that must behave IDENTICALLY on all three. That
 * legacy component has since been deleted, so this is no longer shared BETWEEN
 * composers — it is simply where the rules live, and where they are pinned
 * (`tests/useComposerText.test.tsx`). These are not cosmetic rules — each
 * encodes an incident:
 *
 *   * APPEND, never replace. The original `setValue(t)` destroyed whatever the
 *     operator had already typed. That is the clobber that was reported.
 *   * The transcript mirrors the append with the SAME join rule, so a message
 *     dictated in several passes diffs as one piece against what was sent.
 *   * Clearing the box clears the transcript in LOCKSTEP. Without it, dictating
 *     again appends onto a discarded transcript and the operator ships one
 *     containing words that are nowhere in his message — inventing a deletion
 *     the STT never made.
 *   * The seed is placed only into an EMPTY box: if the operator has started
 *     rewriting, their current words win.
 *
 * A second hand-written copy of those four rules is a second chance to get one
 * of them backwards, silently, in the composer nobody re-tested. That was the
 * argument for extracting it while two composers existed; with one left it is
 * the argument for not folding it back in.
 */
export interface ComposerText {
  /** The textarea's value. */
  value: string;
  /** The textarea's onChange — clears the voice state when emptied. */
  setValue: (next: string) => void;
  /** True once a transcript seeded the box: the next send is tagged 'voice'. */
  voiceSeeded: boolean;
  /** The send `kind` for the current box state. */
  kind: ChatKind;
  /** A transcript arrived — append it to the box AND to the raw record. */
  appendTranscript: (text: string) => void;
  /**
   * The transcript to send alongside the message, or undefined.
   *
   * An OVER-LONG transcript is DROPPED, not sent: capture is telemetry, and
   * letting it fail the zod bound would 400 the whole turn — the operator would
   * lose a message he spoke, to a learning feature. The send still goes through
   * tagged 'voice'; only the learning is skipped.
   */
  takeTranscript: () => string | undefined;
  /** Clear the box + the voice state (after a send). */
  reset: () => void;
}

export interface UseComposerTextOptions {
  /**
   * Text handed back after a send failed against a dead session (#94c). The
   * operator wrote it; losing it to a deploy restart is what turned a background
   * annoyance into "my reply is gone".
   */
  seedText?: string | null;
  /** Called once the seed has been placed, so it is offered exactly once. */
  onSeedConsumed?: () => void;
}

export function useComposerText(options: UseComposerTextOptions = {}): ComposerText {
  const { seedText = null, onSeedConsumed } = options;
  const [value, setValueState] = useState('');
  const [voiceSeeded, setVoiceSeeded] = useState(false);
  // The RAW STT text this message was seeded from — ONLY the transcribed
  // segments, joined by the same single-space rule the textarea append uses, and
  // never the operator's own typed words.
  //
  // WHY ONLY THE SPOKEN PART. The backend diffs transcript-vs-sent and reads the
  // `replace` spans as "the STT heard X, I meant Y". Text the operator TYPED was
  // never heard by the STT, so it cannot have been mis-heard; folding it in would
  // make his own edits to his own typing eligible to be learned as vocabulary.
  // Keeping it out means a typed word only ever appears as a diff INSERT, which
  // the extractor ignores by design.
  //
  // A ref, not state: it is never rendered, and a ref is always current at the
  // moment a submit reads it (no stale-closure window between an async
  // transcription landing and the next render).
  const transcriptRef = useRef('');

  // Restore the held text — but never over live typing. Re-composing is a choice
  // the operator has visibly made, and clobbering it would be the same loss in
  // the other direction.
  useEffect(() => {
    if (!seedText) return;
    setValueState((prev) => (prev.trim() ? prev : seedText));
    onSeedConsumed?.();
  }, [seedText, onSeedConsumed]);

  const setValue = useCallback((next: string) => {
    setValueState(next);
    // Cleared back to empty → the next send is a plain text turn again. The
    // transcript clears in LOCKSTEP: nothing spoken survives in the box, so
    // keeping it would diff the next typed message against words the operator
    // already threw away.
    if (next.trim().length === 0) {
      setVoiceSeeded(false);
      transcriptRef.current = '';
    }
  }, []);

  const appendTranscript = useCallback((text: string) => {
    // APPEND, never replace. A single space joins the two; no space when the
    // input was empty, so a voice-only message carries no leading whitespace.
    setValueState((prev) => {
      const base = prev.trimEnd();
      return base ? `${base} ${text}` : text;
    });
    // Mirror the append into the raw-transcript record with the SAME join rule.
    transcriptRef.current = transcriptRef.current
      ? `${transcriptRef.current} ${text}`
      : text;
    setVoiceSeeded(true);
  }, []);

  const takeTranscript = useCallback((): string | undefined => {
    const raw = voiceSeeded ? transcriptRef.current.trim() : '';
    if (!raw) return undefined;
    if (raw.length > MAX_TRANSCRIPT_CHARS) {
      // ILB: a silently-absent transcript is indistinguishable from a broken
      // capture. Lengths only — the transcript itself is the operator's words
      // and never belongs in a console.
      console.warn(
        `[composer] transcript dropped: ${raw.length} chars exceeds ` +
          `${MAX_TRANSCRIPT_CHARS}; the turn is sent without it`,
      );
      return undefined;
    }
    return raw;
  }, [voiceSeeded]);

  const reset = useCallback(() => {
    setValueState('');
    setVoiceSeeded(false);
    transcriptRef.current = '';
  }, []);

  return {
    value,
    setValue,
    voiceSeeded,
    kind: voiceSeeded ? 'voice' : 'text',
    appendTranscript,
    takeTranscript,
    reset,
  };
}
