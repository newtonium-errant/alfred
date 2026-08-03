import type { FeedItem } from './feed';

// SERVER-ONLY (read at poll time). The push-ELIGIBILITY policy — which of the
// needs-you feed items the doorbell may ring for. #27 slice 3 ships the STRICTEST
// default (operator ruling C): a push fires ONLY for an `email_urgent` item whose
// high tier came from the sender-override list (`evidence.high_source ===
// "override"`), NOT for an LLM-verdict high and NOT for any other needs-you kind.
//
// Widening is an OPERATOR CONFIG FLIP, never a code change — set `PUSH_POLICY`:
//   * unset / "email_urgent_override"  → override-highs only (default, strictest)
//   * "email_urgent_all"               → any email_urgent (LLM + override highs)
//   * "needs_you"                      → any needs-you item (the pre-#27 behavior)
//
// This gates WHETHER the doorbell rings, never WHAT the push says: the payload is
// title + kind + url regardless (the lock-screen privacy pin in pushPayload.ts).
// The poller already restricts to needs-you items (fetchNeedsYouItems), so this
// predicate only ever narrows within that set.
//
// Widening the policy is expected to produce a ONE-TIME burst — the newly-eligible
// items that were already open (previously suppressed) all ring on the next poll. That
// is correct, not a bug, and is bounded by the current open needs-you count.

export type PushPolicy = 'email_urgent_override' | 'email_urgent_all' | 'needs_you';

/** The configured policy (default = strictest). An unrecognized value falls back
 * to the strict default rather than silently widening. */
export function readPushPolicy(): PushPolicy {
  const v = (process.env.PUSH_POLICY || '').trim();
  if (v === 'email_urgent_all') return 'email_urgent_all';
  if (v === 'needs_you') return 'needs_you';
  return 'email_urgent_override';
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
