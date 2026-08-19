"""The sort rotation — which unslotted items get dealt for sorting today.

The 2026-08-14 routing session ratified a deck rotation over the dark backlog
("2-3/day suggestion cards") and it was never built. The operator's 2026-08-19
report is the same gap seen from the other end: four items under "NOT SORTED
YET" with no way to sort them. This module is the selection half of that lane —
:mod:`alfred.tier.sort_writer` is the write half.

## The population is the BOARD'S OWN ANSWER, not a second opinion

``compute_today_view`` already stamps every lane entry with ``slot`` +
``slot_rule`` via :func:`alfred.tier.slots.classify_slot`, and the board renders
its "Not sorted yet" residue by reading exactly that (``board.boardSlotOf`` over
``evidence.slot``). So the population here is ``entry.slot == SLOT_UNSLOTTED``
over the same projection — a READ of the classifier's verdict, never a
re-implementation of its rules. Change a rule in ``slots.py`` and the board and
this rotation move together, because there is only one answer to move.

## Not every unslotted item can be sorted, and the ones that can't are COUNTED

A slot ruling has to live somewhere durable or it is not a ruling. Task-origin
and routine-item-origin entries have a backing record with a ``slot:`` field
(see :mod:`alfred.tier.sort_writer`); a curated FREE-TEXT T3 intention has no
backing record at all. Dealing that shape a card whose verbs could only fail
would rebuild the exact defect this lane exists to remove — an affordance that
does not afford. So it is dropped from the population and reported in the ILB
line instead of being silently absent.

## The cap bounds what is ON SCREEN, not what is minted

And the reason is a mechanism, not a preference. ``FeedStore.reconcile`` treats
DEFERRED as present for absent-detection: any item of this kind missing from the
emitted set is RETIRED. So a deferred sort card omitted to respect the cap would
have its defer window destroyed and would come straight back — the opposite of
what the operator asked for by deferring it.

Hence two bands in :func:`select_rotation`:

  * **held** — every still-unslotted item the store already has parked. Carried
    in the emit unconditionally and OUTSIDE the cap. They cost nothing on
    screen (``_revival_suppressed`` holds them while their window is open) and
    carrying them is the only thing that keeps the window alive.
  * **visible** — up to ``cap`` items, CONTINUING ones before FRESH ones.

Continuing-before-fresh is deliberate: a worklist that reshuffles every morning
never lets the operator finish anything, and the defer verbs are the intended
way to say "not this one". So a card stays until it is sorted or deferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred.tier import slots

log = structlog.get_logger(__name__)

#: The feed kind this rotation deals. Spelled here because three packages have
#: to say it (``brief.feed_producer`` emits, ``daily_sync.action_router`` gates,
#: ``feed.model`` registers) — the ``KIND_REMINDER_RETURNED`` precedent.
SORT_KIND = "sort_suggestion"

#: Cards offered per fire, before the operator has answered any. Config-tunable
#: (``brief.sort_rotation.cap``); 3 matches the ratified "2-3/day" and the
#: board's own ``CANDIDATE_CAP``.
DEFAULT_ROTATION_CAP = 3


def is_sortable(entry: Any) -> bool:
    """Whether a slot ruling on ``entry`` has a durable home to be written to.

    Mirrors the two branches :func:`alfred.tier.sort_writer.assign_slot`
    implements, and deliberately asks the SAME question the writer asks rather
    than a proxy for it: a task needs a ``path``, a routine item needs BOTH a
    ``routine_record`` and an ``item_text``. A curated free-text T3 entry
    carries ``origin="routine_item"`` with neither, which is exactly the shape
    with nowhere to write.
    """
    origin = getattr(entry, "origin", "")
    if origin == "task":
        return bool((getattr(entry, "path", "") or "").strip())
    if origin == "routine_item":
        return bool(
            (getattr(entry, "routine_record", "") or "").strip()
            and (getattr(entry, "item_text", "") or "").strip()
        )
    return False


def unslotted_entries(view: Any) -> tuple[list[Any], list[Any]]:
    """Split a :class:`~alfred.tier.compute.TodayView`'s lanes into
    ``(sortable, unwritable)`` unslotted entries.

    Reads the projection's OWN ``slot`` stamp — see the module docstring on why
    this is a read and not a second classifier. Entries already in a canonical
    slot are not in either list: they have an answer and need no card.
    """
    sortable: list[Any] = []
    unwritable: list[Any] = []
    for entry in (*getattr(view, "t1", []), *getattr(view, "t2", []), *getattr(view, "t3", [])):
        if getattr(entry, "slot", slots.SLOT_UNSLOTTED) != slots.SLOT_UNSLOTTED:
            continue
        (sortable if is_sortable(entry) else unwritable).append(entry)
    return sortable, unwritable


@dataclass
class RotationSelection:
    """What this fire deals, and what it carries without dealing."""

    #: Up to ``cap`` entries the operator sees as cards.
    visible: list[Any] = field(default_factory=list)
    #: Deferred entries carried purely so ``reconcile`` does not retire them.
    held: list[Any] = field(default_factory=list)
    #: Population members past the cap — not emitted this fire.
    withheld: list[Any] = field(default_factory=list)

    @property
    def emitted(self) -> list[Any]:
        """The full open-set to hand ``reconcile``. Held items MUST be in it."""
        return [*self.visible, *self.held]


def select_rotation(
    entries: list[Any],
    tracked: dict[str, str],
    *,
    cap: int = DEFAULT_ROTATION_CAP,
    key_of,
) -> RotationSelection:
    """Choose this fire's cards.

    ``entries`` is the sortable unslotted population. ``tracked`` maps a feed
    item id to its stored state, for THIS kind only. ``key_of`` maps an entry to
    its feed item id (threaded rather than imported so this module stays free of
    the feed package — the producer owns id construction).

    ``cap`` below 1 is clamped to 0, which deals nothing while STILL carrying
    held items: turning the rotation down must never destroy a defer window the
    operator is relying on.
    """
    from alfred.feed.model import STATE_DEFERRED, STATE_OPEN

    limit = max(0, int(cap))
    held: list[Any] = []
    continuing: list[Any] = []
    fresh: list[Any] = []
    for entry in entries:
        state = tracked.get(key_of(entry))
        if state == STATE_DEFERRED:
            held.append(entry)
        elif state == STATE_OPEN:
            continuing.append(entry)
        else:
            # Absent, acked, acted or retired. An ACTED item still in the
            # population means the sort did not take (the write failed, or the
            # ruling did not reach the classifier) — it is still unsorted, so it
            # is still owed a card. Re-dealing it is the honest answer.
            fresh.append(entry)

    def order(items: list[Any]) -> list[Any]:
        # Deterministic and human-sensible: by display name, then by id so two
        # identically-named items keep a fixed order across fires.
        return sorted(items, key=lambda e: (str(getattr(e, "name", "")).lower(), key_of(e)))

    ordered = [*order(continuing), *order(fresh)]
    return RotationSelection(
        visible=ordered[:limit],
        held=order(held),
        withheld=ordered[limit:],
    )


def log_rotation(
    selection: RotationSelection,
    *,
    unwritable: list[Any],
    cap: int,
    instance: str,
) -> None:
    """Emit the rotation signal — ALWAYS, including when nothing was dealt.

    Intentionally-left-blank: a morning with no sort cards has three very
    different causes — nothing is unslotted (the good end state), everything
    unslotted is already parked, or the producer never ran — and without this
    line they are the same silence. ``unwritable`` is reported separately
    because "the classifier could not place it AND nothing can record your
    answer" is the one bucket no verb in this lane can ever clear, so an
    operator asking "why is that row still not sortable?" should find the count
    rather than nothing.
    """
    log.info(
        "tier.sort.rotation",
        instance=instance,
        cap=cap,
        dealt=len(selection.visible),
        held_deferred=len(selection.held),
        withheld_over_cap=len(selection.withheld),
        unwritable=len(unwritable),
        unwritable_names=sorted(
            str(getattr(e, "name", "") or "")[:80] for e in unwritable
        )[:10],
        reason=(
            "unwritable items are unslotted entries with no backing record to "
            "hold a slot ruling (free-text T3 intentions) — they are not dealt "
            "because no verb could apply to them"
        ),
    )


__all__ = [
    "DEFAULT_ROTATION_CAP",
    "SORT_KIND",
    "RotationSelection",
    "is_sortable",
    "log_rotation",
    "select_rotation",
    "unslotted_entries",
]
