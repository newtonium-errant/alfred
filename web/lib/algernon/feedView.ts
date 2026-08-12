// Which VIEW of the Awareness feed is showing. Two presentations of one store —
// never two surfaces, never two verb sets (see pages/feed.tsx).
//
// Pure and DOM-free so the URL contract is unit-pinned: the view rides the query
// string (`/feed?view=timeline`) so a chosen view is linkable, survives a reload,
// and survives an iOS PWA resume — the three places a `useState`-only toggle
// quietly loses it.

export type FeedView = 'list' | 'timeline';

export const FEED_VIEWS: readonly { id: FeedView; label: string; hint: string }[] = [
  { id: 'list', label: 'List', hint: 'Grouped by what needs you' },
  { id: 'timeline', label: 'Timeline', hint: 'Laid out against time' },
] as const;

/** The default when the URL says nothing — the shipped, familiar form. */
export const DEFAULT_FEED_VIEW: FeedView = 'list';

export function isFeedView(raw: unknown): raw is FeedView {
  return raw === 'list' || raw === 'timeline';
}

/**
 * Read the view out of a Next router query value.
 *
 * `router.query` hands back `string | string[] | undefined` — a repeated param
 * (`?view=list&view=timeline`) arrives as an ARRAY, and an unrecognised value
 * arrives as a plain string. Both fall back to the default rather than throwing
 * or rendering nothing: a mistyped URL should show the operator their feed, not
 * an error.
 */
export function feedViewFromQuery(raw: string | string[] | undefined): FeedView {
  const value = Array.isArray(raw) ? raw[0] : raw;
  return isFeedView(value) ? value : DEFAULT_FEED_VIEW;
}

/**
 * The query object for a view. The default view carries NO parameter, so the
 * feed's canonical URL stays `/feed` and only a deliberate choice shows up in
 * the address bar.
 */
export function feedViewQuery(view: FeedView): Record<string, string> {
  return view === DEFAULT_FEED_VIEW ? {} : { view };
}
