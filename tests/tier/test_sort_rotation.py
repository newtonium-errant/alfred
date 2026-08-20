"""The sort rotation's selection half — bands, cap, ordering, and the signal.

The design under test is 85aed5a5's recorded brief: population as a READ of the
projection's own ``slot`` stamp, writability as a population filter, the
reconcile-retires-deferred trap answered by carrying every deferred item
OUTSIDE the cap, and empty-fire-authoritative. The producer half (evidence
stamping, reconcile wiring) is pinned in ``tests/brief/test_sort_rotation_feed.py``;
this file pins the pure selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from alfred.feed.model import (
    ATTENTION_FYI,
    KIND_DEFAULTS,
    KIND_SORT_SUGGESTION,
    KINDS,
    MODE_FYI,
    STATE_ACTED,
    STATE_DEFERRED,
    STATE_OPEN,
)
from alfred.tier import slots
from alfred.tier.sort_rotation import (
    DEFAULT_ROTATION_CAP,
    SORT_KIND,
    is_sortable,
    log_rotation,
    select_rotation,
    unslotted_entries,
)


@dataclass
class _Entry:
    name: str
    origin: str = "task"
    path: str = ""
    routine_record: str | None = None
    item_text: str | None = None
    tier: int = 2
    slot: str = slots.SLOT_UNSLOTTED
    slot_rule: str = slots.RULE_NONE


@dataclass
class _View:
    t1: list = field(default_factory=list)
    t2: list = field(default_factory=list)
    t3: list = field(default_factory=list)


def _task(name: str) -> _Entry:
    return _Entry(name=name, origin="task", path=f"task/{name}.md")


def _key(e: _Entry) -> str:
    return f"{SORT_KIND}:task:{e.path}"


# --- the kind's registration --------------------------------------------------


def test_the_kind_is_registered_and_its_spellings_agree() -> None:
    """Three packages spell this kind; the tier module's copy and the feed
    package's copy are pinned equal, and the kind is a real member of KINDS."""
    assert SORT_KIND == KIND_SORT_SUGGESTION == "sort_suggestion"
    assert KIND_SORT_SUGGESTION in KINDS


def test_the_kind_defaults_are_fyi_fyi_the_quiet_pair() -> None:
    """(FYI, FYI) is the measured choice, not a shrug: ``isNeedsYouItem`` is
    ``attention == needs_you OR mode == decide``, the push poller fetches by
    that predicate with no kind allowlist, and the default policy admits
    everything — so MODE_DECIDE rings the phone regardless of attention. A
    backlog-grooming card must never ring it. Downgrading EITHER member of this
    pair to decide/needs_you re-wires the doorbell with every other test green,
    which is why the pair itself is pinned."""
    assert KIND_DEFAULTS[KIND_SORT_SUGGESTION] == (MODE_FYI, ATTENTION_FYI)


# --- writability as a population filter ---------------------------------------


def test_is_sortable_asks_the_writers_own_question() -> None:
    """Mirrors ``sort_writer.assign_slot``'s two branches: a task needs a path,
    a routine item needs BOTH record and text. The curated free-text T3 shape
    (origin routine_item, neither field) is exactly the unwritable residue."""
    assert is_sortable(_task("A")) is True
    assert is_sortable(_Entry(name="B", origin="task", path="")) is False
    assert is_sortable(
        _Entry(name="C", origin="routine_item", routine_record="Bills", item_text="C")
    ) is True
    assert is_sortable(
        _Entry(name="D", origin="routine_item", routine_record=None, item_text=None)
    ) is False
    assert is_sortable(_Entry(name="E", origin="mystery")) is False


def test_unslotted_entries_reads_the_projections_own_stamp() -> None:
    """Population is a READ of ``entry.slot``, never a re-classification — a
    slotted entry is out of both lists whatever its other fields say, and the
    sortable/unwritable split is exactly ``is_sortable``. The slotted DUTY entry
    here is the positive control proving the filter excludes rather than the
    scan being empty."""
    sorted_away = _Entry(name="Done already", origin="task", path="task/x.md", slot=slots.SLOT_DUTY)
    sortable_one = _task("Sortable")
    unwritable_one = _Entry(name="Free text", origin="routine_item")
    view = _View(t1=[sorted_away], t2=[sortable_one], t3=[unwritable_one])

    sortable, unwritable = unslotted_entries(view)

    assert [e.name for e in sortable] == ["Sortable"]
    assert [e.name for e in unwritable] == ["Free text"]


# --- the bands ----------------------------------------------------------------


def test_fresh_entries_are_dealt_up_to_the_cap_and_the_rest_withheld() -> None:
    entries = [_task(n) for n in ("A", "B", "C", "D", "E")]
    sel = select_rotation(entries, {}, cap=3, key_of=_key)
    assert [e.name for e in sel.visible] == ["A", "B", "C"]
    assert [e.name for e in sel.withheld] == ["D", "E"]
    assert sel.held == []
    assert [e.name for e in sel.emitted] == ["A", "B", "C"]


def test_continuing_cards_outrank_fresh_ones() -> None:
    """A worklist that reshuffles every morning never lets the operator finish
    anything — an OPEN card stays dealt ahead of a fresh one."""
    entries = [_task(n) for n in ("A", "B", "C", "D")]
    tracked = {_key(entries[3]): STATE_OPEN}  # D is already on the deck
    sel = select_rotation(entries, tracked, cap=2, key_of=_key)
    assert [e.name for e in sel.visible] == ["D", "A"]
    assert [e.name for e in sel.withheld] == ["B", "C"]


def test_deferred_entries_are_carried_outside_the_cap() -> None:
    """THE RETIREMENT TRAP, answered. ``FeedStore.reconcile`` treats DEFERRED as
    present for absent-detection, so a deferred card omitted from the emit is
    RETIRED and its window destroyed. Every still-in-population deferred entry
    is therefore in ``emitted`` unconditionally and costs no visible slot."""
    entries = [_task(n) for n in ("A", "B", "C", "D")]
    tracked = {_key(entries[0]): STATE_DEFERRED, _key(entries[1]): STATE_DEFERRED}
    sel = select_rotation(entries, tracked, cap=2, key_of=_key)
    assert [e.name for e in sel.held] == ["A", "B"]
    assert [e.name for e in sel.visible] == ["C", "D"]
    # The emit set = visible + held — the reconcile must see BOTH.
    assert {e.name for e in sel.emitted} == {"A", "B", "C", "D"}


def test_cap_zero_is_a_mute_that_still_preserves_defer_windows() -> None:
    """Turning the dial to 0 deals nothing while STILL carrying held items —
    the mute must never destroy a window the operator is relying on. Negative
    caps clamp identically."""
    entries = [_task(n) for n in ("A", "B")]
    tracked = {_key(entries[0]): STATE_DEFERRED}
    for cap in (0, -3):
        sel = select_rotation(entries, tracked, cap=cap, key_of=_key)
        assert sel.visible == []
        assert [e.name for e in sel.held] == ["A"]
        assert [e.name for e in sel.emitted] == ["A"]


def test_an_acted_but_still_unsorted_entry_is_dealt_again() -> None:
    """An ACTED item still in the population means the sort did not take (the
    write failed, or the ruling never reached the classifier) — it is still
    unsorted, so it is still owed a card. Re-dealing is the honest answer."""
    entries = [_task("A")]
    tracked = {_key(entries[0]): STATE_ACTED}
    sel = select_rotation(entries, tracked, cap=3, key_of=_key)
    assert [e.name for e in sel.visible] == ["A"]


def test_ordering_is_deterministic_by_name_then_key() -> None:
    entries = [_task(n) for n in ("b", "A", "a")]
    sel = select_rotation(entries, {}, cap=5, key_of=_key)
    # Case-insensitive by display name, key as tiebreaker → A/a by key, then b.
    assert [e.name for e in sel.visible] == ["A", "a", "b"]


def test_the_default_cap_is_the_ratified_two_to_three_a_day() -> None:
    assert DEFAULT_ROTATION_CAP == 3


# --- the signal (intentionally-left-blank) ------------------------------------


def test_log_rotation_fires_with_the_full_accounting() -> None:
    """The ILB line: dealt / held / withheld / unwritable are all carried, so a
    morning with no cards is attributable to a specific bucket rather than
    being one silence with three causes."""
    entries = [_task(n) for n in ("A", "B", "C", "D")]
    tracked = {_key(entries[0]): STATE_DEFERRED}
    sel = select_rotation(entries, tracked, cap=2, key_of=_key)
    unwritable = [_Entry(name="Free text", origin="routine_item")]

    with structlog.testing.capture_logs() as captured:
        log_rotation(sel, unwritable=unwritable, cap=2, instance="salem")

    matches = [c for c in captured if c.get("event") == "tier.sort.rotation"]
    assert len(matches) == 1
    line = matches[0]
    assert line["instance"] == "salem"
    assert line["cap"] == 2
    assert line["dealt"] == 2
    assert line["held_deferred"] == 1
    assert line["withheld_over_cap"] == 1
    assert line["unwritable"] == 1
    assert line["unwritable_names"] == ["Free text"]


def test_log_rotation_fires_on_a_completely_empty_day_too() -> None:
    """The empty case IS the case the rule exists for — nothing unsorted is the
    rotation's goal state, and it must be distinguishable from a producer that
    never ran."""
    sel = select_rotation([], {}, cap=3, key_of=_key)
    with structlog.testing.capture_logs() as captured:
        log_rotation(sel, unwritable=[], cap=3, instance="salem")
    matches = [c for c in captured if c.get("event") == "tier.sort.rotation"]
    assert len(matches) == 1
    assert matches[0]["dealt"] == 0
    assert matches[0]["unwritable"] == 0
