import { useCallback, useMemo, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { CONTEST_ACTION } from '../../lib/algernon/feedConstants';
import { isNeedsYouItem } from '../../lib/algernon/feedNeedsYou';
import { ApiError } from '../../lib/algernon/http';

// The Awareness feed's board state: split the open items into a needs-you group
// (decisions — routed to the deck) above an FYI group (glance items with an Ack).
// Ack is an IMMEDIATE act (feedApi.act id, "ack") with an OPTIMISTIC remove —
// the row is dropped on tap and restored if the POST fails. Error routing mirrors
// the deck: 401 → onAuthExpired; 502/503 → banner (server-side, never a logout);
// stale/timeout → a benign toast + restore.
//
// #63a adds `contest` on the same optimistic shape, in the opposite direction: an
// acked row LEAVES the board, a contested one CROSSES it (FYI → needs-you). Both
// revert on failure, because the operator must never be shown a verdict that
// didn't land.

export interface FeedBoardToast {
  message: string;
}

export interface UseFeedBoardOptions {
  items: FeedItem[];
  onAuthExpired?: () => void;
}

export interface UseFeedBoardResult {
  needsYou: FeedItem[];
  fyi: FeedItem[];
  toast: FeedBoardToast | null;
  banner: string | null;
  ack: (id: string) => void;
  /** #63a — "not right": contest an attribution inference (FYI → needs-you). */
  contest: (id: string) => void;
  dismissToast: () => void;
}

// isNeedsYou moved to lib/algernon/feedNeedsYou.ts (shared with the push
// notifier — one source of truth for "what needs a decision").

export function useFeedBoard(opts: UseFeedBoardOptions): UseFeedBoardResult {
  const { items, onAuthExpired } = opts;
  // Locally-acked ids (optimistic remove); starts empty each mount.
  const [ackedIds, setAckedIds] = useState<ReadonlySet<string>>(() => new Set());
  // Locally-contested ids (optimistic PROMOTE to needs-you). Server truth catches
  // up at the next fetch — the producer re-derives the tier from the vault entry's
  // own contested flag — so this only has to hold for the current render.
  const [contestedIds, setContestedIds] = useState<ReadonlySet<string>>(() => new Set());
  const [toast, setToast] = useState<FeedBoardToast | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const visible = useMemo(() => items.filter((it) => !ackedIds.has(it.id)), [items, ackedIds]);
  const isNeedsYou = useCallback(
    (it: FeedItem) => isNeedsYouItem(it) || contestedIds.has(it.id),
    [contestedIds],
  );
  const needsYou = useMemo(() => visible.filter(isNeedsYou), [visible, isNeedsYou]);
  const fyi = useMemo(() => visible.filter((it) => !isNeedsYou(it)), [visible, isNeedsYou]);

  const restore = useCallback((id: string) => {
    setAckedIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const uncontest = useCallback((id: string) => {
    setContestedIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const ack = useCallback(
    (id: string) => {
      // Optimistic remove first.
      setAckedIds((prev) => new Set(prev).add(id));
      feedApi.act(id, 'ack').catch((e: unknown) => {
        if (e instanceof ApiError) {
          if (e.status === 401) {
            onAuthExpired?.();
            return; // leave the redirect to the caller; don't restore mid-nav
          }
          if (e.status === 502 || e.status === 503 || e.code === 'feed_upstream_unavailable' || e.code === 'transport_unreachable' || e.code === 'not_configured') {
            setBanner("The feed can't reach Algernon right now — this is a server-side issue, not your session.");
            restore(id);
            return;
          }
          if (e.status === 409 || e.code === 'stale_item') {
            // Already gone at the source — the optimistic remove was right. Keep it.
            return;
          }
        }
        // Any other failure: restore the row so the operator can retry.
        setToast({ message: "Couldn't acknowledge that — it's back; try again." });
        restore(id);
      });
    },
    [onAuthExpired, restore],
  );

  const contest = useCallback(
    (id: string) => {
      // Optimistic PROMOTE — the row crosses from FYI to needs-you on tap.
      setContestedIds((prev) => new Set(prev).add(id));
      feedApi.act(id, CONTEST_ACTION).catch((e: unknown) => {
        if (e instanceof ApiError) {
          if (e.status === 401) {
            onAuthExpired?.();
            return; // leave the redirect to the caller; don't revert mid-nav
          }
          if (e.status === 502 || e.status === 503 || e.code === 'feed_upstream_unavailable' || e.code === 'transport_unreachable' || e.code === 'not_configured') {
            setBanner("The feed can't reach Algernon right now — this is a server-side issue, not your session.");
            uncontest(id);
            return;
          }
          if (e.status === 409 || e.code === 'stale_item') {
            // The batch moved on. The contest did NOT land, so the row goes back
            // — unlike an ack, where "already gone at the source" means the
            // optimistic removal was right. Nothing recorded the disagreement
            // here, and pretending otherwise would bury the correction signal.
            setToast({ message: "That one moved on before the contest landed — it'll be back at the next sync." });
            uncontest(id);
            return;
          }
        }
        setToast({ message: "Couldn't flag that one — it's back under awareness; try again." });
        uncontest(id);
      });
    },
    [onAuthExpired, uncontest],
  );

  const dismissToast = useCallback(() => setToast(null), []);

  return { needsYou, fyi, toast, banner, ack, contest, dismissToast };
}
