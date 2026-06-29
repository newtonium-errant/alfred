import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ChatThread } from '../components/chat/ChatThread';
import type { ChatMessage } from '../lib/algernon/types';

// scrollIntoView is stubbed globally in vitest.setup.ts (jsdom lacks it).

const msgs: ChatMessage[] = [
  { id: '1', role: 'user', text: 'what is on today?', ts: '' },
  { id: '2', role: 'assistant', text: 'A couple of rhythms are up for grabs.', ts: '' },
];

describe('ChatThread', () => {
  it('shows the warm empty state when there are no messages and not sending', () => {
    render(<ChatThread messages={[]} sending={false} />);
    expect(screen.getByTestId('chat-empty')).not.toBeNull();
    expect(screen.queryByTestId('chat-thread')).toBeNull();
  });

  it('renders user + assistant bubbles', () => {
    render(<ChatThread messages={msgs} sending={false} />);
    expect(screen.getByTestId('chat-thread')).not.toBeNull();
    expect(screen.getByText('what is on today?')).not.toBeNull();
    expect(screen.getByText('A couple of rhythms are up for grabs.')).not.toBeNull();
    expect(screen.getByTestId('msg-user')).not.toBeNull();
    expect(screen.getByTestId('msg-assistant')).not.toBeNull();
  });

  it('shows the typing indicator while sending', () => {
    render(<ChatThread messages={msgs} sending />);
    expect(screen.getByTestId('typing-indicator')).not.toBeNull();
  });

  it('shows the typing indicator even with no messages yet (first turn in flight)', () => {
    render(<ChatThread messages={[]} sending />);
    expect(screen.getByTestId('typing-indicator')).not.toBeNull();
    expect(screen.queryByTestId('chat-empty')).toBeNull();
  });
});
