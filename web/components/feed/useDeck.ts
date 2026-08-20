import { useCallback, useEffect, useRef, useState } from 'react';
import { feedApi, type FeedActResult, type FeedItem } from '../../lib/algernon/feed';
import {
  CORRECT_ACTION,
  DEFER_QUICK_ACTION,
  holdChoicesFor,
  isHeavyVerb,
  ONE_OFF_ACTION,
  ROUTINE_MATCH_KIND,
  SNOOZE_INDEFINITE_ACTION,
  SNOOZE_LABELS,
  UNDO_MS,
  UNSNOOZE_ACTION,
  verbsFromActions,
  snoozeActionFor,
  snoozeIsBacked,
  type SnoozeAction,
  type Verdict,
} from '../../lib/algernon/feedConstants';
import { ApiError } from '../../lib/algernon/http';
import { ACT_UNCONFIRMED_MESSAGE, isInconclusive, refusalReason } from '../../lib/algernon/actConfirm';
import {
  clearAllUnrecorded,
  clearUnrecorded,
  readUnrecorded,
  recordUnrecorded,
  verdictNoun,
  type UnrecordedVerdict,
} from '../../lib/algernon/deckUnrecorded';

// The deck state machine — deliberately DOM-free so the intricate parts (the
// delayed act, the two-step heavy confirm, snooze, undo, and error routing) are
// unit-testable with fake timers + a mocked feedApi, while Deck.tsx supplies the
// pointer-drag + rendering on top.
//
// DELAYED ACT (light verbs + a heavy verb's confirm-tap): a commit advances
// the card immediately (optimistic) but the POST is DEFERRED. It fires when the
// 3.5s undo window expires OR the next commit lands (flush-in-order) — and UNDO
// cancels it before it ever fires (never an un-act). SNOOZE rides the same path:
// it POSTs on a kind the backend can snooze (#14) and is a pure client-side
// set-aside on every other kind, which is the same code path with a null
// actionId. A HEAVY VERB's FIRST swipe does not commit — either direction — it
// reveals a confirm-tap showing what that verb will write (D4).
//
// THE CARD COMES BACK WHEN THE VERDICT DOESN'T LAND (the 2026-08-15 incident).
// The deferral above is deliberate and stays — but it used to mean the advance
// never consulted the POST at all, so a refused act left the operator's verdict
// recorded nowhere while the deck sailed past. Five swipes, five 409s, five
// verdicts gone, and at most ONE toast that named no card. So a definite
// server refusal now RETURNS the card to the queue and writes the verdict into
// the unrecorded ledger (`deckUnrecorded`), which names it on screen and
// survives the deck's own unmount.
//
// What is returned and what is not turns on ONE distinction, inherited from #62:
// did the server ANSWER? A 4xx/5xx is an answer — the verdict definitely did not
// land, so the card comes back. A timeout or a dropped connection is NOT an
// answer — the act may well have committed, and handing the card back would
// invite a second, duplicate verdict on a decision that already stuck. Those keep
// the reconcile toast and nothing else. A genuine 401 (the BFF's own
// invalid_session — the transport's wrong-peer 401 is mapped to 502 by the BFF)
// routes to onAuthExpired; the session is ending and there is no deck to return
// a card to.
//
// Benign/system outcomes still surface as a TOAST (timeout — the next list poll
// reconciles truth) or a fatal BANNER (server-config 502/503 — never a logout).
// A returned card does NOT toast: the toast is what failed the operator here
// (one line, no name, replaced by the next one), and the notice that names the
// card is what replaces it.

export interface DeckToast {
  message: string;
  canUndo: boolean;
}

/** The status the store answers when an act arrives after the item was decided. */
const STATUS_ALREADY_ACTED = 'already_acted';

/**
 * A server-config failure the operator can do nothing about.
 *
 * One local owner for the ladder that was written inline. The same five terms
 * are ALSO spelled out twice in `useFeedBoard`; consolidating across files is a
 * separate lane, but this file no longer has two chances to drift from itself.
 */
function isFatalTransport(e: ApiError): boolean {
  return (
    e.status === 502 ||
    e.status === 503 ||
    e.code === 'feed_upstream_unavailable' ||
    e.code === 'transport_unreachable' ||
    e.code === 'not_configured'
  );
}

/**
 * What KIND of failure this was — and, decisively, whether the server answered.
 *
 * `refused` is the one that returns a card: the server heard us and said no, so
 * the verdict is definitely unrecorded. `inconclusive` deliberately does NOT
 * return one (see the header) — that is #62's rule, and inverting it here would
 * turn "we never heard back" into a duplicate act on a decision that landed.
 */
type ActFailure = 'auth' | 'fatal' | 'inconclusive' | 'refused' | 'unknown';

function classifyActFailure(e: unknown): ActFailure {
  if (!(e instanceof ApiError)) return 'unknown';
  if (e.status === 401) return 'auth';
  if (isFatalTransport(e)) return 'fatal';
  if (isInconclusive(e)) return 'inconclusive';
  return 'refused';
}

/**
 * What to tell the operator after a ↑, given what the gesture actually did.
 *
 * THREE outcomes hide behind one gesture, and they come back at three different
 * times, so one sentence cannot cover them honestly:
 *
 *   - no action id  → nothing was written; the item is still open server-side,
 *                     so it genuinely does return at the next sync.
 *   - a dated rung  → it returns when the duration runs out. Not the next sync;
 *                     that is the entire point of the store.
 *   - indefinite    → it returns when the operator says so, and never on its own.
 *                     This is the FULL-SWIPE default (the preserved Park muscle
 *                     memory), so it is also the most-used path of the three.
 *
 * (Urgency can break any of them through early, which is the one thing the
 * toast deliberately doesn't promise — a card that comes back BEFORE you were
 * told it would is a good surprise, and the card itself explains why via
 * `evidence.snooze_breakthrough`.)
 */
function snoozeToast(actionId: string | null): string {
  if (actionId === null) return 'Set aside — it resurfaces at the next sync.';
  if (actionId === SNOOZE_INDEFINITE_ACTION) return 'Snoozed until you say otherwise.';
  const label = SNOOZE_LABELS[actionId as SnoozeAction];
  return label ? `Snoozed for ${label.toLowerCase()}.` : 'Snoozed.';
}

export interface UseDeckOptions {
  items: FeedItem[];
  onAuthExpired?: () => void;
  onSnoozePersist?: (id: string) => void;
  onUnsnoozePersist?: (id: string) => void;
}

export interface UseDeckResult {
  current: FeedItem | null;
  upcoming: FeedItem[];
  remaining: number;
  /** The this-session snoozed cards, retained for the snoozed drill-down. */
  snoozed: FeedItem[];
  /**
   * EVERY card still behind the current one, in order — as opposed to
   * `upcoming`, which is the two the visual stack draws.
   *
   * The workstation posture has room to show what is coming and the phone does
   * not; that is the whole difference between the two, and it needs the real
   * remainder rather than the render slice. Derived from the same queue and
   * index as everything else here, so it cannot disagree with `remaining`.
   */
  ahead: FeedItem[];
  snoozedCount: number;
  confirmingId: string | null;
  /** Which direction is armed on that card ('affirm' | 'reject'), or null. */
  confirmingVerdict: 'affirm' | 'reject' | null;
  toast: DeckToast | null;
  banner: string | null;
  /**
   * Every verdict the server refused and has not been settled since — oldest
   * first, ACCUMULATED rather than replaced.
   *
   * A burst is the normal shape of this failure (one dead classifier batch
   * refuses every card in it), and the toast this replaces could show exactly
   * one of them. Five refusals must read as five.
   */
  unrecorded: UnrecordedVerdict[];
  /** The ids in `unrecorded` — the cards that carry the verdict-unrecorded mark. */
  unrecordedIds: Set<string>;
  /** The operator has read the list: clear it (the cards stay in the deck). */
  acknowledgeUnrecorded: () => void;
  cleared: boolean;
  affirm: () => void;
  /**
   * Affirm the top card WITH a chosen alternative — the hold-selector's commit
   * (affirm-with-hold-modifier). ONE INTERACTION: this is the same optimistic
   * commit + undo window as `affirm`, with the chosen verb in place of the
   * suggested one; never a second confirm. The verb must be one of the card's
   * own served co-equal choices (`holdChoicesFor`) — anything else is a no-op,
   * because a POST the wire never offered is a 400 in the operator's hand.
   */
  affirmWith: (verb: string) => void;
  reject: () => void;
  /** Defer the top card. No argument = the indefinite rung (a full ↑ swipe). */
  snooze: (action?: SnoozeAction) => void;
  confirmHeavy: () => void;
  cancelHeavy: () => void;
  undo: () => void;
  /** Deal a snoozed card back into the deck queue (un-snooze + re-enter immediately). */
  dealNow: (item: FeedItem) => void;
  /** The re-tier action_id in flight for the current email card (#28), or null. */
  reTiering: string | null;
  /** Re-tier the current email card: await the act, flip only on `acted`. Awaitable. */
  reTier: (tierId: string) => Promise<void>;
  /** The #13 correction in flight for the current routine_match card — the chosen
   *  item text, or the ONE_OFF_ACTION sentinel, or null when idle. */
  correcting: string | null;
  /** Reject the routine match AND teach the right answer. `target` = the item the
   *  operator picked; `null` = "nothing, this was a one-off". Awaitable. */
  correctRoutine: (target: string | null) => Promise<void>;
  dismissToast: () => void;
}

interface Pending {
  item: FeedItem;
  actionId: string | null; // null = a client-side set-aside (no POST)
  verdict: Verdict;
  restoreIndex: number;
  restoreSnooze: boolean;
}

export function useDeck(opts: UseDeckOptions): UseDeckResult {
  const { items, onAuthExpired, onSnoozePersist, onUnsnoozePersist } = opts;

  const [index, setIndex] = useState(0);
  // WHICH card and WHICH direction is armed. An id alone was enough while only
  // affirm could arm; now that a heavy REJECT arms too, the pair is what tells
  // the confirm tap which transaction it is committing — and stops a card armed
  // one way from being committed the other.
  const [confirming, setConfirming] = useState<{ id: string; verdict: 'affirm' | 'reject' } | null>(null);
  const confirmingId = confirming?.id ?? null;
  const [toast, setToast] = useState<DeckToast | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  // The this-session snoozed cards, RETAINED (not just counted) so the snoozed
  // drill-down can list them (title + kind) + deal them back. undo un-snoozes the last.
  const [snoozed, setSnoozed] = useState<FeedItem[]>([]);
  // Cards re-entering the deck behind the cursor — re-appended to the tail of the queue
  // so one becomes dealable immediately without disturbing the index. TWO sources, and
  // they share the mechanism because they are the same question: a card dealt back from
  // the snoozed drill (`dealNow`), and a card whose verdict the server refused
  // (`returnCard`). The `__deckKey` prefix says which.
  const [readded, setReadded] = useState<FeedItem[]>([]);
  // The re-tier action_id in flight for the current email card (#28), or null. Unlike a
  // swipe (optimistic advance + deferred POST), a re-tier AWAITS the act and only flips
  // on `acted` — a calibration CORRECTION must be honestly confirmed, not optimistic.
  const [reTiering, setReTiering] = useState<string | null>(null);
  // The #13 correction in flight (chosen item text, or the one-off sentinel).
  // Like re-tier and for the same reason: a taught answer must be confirmed by
  // the server before the card leaves, because the server is the only thing that
  // knows whether the pick was a real routine item.
  const [correcting, setCorrecting] = useState<string | null>(null);
  // The unrecorded-verdict ledger, mirrored from `deckUnrecorded` storage. The
  // STORE is the source of truth (it is what survives unmount and tab close);
  // this is the render copy.
  const [unrecorded, setUnrecorded] = useState<UnrecordedVerdict[]>([]);

  // Hydrate the ledger on mount — NOT as lazy `useState(readUnrecorded)`, which
  // would read storage during the first client render and disagree with the
  // server's HTML (no notice) that React is hydrating against. An effect runs
  // after that reconciliation, so the notice appears without a mismatch.
  //
  // This is also the SECOND HALF of the unmount path: a failure whose answer
  // arrived when there was no deck left wrote itself to storage from a dead
  // closure, and this is where it comes back into view.
  useEffect(() => {
    const stored = readUnrecorded();
    if (stored.length > 0) setUnrecorded(stored);
  }, []);

  const pendingRef = useRef<Pending | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const readdSeqRef = useRef(0);
  // The ledger as of this render, readable from an async outcome handler without
  // making it a dependency (which would re-create the dispatcher on every debt).
  const unrecordedRef = useRef(unrecorded);
  unrecordedRef.current = unrecorded;

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  // The IMMEDIATE paths' error routing (dealNow's un-snooze, and the awaited
  // re-tier / correct). Unchanged in behaviour: those callers still have their
  // card in front of them, so a toast is attributable there in the way it is
  // not for a deferred act.
  const routeError = useCallback(
    (e: unknown) => {
      switch (classifyActFailure(e)) {
        case 'auth':
          onAuthExpired?.();
          return;
        case 'fatal':
          setBanner("The deck can't reach Algernon right now — this is a server-side issue, not your session.");
          return;
        // #62: shared predicate, one owner. The act MAY have landed — the deck's
        // own reconciliation is its list poll plus the resume refetch, and the
        // message below is only true because those now actually run.
        case 'inconclusive':
          setToast({ message: ACT_UNCONFIRMED_MESSAGE, canUndo: false });
          return;
        case 'refused': {
          const err = e as ApiError;
          if (err.status === 409 || err.code === 'stale_item') {
            setToast({ message: "That one had already moved on — it'll resurface at the next sync.", canUndo: false });
            return;
          }
          setToast({ message: refusalReason(err), canUndo: false });
          return;
        }
        default:
          setToast({ message: 'That action failed.', canUndo: false });
      }
    },
    [onAuthExpired],
  );

  // HAND THE CARD BACK. The deferred act for `p` did not land, so the verdict is
  // unrecorded: ledger it (storage first — see below), and re-enter the card.
  //
  // RE-ENTERED AT THE TAIL, not at the index it left from. The failure arrives
  // while a LATER card is under the operator's thumb — that is the whole shape of
  // the deferral — and materialising a card at the cursor would put a different
  // card under a gesture already in motion. That is the same class of harm this
  // lane exists to fix: a verdict landing somewhere the operator did not aim it.
  // The tail is the audited mechanism `dealNow` already uses for re-entry.
  //
  // The ledger write comes FIRST and is a plain storage call, so it works
  // identically from a live render and from the unmount closure, where the
  // setState calls below are no-ops and there is no component left to tell.
  const returnCard = useCallback(
    (p: Pending, reason: string) => {
      const ledger = recordUnrecorded({
        id: p.item.id,
        title: p.item.title,
        verdict: p.verdict,
        actionId: p.actionId ?? '',
        reason,
        at: Date.now(),
      });
      setUnrecorded(ledger);
      // A defer that did not land is not a defer. Undo the session set-aside AND
      // the client-side hide-list — `deckSnooze` is applied at the next load, so
      // leaving it would filter out the very card being handed back, and the
      // return would survive exactly until the operator reloaded.
      if (p.restoreSnooze) {
        setSnoozed((prev) => prev.filter((it) => it.id !== p.item.id));
        onUnsnoozePersist?.(p.item.id);
      }
      const seq = readdSeqRef.current++;
      setReadded((prev) => [...prev, { ...p.item, __deckKey: `unrecorded-${seq}` } as FeedItem]);
    },
    [onUnsnoozePersist],
  );

  // Fire ONE deferred act and account for its outcome — the accounting the
  // advance never used to do.
  //
  // TWO-ARGUMENT `.then(onOk, onErr)`, deliberately, not `.then().catch()`: with
  // a trailing catch, a throw from inside the SUCCESS handler would be caught by
  // the failure handler and hand back a card whose act had actually landed. The
  // operator would then re-decide a decision that stuck. Splitting the handlers
  // makes that structurally impossible rather than merely unlikely.
  const dispatchAct = useCallback(
    (p: Pending) => {
      if (p.actionId === null) return; // set-aside → no POST
      feedApi.act(p.item.id, p.actionId).then(
        (res) => {
          if (!res.ok) {
            // A refusal that arrived on a 2xx. The transport maps every current
            // ok=false status to a non-2xx, so this is the defensive half of the
            // same contract rather than a live path — and it is the half that
            // matters if the server lane ever answers a refusal with a 200,
            // because `FeedActResult.ok` is what both sides agree on.
            returnCard(p, res.detail || res.status);
            return;
          }
          if (res.status === STATUS_ALREADY_ACTED) {
            // ILB: the act was a no-op because the item was already decided.
            // NAMED, because it lands while another card is on screen — and NOT
            // returned, because the server's answer would be the same forever.
            setToast({
              message: `“${p.item.title}” was already decided — your ${verdictNoun(p.verdict)} didn't change it.`,
              canUndo: false,
            });
            return;
          }
          // It landed. Settle any debt this card was carrying from an earlier
          // refusal — the operator re-gave the verdict and it stuck this time.
          // Gated on the ref rather than done inside a state updater: clearing
          // writes to storage, and a state updater is not a place for that.
          if (unrecordedRef.current.some((u) => u.id === p.item.id)) {
            setUnrecorded(clearUnrecorded(p.item.id));
          }
        },
        (e) => {
          // Only a REFUSAL and a FATAL are answers ("no", and "not from here");
          // both mean the verdict is definitely unrecorded. 'inconclusive' and
          // 'unknown' are the ABSENCE of an answer and must not be dressed as one.
          const kind = classifyActFailure(e);
          if (kind === 'refused') {
            // No toast. The toast is what failed the operator in the incident —
            // unnamed, late, and overwritten by the next one — and the notice
            // that names the card now carries this.
            returnCard(p, refusalReason(e as ApiError));
            return;
          }
          routeError(e); // auth → expire · fatal → banner · otherwise → toast
          if (kind === 'fatal') returnCard(p, refusalReason(e as ApiError));
        },
      );
    },
    [returnCard, routeError],
  );

  // The unmount path needs the CURRENT dispatcher, but must not re-register the
  // cleanup on every render (that would fire the pending act on each re-render).
  // A ref holds the latest; the effect below stays mount-scoped.
  const dispatchRef = useRef(dispatchAct);
  dispatchRef.current = dispatchAct;

  // Fire the deferred POST for the current pending commit (if any), in order.
  const flushPending = useCallback(() => {
    clearTimer();
    const p = pendingRef.current;
    pendingRef.current = null;
    if (!p) return;
    dispatchAct(p);
  }, [dispatchAct]);

  // Flush on unmount so a pending act isn't silently dropped when leaving /deck.
  //
  // This used to end in `.catch(() => {})` — the most complete of the four
  // silences, because the LAST card of a session always leaves by this door. It
  // now goes through the same dispatcher: there is no component left to show a
  // notice, so the ledger write is the whole point, and the next mount reads it.
  useEffect(() => {
    return () => {
      const p = pendingRef.current;
      clearTimer();
      pendingRef.current = null;
      if (p) dispatchRef.current(p);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const commit = useCallback(
    (verdict: Verdict, actionId: string | null, item: FeedItem) => {
      flushPending(); // fire the previous deferred act BEFORE starting a new one
      const restoreIndex = index;
      // Both defer verdicts set the card aside for the session; only their
      // NAMES differ (and, once the backend verb is wired, whether the snooze
      // also persists). Sharing the mechanism is fine — sharing the word is
      // what the ruling forbids.
      const defers = verdict === 'snooze' || verdict === 'skip';
      if (defers) {
        setSnoozed((prev) => [...prev, item]);
        onSnoozePersist?.(item.id);
      }
      setConfirming(null);
      setIndex((i) => i + 1);
      pendingRef.current = { item, actionId, verdict, restoreIndex, restoreSnooze: defers };
      setToast({
        message:
          verdict === 'snooze'
            ? snoozeToast(actionId)
            : verdict === 'skip'
              ? 'Skipped — it may resurface at the next sync.'
              : verdict === 'reject'
                // A reject whose SERVED verb is the quick defer (the rotation
                // card's "Not now") is a postponement, not a judgment — saying
                // "Rejected." over it would claim a decision the operator
                // deliberately did not make. The server's own detail for this
                // verb is the model: set aside, back at the next sync.
                ? actionId === DEFER_QUICK_ACTION
                  ? 'Not now — it comes back at the next sync.'
                  : 'Rejected.'
                : 'Confirmed.',
        canUndo: true,
      });
      clearTimer();
      timerRef.current = setTimeout(() => {
        flushPending();
        setToast(null);
      }, UNDO_MS);
    },
    [flushPending, index, onSnoozePersist],
  );

  // The effective deck queue: the base items plus any cards dealt back from the snoozed
  // view (appended at the tail). The index walks this combined queue, so a re-dealt card
  // becomes reachable without shifting the cards already behind the cursor.
  const queue = readded.length > 0 ? items.concat(readded) : items;
  const current = index < queue.length ? queue[index] : null;

  const affirm = useCallback(() => {
    if (!current) return;
    const verbs = verbsFromActions(current);
    if (!verbs || verbs.affirm === null) return; // no affirm verb served for this item
    // A heavy affirm's FIRST swipe reveals the confirm stage (does not commit).
    if (isHeavyVerb(verbs, 'affirm') && !(confirming?.id === current.id && confirming.verdict === 'affirm')) {
      setConfirming({ id: current.id, verdict: 'affirm' });
      return;
    }
    commit('affirm', verbs.affirm, current);
  }, [current, confirming, commit]);

  // The hold-selector's commit. Deliberately NOT routed through `affirm` (which
  // reads the suggested verb) and deliberately identical in shape to it: same
  // `commit`, same verdict, same undo window. Choices are the wire's own — the
  // guard drops any verb the item's served group does not carry, so a stale
  // sheet can no-op but can never post an unoffered verb. Heavy weights don't
  // arise here (a co-equal choice is light by the pattern's contract — the
  // selector IS the deliberate step), so there is no arm stage on this path.
  const affirmWith = useCallback(
    (verb: string) => {
      if (!current) return;
      const choices = holdChoicesFor(current, 'affirm');
      if (!choices || !choices.some((c) => c.verb === verb)) return;
      commit('affirm', verb, current);
    },
    [current, commit],
  );

  const confirmHeavy = useCallback(() => {
    if (!current || confirming?.id !== current.id) return;
    const verbs = verbsFromActions(current);
    if (!verbs) return;
    // The armed DIRECTION decides what commits. Reading `affirm` here
    // unconditionally — as this did while only affirm could arm — would make a
    // confirm tap on an armed REJECT fire the opposite verb, which on an
    // attribution card is the difference between agreeing with a record and
    // cutting a section out of it.
    if (confirming.verdict === 'reject') {
      if (verbs.reject === null) return;
      commit('reject', verbs.reject, current);
      return;
    }
    if (verbs.affirm === null) return;
    commit('affirm', verbs.affirm, current);
  }, [current, confirming, commit]);

  const cancelHeavy = useCallback(() => setConfirming(null), []);

  const reject = useCallback(() => {
    if (!current) return;
    const verbs = verbsFromActions(current);
    if (!verbs) return;
    // A rejectDefers lane (the slot candidate) has no backend decline path in
    // slots v1, so the LEFT gesture SETS THE CARD ASIDE for the session (no
    // POST) rather than declining it; it may resurface at the next sync.
    //
    // It rides the same client-side mechanism as an unbacked snooze but is a
    // DIFFERENT verdict, because Skip and Snooze mean opposite things and the
    // ruling behind #14 forbids one control meaning both. Keeping them apart
    // here is what lets the toast and the staged list name each honestly.
    if (verbs.rejectDefers) {
      commit('skip', null, current);
      return;
    }
    if (verbs.reject === null) return; // no reject action (e.g. pending)
    // A heavy REJECT arms exactly as a heavy affirm does. This is the gap that
    // let an attribution reject — which strips the marked section out of the
    // record body — commit on a single left swipe.
    if (isHeavyVerb(verbs, 'reject') && !(confirming?.id === current.id && confirming.verdict === 'reject')) {
      setConfirming({ id: current.id, verdict: 'reject' });
      return;
    }
    commit('reject', verbs.reject, current);
  }, [current, confirming, commit]);

  // ↑ — the one defer verb (#14). A full swipe passes no argument and gets the
  // indefinite rung (the old Park); the hold-band menu passes a duration.
  //
  // Routed through the SAME delayed-act machinery as every other swipe rather
  // than awaited like re-tier: a defer is knowable client-side (the card goes
  // away either way), and the 3.5s undo window is worth more here than an
  // awaited confirmation — an operator who flicks ↑ by accident gets it back
  // without a round trip, and the POST is cancelled before it ever fires.
  //
  // `snoozeActionFor` returns null on a kind with no backend snooze verb, and a
  // null actionId is exactly today's no-POST set-aside. So the gesture is
  // uniform and the PERSISTENCE is honest per-kind, with no branch here.
  const snooze = useCallback(
    (action: SnoozeAction = SNOOZE_INDEFINITE_ACTION) => {
      if (!current) return;
      commit('snooze', snoozeActionFor(current, action), current);
    },
    [current, commit],
  );

  const undo = useCallback(() => {
    const p = pendingRef.current;
    if (!p) return;
    clearTimer();
    pendingRef.current = null; // cancel the deferred POST — never an un-act
    if (p.restoreSnooze) {
      setSnoozed((prev) => prev.filter((it) => it.id !== p.item.id));
      onUnsnoozePersist?.(p.item.id);
    }
    setIndex(p.restoreIndex);
    setConfirming(null);
    setToast(null);
  }, [onUnsnoozePersist]);

  // Deal a snoozed card back into the deck: drop it from the snooze set (client + persist)
  // and re-append it to the queue tail so it's dealable immediately. The clone carries a
  // fresh render-key (__deckKey) so a re-dealt id can never collide in the visible stack
  // (deal → re-snooze → deal-again is legal); id is preserved so POST/persist stay correct.
  //
  // When the snooze REACHED the backend, dealing it back must un-reach it —
  // otherwise the card returns to this session's deck while the store still says
  // "snoozed", and the board hides it again on the next sync. That divergence is
  // the whole failure class the delayed-act design avoids elsewhere.
  //
  // Two cases, and the first is why this isn't an unconditional POST: if the
  // snooze is still SITTING IN THE UNDO WINDOW, nothing has been written yet, so
  // cancelling the pending act is both correct and cheaper than POSTing an
  // unsnooze against a row the server has never heard of (which answers
  // `already_acted` and reads as an error in the log).
  const dealNow = useCallback(
    (target: FeedItem) => {
      const p = pendingRef.current;
      const pendingThisSnooze =
        p !== null && p.item.id === target.id && p.verdict === 'snooze' && p.actionId !== null;
      if (pendingThisSnooze) {
        clearTimer();
        pendingRef.current = null; // the snooze never happened — nothing to undo
        setToast(null);
      } else if (snoozeIsBacked(target)) {
        flushPending(); // any OTHER deferred act fires first, in order
        feedApi.act(target.id, UNSNOOZE_ACTION).catch(routeError);
      }
      setSnoozed((prev) => prev.filter((it) => it.id !== target.id));
      onUnsnoozePersist?.(target.id);
      const seq = readdSeqRef.current++;
      setReadded((prev) => [...prev, { ...target, __deckKey: `readd-${seq}` } as FeedItem]);
    },
    [flushPending, onUnsnoozePersist, routeError],
  );

  // Re-tier the current email card (#28): POST the chosen tier action_id, AWAIT it, and
  // flip the card off the deck ONLY on `acted` — no optimistic advance, nothing "greens"
  // before the backend confirms the correction. A pending swipe act flushes first
  // (ordering). `acted` → advance + honest no-undo toast; already-moved-on → advance
  // (stale); ApiError → routeError; any other status → keep the card + honest detail.
  // Awaitable so the caller can close the picker once the act resolves.
  const reTier = useCallback(
    async (tierId: string) => {
      const item = current;
      if (!item || reTiering) return;
      flushPending(); // fire any deferred swipe act BEFORE the re-tier (in order)
      setReTiering(tierId);
      let res: FeedActResult;
      try {
        res = await feedApi.act(item.id, tierId);
      } catch (e) {
        setReTiering(null);
        routeError(e);
        return;
      }
      setReTiering(null);
      if (res.ok && res.status === 'acted') {
        setIndex((i) => i + 1); // flip-on-acted: leaves the deck like any other act
        setToast({ message: `Re-tiered to ${tierId.toUpperCase()}.`, canUndo: false });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else if (res.status === 'already_acted' || res.status === 'stale_item') {
        setIndex((i) => i + 1); // already moved on server-side → remove the stale card
        setToast({ message: "That one had already moved on — it'll resurface at the next sync.", canUndo: false });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else {
        // invalid_action / error / unknown → keep the card, honest toast (retry possible).
        setToast({ message: res.detail || `Couldn't re-tier (${res.status}).`, canUndo: false });
      }
    },
    [current, reTiering, flushPending, routeError],
  );

  // Reject the current routine_match card AND say what it meant (#13). `target` is
  // the routine item the operator picked; `null` is the one-off door ("nothing").
  //
  // AWAITED, never optimistic — deliberately unlike a swipe. A swipe's outcome is
  // knowable client-side; a correction's is not: only the server can say whether
  // the pick is still a live routine item, and it refuses the WHOLE verdict if it
  // isn't. Advancing first would show the operator a card that left the deck
  // having taught nothing. On refusal the card stays and the toast carries the
  // server's own words, so a retry is possible from the same screen.
  const correctRoutine = useCallback(
    async (target: string | null) => {
      const item = current;
      if (!item || correcting) return;
      if (item.kind !== ROUTINE_MATCH_KIND) return;
      const chosen = (target || '').trim();
      // A `correct` with an empty pick is refused server-side; don't spend a
      // round trip to be told so. The one-off door is the explicit null.
      if (target !== null && !chosen) return;
      flushPending(); // fire any deferred swipe act BEFORE this one (in order)
      setCorrecting(target === null ? ONE_OFF_ACTION : chosen);
      let res: FeedActResult;
      try {
        res = await feedApi.act(
          item.id,
          target === null ? ONE_OFF_ACTION : CORRECT_ACTION,
          target === null ? undefined : chosen,
        );
      } catch (e) {
        setCorrecting(null);
        routeError(e);
        return;
      }
      setCorrecting(null);
      if (res.ok && res.status === 'acted') {
        setIndex((i) => i + 1);
        setToast({
          message: target === null ? 'Noted — a one-off.' : `Noted — it means “${chosen}”.`,
          canUndo: false,
        });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else if (res.status === 'already_acted' || res.status === 'stale_item') {
        setIndex((i) => i + 1);
        setToast({ message: "That one had already moved on — it'll resurface at the next sync.", canUndo: false });
        clearTimer();
        timerRef.current = setTimeout(() => setToast(null), UNDO_MS);
      } else {
        // Refused (an unpickable item, a vault the server can't read) → KEEP the
        // card and show what the server said. Never a cheerful green over a
        // verdict that didn't land.
        setToast({ message: res.detail || `Couldn't record that (${res.status}).`, canUndo: false });
      }
    },
    [current, correcting, flushPending, routeError],
  );

  const dismissToast = useCallback(() => setToast(null), []);

  // The ONE control on the notice. It clears the list AND the marks on the cards,
  // because a single control that means one thing beats two that the operator has
  // to tell apart mid-burst — and because a "hide" that kept the ledger would
  // strand any entry whose card is no longer in the served batch, which is
  // exactly the entry with no other way to be settled. The cards themselves stay
  // in the deck either way; acknowledging is reading, not deciding.
  const acknowledgeUnrecorded = useCallback(() => {
    setUnrecorded(clearAllUnrecorded());
  }, []);

  const remaining = Math.max(0, queue.length - index);
  const upcoming = queue.slice(index + 1, index + 3);
  const ahead = queue.slice(index + 1);
  const cleared = index >= queue.length;

  return {
    current,
    upcoming,
    ahead,
    remaining,
    snoozed,
    snoozedCount: snoozed.length,
    confirmingId,
    confirmingVerdict: confirming?.verdict ?? null,
    toast,
    banner,
    unrecorded,
    unrecordedIds: new Set(unrecorded.map((u) => u.id)),
    acknowledgeUnrecorded,
    cleared,
    affirm,
    affirmWith,
    reject,
    snooze,
    confirmHeavy,
    cancelHeavy,
    undo,
    dealNow,
    reTiering,
    reTier,
    correcting,
    correctRoutine,
    dismissToast,
  };
}
