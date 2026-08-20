"""The sort proposer — the rule table, the shape key, and the learning loop.

The platform standard under test (CLAUDE.md "Self-correcting by design"): the
proposer is a JUDGMENT, so it must capture its correction signal, feed it back,
and keep the loop operator-approved. The capture WRITE from the act path is
pinned in ``tests/feed/test_sort_suggestion_act.py``; this file pins the pure
halves — the table, the fold, and the override arithmetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from alfred.tier import slots
from alfred.tier.sort_proposal import (
    MIN_RULINGS_FOR_OVERRIDE,
    PROPOSE_RULE_DEFAULT,
    PROPOSE_RULE_DUE,
    PROPOSE_RULE_LEARNED,
    PROPOSE_RULE_RECURRING,
    PROPOSE_RULE_T3,
    corrections_path_for,
    heuristic_for_shape,
    heuristic_proposal,
    learned_slot_for,
    learning_summary,
    load_tally,
    propose_slot,
    record_ruling,
    shape_of,
)


@dataclass
class _Entry:
    name: str = "X"
    origin: str = "task"
    path: str = "task/X.md"
    due_iso: str | None = None
    tier: int = 2


# --- the rule table -----------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "slot", "rule"),
    [
        # Rule 1 — a visible deadline reads as an obligation. Reachable only for
        # dated NON-task entries (a dated task is classifier rule 6's, and never
        # reaches the rotation) — pinned on the reachable shape.
        (
            _Entry(origin="routine_item", due_iso="2026-08-21", tier=2),
            slots.SLOT_DUTY,
            PROPOSE_RULE_DUE,
        ),
        # Rule 2 — the T3 lane exists for restoration.
        (_Entry(origin="routine_item", tier=3), slots.SLOT_FUEL, PROPOSE_RULE_T3),
        # Rule 3 — it recurs: the shape of a practice.
        (_Entry(origin="routine_item", tier=2), slots.SLOT_RHYTHM, PROPOSE_RULE_RECURRING),
        # Rule 4 — an undated task the operator curated onto today's list.
        (_Entry(origin="task", tier=2), slots.SLOT_DUTY, PROPOSE_RULE_DEFAULT),
        # Precedence: due beats T3 beats recurring.
        (
            _Entry(origin="routine_item", due_iso="2026-08-21", tier=3),
            slots.SLOT_DUTY,
            PROPOSE_RULE_DUE,
        ),
        (_Entry(origin="task", tier=3), slots.SLOT_FUEL, PROPOSE_RULE_T3),
    ],
)
def test_the_rule_table(entry: _Entry, slot: str, rule: str) -> None:
    p = heuristic_proposal(entry)
    assert (p.slot, p.rule) == (slot, rule)


def test_the_table_is_total_even_on_a_shapeless_entry() -> None:
    """A proposer that can decline would rebuild the classifier's rule 7 one
    layer up — the card must always carry a proposal."""
    p = heuristic_proposal(object())
    assert p.slot in slots.CANONICAL_SLOTS
    assert p.rule == PROPOSE_RULE_DEFAULT


# --- the shape key ------------------------------------------------------------


def test_shape_is_exactly_the_heuristics_discriminators() -> None:
    assert shape_of(_Entry(origin="task", due_iso=None, tier=2)) == "task|due:n|t2"
    assert (
        shape_of(_Entry(origin="routine_item", due_iso="2026-08-21", tier=3))
        == "routine_item|due:y|t3"
    )
    # Degraded fields collapse to marked unknowns rather than crashing.
    assert shape_of(object()) == "unknown|due:n|t?"


@pytest.mark.parametrize(
    "entry",
    [
        _Entry(origin="task", tier=2),
        _Entry(origin="routine_item", tier=3),
        _Entry(origin="routine_item", due_iso="2026-08-21", tier=1),
        _Entry(origin="task", tier=3),
    ],
)
def test_heuristic_for_shape_round_trips_the_live_table(entry: _Entry) -> None:
    """The readout re-derives the displaced answer FROM THE SHAPE KEY alone —
    possible only while the shape stays the heuristic's own input tuple, so the
    round-trip is the pin that keeps them from drifting apart."""
    assert heuristic_for_shape(shape_of(entry)) == heuristic_proposal(entry)


# --- the override arithmetic --------------------------------------------------


def test_no_override_below_the_ruling_threshold() -> None:
    assert learned_slot_for({"fuel": MIN_RULINGS_FOR_OVERRIDE - 1}) is None


def test_a_strict_majority_at_threshold_proposes() -> None:
    assert learned_slot_for({"fuel": 2, "duty": 1}) == "fuel"


def test_a_tie_proposes_nothing() -> None:
    """An override the evidence cannot pick unambiguously is the table's to
    keep — 2/2 is four rulings and no majority."""
    assert learned_slot_for({"fuel": 2, "duty": 2}) is None


def test_a_plurality_short_of_majority_proposes_nothing() -> None:
    assert learned_slot_for({"fuel": 2, "duty": 1, "rhythm": 1}) is None


def test_junk_slots_are_ignored_by_the_fold_and_the_override() -> None:
    assert learned_slot_for({"unslotted": 5, "banana": 4}) is None


def test_propose_slot_prefers_a_learned_override_that_differs() -> None:
    entry = _Entry(origin="task", tier=2)  # heuristic: duty (default rule)
    tally = {shape_of(entry): {"fuel": 3}}
    p = propose_slot(entry, tally)
    assert (p.slot, p.rule) == (slots.SLOT_FUEL, PROPOSE_RULE_LEARNED)


def test_an_override_that_agrees_with_the_table_reports_the_table() -> None:
    """A learned answer equal to the heuristic IS the heuristic — reporting it
    as learned would overstate what the store contributed."""
    entry = _Entry(origin="task", tier=2)
    tally = {shape_of(entry): {"duty": 5}}
    p = propose_slot(entry, tally)
    assert (p.slot, p.rule) == (slots.SLOT_DUTY, PROPOSE_RULE_DEFAULT)


def test_an_empty_tally_is_the_static_table() -> None:
    p = propose_slot(_Entry(origin="routine_item", tier=2), {})
    assert (p.slot, p.rule) == (slots.SLOT_RHYTHM, PROPOSE_RULE_RECURRING)


# --- the store ----------------------------------------------------------------


def test_record_ruling_appends_and_load_tally_folds(tmp_path: Path) -> None:
    p = tmp_path / "sort_corrections.jsonl"
    record_ruling(p, shape="task|due:n|t2", proposed="duty", chosen="duty",
                  proposed_rule=PROPOSE_RULE_DEFAULT, feed_item_id="sort_suggestion:task:x")
    record_ruling(p, shape="task|due:n|t2", proposed="duty", chosen="fuel",
                  proposed_rule=PROPOSE_RULE_DEFAULT)

    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert [r["confirmed"] for r in rows] == [True, False]
    assert rows[0]["chosen"] == "duty" and rows[1]["chosen"] == "fuel"

    assert load_tally(p) == {"task|due:n|t2": {"duty": 1, "fuel": 1}}


def test_load_tally_degrades_on_garbage_never_raises(tmp_path: Path) -> None:
    p = tmp_path / "sort_corrections.jsonl"
    p.write_text(
        'not json\n'
        '{"shape": "task|due:n|t2", "chosen": "unslotted"}\n'  # non-canonical chosen
        '{"chosen": "duty"}\n'  # no shape
        '{"shape": "task|due:n|t2", "chosen": "duty"}\n',
        encoding="utf-8",
    )
    assert load_tally(p) == {"task|due:n|t2": {"duty": 1}}
    assert load_tally(tmp_path / "absent.jsonl") == {}
    assert load_tally(None) == {}


def test_corrections_path_sits_beside_the_feed_store() -> None:
    """One derivation, both writers: instance-scoped because the feed store
    path already is (the 2026-07-31 shared-cwd lesson — never a cwd-relative
    default shared across instances)."""
    assert corrections_path_for("/data/salem/feed.jsonl") == Path(
        "/data/salem/sort_corrections.jsonl"
    )


def test_learning_summary_reports_rate_and_active_overrides(tmp_path: Path) -> None:
    p = tmp_path / "sort_corrections.jsonl"
    # task|due:n|t2 heuristic is duty; three fuel rulings flip it.
    for chosen in ("fuel", "fuel", "fuel", "duty"):
        record_ruling(p, shape="task|due:n|t2", proposed="duty", chosen=chosen)
    s = learning_summary(p)
    assert s["rulings"] == 4
    assert s["corrected"] == 3
    assert s["correction_rate"] == 0.75
    assert s["active_overrides"] == [
        {
            "shape": "task|due:n|t2",
            "proposes": "fuel",
            "displaces": "duty",
            "displaced_rule": PROPOSE_RULE_DEFAULT,
            "rulings": 4,
        }
    ]


def test_learning_summary_on_an_empty_store_says_so(tmp_path: Path) -> None:
    """ILB for the readout: zero rulings renders as an explicit zero, never a
    crash or a missing key."""
    s = learning_summary(tmp_path / "absent.jsonl")
    assert s == {
        "rulings": 0,
        "corrected": 0,
        "correction_rate": 0.0,
        "shapes": {},
        "active_overrides": [],
    }
