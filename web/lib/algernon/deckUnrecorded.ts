import type { Verdict } from './feedConstants';

// The deck's UNRECORDED-VERDICT ledger — the verdicts the operator gave that the
// server refused, held until the operator has actually seen them.
//
// ## The incident this exists to prevent
//
// 2026-08-15, 15:08Z. The operator swiped five email_tier cards in one burst —
// two spam, three confirm. Every act came back 409 `stale_item`
// (`aged_out_of_last_batch`: the classifier was quota-dead and its batch had
// aged out). The deck advanced past all five anyway, because the POST is
// deferred behind the undo window and the advance never consults it. His five
// verdicts were recorded nowhere. The same five cards greeted him on the feed
// banner. The only thing he got was, at most, ONE toast — "That one had already
// moved on — it'll resurface at the next sync" — which named no card, arrived
// while a DIFFERENT card was on screen, and never said the words that mattered:
// your verdict was not recorded.
//
// ## Why a STORE and not just React state
//
// The failure can arrive after the deck is gone. The last card's act fires from
// the unmount cleanup, and its answer lands when there is no component left to
// tell. That path used to be `.catch(() => {})` — a deliberate discard, and the
// most silent of the four softeners. A ledger that lives only in hook state
// cannot fix it, because the state is already unmounted when the truth arrives.
// So the WRITE side is a plain function call that touches storage and nothing
// else: it works identically from a live render and from a dead closure.
//
// ## localStorage, deliberately — NOT sessionStorage
//
// Its sibling `deckSnooze` is session-scoped on purpose (a client-side snooze
// means "not in this sitting", so it should die with the tab). This is the
// opposite kind of fact. An unrecorded verdict is a DEBT: the operator decided
// something and the system failed to keep it. Closing the tab does not settle a
// debt, and a ledger that forgets on tab close would re-run the incident with an
// extra step. It survives until the verdict is re-given and lands, or until the
// operator acknowledges it.
//
// Deliberately UNBOUNDED. A cap would have to drop something, and every
// candidate for dropping is a verdict the operator gave and has not yet seen
// reported — which is the bug. In practice the list is bounded by the deck's own
// size and drains as entries are re-acted or acknowledged; a storage quota
// failure degrades to in-memory-only for the session (below), never to a silent
// trim.

/** The `localStorage` key. Exported so a test can seed it the way the deck does. */
export const DECK_UNRECORDED_KEY = 'algernon_deck_unrecorded';

/**
 * One verdict the server refused.
 *
 * `reason` is the server's OWN words where it had any (`FeedActResult.detail`,
 * e.g. "aged out of the last batch"), else its machine status. `title` is
 * carried because the notice must be able to NAME the card even when that card
 * is no longer in the served batch — which is precisely the case in the
 * incident, and the case where the operator has the least other way to find out.
 */
export interface UnrecordedVerdict {
  id: string;
  title: string;
  verdict: Verdict;
  actionId: string;
  reason: string;
  at: number;
}

function isEntry(x: unknown): x is UnrecordedVerdict {
  if (typeof x !== 'object' || x === null) return false;
  const e = x as Record<string, unknown>;
  return typeof e.id === 'string' && typeof e.title === 'string' && typeof e.actionId === 'string';
}

/**
 * The ledger, oldest first. Empty on the server, on a parse failure, or when
 * `localStorage` is unavailable.
 *
 * Degrading to EMPTY is the only safe direction here, and it is the opposite
 * trade from the snooze hide-list: a hide-list that cannot be read must hide
 * nothing (hiding everything would blank the deck), and a ledger that cannot be
 * read must claim nothing (inventing a debt would send the operator to re-decide
 * a card that was decided correctly). Both degrade toward saying less.
 */
export function readUnrecorded(): UnrecordedVerdict[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(DECK_UNRECORDED_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? arr.filter(isEntry) : [];
  } catch {
    return [];
  }
}

function write(entries: UnrecordedVerdict[]): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(DECK_UNRECORDED_KEY, JSON.stringify(entries));
  } catch {
    /* storage unavailable / quota → the session's in-memory ledger still shows
       it; the persistence is what degrades, never the notice on screen. */
  }
}

/**
 * Record one refused verdict, replacing any earlier entry for the same card.
 *
 * REPLACE, not append: the operator swiping the same card twice and being
 * refused twice has ONE unpaid debt, not two, and a list that grew a row per
 * retry would bury the other four cards it is supposed to be naming.
 *
 * Callable from a dead closure (the unmount flush) — it touches storage only.
 * Returns the new ledger so a live caller can mirror it into React state without
 * a second read.
 */
export function recordUnrecorded(entry: UnrecordedVerdict): UnrecordedVerdict[] {
  const next = [...readUnrecorded().filter((e) => e.id !== entry.id), entry];
  write(next);
  return next;
}

/** Drop one card's entry — it was re-given and landed. Returns the new ledger. */
export function clearUnrecorded(id: string): UnrecordedVerdict[] {
  const next = readUnrecorded().filter((e) => e.id !== id);
  write(next);
  return next;
}

/** The operator has SEEN the list. Returns the new (empty) ledger. */
export function clearAllUnrecorded(): UnrecordedVerdict[] {
  write([]);
  return [];
}

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
