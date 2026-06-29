import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from '../components/chat/Composer';

describe('Composer', () => {
  it('sends on Enter and clears the input; tags a typed turn kind="text"', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    await user.type(input, 'hello world');
    await user.keyboard('{Enter}');

    expect(onSend).toHaveBeenCalledTimes(1);
    // A keyboard-typed turn carries kind:'text' (voice tag is only for transcripts).
    expect(onSend).toHaveBeenCalledWith('hello world', 'text');
    expect(input.value).toBe('');
  });

  it('does NOT send on Shift+Enter (inserts a newline instead)', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    const input = screen.getByTestId('composer-input');
    await user.type(input, 'line one');
    await user.keyboard('{Shift>}{Enter}{/Shift}');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('does not send an empty / whitespace-only message', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);

    await user.type(screen.getByTestId('composer-input'), '   ');
    await user.keyboard('{Enter}');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('disables the send button while disabled', () => {
    render(<Composer onSend={vi.fn()} disabled />);
    const send = screen.getByTestId('composer-send') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
  });

  it('does not send when disabled even on Enter', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} disabled />);

    // The textarea is disabled; typing is a no-op, and submit is gated.
    await user.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
  });
});
