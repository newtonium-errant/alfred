/**
 * #82 WARN-2 — both front-end error switches carry `image_too_large`.
 *
 * These switch on the CODE and fall through to a generic string for anything
 * unrecognised. So a box that correctly emits a new code, and a front end that
 * has not been taught it, produces exactly the bug #82 exists to fix — the
 * operator reads "Something went wrong. Please try again." for a failure that
 * retrying cannot clear. Both switches need the case; both are pinned.
 */
import { describe, expect, it } from 'vitest';

import { ApiError } from '../lib/algernon/http';
import { IMAGE_TOO_LARGE_FALLBACK, friendlyError } from '../lib/algernon/useChat';
import { askErrorMessage } from '../components/player/usePlayerAsk';

/** The copy the box classifier ships in `detail`. */
const BOX_DETAIL =
  'One of the images earlier in this conversation is too large for a chat ' +
  "with this many images in it. Retrying won't clear it — the image is part " +
  'of the history every message resends. Start a new chat and re-attach the ' +
  'images you still need, and send fewer at a time.';

const cases: Array<[string, (e: unknown) => string]> = [
  ['useChat.friendlyError', friendlyError],
  ['usePlayerAsk.askErrorMessage', askErrorMessage],
];

describe.each(cases)('%s', (_name, render) => {
  it('renders the box copy for image_too_large', () => {
    // The box owns the wording — one source of truth across three surfaces.
    const out = render(new ApiError(502, 'image_too_large', BOX_DETAIL));
    expect(out).toBe(BOX_DETAIL);
  });

  it('never tells the operator to retry a deterministic 400', () => {
    // The actual harm. Asserted on the rendered string, because that is what
    // the operator reads — not on the code that selected it.
    const out = render(new ApiError(502, 'image_too_large', BOX_DETAIL)).toLowerCase();
    expect(out).not.toContain('try again');
    expect(out).toContain('new chat');
  });

  it('falls back to local copy when the response carried no detail', () => {
    // A truncated or proxied error response can lose `detail`; the case must
    // still be actionable rather than dropping to the generic string.
    const out = render(new ApiError(502, 'image_too_large'));
    expect(out).toBe(IMAGE_TOO_LARGE_FALLBACK);
    expect(out.toLowerCase()).not.toContain('try again');
  });

  it('still says "try again" for a genuine transient failure', () => {
    // The other direction: engine_error IS retryable, and must keep saying so.
    // Without this, "strip every try-again" would pass the pins above while
    // making transient copy worse.
    expect(render(new ApiError(502, 'engine_error', 'boom')).toLowerCase())
      .toContain('try again');
  });

  it('falls through to the generic string for an unknown code', () => {
    const out = render(new ApiError(500, 'some_future_code', 'x'));
    expect(out.toLowerCase()).toContain('went wrong');
  });
});
