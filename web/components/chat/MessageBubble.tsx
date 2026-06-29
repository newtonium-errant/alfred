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
          'max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-base',
          isUser
            ? 'bg-honeydew-500 text-white'
            : 'border border-honeydew-200 bg-cream text-honeydew-900'
        )}
      >
        {text}
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
