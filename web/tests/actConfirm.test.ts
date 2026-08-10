import { describe, expect, it, vi } from 'vitest';
import {
  ACT_LANDED_MESSAGE,
  ACT_UNCONFIRMED_MESSAGE,
  isInconclusive,
  supersede,
  VERIFY_TIMEOUT_MS,
  verifyActLanded,
} from '../lib/algernon/actConfirm';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

// #62 — the shared act-confirmation mechanism.
//
// The incident: an act COMMITTED server-side in 30ms, the phone never saw the
// response, and the client rendered "that didn't confirm in time" with the tick
// reverted. Twelve hours later the resumed PWA still showed it pending under
// that red line. The system was right and told the operator it had failed.
//
// The distinction every test here turns on: a network failure is not an act
// failure. It is a failure to LEARN THE OUTCOME.

function item(over: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'itm-1', kind: 'routine', instance: 'Salem', title: 'RRTS Payroll',
    mode: 'ring', attention: 'normal', evidence: {}, actions: [],
    state: 'pending', created_at: '', acted_at: null, expires_at: null,
    source_ref: {}, ...over,
  };
}

// ---------------------------------------------------------------------------
// isInconclusive — the gate that decides whether a second opinion is warranted
// ---------------------------------------------------------------------------

describe('isInconclusive separates "no answer" from "answered no"', () => {
  it.each([
    ['504', new ApiError(504, 'gateway_timeout')],
    ['timeout code', new ApiError(0, 'timeout')],
    ['network_error', new ApiError(0, 'network_error')],
    ['gateway_timeout code', new ApiError(0, 'gateway_timeout')],
  ])('%s is inconclusive — the act may have committed', (_label, e) => {
    expect(isInconclusive(e)).toBe(true);
  });

  it.each([
    ['409 stale', new ApiError(409, 'stale_item')],
    ['400 invalid', new ApiError(400, 'invalid_action')],
    ['422 refusal', new ApiError(422, 'error')],
    ['500', new ApiError(500, 'server_error')],
    ['503 upstream', new ApiError(503, 'transport_unreachable')],
    ['401', new ApiError(401, 'invalid_session')],
  ])('%s is a real ANSWER — no second opinion', (_label, e) => {
    expect(isInconclusive(e)).toBe(false);
  });

  it('a non-ApiError is not inconclusive', () => {
    // A thrown TypeError is a bug in our code, not a network condition. Probing
    // the server about it would be nonsense.
    expect(isInconclusive(new Error('boom'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// verifyActLanded — bounded, and 'unknown' is its own answer
// ---------------------------------------------------------------------------

describe('verifyActLanded asks the server what actually happened', () => {
  const landedIfDone = (i: FeedItem) => i.state === 'acted';

  it('reports LANDED when the server shows the act applied', async () => {
    const list = vi.fn().mockResolvedValue({ items: [item({ state: 'acted' })], count: 1 });
    expect(await verifyActLanded('itm-1', landedIfDone, { list })).toBe('landed');
    expect(list).toHaveBeenCalledTimes(1);
  });

  it('reports NOT_LANDED when the server shows it did not', async () => {
    const list = vi.fn().mockResolvedValue({ items: [item({ state: 'pending' })], count: 1 });
    expect(await verifyActLanded('itm-1', landedIfDone, { list })).toBe('not_landed');
  });

  it('retries ONCE and succeeds on the second attempt', async () => {
    const list = vi.fn()
      .mockRejectedValueOnce(new ApiError(0, 'network_error'))
      .mockResolvedValueOnce({ items: [item({ state: 'acted' })], count: 1 });
    expect(await verifyActLanded('itm-1', landedIfDone, { list })).toBe('landed');
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('is BOUNDED — never an infinite probe loop', async () => {
    // The likeliest caller is a phone that just lost the network. An unbounded
    // probe would hammer a dying connection at the worst possible moment.
    const list = vi.fn().mockRejectedValue(new ApiError(0, 'network_error'));
    expect(await verifyActLanded('itm-1', landedIfDone, { list })).toBe('unknown');
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('returns UNKNOWN — not not_landed — when the item is absent', async () => {
    // Load-bearing. Acted items normally stay listed as `acted`, so absence
    // means something we do not model. Reading it as failure would be the
    // original overreach in a new place.
    const list = vi.fn().mockResolvedValue({ items: [], count: 0 });
    expect(await verifyActLanded('itm-1', landedIfDone, { list })).toBe('unknown');
  });

  it('never throws its own error at the caller', async () => {
    // The operator asked to complete an item, not to hear about our diagnostic
    // call. A verify that surfaces a NEW error is worse than no verify.
    const list = vi.fn().mockRejectedValue(new TypeError('exploded'));
    await expect(verifyActLanded('itm-1', landedIfDone, { list })).resolves.toBe('unknown');
  });
});

// ---------------------------------------------------------------------------
// supersede — an override must not outlive the question it answered
// ---------------------------------------------------------------------------

describe('supersede retires overrides that server truth has answered', () => {
  it('drops a settled override so fresh state can show through', () => {
    const overrides = { 'itm-1': { busy: false, done: false, error: 'stale line' } };
    const next = supersede(overrides, [item({ state: 'acted' })], () => true);
    expect(next['itm-1']).toBeUndefined();
  });

  it('KEEPS a busy override — the act is still in flight', () => {
    // A render landing mid-flight must not clear the spinner; that answer has
    // not arrived yet.
    const overrides = { 'itm-1': { busy: true, done: false } };
    const next = supersede(overrides, [item({ state: 'acted' })], () => true);
    expect(next['itm-1']).toBeDefined();
  });

  it('keeps an override for an item the render did not include', () => {
    const overrides = { 'itm-1': { busy: false, done: true } };
    const next = supersede(overrides, [item({ id: 'other' })], () => true);
    expect(next['itm-1']).toBeDefined();
  });

  it('respects the per-hook resolved predicate', () => {
    const overrides = { 'itm-1': { busy: false, done: true } };
    const kept = supersede(overrides, [item({ state: 'pending' })], (i) => i.state === 'acted');
    expect(kept['itm-1']).toBeDefined();
  });

  it('returns the SAME object when nothing changed', () => {
    // Identity stability matters: this runs on every feed render, and a fresh
    // object each time would re-trigger every downstream memo.
    const overrides = { 'itm-1': { busy: true, done: false } };
    expect(supersede(overrides, [item()], () => true)).toBe(overrides);
  });
});

// ---------------------------------------------------------------------------
// copy
// ---------------------------------------------------------------------------

describe('the copy tells the truth about what happened', () => {
  it('the landed line explains the pause without claiming failure', () => {
    expect(ACT_LANDED_MESSAGE).toContain('landed');
    expect(ACT_LANDED_MESSAGE.toLowerCase()).not.toMatch(/fail|didn't|could not/);
  });

  it('the unconfirmed line still promises reconciliation — now truthfully', () => {
    // Kept from before #62 deliberately. It promised something the client never
    // did; with verify + supersession + resume refetch it describes something
    // that actually happens, so it stays rather than being softened.
    expect(ACT_UNCONFIRMED_MESSAGE).toContain('next sync will reconcile');
  });
});

// ---------------------------------------------------------------------------
// #62 gate, WARN-2 — the verify's own request budget must REACH the call
// ---------------------------------------------------------------------------
//
// The bound was promised "in two directions" and only one was delivered. Two
// attempts bounded the COUNT; each attempt still inherited the 70s browser
// default, so a verify could run ~140s on top of the act's own 70s — a ~3.5
// minute spinner on a phone that had just lost network, which is precisely the
// case that constructs an inconclusive failure.
//
// Pinned at the CALL SITE, not on the constant. Every other test here injects a
// fake `list`, so a constant that is declared and never threaded would leave
// them all green — the same trap as #57's client_max_size, where the value was
// correct and never reached `web.Application`.

describe('the verify carries its own short timeout to the wire', () => {
  it('THREADS timeoutMs into the list call', async () => {
    const list = vi.fn().mockResolvedValue({ items: [], count: 0 });
    await verifyActLanded('itm-1', () => true, { list });

    expect(list).toHaveBeenCalledWith({}, { timeoutMs: VERIFY_TIMEOUT_MS });
  });

  it('threads it on the RETRY too — the second attempt is the slow one', async () => {
    // The retry runs when the first attempt already failed, i.e. when the
    // network is worst. An unbudgeted retry is the half that actually hurts.
    const list = vi.fn()
      .mockRejectedValueOnce(new ApiError(0, 'network_error'))
      .mockResolvedValueOnce({ items: [], count: 0 });
    await verifyActLanded('itm-1', () => true, { list });

    expect(list).toHaveBeenCalledTimes(2);
    for (const call of list.mock.calls) {
      expect(call[1]).toEqual({ timeoutMs: VERIFY_TIMEOUT_MS });
    }
  });

  it('the budget is far below the 70s browser default', () => {
    // The value itself, so a later edit that "simplifies" it back toward the
    // default has to argue with a number. Longer than any healthy round trip,
    // shorter than an operator's patience with a spinner.
    expect(VERIFY_TIMEOUT_MS).toBeLessThanOrEqual(10_000);
    expect(VERIFY_TIMEOUT_MS).toBeGreaterThanOrEqual(3_000);
  });

  it('worst-case verify time stays a fraction of the act budget', () => {
    // Two attempts at the budget must not approach the 70s the act itself may
    // already have burned — that sum is what the operator experiences.
    expect(VERIFY_TIMEOUT_MS * 2).toBeLessThan(70_000 / 2);
  });
});
