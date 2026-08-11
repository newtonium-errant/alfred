import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ApiError } from '../lib/algernon/http';
import { friendlyError } from '../lib/algernon/useChat';

vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: () => <button type="button">mock-stt</button>,
}));

import { Composer } from '../components/chat/Composer';

// #94 (c)+(d) — the operator's words survive, and the copy tells the truth.

describe('honest copy (#94d)', () => {
  it('turn_in_flight says what is happening, not "something went wrong"', () => {
    // Nothing DID go wrong — the previous message is still being answered.
    // The generic copy was both inaccurate and alarming.
    const msg = friendlyError(new ApiError(409, 'turn_in_flight'));
    expect(msg).toContain('still being answered');
    expect(msg).not.toContain('Something went wrong');
    // And it must not invite a retry that would be refused identically.
    expect(msg.toLowerCase()).not.toContain('try again');
  });

  it('engine_unavailable prefers the server detail', () => {
    const msg = friendlyError(
      new ApiError(502, 'engine_unavailable', 'upstream 503, send it again'),
    );
    expect(msg).toBe('upstream 503, send it again');
  });

  it('engine_unavailable has an honest floor when detail is lost', () => {
    const msg = friendlyError(new ApiError(502, 'engine_unavailable'));
    expect(msg.toLowerCase()).toContain('send it again');
    expect(msg.toLowerCase()).not.toContain('new chat');
  });

  it('leaves the deterministic image copy alone', () => {
    // #82's guarantee must survive: never "try again" for a permanent failure.
    const msg = friendlyError(new ApiError(502, 'image_too_large', 'too big'));
    expect(msg).toBe('too big');
  });
});

describe('composed text survives a dead session (#94c)', () => {
  it('seeds the composer with the held text', () => {
    const onSeedConsumed = vi.fn();
    render(
      <Composer onSend={vi.fn()} seedText="the reply I had composed" onSeedConsumed={onSeedConsumed} />,
    );
    const box = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    expect(box.value).toBe('the reply I had composed');
    expect(onSeedConsumed).toHaveBeenCalled();
  });

  it('does NOT clobber text the operator has already started rewriting', () => {
    // Re-composing is a visible choice; overwriting it would be the same loss
    // pointed the other way.
    const { rerender } = render(<Composer onSend={vi.fn()} seedText={null} />);
    const box = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'already rewriting this' } });
    rerender(<Composer onSend={vi.fn()} seedText="the old held text" />);
    expect(box.value).toBe('already rewriting this');
  });

  it('a null seed leaves the composer empty', async () => {
    render(<Composer onSend={vi.fn()} seedText={null} />);
    const box = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe(''));
  });
});
