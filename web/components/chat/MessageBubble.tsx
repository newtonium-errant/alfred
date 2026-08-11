import { FencedText } from '../markdown/FencedText';
import { cn, formatMessageTime } from '../../lib/utils';
import type { ChatRole } from '../../lib/algernon/types';

// One chat message. Text is rendered as escaped React children (never
// dangerouslySetInnerHTML) — the untrusted-data discipline: model + vault text
// is free text. `whitespace-pre-wrap` preserves the assistant's line breaks.
// `ts` is the turn's ISO-8601 stamp; a muted time renders only when it formats to
// a non-empty string (empty/invalid → nothing, never "Invalid Date").
export function MessageBubble({
  role,
  text,
  ts,
}: {
  role: ChatRole;
  text: string;
  ts: string;
}) {
  const isUser = role === 'user';
  const time = formatMessageTime(ts);
  return (
    <div
      className={cn('flex', isUser ? 'justify-end' : 'justify-start')}
      data-testid={`msg-${role}`}
    >
      <div
        className={cn(
          'max-w-[85%] break-words rounded-2xl px-4 py-2.5 text-base',
          isUser
            ? 'bg-honeydew-500 text-white'
            : 'border border-honeydew-200 bg-cream text-honeydew-900'
        )}
      >
        {/* #85: a ```csv block in a reply becomes downloadable. The pre-wrap
            moved from this container onto the text segments so a fence can
            render its own panel; unfenced messages emit the same single
            pre-wrap block as before. */}
        <FencedText
          text={text}
          nameHint={`message-${role}`}
          className="whitespace-pre-wrap break-words"
        />
        {time && (
          <time
            dateTime={ts}
            data-testid={`msg-time-${role}`}
            className="mt-1 block text-xs opacity-60"
          >
            {time}
          </time>
        )}
      </div>
    </div>
  );
}
