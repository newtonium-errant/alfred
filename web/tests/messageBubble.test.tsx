import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageBubble } from '../components/chat/MessageBubble';

// MessageBubble renders a muted <time> ONLY for a non-empty, parseable stamp.
// Pre-stamp / optimistic bubbles (ts === '') and invalid stamps render no time
// (never "Invalid Date").

describe('MessageBubble', () => {
  it('renders no <time> when ts is empty', () => {
    render(<MessageBubble role="user" text="hi" ts="" />);
    expect(screen.getByText('hi')).not.toBeNull();
    expect(screen.queryByTestId('msg-time-user')).toBeNull();
  });

  it('renders a <time dateTime> with the full ISO when ts is valid', () => {
    render(<MessageBubble role="assistant" text="answer" ts="2026-06-29T14:05:00Z" />);
    const t = screen.getByTestId('msg-time-assistant') as HTMLTimeElement;
    expect(t).not.toBeNull();
    expect(t.getAttribute('dateTime')).toBe('2026-06-29T14:05:00Z');
    expect((t.textContent || '').length).toBeGreaterThan(0);
    expect(t.textContent).not.toContain('Invalid');
  });

  it('renders no <time> for an unparseable stamp', () => {
    render(<MessageBubble role="user" text="hi" ts="not-a-date" />);
    expect(screen.queryByTestId('msg-time-user')).toBeNull();
  });
});
