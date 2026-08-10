import { feedApi, type FeedItem } from './feed';
import { ApiError } from './http';

// #62 — the shared act-confirmation mechanism for every feed hook.
//
// ## The incident this exists to prevent
//
// 2026-08-07, 23:43 ADT. The operator tapped ✓ on "RRTS Payroll". The act
// COMMITTED server-side in 30ms — BFF logged upstream=200, the feed store went
// state=acted, the vault routine record was stamped. The phone never saw the
// response (network drop, or the 70s abort during iOS PWA suspension). The
// client caught a timeout and rendered "That didn't confirm in time" with the
// optimistic tick REVERTED. Twelve hours later the resumed PWA still showed the
// item pending under that same red line.
//
// The system did the right thing and told the operator it had failed. That is
// worse than an outage: an outage is honestly reported, this teaches the
// operator to distrust a surface that was working. Trust lost this way is not
// repaid by the next success.
//
// ## Three defects, one root
//
// A network failure was treated as an ACT failure. It is not — it is a failure
// to LEARN THE OUTCOME, and those need different words and different behaviour:
//
//   1. no verification — the client declared failure without ever asking the
//      server what happened;
//   2. the override then MASKED server truth for the page's lifetime, so even a
//      refetch showing `acted` could not correct the display;
//   3. nothing refetched on resume, so a deck reopened hours later rendered
//      last night's DOM as if it were current.
//
// This module owns (1) and the shared vocabulary; `useResumeRefetch` owns (3);
// each hook's override reducer owns (2). They are here rather than copied into
// the four hooks because the four already prove the point — the identical
// timeout branch was pasted into useRingCompletion, useDeck, useSnooze and
// useSlotAccept, which is exactly why one incident implicated all four.

/**
 * Did the request fail WITHOUT a real answer from the server?
 *
 * The load-bearing distinction in this whole module. A 4xx/5xx IS an answer —
 * the server heard us and said no, so the act genuinely did not happen and the
 * error copy is true. A timeout or a dropped connection is NOT an answer: the
 * act may have committed, and declaring failure is a guess presented as a fact.
 *
 * Only this predicate may open the verify path. Widening it to cover statuses
 * that carry a real answer would make the client re-probe after a legitimate
 * refusal, which is how a "no" turns into a retry storm.
 */
export function isInconclusive(e: unknown): boolean {
  if (!(e instanceof ApiError)) return false;
  return (
    e.status === 504 ||
    e.code === 'timeout' ||
    e.code === 'gateway_timeout' ||
    e.code === 'network_error'
  );
}

export type VerifyOutcome = 'landed' | 'not_landed' | 'unknown';

/** Copy for each outcome. One owner, so the four hooks cannot drift apart. */
export const ACT_LANDED_MESSAGE =
  'That landed — the reply got lost on the way back.';

/**
 * Kept from the pre-#62 wording, and it is only now TRUE.
 *
 * It promised reconciliation the client never performed. With the verify above,
 * override supersession, and resume refetch, the sentence describes something
 * that actually happens — so it stays rather than being softened into a hedge.
 */
export const ACT_UNCONFIRMED_MESSAGE =
  "That didn't confirm in time — the next sync will reconcile it.";

/**
 * The verify's OWN request budget, deliberately far below the 70s browser
 * default (#62 gate, WARN-2).
 *
 * The caller is by definition on a bad connection — that is what produced the
 * inconclusive failure in the first place. Inheriting the default meant two
 * attempts could run ~140s on top of the act's own 70s, leaving a spinner up
 * for roughly three and a half minutes on a phone that had just lost network.
 * The docstring below promised a bound "in two directions"; this is the second
 * one, and without it the first (two attempts) bounds only the COUNT.
 *
 * 8s is chosen to be longer than any healthy round trip and shorter than an
 * operator's patience with a spinner. If the network is well enough to answer,
 * it answers inside this; if it is not, waiting longer does not help.
 */
export const VERIFY_TIMEOUT_MS = 8_000;

interface VerifyDeps {
  list: typeof feedApi.list;
}

/**
 * Ask the server what actually happened, ONCE, with one retry.
 *
 * Bounded on purpose and in two directions. A phone that just lost the network
 * is the likeliest caller, so an unbounded probe loop would hammer a dying
 * connection at exactly the wrong moment; and an operator staring at a spinner
 * needs an answer, not eventual consistency. Two attempts, then give up and say
 * so.
 *
 * Returns:
 *   'landed'     — the server shows the act applied. Render success.
 *   'not_landed' — the server shows it did not. The error copy is true.
 *   'unknown'    — we could not find out (both attempts failed, or the item is
 *                  no longer listed). NOT the same as 'not_landed', and the
 *                  caller must not collapse them: claiming failure we did not
 *                  observe is the original bug in a new place.
 *
 * An item MISSING from the list is deliberately 'unknown' rather than
 * 'not_landed'. Acted items normally stay listed as `acted`, so absence means
 * something we do not model — filtered, expired, rolled over — and guessing
 * from it would be the same overreach that caused the incident.
 */
export async function verifyActLanded(
  itemId: string,
  landed: (item: FeedItem) => boolean,
  deps: VerifyDeps = { list: feedApi.list },
): Promise<VerifyOutcome> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const res = await deps.list({}, { timeoutMs: VERIFY_TIMEOUT_MS });
      const item = res.items.find((i) => i.id === itemId);
      if (!item) return 'unknown';
      return landed(item) ? 'landed' : 'not_landed';
    } catch {
      // Fall through to the retry; a verify that throws must never surface as
      // a NEW error to the operator — they asked to complete an item, not to
      // hear about our diagnostic call.
    }
  }
  return 'unknown';
}

/**
 * Should an optimistic override be dropped in favour of fresh server state?
 *
 * Defect (2). An override recorded the answer to "what happened to my tap".
 * Once the server has ANSWERED that question in a later render, the override is
 * a stale opinion sitting on top of a fact — and `effectiveDone` preferring it
 * is what kept a 12-hour-old red line on screen.
 *
 * `resolved` is per-hook because only the hook knows which server state settles
 * its own question (done-ness for rings, snoozed-ness for snooze, and so on).
 * The LIFECYCLE is shared; the predicate is not.
 *
 * Busy overrides are never superseded: an act is still in flight and its answer
 * has not arrived, so a render landing mid-flight must not clear the spinner.
 */
export function supersede<O extends { busy: boolean }>(
  overrides: Record<string, O>,
  items: FeedItem[],
  resolved: (item: FeedItem, override: O) => boolean,
): Record<string, O> {
  let changed = false;
  const next: Record<string, O> = {};
  for (const [id, o] of Object.entries(overrides)) {
    const item = items.find((i) => i.id === id);
    if (!o.busy && item && resolved(item, o)) {
      changed = true;
      continue; // drop it — server truth now speaks for this item
    }
    next[id] = o;
  }
  return changed ? next : overrides;
}
