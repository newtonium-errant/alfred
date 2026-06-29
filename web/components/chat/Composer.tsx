import { KeyboardEvent, useState } from 'react';
import { Textarea } from '../ui/textarea';
import { Button } from '../ui/button';
import { VoiceCapture } from './VoiceCapture';
import type { ChatKind } from '../../lib/algernon/types';

// The message composer. Enter sends; Shift+Enter inserts a newline. An empty /
// whitespace-only message never sends. `disabled` covers the in-flight + booting
// states (the caller passes it) so a user can't double-send a turn. A confirmed
// voice transcript pre-fills the EDITABLE textarea (never auto-submits — the
// operator edits then presses Send); a transcript-seeded send is tagged
// kind:'voice' so the backend turn counter reflects it (decision H).
export function Composer({
  onSend,
  disabled = false,
}: {
  onSend: (text: string, kind?: ChatKind) => void;
  disabled?: boolean;
}) {
  const [value, setValue] = useState('');
  // True once a voice transcript seeded the input — tags the next send as 'voice'.
  const [voiceSeeded, setVoiceSeeded] = useState(false);

  function submit() {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text, voiceSeeded ? 'voice' : 'text');
    setValue('');
    setVoiceSeeded(false);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <VoiceCapture
        idPrefix="composer-voice"
        disabled={disabled}
        onTranscript={(t) => {
          setValue(t);
          setVoiceSeeded(true);
        }}
      />
      <form
        className="flex items-end gap-2"
        data-testid="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Textarea
          data-testid="composer-input"
          aria-label="Message"
          placeholder="Message…"
          rows={1}
          value={value}
          disabled={disabled}
          onChange={(e) => {
            setValue(e.target.value);
            // Cleared back to empty → the next send is a plain text turn again.
            if (e.target.value.trim().length === 0) setVoiceSeeded(false);
          }}
          onKeyDown={onKeyDown}
          className="max-h-40 min-h-[44px]"
        />
        <Button
          type="submit"
          data-testid="composer-send"
          disabled={disabled || value.trim().length === 0}
        >
          Send
        </Button>
      </form>
    </div>
  );
}
