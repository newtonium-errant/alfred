import type { FeedItem } from './feed';
import type { PushPayload } from './pushPayload';

// THE DIGEST — one doorbell per poll batch instead of one per item.
//
// WHY. The poller sent a separate push for every newly-seen eligible item, and a
// brief fire produces several at once: the operator got 6+ phone buzzes from a
// single sync. Six wakeups is not six times the information — it is one piece of
// information delivered six times, and the cost lands on the one surface that
// interrupts him.
//
// WHAT IS AND IS NOT CHANGING. This is PRESENTATION ONLY. The eligibility gate
// (`pushPolicy.isPushEligible`, default `needs_you` — every needs-you item may
// ring) is untouched: exactly the same items ring, they simply arrive collapsed.
// Nothing here can cause an item to ring that policy excluded, and nothing here
// can silence an item policy admitted.
//
// THE ONE EXCEPTION, RATIFIED. `email_urgent` rings INDIVIDUALLY. A genuinely
// urgent email is the one kind whose whole value is being singled out; folding it
// into "5 things need you" would bury the item the operator most needs to see
// behind a count.

/** The kind that is never folded into a digest (ratified exception).
 *
 * SPELLED HERE rather than imported from `pushPolicy`, which owns the gate this
 * lane must not touch. The two literals are pinned against each other by a drift
 * test that drives `isPushEligible` with this constant — so they cannot diverge
 * without a red, and the policy module keeps its single responsibility. */
export const EMAIL_URGENT_KIND = 'email_urgent';

/** The synthetic kind on a digest payload. Not a feed kind — no item has it. */
export const DIGEST_KIND = 'digest';

/** Where a digest tap lands: the deck, where needs-you work is answered. */
export const DIGEST_URL = '/deck';

/**
 * THE ROLLING TAG, and the ruling behind it.
 *
 * A browser collapses notifications sharing a `tag` — the newer REPLACES the
 * older. The digest deliberately uses one fixed tag so that a second batch
 * arriving while the first is still on screen replaces it rather than stacking:
 * two summaries visible at once invite the operator to add them up, and the
 * older one is stale the moment the newer exists.
 *
 * THAT CHOICE IS WHY `digestPayloadFor` COUNTS ALL OUTSTANDING WORK RATHER THAN
 * ONLY THE NEW ARRIVALS. The two decisions are one decision. A replacing
 * notification must be a complete statement of what is waiting, or the second
 * batch would replace "3 things need you" with "2 things need you" while five
 * were actually outstanding — the collapse would under-report, which is a worse
 * failure than the noise it set out to fix.
 */
export const DIGEST_TAG = 'algernon-digest';

/** Below this, a "digest" is just the item with its title thrown away. */
export const DIGEST_MIN_ITEMS = 2;

/**
 * A PER-ITEM tag for an individually-ringing item.
 *
 * Load-bearing, not cosmetic: the service worker's tag was the deep link, so
 * EVERY needs-you push already shared the tag `/deck` and silently replaced its
 * predecessor in the tray. Under the digest that would mean an urgent email
 * being wiped out by the next digest — the ratified exception defeated by a
 * collapse rule one layer down. Keying on the item id keeps each urgent item its
 * own notification, which is the entire point of exempting it.
 */
export function pushTagFor(item: FeedItem): string {
  return `item:${item.id}`;
}

/**
 * Plural nouns for the digest sentence, in the operator's own words ("4 tasks,
 * 1 health need you").
 *
 * DELIBERATELY NOT `feedConstants.kindLabel`. That map is UI chrome — chips and
 * headings, singular and title-cased — and it is free to change for layout
 * reasons that have nothing to do with a lock screen. Push copy is a separate
 * audience with a separate register, and coupling them would make a chip rename
 * silently rewrite a notification. Uncountable nouns ("health") are listed with
 * no plural on purpose; a generic +'s' would emit "1 healths".
 *
 * An unlisted kind degrades to its underscore-free kind string rather than being
 * dropped — an unnamed item must still be COUNTED, because a digest that
 * silently omits a kind is the under-report this design exists to prevent.
 */
export const PUSH_KIND_NOUNS: Record<string, string> = {
  slot_suggestion: 'tasks',
  email_tier: 'emails',
  email_urgent: 'urgent emails',
  health: 'health',
  proposal: 'proposals',
  recurrence: 'recurrences',
  routine_match: 'routine matches',
  attribution: 'attributions',
  ticket_notice: 'tickets',
  friction: 'friction',
  pattern_surfaced: 'patterns',
  event: 'events',
  weather: 'weather',
  radar: 'radar',
  ops_notable: 'ops notes',
  peer_digest: 'peer digests',
  notegen_readout: 'notes',
  pending: 'pending',
};

export function pushKindNoun(kind: string): string {
  return PUSH_KIND_NOUNS[kind] || kind.replace(/_/g, ' ');
}

/**
 * Split a batch into the items that ring alone and the ones that collapse.
 *
 * Total by construction — every input lands in exactly one bucket, so no item
 * can be dropped by the partition itself.
 */
export function partitionForPush(items: FeedItem[]): {
  individual: FeedItem[];
  digestable: FeedItem[];
} {
  const individual: FeedItem[] = [];
  const digestable: FeedItem[] = [];
  for (const it of items) {
    if (it.kind === EMAIL_URGENT_KIND) individual.push(it);
    else digestable.push(it);
  }
  return { individual, digestable };
}

/**
 * "4 tasks, 1 health" — counts by kind, most-numerous first, kind name breaking
 * ties so the sentence is deterministic for a given set (a wobbling summary
 * would make two identical states look like two different ones).
 */
export function digestBreakdown(items: FeedItem[]): string {
  const counts = new Map<string, number>();
  for (const it of items) counts.set(it.kind, (counts.get(it.kind) ?? 0) + 1);
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([kind, n]) => `${n} ${pushKindNoun(kind)}`)
    .join(', ');
}

/**
 * The digest payload for the FULL outstanding set (see `DIGEST_TAG` for why it
 * is the outstanding set and not the new arrivals).
 *
 * The title carries the whole sentence rather than leaning on the body, because
 * a service worker older than this change renders its own body from `kind` and
 * would show "digest · needs you" underneath. Putting the count in the title
 * keeps the notification honest on a stale worker too — the operator still
 * learns how much is waiting, whichever worker is installed.
 */
export function digestPayloadFor(items: FeedItem[]): PushPayload {
  return {
    title: `${digestBreakdown(items)} need you`,
    kind: DIGEST_KIND,
    url: DIGEST_URL,
    body: 'Tap to open the deck',
    tag: DIGEST_TAG,
  };
}
