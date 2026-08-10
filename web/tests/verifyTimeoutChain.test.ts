import { beforeEach, describe, expect, it, vi } from 'vitest';

// #62 gate WARN-2, follow-up — the pin that watches the timeout ARRIVE.
//
// The shallow version of this test (in actConfirm.test.ts) injects a fake
// `list` and asserts verifyActLanded passed it the right arguments. That proves
// one hop. It does NOT prove the value survives the rest of the chain, and the
// reviewer's point is exactly that: every actConfirm test injects a fake list,
// which is WHY the 70s default was invisible until the gate. A pin blind to the
// real chain is how the defect hid in the first place, so repeating that shape
// to guard the fix would be circular.
//
// This drives the REAL chain — verifyActLanded -> the real feedApi.list -> the
// fetch layer — with only `getJson` stubbed, and asserts the budget lands where
// the abort timer is actually set.
//
// `http` is partially mocked: ApiError must stay REAL, because verifyActLanded
// and its callers discriminate on `instanceof ApiError`, and a stubbed class
// would quietly make every error look like a non-ApiError.

const { mockGetJson } = vi.hoisted(() => ({ mockGetJson: vi.fn() }));
vi.mock('../lib/algernon/http', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/algernon/http')>()),
  getJson: mockGetJson,
}));

import { VERIFY_TIMEOUT_MS, verifyActLanded } from '../lib/algernon/actConfirm';
import { feedApi } from '../lib/algernon/feed';

beforeEach(() => {
  mockGetJson.mockReset();
  mockGetJson.mockResolvedValue({ items: [], count: 0 });
});

describe('the verify budget reaches the fetch layer, not just the deps object', () => {
  it('verifyActLanded -> real feedApi.list -> getJson carries timeoutMs', async () => {
    // No injected `list`. The default deps bind the REAL feedApi, so this
    // exercises every hop the browser exercises.
    await verifyActLanded('itm-1', () => true);

    expect(mockGetJson).toHaveBeenCalled();
    const [, opts] = mockGetJson.mock.calls[0];
    expect(opts).toEqual({ timeoutMs: VERIFY_TIMEOUT_MS });
  });

  it('carries it on the retry hop too', async () => {
    mockGetJson
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({ items: [], count: 0 });

    await verifyActLanded('itm-1', () => true);

    expect(mockGetJson).toHaveBeenCalledTimes(2);
    for (const [, opts] of mockGetJson.mock.calls) {
      expect(opts).toEqual({ timeoutMs: VERIFY_TIMEOUT_MS });
    }
  });

  it('feedApi.list FORWARDS its options — the hop the shallow pin cannot see', () => {
    // Stated separately because this is the link that would break silently: a
    // `list` that accepted `opts` and dropped it would leave the shallow pin
    // green and restore the 70s default with nothing to show for it.
    void feedApi.list({ state: 'open' }, { timeoutMs: 1234 });
    expect(mockGetJson).toHaveBeenCalledWith('/api/feed/list?state=open', { timeoutMs: 1234 });
  });

  it('an ordinary caller still gets the browser default', () => {
    // The budget is scoped to the verify. Narrowing it for every feed read
    // would turn a slow-but-working connection into a broken one.
    void feedApi.list({});
    expect(mockGetJson).toHaveBeenCalledWith('/api/feed/list', {});
  });
});
