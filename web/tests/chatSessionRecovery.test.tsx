import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ApiError } from '../lib/algernon/http';
import { friendlyError } from '../lib/algernon/useChat';

vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: () => <button type="button">mock-stt</button>,
  // `UnifiedComposer` imports this alongside the component; without it the
  // mocked module has no such export and the import is `undefined` at call time.
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));

import { UnifiedComposer } from '../components/chat/UnifiedComposer';

// #94 (c)+(d) — the operator's words survive, and the copy tells the truth.
//
// THE SEED PINS BELOW MOVED DOORS. They were written against the legacy
// `Composer`, which the composer-deletion lane removed; they now drive
// `UnifiedComposer`, which is what /chat renders. The property is unchanged and
// so are the assertions — what changed is that they are now made about the
// composer an operator's held text actually comes back into. The rule itself
// lives in `useComposerText` and is pinned there directly
// (`useComposerText.test.tsx`); these pins are the door's half of it, and the
// two are not redundant: a door that stopped passing `seedText` through would
// leave every hook pin green and lose the text in the field.

/** The composer asks for its ingest/batch targets on mount; keep it off the network. */
function stubTargets() {
  (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ targets: [] }),
  }));
}

function renderComposer(props: Partial<React.ComponentProps<typeof UnifiedComposer>> = {}) {
  stubTargets();
  return render(
    <UnifiedComposer
      onSend={vi.fn()}
      instance="salem"
      instanceLabel="Salem"
      submitIngest={vi.fn()}
      submitBatchRequest={vi.fn()}
      {...props}
    />,
  );
}

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
  it('seeds the composer with the held text', async () => {
    const onSeedConsumed = vi.fn();
    renderComposer({ seedText: 'the reply I had composed', onSeedConsumed });

    const box = screen.getByTestId('unified-input') as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe('the reply I had composed'));
    expect(onSeedConsumed).toHaveBeenCalled();
  });

  it('does NOT clobber text the operator has already started rewriting', async () => {
    // Re-composing is a visible choice; overwriting it would be the same loss
    // pointed the other way.
    const { rerender } = renderComposer({ seedText: null });
    const box = screen.getByTestId('unified-input') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'already rewriting this' } });

    rerender(
      <UnifiedComposer
        onSend={vi.fn()}
        instance="salem"
        instanceLabel="Salem"
        submitIngest={vi.fn()}
        submitBatchRequest={vi.fn()}
        seedText="the old held text"
      />,
    );

    await waitFor(() => expect(box.value).toBe('already rewriting this'));
  });

  it('a null seed leaves the composer empty', async () => {
    renderComposer({ seedText: null });
    const box = screen.getByTestId('unified-input') as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe(''));
  });
});
