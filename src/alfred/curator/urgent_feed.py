"""``email_urgent``'s return path — the reconciler its defer verbs were missing.

WHAT WAS WRONG. #102 folded the generic defer verbs into every non-excluded
decide kind, but a defer is a PROMISE: the item is not judged, only moved, so it
MUST come back. The only mechanism that brings one back is a ``reconcile`` pass
over the kind — ``_revival_suppressed`` is the sole consumer of
``defer_window_open`` and is reached from ``reconcile`` alone — and nothing ever
reconciled ``email_urgent``. The classifier upserts a card per high-priority
email and no producer owns the kind's open set, so a deferred urgent card was
terminal: off every open query, with nothing left that could return it. The
verbs were withdrawn (``DEFER_EXCLUDED_KINDS``) as a fail-safe close; this
module is the fix that lets them come back.

WHY THE SWEEP IS THE WHOLE JOB. An urgent card has no external state that closes
it. A task's card retires when the task is done; an email interrupt is finished
only when the operator says so, and the ack is a store-only write. So there is
nothing for absent-detection to detect: every live card is handed back as open,
the reconcile's ``absent`` set is always empty by construction, and the ONLY
observable effect is the one that was missing — a lapsed defer returning.

Two things this deliberately does NOT do. It does not retire cards whose email
record was deleted: the ack still works on a store-only op, so a card pointing
at a missing note is dismissible rather than stuck, and a second closing path
nobody asked for is how a card starts vanishing for reasons the operator cannot
see. And it does not hold a card longer than its window: the cadence below is
about COST, never about the window, which ``defer_window_open`` alone decides.
"""

from __future__ import annotations

from alfred.feed import FeedEmitHandle, FeedStore, try_feed_reconcile
from alfred.feed.sweep import collect_live_cards

from .utils import get_logger

log = get_logger(__name__)

#: The kind this module owns the return path for. Spelled here as a literal —
#: ``tests/feed/test_defer_return_path.py`` resolves the ``try_feed_reconcile``
#: call below through this module's namespace to verify the declaration in
#: ``DEFER_RETURN_PATH``, so the name must be readable from the source.
URGENT_KIND = "email_urgent"

#: Seconds between sweeps. The curator loop polls every 5s and a sweep folds the
#: whole store, so firing every tick would pay a 5-second price for a question
#: whose finest answer is a DAY (the shortest dated defer rung). One minute is
#: invisible against that and matches the ``rescan_interval`` cadence already in
#: the same loop.
#:
#: NOT config: nothing about this number is per-instance, and a knob invites the
#: idea that it controls when a defer returns. It does not — it controls how
#: often we ask. ``defer_window_open`` decides the answer.
SWEEP_INTERVAL_SECONDS = 60


def sweep_urgent_cards(handle: FeedEmitHandle | None) -> dict[str, int] | None:
    """Return any ``email_urgent`` card whose defer window has lapsed.

    ``None`` when nothing ran — the feed is off, or the store holds no live
    urgent card, which is the ordinary state on most sweeps and deliberately
    silent (a per-tick zero line would bury the sweeps that did something).

    NEVER RAISES. This runs inside the curator's poll loop, and a feed fault
    must not cost the operator their inbox processing — the belt discipline
    ``feed/belt.py`` states for sync and brief, applied to the daemon that hosts
    the classifier.
    """
    if handle is None or not handle.enabled:
        return None
    try:
        store = FeedStore(handle.store_path)
        prepared = collect_live_cards(
            store, URGENT_KIND,
            load_failed_event="curator.urgent_sweep_load_failed",
            card_failed_event="curator.urgent_sweep_card_failed",
        )
        if prepared.empty:
            return None
        # The reconcile call lives HERE, with the kind spelled literally, so the
        # class pin can verify this module really is the return path it is
        # declared to be. See feed/sweep.py's docstring for why the shared
        # helper stops one line short of making this call itself.
        return try_feed_reconcile(store, URGENT_KIND, prepared.open_items)
    except Exception as exc:  # noqa: BLE001 — the feed can never break its host
        log.warning(
            "curator.urgent_sweep_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            detail="no urgent-card return this sweep — the rest of the tick is unaffected",
        )
        return None
