// The deck's client-side snooze hide-list — ONE owner, because two surfaces
// depend on it and only one of them used to know it existed.
//
// WHY THIS IS SHARED (the bug that moved it here, 2026-08-12): the operator's
// screenshots showed the feed promising "2 decisions waiting → Open the deck",
// on a deck that answered "DECK CLEAR — 2 snoozed". Both surfaces read the same
// `/feed/items` list, but a snooze lives in one of TWO unrelated places:
//
//   * HERE — a `sessionStorage` hide-list the DECK writes when a card is set
//     aside, and applies once at load;
//   * `useSnooze` — in-memory React overrides plus a server write, scoped to
//     the page session that made them.
//
// Neither could see the other. So a card snoozed IN THE DECK stayed invisible to
// the feed, whose banner counted it as waiting and sent the operator to a deck
// that had already refused to deal it — a promise of a trip to a wall. The fix
// is not a filter on one page; it is giving "what the deck will refuse to deal"
// a single owner that both pages consult.
//
// SESSION-SCOPED BY DESIGN, not by omission: a snooze here defers to the next
// sync (Phase B; no store write), so `sessionStorage` — cleared with the tab —
// is the honest lifetime for it. It is not a substitute for the server-side
// snooze verbs, which persist; it is the deck's own "not in this sitting".

/** The `sessionStorage` key. Exported so a test can seed it the way the deck does. */
export const DECK_SNOOZE_KEY = 'algernon_deck_snoozed';

/**
 * The ids the deck is currently hiding. Empty set on the server, on a parse
 * failure, or when `sessionStorage` is unavailable — a hide-list that cannot be
 * read must degrade to hiding NOTHING, never to hiding everything, because the
 * failure mode of the latter is an empty deck with no explanation.
 */
export function readDeckSnoozed(): Set<string> {
  if (typeof window === 'undefined') return new Set();
  try {
    const raw = window.sessionStorage.getItem(DECK_SNOOZE_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []);
  } catch {
    return new Set();
  }
}

export function writeDeckSnoozed(ids: Set<string>): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(DECK_SNOOZE_KEY, JSON.stringify([...ids]));
  } catch {
    /* sessionStorage unavailable → snooze is best-effort in-memory only */
  }
}
