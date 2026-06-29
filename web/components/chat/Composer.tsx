import { KeyboardEvent, useState } from 'react';
import { Textarea } from '../ui/textarea';
import { Button } from '../ui/button';

// The message composer. Enter sends; Shift+Enter inserts a newline. An empty /
// whitespace-only message never sends. `disabled` covers the in-flight + booting
// states (the caller passes it) so a user can't double-send a turn.
export function Composer({
  onSend,
  disabled = false,
}: {
  onSend: (text: string) => void;
  disabled?: boolean;
}) {
  const [value, setValue] = useState('');

  function submit() {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue('');
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
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
        onChange={(e) => setValue(e.target.value)}
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
  );
}
