import type { FeedItem } from './feed';

// SERVER-ONLY (read at poll time). The push-ELIGIBILITY policy — which of the
// needs-you feed items the doorbell may ring for.
//
// THE DEFAULT IS `needs_you` — operator ruling, and a deliberate reversal of #27
// slice 3's strictest-default. That default shipped when nothing had rung yet and
// the risk worth managing was a doorbell that cried wolf. The ruling that
// replaced it: reminders, urgent email, and anything dealt needs-you/decide
// RING; routine cards wait silently. The needs-you set is already the "this
// needs a person" set, so gating inside it was suppressing exactly the items the
// operator asked to be interrupted for.
//
// NARROWING is now the config flip, and the same door works both ways —
// set `PUSH_POLICY`:
//   * unset / "needs_you"        → any needs-you item (default, the ruling)
//   * "email_urgent_all"         → any email_urgent (LLM + override highs)
//   * "email_urgent_override"    → override-highs only (#27's original strictest)
//
// This gates WHETHER the doorbell rings, never WHAT the push says: the payload is
// title + kind + url regardless (the lock-screen privacy pin in pushPayload.ts).
// The poller already restricts to needs-you items (fetchNeedsYouItems), so this
// predicate only ever narrows within that set.
//
// THE WIDENING BURST IS NOW A DEPLOY EVENT, not a config event. Every needs-you
// item that is already open and was suppressed under the old default becomes
// eligible at once, so the first poll after this ships rings for all of them.
// That is correct rather than a bug, but the bound is open needs-you count
// MULTIPLIED BY the subscription count — one push per item per subscription —
// which this comment first stated as the item count alone and thereby halved at
// today's subs=2. The number matters precisely because it is what someone reads
// before deciding whether to deploy into a full queue, and it lands unannounced
// on a restart rather than when somebody types a flag.

export type PushPolicy = 'email_urgent_override' | 'email_urgent_all' | 'needs_you';

/** The configured policy (default = `needs_you`, per the operator ruling).
 *
 * An unrecognized value falls back to the DEFAULT, which now means a typo widens
 * rather than narrows. That is the honest reading of the ruling — the operator
 * asked to be interrupted by needs-you work, so the failure a mistyped flag
 * should produce is "you were told about something" rather than a doorbell that
 * silently went quiet. Silence is the failure mode nobody notices. */
export function readPushPolicy(): PushPolicy {
  const v = (process.env.PUSH_POLICY || '').trim();
  if (v === 'email_urgent_all') return 'email_urgent_all';
  if (v === 'email_urgent_override') return 'email_urgent_override';
  return 'needs_you';
}

/** Whether the doorbell may ring for `item` under `policy`. Items reaching here
 * are already needs-you (the poller fetches only those), so this only narrows. */
export function isPushEligible(item: FeedItem, policy: PushPolicy): boolean {
  switch (policy) {
    case 'needs_you':
      return true;
    case 'email_urgent_all':
      return item.kind === 'email_urgent';
    case 'email_urgent_override':
    default:
      return item.kind === 'email_urgent' && item.evidence?.['high_source'] === 'override';
  }
}
