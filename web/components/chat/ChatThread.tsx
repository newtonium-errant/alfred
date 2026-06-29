import { useEffect, useRef } from 'react';
import { EmptyState } from '../EmptyState';
import { MessageBubble } from './MessageBubble';
import { TypingIndicator } from './TypingIndicator';
import type { ChatMessage } from '../../lib/algernon/types';

// The message list. Empty + not-sending → the warm EmptyState (never a blank
// pane). Auto-scrolls to the latest message / typing indicator.
export function ChatThread({
  messages,
  sending,
}: {
  messages: ChatMessage[];
  sending: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.length, sending]);

  if (messages.length === 0 && !sending) {
    return (
      <EmptyState
        icon="💬"
        title="Say hello"
        message="Start the conversation — ask about a project, a person, or what's on today."
        testId="chat-empty"
      />
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="chat-thread">
      {messages.map((m) => (
        <MessageBubble key={m.id} role={m.role} text={m.text} ts={m.ts} />
      ))}
      {sending && <TypingIndicator />}
      <div ref={endRef} />
    </div>
  );
}
