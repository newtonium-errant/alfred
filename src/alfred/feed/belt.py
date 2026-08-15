"""The swallowed belt — the feed must be INCAPABLE of breaking sync or brief.

Every producer-side feed write enters via :func:`try_feed_reconcile`, which
catches ALL exceptions, logs ONE structured line, and NEVER propagates. A feed
store bug (disk full, corrupt file, a model regression) must degrade to "no feed
update this fire", not a broken brief or a stalled sync daemon.

ILB (``feedback_intentionally_left_blank.md``): a SUCCESSFUL reconcile logs
``feed.reconcile`` with ``ok=True`` + counts on EVERY run of EVERY kind, so
"feed did nothing this fire" is distinguishable from "feed silently broke".
"""

from __future__ import annotations

import structlog

from .model import FeedItem
from .store import FeedStore

log = structlog.get_logger(__name__)


def try_feed_reconcile(
    store: FeedStore,
    kind: str,
    open_items: list[FeedItem],
    *,
    empty_is_authoritative: bool = False,
) -> dict[str, int] | None:
    """Reconcile ``kind``'s open set through the belt. Returns the counts on
    success, ``None`` on failure — and NEVER raises into the caller.

    ``empty_is_authoritative`` passes straight through to
    :meth:`FeedStore.reconcile` — see there for what the caller is declaring.
    It is a property of the CALLER (does it separate a failed read from a
    genuinely empty one before reaching here?), not of the kind, which is why
    it rides on the call rather than living in a per-kind table."""
    try:
        counts = store.reconcile(
            kind, open_items, empty_is_authoritative=empty_is_authoritative,
        )
    except Exception as exc:  # noqa: BLE001 — the whole point: the feed can't break the producer
        log.warning(
            "feed.reconcile_failed",
            kind=kind,
            open_count=len(open_items),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    log.info(
        "feed.reconcile",
        ok=True,
        kind=kind,
        # The size of the INCOMING open set — what the producer emitted this
        # fire, INCLUDING any items suppressed below. It is not the count of
        # items written, and not the store's resulting open count.
        open=counts["open"],
        acted=counts["acted"],
        # Non-zero only when the breaker refused a wholesale wipe. Emitted
        # always, including 0, for the same ILB reason as ``suppressed``: a
        # kind that stops retiring must be explicable as "refused, and here is
        # the count" rather than looking like the producer went quiet.
        refused=counts.get("refused", 0),
        # Snapshot items whose decision was kept sticky because their content
        # is unchanged (per-kind revival policy). Always emitted, including 0:
        # a card that stops re-appearing must be explicable as "we chose not to
        # revive it" rather than looking like the producer went quiet.
        suppressed=counts["suppressed"],
        # Defer (D2), both directions. Same ILB reasoning as ``suppressed``, and
        # the pair matters more here because a defer is a PROMISE: ``held`` says
        # "parked, still inside its window", ``returned`` says "the window
        # lapsed and it is back". Always emitted, including 0, so an operator
        # can see the promise being kept on a quiet day.
        deferred_held=counts.get("deferred_held", 0),
        defer_returned=counts.get("defer_returned", 0),
    )
    return counts
