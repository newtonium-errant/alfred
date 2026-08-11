import { describe, expect, it } from 'vitest';
import { ApiError } from '../lib/algernon/http';
import { isRecoverable } from '../lib/algernon/useChat';

// #94(b) — the client must honour the SERVER's retryability verdict.
//
// THE INCIDENT. Two Anthropic 500s killed a session's turns on 2026-08-11. The
// backend shipped `retryable` on the wire (classification_payload has carried
// it since #82), and the client DROPPED it — `isRecoverable` re-decided from a
// local code list that did not contain the code. So the pending turn was
// cleared, no retry button appeared, and the operator typed "Try now" by hand.
//
// The fix defers to the server when it has an opinion. That is the same
// doctrine as the health-status predicate: consume the decision, never
// re-localize the comparison. A future retryable condition becomes a
// classifier change with no client edit.
//
// `isRecoverable` is EXPORTED for these pins (same reason `friendlyError` is):
// a mirrored decision table in the test file would pass against a build where
// the production predicate was broken — which is this very bug's shape, a
// verdict computed in one place and ignored in another.

describe('ApiError carries the server verdict', () => {
  it('keeps retryable when the response supplied it', () => {
    const err = new ApiError(502, 'engine_unavailable', 'briefly unavailable', true);
    expect(err.retryable).toBe(true);
  });

  it('keeps an explicit FALSE distinct from absent', () => {
    // The distinction the whole fix rests on: `false` is a verdict, `undefined`
    // is an abstention. Collapsing them would make every unclassified failure
    // permanent — the incident, generalised.
    const deterministic = new ApiError(502, 'image_too_large', 'too big', false);
    const noOpinion = new ApiError(502, 'engine_error', 'boom');
    expect(deterministic.retryable).toBe(false);
    expect(noOpinion.retryable).toBeUndefined();
  });

  it('defaults to undefined for the pre-#94 three-arg call', () => {
    // Every existing call site still compiles and still means "no opinion".
    const err = new ApiError(500, 'network_error');
    expect(err.retryable).toBeUndefined();
    expect(err.code).toBe('network_error');
  });
});

describe('the recoverable decision', () => {
  it('treats a server-flagged engine_unavailable as RECOVERABLE', () => {
    // The incident, inverted: this is the case that used to clear the turn.
    const err = new ApiError(502, 'engine_unavailable', 'upstream 500', true);
    expect(isRecoverable(err)).toBe(true);
  });

  it('treats a server-flagged image_too_large as DEFINITIVE', () => {
    // #82's guarantee must survive #94: a deterministic 400 keeps its dead end,
    // because a retry button there invites a guaranteed failure.
    const err = new ApiError(502, 'image_too_large', 'dimensions', false);
    expect(isRecoverable(err)).toBe(false);
  });

  it('falls back to the local list when the server abstained', () => {
    // Network and timeout failures never reach the server's classifier at all,
    // so the local set is the only thing that can answer for them.
    expect(isRecoverable(new ApiError(0, 'network_error'))).toBe(true);
    expect(isRecoverable(new ApiError(504, 'gateway_timeout'))).toBe(true);
    expect(isRecoverable(new ApiError(502, 'engine_error'))).toBe(false);
  });

  it('lets the server OVERRIDE the local list in both directions', () => {
    // The property that makes this one source of truth rather than two.
    expect(isRecoverable(new ApiError(0, 'network_error', undefined, false)))
      .toBe(false);
    expect(isRecoverable(new ApiError(502, 'some_new_code', undefined, true)))
      .toBe(true);
  });

  it('is false for a non-ApiError', () => {
    expect(isRecoverable(new Error('boom'))).toBe(false);
    expect(isRecoverable(null)).toBe(false);
  });
});
