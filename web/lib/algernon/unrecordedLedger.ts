// THE UNRECORDED-ACT LEDGER — the storage mechanism behind every surface that
// defers a write behind an undo window.
//
// ## The incident this exists to prevent
//
// 2026-08-15, 15:08Z. The operator swiped five email_tier cards in one burst —
// two spam, three confirm. Every act came back 409 `stale_item`
// (`aged_out_of_last_batch`: the classifier was quota-dead and its batch had
// aged out). The deck advanced past all five anyway, because the POST is
// deferred behind the undo window and the advance never consulted it. His five
// verdicts were recorded nowhere. The full account lives in `deckUnrecorded`,
// which was the first surface to grow one of these.
//
// ## Why a STORE and not just React state
//
// The failure can arrive after the surface is gone. A deferred act fires from
// the unmount cleanup, and its answer lands when there is no component left to
// tell. A ledger that lives only in hook state cannot fix that, because the
// state is already unmounted when the truth arrives. So every write here is a
// PLAIN FUNCTION CALL that touches storage and nothing else: it works
// identically from a live render and from a dead closure.
//
// ## localStorage, deliberately — NOT sessionStorage
//
// Its sibling `deckSnooze` is session-scoped on purpose (a client-side snooze
// means "not in this sitting", so it should die with the tab). This is the
// opposite kind of fact. An unrecorded act is a DEBT: the operator decided
// something and the system failed to keep it. Closing the tab does not settle a
// debt, and a ledger that forgets on tab close would re-run the incident with an
// extra step. It survives until the act is re-given and lands, or until the
// operator acknowledges it.
//
// Deliberately UNBOUNDED. A cap would have to drop something, and every
// candidate for dropping is an act the operator gave and has not yet seen
// reported — which is the bug. In practice the list is bounded by the surface's
// own size and drains as entries are re-acted or acknowledged; a storage quota
// failure degrades to in-memory-only for the session (below), never to a silent
// trim.
//
// ## ONE KEY PER SURFACE, never a shared one
//
// `createUnrecordedLedger` is called once per surface with its own key, and the
// separation is load-bearing rather than tidy: the ONE control on each notice
// clears the whole list, so a shared key would let acknowledging on the deck
// silently settle a debt the operator has never seen on the board. A debt is
// paid by the person who was shown it.
//
// ## Why this is a factory and not four exported functions
//
// It was four exported functions, on the deck, for exactly one surface. The
// board is the second, and the commit that adds the second reader is the commit
// that would otherwise create the second spelling of `localStorage` handling,
// JSON tolerance and the degrade-toward-saying-less rule below. The deck's key
// and entry shape are unchanged by the lift — its own suite, untouched, is what
// says so.

/**
 * The fields every ledger entry carries, on every surface.
 *
 * `reason` is the server's OWN words where it had any (`FeedActResult.detail`,
 * e.g. "aged out of the last batch"), else its machine status. `title` is
 * carried because the notice must be able to NAME the thing even when it is no
 * longer in the served batch — which is precisely the case in the incident, and
 * the case where the operator has the least other way to find out.
 *
 * A surface may extend this (the deck adds the `verdict` it gave); the storage
 * layer round-trips whole objects, so extra fields survive untouched.
 */
export interface UnrecordedAct {
  id: string;
  title: string;
  actionId: string;
  reason: string;
  at: number;
}

export interface UnrecordedLedger<E extends UnrecordedAct> {
  /** The `localStorage` key. Exported so a test can seed it the way the app does. */
  readonly key: string;
  /** The ledger, oldest first. */
  read(): E[];
  /** Record one refused act, replacing any earlier entry for the same id. */
  record(entry: E): E[];
  /** Drop one id's entry — it was re-given and landed. */
  clear(id: string): E[];
  /** The operator has SEEN the list. */
  clearAll(): E[];
}

/**
 * The fields the notice cannot render without.
 *
 * A typo'd or half-written entry is dropped rather than rendered blank: a row
 * that names nothing tells the operator they lost something without telling
 * them what, which is worse than the silence it replaced.
 */
function isEntry<E extends UnrecordedAct>(x: unknown): x is E {
  if (typeof x !== 'object' || x === null) return false;
  const e = x as Record<string, unknown>;
  return typeof e.id === 'string' && typeof e.title === 'string' && typeof e.actionId === 'string';
}

export function createUnrecordedLedger<E extends UnrecordedAct>(key: string): UnrecordedLedger<E> {
  /**
   * Empty on the server, on a parse failure, or when `localStorage` is
   * unavailable.
   *
   * Degrading to EMPTY is the only safe direction here, and it is the opposite
   * trade from the snooze hide-list: a hide-list that cannot be read must hide
   * nothing (hiding everything would blank the surface), and a ledger that
   * cannot be read must claim nothing (inventing a debt would send the operator
   * to re-decide something that was decided correctly). Both degrade toward
   * saying less.
   */
  function read(): E[] {
    if (typeof window === 'undefined') return [];
    try {
      const raw = window.localStorage.getItem(key);
      const arr = raw ? (JSON.parse(raw) as unknown) : [];
      return Array.isArray(arr) ? arr.filter((x): x is E => isEntry<E>(x)) : [];
    } catch {
      return [];
    }
  }

  function write(entries: E[]): void {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(key, JSON.stringify(entries));
    } catch {
      /* storage unavailable / quota → the session's in-memory ledger still shows
         it; the persistence is what degrades, never the notice on screen. */
    }
  }

  return {
    key,
    read,
    /**
     * REPLACE, not append: the operator acting on the same thing twice and
     * being refused twice has ONE unpaid debt, not two, and a list that grew a
     * row per retry would bury the other entries it is supposed to be naming.
     *
     * Callable from a dead closure (the unmount flush) — it touches storage
     * only. Returns the new ledger so a live caller can mirror it into React
     * state without a second read.
     */
    record(entry: E): E[] {
      const next = [...read().filter((e) => e.id !== entry.id), entry];
      write(next);
      return next;
    },
    clear(id: string): E[] {
      const next = read().filter((e) => e.id !== id);
      write(next);
      return next;
    },
    clearAll(): E[] {
      write([]);
      return [];
    },
  };
}
