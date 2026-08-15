import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { isNeedsYouItem } from '../lib/algernon/feedNeedsYou';
import { isPushEligible, readPushPolicy } from '../lib/algernon/pushPolicy';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// THE PENDING-KIND RING, CLOSED BY PIN RATHER THAN BY DERIVATION.
//
// The PY-A leg closure established that a `pending` item reaches the phone —
// but established it by ARGUMENT: `KIND_DEFAULTS` gives it needs_you,
// `fetchNeedsYouItems` filters by attention with no kind allowlist, and the
// default policy admits every needs-you item, therefore it rings. Every step
// of that is true and none of it is a test. A derivation guards nothing: the
// day one of those three lines moves, the argument stops holding and nothing
// goes red.
//
// This is the same shape as `reminderReturnedRing`, and for the same reason —
// including the part that makes it worth writing: the fixture's mode and
// attention are READ FROM THE PYTHON SOURCE rather than typed here. A
// hand-written `attention: 'needs_you'` fixture would keep passing while the
// registration it claims to guard was downgraded to fyi and the operator's
// phone went quiet. Downgrade `KIND_DEFAULTS["pending"]` and this goes red,
// which is the whole point of reading across the language boundary.

const MODEL_PY = join(process.cwd(), '..', 'src', 'alfred', 'feed', 'model.py');
const KIND = 'pending';

/** Top-level `NAME = "value"` constants, so a tuple of symbols can be resolved. */
function pythonStringConstants(source: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /^([A-Z][A-Z0-9_]*)\s*=\s*["']([^"']+)["']\s*$/gm;
  for (const m of source.matchAll(re)) out[m[1]] = m[2];
  return out;
}

/**
 * The `(mode, attention)` Python actually assigns to `pending`.
 *
 * Throws rather than defaulting if the entry is missing. `KIND_DEFAULTS.get`
 * falls back to `(fyi, fyi)` in production, so an absent entry is exactly the
 * silent-doorbell case — and a test that quietly substituted that default would
 * report the bug as a pass.
 */
function pythonDefaultsForPending(): { mode: string; attention: string } {
  const src = readFileSync(MODEL_PY, 'utf8');
  const consts = pythonStringConstants(src);
  const m = src.match(
    new RegExp(`^\\s*["']${KIND}["']\\s*:\\s*\\(\\s*([A-Za-z0-9_"']+)\\s*,\\s*([A-Za-z0-9_"']+)\\s*\\)`, 'm'),
  );
  if (!m) throw new Error(`KIND_DEFAULTS has no "${KIND}" entry — the doorbell's wiring is gone`);
  const resolve = (tok: string): string => {
    const literal = tok.match(/^["'](.+)["']$/);
    if (literal) return literal[1];
    const v = consts[tok];
    if (!v) throw new Error(`could not resolve ${tok} in model.py`);
    return v;
  };
  return { mode: resolve(m[1]), attention: resolve(m[2]) };
}

const PY = pythonDefaultsForPending();

function pendingItem(): FeedItem {
  return withServedActions({
    id: 'pending:vault/task/Fix the latch.md',
    kind: KIND,
    instance: 'salem',
    title: 'Waiting on: the latch part',
    // FROM PYTHON, not from a literal — see the header.
    mode: PY.mode,
    attention: PY.attention,
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-15T09:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  } as unknown as FeedItem);
}

describe('a pending item reaches the doorbell', () => {
  it('survives BOTH gates the poller applies, under the DEFAULT policy', () => {
    // The two gates in the order `runPollOnce` applies them: the fetch filter
    // (`fetchNeedsYouItems` → isNeedsYouItem) and then the policy narrowing
    // (`isPushEligible`). Asserting them together is the point — either one
    // alone leaves the other free to drop the item.
    const item = pendingItem();
    const policy = readPushPolicy();
    expect(policy).toBe('needs_you'); // the default, unset PUSH_POLICY
    expect(isNeedsYouItem(item)).toBe(true);
    expect(isPushEligible(item, policy)).toBe(true);
  });

  it('reads needs_you from Python — the fixture is not asserting itself', () => {
    // The guard on the guard. If this file ever stops reading model.py and goes
    // back to a typed literal, the test above becomes a tautology that cannot
    // see a downgrade. Naming the expected value HERE, once, is what makes the
    // derivation checkable without making the pin above depend on the literal.
    expect(PY.attention).toBe('needs_you');
    expect(PY.mode).toBe('decide');
  });

  it('the STRICT policies narrow it away — the control', () => {
    // Proof the eligibility assertion above is doing work rather than passing
    // for everything. Under an email-only policy a pending item is correctly
    // filtered out, so `isPushEligible` genuinely discriminates and the default
    // admitting it is a fact about the default rather than about the function.
    const item = pendingItem();
    expect(isPushEligible(item, 'email_urgent_all')).toBe(false);
    expect(isPushEligible(item, 'email_urgent_override')).toBe(false);
  });
});
