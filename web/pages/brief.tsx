import type { GetServerSideProps } from 'next';

/**
 * `/brief` is RETIRED — the player replaces it (console-completion arc,
 * operator-ratified 2026-08-14).
 *
 * The route survives only as this redirect. Keeping the URL alive is the point:
 * it is in the operator's muscle memory, it may sit in a bookmark or an old push
 * payload, and a 404 would punish them for a decision they did not make.
 *
 * WHAT MOVED, AND WHY THE MOVE WAS NOT JUST A REDIRECT. This page was the only
 * surface in the PWA that fetched `kind=daily_sync`, and the only full-text
 * render of EITHER artifact — home fetches the brief but reads only its `date`
 * to label a summary card, and never touches `markdown`. So retiring the page
 * without moving what it rendered would have made both the Morning Brief and
 * the Daily Sync unreachable. Both now render on `/player` below the narration,
 * which is what "the player replaces it" has to mean: the player inherits what
 * this page CARRIED, not merely its URL.
 *
 * `getServerSideProps` rather than a `next.config.js` rule — it is the only
 * pure-redirect precedent in this tree (`pages/auth/callback.tsx`), and it keeps
 * the decision in the file whose retirement it documents. `permanent: false`
 * matches that precedent and leaves the ruling reversible without fighting a
 * cached 308 in every browser that ever saw it.
 *
 * Re-introduction is pinned by `tests/briefRetired.test.ts`.
 */
export const getServerSideProps: GetServerSideProps = async () => ({
  redirect: { destination: '/player', permanent: false },
});

// Never rendered — `getServerSideProps` redirects on every request. Next still
// requires a default export for the route to exist at all.
export default function BriefRetired() {
  return null;
}
