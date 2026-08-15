import type { Verdict } from './feedConstants';
import { createUnrecordedLedger, type UnrecordedAct } from './unrecordedLedger';

// The DECK's unrecorded-verdict ledger — the verdicts the operator gave that the
// server refused, held until the operator has actually seen them.
//
// ## The incident this exists to prevent
//
// 2026-08-15, 15:08Z. The operator swiped five email_tier cards in one burst —
// two spam, three confirm. Every act came back 409 `stale_item`
// (`aged_out_of_last_batch`: the classifier was quota-dead and its batch had
// aged out). The deck advanced past all five anyway, because the POST is
// deferred behind the undo window and the advance never consulted it. His five
// verdicts were recorded nowhere. The same five cards greeted him on the feed
// banner. The only thing he got was, at most, ONE toast — "That one had already
// moved on — it'll resurface at the next sync" — which named no card, arrived
// while a DIFFERENT card was on screen, and never said the words that mattered:
// your verdict was not recorded.
//
// ## What lives here, and what moved
//
// The STORAGE MECHANISM — localStorage over sessionStorage, the unbounded list,
// the degrade-toward-saying-less read, replace-not-append — is now
// `unrecordedLedger`, shared with the board's grace flush, which is the same
// deferred-write shape with the same silence at its unmount. Its reasoning lives
// there in full. THIS file keeps what is the deck's alone: its key, the
// `verdict` its entries carry, and the words for that verdict.
//
// The key and the entry shape are UNCHANGED by that lift. There are live
// entries in production browsers written by the pre-lift code, and a ledger that
// forgot them on deploy would drop exactly the debts it exists to keep.

/** The `localStorage` key. Exported so a test can seed it the way the deck does. */
export const DECK_UNRECORDED_KEY = 'algernon_deck_unrecorded';

/**
 * One verdict the server refused.
 *
 * The base fields are the shared ledger's (see `UnrecordedAct` for why `reason`
 * and `title` are carried). `verdict` is the deck's own: it is WHAT the operator
 * decided, and the notice has to say it back to them.
 */
export interface UnrecordedVerdict extends UnrecordedAct {
  verdict: Verdict;
}

const ledger = createUnrecordedLedger<UnrecordedVerdict>(DECK_UNRECORDED_KEY);

/** The ledger, oldest first. Empty when storage is unreadable — never invented. */
export const readUnrecorded = ledger.read;
/** Record one refused verdict, replacing any earlier entry for the same card. */
export const recordUnrecorded = ledger.record;
/** Drop one card's entry — it was re-given and landed. Returns the new ledger. */
export const clearUnrecorded = ledger.clear;
/** The operator has SEEN the list. Returns the new (empty) ledger. */
export const clearAllUnrecorded = ledger.clearAll;

/**
 * What the operator DID, as a noun, for copy that has to say it back to them.
 *
 * One owner because two surfaces say it — the hook's already-decided toast and
 * the notice's per-card line — and a card that reads "your rejection" in one
 * place and "your reject" in the other reads as two different systems talking.
 * `null` is unreachable from `commit` (every commit passes a verdict) and is
 * carried only because `Verdict` admits it.
 */
const VERDICT_NOUN: Record<Exclude<Verdict, null>, string> = {
  affirm: 'confirmation',
  reject: 'rejection',
  snooze: 'snooze',
  skip: 'set-aside',
};

export function verdictNoun(v: Verdict): string {
  return v === null ? 'verdict' : VERDICT_NOUN[v];
}
