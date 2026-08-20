import { useCallback, useRef, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { holdChoicesForVerb } from '../../lib/algernon/feedConstants';
import { ApiError } from '../../lib/algernon/http';
import { RING_ACTION_DONE, RING_ACTION_UNDO, ringItemDone } from '../../lib/algernon/rings';
import {
  ACT_LANDED_MESSAGE,
  ACT_UNCONFIRMED_MESSAGE,
  isInconclusive,
  refusalReason,
  SESSION_EXPIRED_REASON,
  supersede,
  verifyActLanded,
  VERIFIED_NOT_LANDED_REASON,
} from '../../lib/algernon/actConfirm';

// The rings-panel completion state machine (Phase C DONE path). DOM-free + driven
// by a mocked feedApi so the per-lane optimistic flip, the ActResult status map,
// undo, and error routing are unit-pinned; RingsHeader renders on top.
//
// Flow: tap ✓ → the item's button goes BUSY (spinner) while POST /feed/act
// {done} fires. On success (acted / already_acted) the segment + dot flip GREEN
// and the row strikes through — held client-side until the next feed render
// reconciles (done items re-emit done=true / stay acted, so it STAYS green). On
// error the flip reverts and a per-item message shows. Undo is the inverse
// (undo_done), allowed only on a done item. A 401 bubbles to onAuthExpired.

interface Override {
  done: boolean;
  busy: boolean;
  error?: string;
  /** A non-failure explanation — the act landed, the reply did not (#62). */
  notice?: string;
}

export interface UseRingCompletionOptions {
  onAuthExpired?: () => void;
}

/**
 * What became of one act — for a caller that DEFERRED it and must account for
 * it somewhere other than this hook's own per-item error line.
 *
 * The distinction is #62's and it is the only one that matters here: did the
 * server ANSWER? `refused` means it did and said no (a 4xx/5xx, a `ok:false`
 * body, a 401, or an inconclusive failure whose verify found the item still
 * open) — so the act definitely did not land and the operator's decision is
 * unrecorded. `unknown` is the ABSENCE of an answer and must never be dressed as
 * one: a timeout the verify could not resolve may well have committed, and
 * reporting it as a failure is the incident this module exists to prevent,
 * wearing new words.
 */
export type ActOutcome = 'landed' | 'refused' | 'unknown';

export interface ActOutcomeReport {
  outcome: ActOutcome;
  /** WHY it did not stick, for a notice that has to say so. Empty unless refused. */
  reason: string;
}

/**
 * Called once per act, with what became of it.
 *
 * OPTIONAL and per-CALL rather than per-hook, because only a DEFERRED act needs
 * it. The rings panel and the feed rows post while the operator is looking at
 * the row that will show the error; the board's grace window posts from a timer
 * or from an unmount cleanup, where this hook's own state has nowhere to render.
 * A hook-wide option would have made every caller opt in to machinery that only
 * one of them can use.
 */
export type ActOutcomeReporter = (report: ActOutcomeReport) => void;

export interface UseRingCompletionResult {
  /** Effective done-ness (optimistic override, else server truth). */
  effectiveDone: (item: FeedItem) => boolean;
  /** Whether an act is in flight for this item. */
  busy: (id: string) => boolean;
  /** The last error message for this item, or null. */
  errorFor: (id: string) => string | null;
  /** A non-failure notice (the act landed late), or null. */
  noticeFor: (id: string) => string | null;
  /**
   * Complete an item (✓).
   *
   * `onOutcome` is for a caller that deferred the act and must account for it
   * beyond this hook's own error line — see `ActOutcomeReporter`.
   */
  complete: (item: FeedItem, onOutcome?: ActOutcomeReporter) => void;
  /**
   * Complete an item WITH a chosen when-family verb — the ✓-hold selector's
   * commit (backdated completion, 2026-08-20). ONE INTERACTION: same optimistic
   * flip, same status map, same error routing as `complete`, with the chosen
   * rung in place of the plain verb; never a second confirm. The verb must be
   * one of the item's own served when-choices (`holdChoicesForVerb`) — anything
   * else is a no-op, because a POST the wire never offered is a 400 in the
   * operator's hand (the deck's `affirmWith` guard, same reasoning).
   */
  completeWith: (item: FeedItem, verb: string, onOutcome?: ActOutcomeReporter) => void;
  /** Undo a completed item. */
  undo: (item: FeedItem) => void;
  /**
   * Retire overrides that a fresh render has answered (#62).
   *
   * Call with each feed render. An override is a record of "what happened to
   * my tap"; once the server has answered that question, keeping it means
   * preferring a stale opinion over a fact — which is what held a 12-hour-old
   * failure line on screen after the act had committed.
   */
  reconcile: (items: FeedItem[]) => void;
}

// Human message for a failed act — mirrors the deck's routeError taxonomy so the
// board speaks the same language. (401 is handled by the caller, not here.)
function messageFor(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409 || e.code === 'stale_item') {
      return "That one moved on — it'll resurface at the next sync.";
    }
    if (
      e.status === 502 ||
      e.status === 503 ||
      e.code === 'feed_upstream_unavailable' ||
      e.code === 'transport_unreachable' ||
      e.code === 'not_configured'
    ) {
      return "Can't reach Algernon right now — this is server-side, not your session.";
    }
    // #62 gate WARN-3: this branch is LIVE on the verify fall-through, so the
    // incident-path copy renders from here. It was the FOURTH paste of a
    // predicate the module docstring claimed had one owner — the three siblings
    // were converted and this one, the primary hook, was not. Consuming the
    // shared owner is the point of the extraction.
    if (isInconclusive(e)) {
      return ACT_UNCONFIRMED_MESSAGE;
    }
    if (e.status === 400 || e.code === 'invalid_action') {
      // Includes graceful degradation: an OLD router that doesn't know `done` yet
      // answers invalid_action → the completion just isn't available until deploy.
      return 'Completing that isn’t available yet — try again after the next sync.';
    }
    // 422 unsupported_item / error (writer domain refusal) — the resolver's own
    // human line where it has one.
    return e.detail || 'That could not be completed.';
  }
  return 'That action failed.';
}

export function useRingCompletion(opts: UseRingCompletionOptions = {}): UseRingCompletionResult {
  const { onAuthExpired } = opts;
  const [overrides, setOverridesState] = useState<Record<string, Override>>({});
  // A ref mirror so an in-flight act can read the CURRENT effective done to revert
  // to (an undo of an optimistically-completed item must revert to done, not to the
  // item's still-open server state).
  const overridesRef = useRef<Record<string, Override>>({});
  const setOverrides = useCallback((updater: (prev: Record<string, Override>) => Record<string, Override>) => {
    setOverridesState((prev) => {
      const next = updater(prev);
      overridesRef.current = next;
      return next;
    });
  }, []);

  const run = useCallback(
    (item: FeedItem, action: string, optimisticDone: boolean, onOutcome?: ActOutcomeReporter) => {
      const cur = overridesRef.current[item.id];
      const revertDone = cur ? cur.done : ringItemDone(item);
      // Enter busy WITHOUT changing done yet (spinner during flight); the green /
      // amber flip lands on the success response.
      setOverrides((prev) => ({ ...prev, [item.id]: { done: revertDone, busy: true, error: undefined } }));
      // TWO-ARGUMENT `.then(onOk, onErr)`, deliberately, not `.then().catch()`.
      // With a trailing catch, a throw from inside the SUCCESS handler is caught
      // by the failure handler — so an act that LANDED would be reported to
      // `onOutcome` as refused, and the board would ledger a debt for a
      // completion that actually stuck. Splitting the handlers makes that
      // structurally impossible rather than merely unlikely. (The deck's
      // `dispatchAct` is split for the same reason and says so.)
      feedApi.act(item.id, action).then(
        (res) => {
          if (res.ok === false) {
            // A refusal that arrived on a 2xx. The transport maps every current
            // ok=false status to a non-2xx, so this is the defensive half of the
            // same contract rather than a live path — but WITHOUT it the status
            // map below reads an unknown status as `optimisticDone` and paints
            // the row GREEN over a write the server declined. `FeedActResult.ok`
            // is what both sides agree on.
            //
            // `=== false` rather than `!res.ok`: the field is required by the
            // contract, so an absent one is a fixture that predates it, not a
            // refusal — and inventing a debt from a missing field is the
            // direction the ledger explicitly degrades AWAY from.
            const reason = res.detail || res.status;
            setOverrides((prev) => ({
              ...prev,
              [item.id]: { done: revertDone, busy: false, error: res.detail || 'That could not be completed.' },
            }));
            onOutcome?.({ outcome: 'refused', reason });
            return;
          }
          // Success statuses: acted / already_acted (done) or undone (open).
          const nowDone =
            res.status === 'acted' || res.status === 'already_acted'
              ? true
              : res.status === 'undone'
                ? false
                : optimisticDone;
          setOverrides((prev) => ({ ...prev, [item.id]: { done: nowDone, busy: false } }));
          onOutcome?.({ outcome: 'landed', reason: '' });
        },
        async (e: unknown) => {
          if (e instanceof ApiError && e.status === 401) {
            onAuthExpired?.();
            // Clear busy but keep the pre-act state (the page redirects on 401).
            setOverrides((prev) => ({ ...prev, [item.id]: { done: revertDone, busy: false } }));
            // REPORTED AS REFUSED even though the session is ending. The request
            // was rejected before it reached the store, so the act definitely did
            // not land — and a ledger is exactly the thing that can still tell
            // the operator so, after the re-login, when no component from this
            // session survives to. (The deck declines to hand a CARD back here
            // because there is no deck left to hand it to; that reasoning is
            // about the card, not about the debt.)
            onOutcome?.({ outcome: 'refused', reason: SESSION_EXPIRED_REASON });
            return;
          }

          // VERIFY BEFORE DECLARING FAILURE (#62). Only for an INCONCLUSIVE
          // failure — a timeout or a dropped connection, where the act may well
          // have committed and we simply never heard. A 4xx/5xx is a real answer
          // and needs no second opinion.
          let report: ActOutcomeReport = { outcome: 'unknown', reason: '' };
          if (isInconclusive(e)) {
            const outcome = await verifyActLanded(item.id, (fresh) => ringItemDone(fresh) === optimisticDone);
            if (outcome === 'landed') {
              // It worked. Say so, and say why the operator saw a pause — the
              // alternative is telling them their action failed when it did not,
              // which is the incident this whole path exists to prevent.
              setOverrides((prev) => ({
                ...prev,
                [item.id]: { done: optimisticDone, busy: false, notice: ACT_LANDED_MESSAGE },
              }));
              onOutcome?.({ outcome: 'landed', reason: '' });
              return;
            }
            // 'not_landed' and 'unknown' both fall through to the error copy.
            // They are NOT merged into one meaning: the message promises the
            // next sync will reconcile, which is now true either way, and
            // claiming a definite failure we did not observe would be the
            // original bug wearing new words. The REPORT keeps them apart for
            // the same reason — only 'not_landed' is an observation.
            if (outcome === 'not_landed') report = { outcome: 'refused', reason: VERIFIED_NOT_LANDED_REASON };
          } else if (e instanceof ApiError) {
            // The server answered, with a status and often its own words.
            report = { outcome: 'refused', reason: refusalReason(e) };
          }
          // Anything that is not an ApiError never reached the answer/no-answer
          // question at all — it stays 'unknown' rather than being counted as a
          // refusal on the operator's behalf.

          setOverrides((prev) => ({ ...prev, [item.id]: { done: revertDone, busy: false, error: messageFor(e) } }));
          onOutcome?.(report);
        },
      );
    },
    [onAuthExpired, setOverrides],
  );

  const complete = useCallback(
    (item: FeedItem, onOutcome?: ActOutcomeReporter) => run(item, RING_ACTION_DONE, true, onOutcome),
    [run],
  );
  const completeWith = useCallback(
    (item: FeedItem, verb: string, onOutcome?: ActOutcomeReporter) => {
      const choices = holdChoicesForVerb(item, RING_ACTION_DONE);
      if (!choices || !choices.some((c) => c.verb === verb)) return;
      run(item, verb, true, onOutcome);
    },
    [run],
  );
  const undo = useCallback((item: FeedItem) => run(item, RING_ACTION_UNDO, false), [run]);

  const effectiveDone = useCallback(
    (item: FeedItem) => {
      const o = overrides[item.id];
      return o ? o.done : ringItemDone(item);
    },
    [overrides],
  );
  const reconcile = useCallback(
    (items: FeedItem[]) => {
      setOverrides((prev) =>
        // The question an override answers is "is this item done?", so a
        // render whose server state already AGREES with the override has
        // answered it — and a render that disagrees is newer truth. Either
        // way the override has nothing left to add.
        supersede(prev, items, () => true),
      );
    },
    [setOverrides],
  );

  const busy = useCallback((id: string) => overrides[id]?.busy ?? false, [overrides]);
  const errorFor = useCallback((id: string) => overrides[id]?.error ?? null, [overrides]);
  const noticeFor = useCallback((id: string) => overrides[id]?.notice ?? null, [overrides]);

  return { effectiveDone, busy, errorFor, noticeFor, complete, completeWith, undo, reconcile };
}
