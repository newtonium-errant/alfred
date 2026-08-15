import { FencedText } from '../markdown/FencedText';
import { cn, formatMessageTime } from '../../lib/utils';
import type { ChatRole } from '../../lib/algernon/types';
import {
  COMMS_QUOTED_CLASS,
  COMMS_TURN_ASSISTANT_CLASS,
  COMMS_TURN_OPERATOR_CLASS,
} from '../../lib/algernon/commsSurface';

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
      {/* THE HULL, as distinct from the voice. The warm utilities stay as the
          unmarked default and the comms class is what the register reaches —
          the `ui-panel` lesson, applied without the marker because the
          transcript does not straddle a warm route (ChatThread renders only on
          the comms surface). The two hulls differ on the register's depth axis
          so operator and computer stay tellable apart by more than alignment;
          the FACE distinction below is the other half and is untouched. */}
      <div
        className={cn(
          'max-w-[85%] break-words rounded-2xl px-4 py-2.5 text-base',
          isUser
            ? cn('bg-honeydew-500 text-white', COMMS_TURN_OPERATOR_CLASS)
            : cn(
                'border border-honeydew-200 bg-cream text-honeydew-900',
                COMMS_TURN_ASSISTANT_CLASS,
              )
        )}
      >
        {/* #85: a ```csv block in a reply becomes downloadable. The pre-wrap
            moved from this container onto the text segments so a fence can
            render its own panel; unfenced messages emit the same single
            pre-wrap block as before.

            THE QUOTATION (comms register): the assistant's turns — and ONLY
            those — carry `.comms-quoted`, which renders them monospace-phosphor:
            the ship's computer speaking. The operator's own turns stay in the
            proportional face, because the operator is not the computer. This is
            a quotation of the CRT register, the same move TimelineView makes on
            sensor-log; the surface stays comms and exactly one region borrows
            another register's voice. Pinned, boundary and all, by
            commsQuotation.test.tsx. */}
        <FencedText
          text={text}
          nameHint={`message-${role}`}
          className={cn(
            'whitespace-pre-wrap break-words',
            !isUser && COMMS_QUOTED_CLASS,
          )}
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
