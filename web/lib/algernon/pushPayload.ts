import type { FeedItem } from './feed';
import { isNeedsYouItem } from './feedNeedsYou';

// The push payload — LOCK-SCREEN PRIVACY is the hard rule. The body carries ONLY
// the item's title, its kind, and a deep link to the surface. It NEVER carries
// evidence content: the `evidence` dict is display data that can name people or
// quote subjects, and a push shows on a locked screen. Pinned by test
// (pushPayload.test.ts asserts no evidence value can appear in the payload).

export const PUSH_TITLE_MAX_CHARS = 120;

export interface PushPayload {
  title: string;
  kind: string;
  /** Same-origin deep link — /deck for a decision, /feed for a glance item. */
  url: string;
}

/** The deep link for an item: decisions open the deck, everything else the feed. */
export function pushDeepLink(item: FeedItem): string {
  return isNeedsYouItem(item) ? '/deck' : '/feed';
}

/**
 * Build the privacy-safe payload for a feed item. Exactly three keys — title,
 * kind, url — and nothing sourced from `evidence`. Title falls back to the kind
 * (then the platform name) and is length-capped so the payload stays small.
 */
export function pushPayloadFor(item: FeedItem): PushPayload {
  return {
    title: (item.title || item.kind || 'Algernon').slice(0, PUSH_TITLE_MAX_CHARS),
    kind: item.kind,
    url: pushDeepLink(item),
  };
}
