import servedActionsDoc from './servedActions.json';
import type { FeedItem } from '../../lib/algernon/feed';

// The verbs the SERVER actually serves, for test fixtures (#102 1b-ii).
//
// WHY THIS FILE EXISTS. The deck no longer holds an opinion about verbs — it
// renders what arrives in `item.actions`. So a fixture that omits `actions` is
// not a neutral fixture: it is a fixture of a DEGRADED payload, and a test built
// on one would be asserting the deck's degraded behaviour while claiming to
// assert its normal behaviour. Every test that needs a card to have verbs must
// hand it the verbs the server would have sent.
//
// ONE COPY, AND IT IS CHECKED. Hand-writing `actions` at each call site would
// re-create the per-kind mirror this lane just deleted — only scattered across
// twenty test files instead of one constant. The data lives in
// `servedActions.json`, which was GENERATED from `actions_for()` and is guarded
// by `tests/feed/test_feed_advertised_actions.py::TestTheFEFixtureMatchesWhatIsServed`:
// that test loads the same JSON and asserts it equals `actions_for(kind)` for
// every kind, verb-for-verb and in order. So this cannot quietly become a second
// source of truth — if the ceiling moves and the fixture does not, python reds.
//
// NOT imported by any production module, and must never be: it describes the
// wire, it does not define it.

export interface ServedActionFixture {
  verb: string;
  label: string;
  weight: 'light' | 'heavy';
  gesture?: string;
  note?: string;
  // These are wire records, and `FeedItem.actions` is typed as such. The index
  // signature is what lets a fixture BE an item's actions without a cast — and
  // it is honest: the server may add a field this interface has not learned yet.
  [key: string]: unknown;
}

const SERVED = servedActionsDoc.kinds as Record<string, ServedActionFixture[]>;

/**
 * The verbs the server serves for `kind` — `[]` for a kind the ceiling does not
 * know (every FYI kind, and `recurrence`, which has no ceiling entry at all).
 *
 * Copied on the way out so a test that mutates its fixture cannot poison the
 * next one.
 */
export function servedActionsFor(kind: string): ServedActionFixture[] {
  return SERVED[kind] ? SERVED[kind].map((a) => ({ ...a })) : [];
}

/**
 * The per-ITEM narrowing, mirroring the server's `actions_for_item`: a slot that
 * is not a genuine candidate is not offered the `accept` the router would refuse.
 * Use this wherever a fixture's slot stage matters.
 */
export function servedActionsForItem(
  kind: string,
  evidence: Record<string, unknown> = {},
): ServedActionFixture[] {
  const verbs = servedActionsFor(kind);
  if (kind === 'slot_suggestion' && evidence.candidate !== true) {
    return verbs.filter((a) => a.verb !== 'accept');
  }
  return verbs;
}

/**
 * Fill a fixture's `actions` with what the server would have served it —
 * applied to the FINAL item, after every override has been merged.
 *
 * Wrapping the whole builder (rather than writing `actions:` inline among the
 * other fields) is what makes this correct when a test overrides `kind` or
 * `evidence`: a slot fixture that sets `candidate: true` in an override gets the
 * candidate's verb list, and one that does not gets the committed list, without
 * either test having to know that the accept verb is stage-dependent.
 *
 * A fixture that set `actions` EXPLICITLY is left alone — that is how a test
 * asks for a degraded payload (a half-deployed backend serving no verbs), which
 * is a real state worth pinning and must stay expressible.
 */
export function withServedActions(item: FeedItem): FeedItem {
  if (Array.isArray(item.actions) && item.actions.length > 0) return item;
  return {
    ...item,
    actions: servedActionsForItem(item.kind, (item.evidence ?? {}) as Record<string, unknown>),
  };
}
