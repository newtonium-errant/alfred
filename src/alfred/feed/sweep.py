"""The defer-return sweep's mechanics, shared by every kind that owns one.

WHY THIS EXISTS. A kind whose producer UPSERTS per-item (rather than reconciling
an open set each fire) has no natural moment when a deferred card comes back:
``_revival_suppressed`` is the sole consumer of ``defer_window_open`` and it is
reached from ``reconcile`` alone. Such a kind therefore needs a sweep that
reconciles its cards against THEMSELVES — the store is the only source that
knows which cards exist, because nothing else does.

``transport.returns_feed`` built the first one. This module is the second
consumer arriving, so the mechanics move here rather than being spelled twice:
the live filter, the as-open normalization, the decided-card exclusion, the
per-card belt, and the id ordering are each subtle enough that two copies would
mean one gets fixed and the other does not.

WHAT DELIBERATELY DOES **NOT** MOVE HERE: the ``try_feed_reconcile`` call
itself. That is not squeamishness about one more line — it is a constraint the
class pin imposes. ``tests/feed/test_defer_return_path.py`` verifies a kind's
declared return path by finding that call IN THE NAMED MODULE and resolving its
kind argument; hoisting the call into this helper would make that argument a
parameter, unresolvable, and every kind's declaration would stop being
checkable at once. So each producer keeps its own call with its kind spelled
literally, and takes only the mechanics from here. The pin constrains the
architecture rather than merely describing it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

import structlog

from .model import STATE_DEFERRED, STATE_OPEN, FeedItem
from .store import FeedStore

log = structlog.get_logger(__name__)

#: A per-card hook: return the card to keep it open (refreshed or not), or
#: ``None`` to let it fall out of the open set and be retired by the reconcile.
#: A kind with no external state to check passes nothing.
RefreshHook = Callable[[FeedItem], FeedItem | None]


def as_open(item: FeedItem) -> FeedItem:
    """The card, normalized to an OPEN payload.

    Every item handed to ``reconcile`` is written at the state it carries, so a
    stored ``deferred`` card passed through unchanged would be re-written as
    deferred and the return would never happen. Normalizing here is safe for the
    still-held case too: ``_revival_suppressed`` reads the STORED state, not
    this one, so a card inside its window is suppressed before this payload is
    ever used.
    """
    return replace(
        item, state=STATE_OPEN, deferred_until=None, acted_at=None, acted_action=None,
    )


@dataclass
class SweepSet:
    """What a sweep decided about one kind's cards, before the reconcile."""

    #: The cards to hand ``try_feed_reconcile`` — normalized to open.
    open_items: list[FeedItem] = field(default_factory=list)
    #: ``(feed_id, reason)`` for every card the refresh hook retired.
    retired: list[tuple[str, str]] = field(default_factory=list)
    #: True when the store held no live card of this kind — the caller should
    #: skip the reconcile entirely rather than reconcile an empty set (which
    #: would be a no-op, but a LOGGED one, every tick, forever).
    empty: bool = True


def collect_live_cards(
    store: FeedStore,
    kind: str,
    *,
    refresh: RefreshHook | None = None,
    load_failed_event: str = "feed.sweep.load_failed",
    card_failed_event: str = "feed.sweep.card_verdict_failed",
) -> SweepSet:
    """The live open/deferred cards of ``kind``, prepared for a reconcile.

    TERMINAL CARDS ARE EXCLUDED, and this is the load-bearing line rather than
    a filter for tidiness: ``reconcile`` upserts everything it is handed at
    ``state=open``, so including an ``acted``/``acked``/``expired``/``retired``
    card would revive it on the next sweep, and the one after, forever. That is
    the groundhog bug this codebase has already fixed twice, and it is one
    comprehension away at all times.

    The filter is written as an OPEN/DEFERRED allowlist rather than a terminal
    denylist on purpose, which is why ``retired`` needed no change here when it
    was added: a state nobody has taught this function about is excluded by
    default, so a future one fails toward leaving cards alone instead of toward
    resurrecting them.

    ``refresh`` is the per-kind question — "is this card's subject still in the
    state that justified it?" — and is belted PER CARD: whatever it raises costs
    that one card its refresh (it is HELD, never retired on a fault) and never
    the sweep. A kind with no such question passes no hook and every live card
    is kept.

    THE LOG EVENT NAMES ARE THE CALLER'S, passed in rather than composed from a
    prefix here, and that is not decoration. A logged event name is an operator
    contract — it is what a grep is written against — so lifting shared
    mechanics must not quietly rename the events its callers already emit. Each
    caller keeps the exact strings it shipped with; a new caller picks its own.

    Raises nothing of its own. The caller's own belt still wraps the reconcile.
    """
    try:
        stored = store.load()
    except Exception as exc:  # noqa: BLE001 — a read fault must not wedge a host
        log.warning(
            load_failed_event,
            kind=kind, error=str(exc), error_type=type(exc).__name__,
        )
        return SweepSet()

    live = [
        item for item in stored.values()
        if item.kind == kind and item.state in (STATE_OPEN, STATE_DEFERRED)
    ]
    if not live:
        return SweepSet()

    result = SweepSet(empty=False)
    # ``str(i.id)`` rather than ``i.id``: the fold does not coerce types, so one
    # card carrying a non-string id would make this sort's own comparison raise
    # TypeError — outside the per-card belt below, taking the whole sweep with
    # it. Sorting the rendered key is total over every id shape.
    for item in sorted(live, key=lambda i: str(i.id)):
        if refresh is None:
            result.open_items.append(as_open(item))
            continue
        try:
            verdict = refresh(item)
        except Exception as exc:  # noqa: BLE001 — one bad card, not the sweep
            log.warning(
                card_failed_event,
                kind=kind, feed_id=item.id,
                error=str(exc), error_type=type(exc).__name__,
                detail="card held open — could not be verified this sweep",
            )
            verdict = as_open(item)
        if verdict is not None:
            result.open_items.append(verdict)
    return result
