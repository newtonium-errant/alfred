import type { FeedItem } from './feed';

// A feed item that needs a decision (routed to the deck), vs a glance/FYI item.
// Prefer the attention tier, falling back to mode for older items. ONE definition
// shared by the board (needs-you / FYI split) and the push notifier (what the
// doorbell rings for) so the two can never drift.
export function isNeedsYouItem(item: FeedItem): boolean {
  return item.attention === 'needs_you' || item.mode === 'decide';
}
