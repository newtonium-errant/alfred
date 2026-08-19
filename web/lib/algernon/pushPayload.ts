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
  /**
   * OPTIONAL notification body. Absent on a per-item push, where the worker
   * composes "<kind> · needs you" itself; present on a digest, which needs to
   * say something the kind cannot. Still subject to the privacy rule above —
   * whatever is put here must be derivable from title/kind/counts, NEVER from
   * `evidence`.
   */
  body?: string;
  /**
   * OPTIONAL collapse key. Notifications sharing a tag replace one another, so
   * this decides what survives in the tray. Omitted, the worker falls back to
   * the deep link — which is what every push used to key on, meaning they ALL
   * shared `/deck` and quietly overwrote each other. Set it deliberately.
   */
  tag?: string;
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
