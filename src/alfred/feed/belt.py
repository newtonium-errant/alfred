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
) -> dict[str, int] | None:
    """Reconcile ``kind``'s open set through the belt. Returns the counts on
    success, ``None`` on failure — and NEVER raises into the caller."""
    try:
        counts = store.reconcile(kind, open_items)
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
        # Snapshot items whose decision was kept sticky because their content
        # is unchanged (per-kind revival policy). Always emitted, including 0:
        # a card that stops re-appearing must be explicable as "we chose not to
        # revive it" rather than looking like the producer went quiet.
        suppressed=counts["suppressed"],
    )
    return counts
