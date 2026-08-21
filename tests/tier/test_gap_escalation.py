"""FUEL-ESCALATION (2026-08-20) — neglect-gap escalation pins.

The operator's model, ratified 2026-08-20: "Fuel and rhythm are equals, and
Duty is mostly for anything that is escalated from either of those" + "Fuel
daily is important. Not adding that fuel three days in a row becomes a more
critical issue."

Mechanism under pin: ``escalate_after_gap_days`` (per-item, sibling of
``warn_after_gap_days``) + the ``routine.tier_defaults.
fuel_escalate_after_gap_days`` config default (explicit ``slot: fuel`` items
only). At gap >= threshold a no-deadline item classifies T1 with
``gap_escalated=True`` and the slot classifier's rule 0 sends it VISITING
Duty; below threshold it keeps its quiet T3/routine-section self.

Discipline per ``tests/tier/test_slot_classifier.py``: every classification
pin drives ``compute_today_view`` (or another production entry point —
aggregator, feed producer) and asserts the STAMPED result; none construct
hand-built entries for the subject under test. Exclusion pins carry their
positive control in the same test (an "excluded" assertion is vacuous until
the same pipeline demonstrably CAN include).

Due-window expectations in the regression pins were fixed by RUNNING the
HEAD classifier (2026-08-20), not derived: garb(thu,esc=1) @ Thu/Wed/Tue →
(1,"due today")/(1,"due tomorrow")/(None,None); rent(monthly d=1, surf=5,
esc=0) @ Sep1/Aug27/Aug26 → (1,"due today")/(2,"surface window (5d before
due)")/(None,None).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from alfred.routine.aggregator import (
    _collect_items_for_today,
    _iter_routine_records,
)
from alfred.routine.config import DuePattern, TierDefaultsConfig
from alfred.tier import slots
from alfred.tier.compute import (
    classify_routine_item,
    compute_auto_t3_candidates,
    compute_self_care_candidates,
    compute_today_view,
)
from alfred.tier.day_plan import build_day_plan

# Thursday 2026-08-20 13:00 UTC — the day the operator stated the model.
NOW = datetime(2026, 8, 20, 13, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 8, 20)

# The ruled default: escalate fuel at a 3-day gap.
FUEL_DEFAULTS = TierDefaultsConfig(fuel_escalate_after_gap_days=3)

REASON_3D = "neglected 3d (escalates at 3d gap)"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    return vault


def _write_routine(vault: Path, name: str, body: str) -> None:
    """``body`` is raw frontmatter YAML below ``type/name/status``."""
    (vault / "routine" / f"{name}.md").write_text(
        f"---\ntype: routine\nname: {name}\nstatus: active\n"
        f"{body}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _fergus_vault(
    tmp_path: Path,
    *,
    last_done: str,
    extra_item_yaml: str = "",
    priority: str = "aspirational",
    slot: str = "fuel",
) -> Path:
    """The operator's exact fuel scenario: Walk Fergus — explicit
    ``slot: fuel``, daily cadence, no per-item escalate field (unless
    ``extra_item_yaml`` adds one)."""
    vault = _vault(tmp_path)
    _write_routine(
        vault,
        "For Self Health",
        "cadence:\n"
        "  type: daily\n"
        "items:\n"
        "  - text: Walk Fergus\n"
        f"    priority: {priority}\n"
        f"    slot: {slot}\n"
        "    target_cadence_days: 1\n"
        f"{extra_item_yaml}"
        "completion_log:\n"
        "  Walk Fergus:\n"
        f"    - {last_done}\n",
    )
    return vault


def _names(lane) -> list[str]:
    return [e.name for e in lane]


def _entry(view, name: str):
    for lane in (view.t1, view.t2, view.t3):
        for e in lane:
            if e.name == name:
                return e
    raise AssertionError(
        f"{name!r} never surfaced in any lane; "
        f"t1={_names(view.t1)} t2={_names(view.t2)} t3={_names(view.t3)}"
    )


# ---------------------------------------------------------------------------
# The operator's exact scenario, both sides — the keystone e2e pin.
# ---------------------------------------------------------------------------


def test_operator_scenario_gap3_escalates_to_t1_duty(tmp_path: Path) -> None:
    """Gap 3 + config default 3 → T1, visiting DUTY, distinct provenance."""
    vault = _fergus_vault(tmp_path, last_done="2026-08-17")  # gap 3
    view = compute_today_view(vault, NOW, FUEL_DEFAULTS)

    assert "Walk Fergus" in _names(view.t1)
    entry = _entry(view, "Walk Fergus")
    assert entry.tier == 1
    assert entry.gap_escalated is True
    assert entry.slot == slots.SLOT_DUTY
    assert entry.slot_rule == slots.RULE_GAP_ESCALATED
    assert entry.source == "auto-gap-escalated"
    assert entry.surface_reason == REASON_3D
    # No due date — a gap escalation is not deadline-anchored.
    assert not entry.due_iso
    # The record's own word is untouched: the visit is an overlay.
    assert entry.explicit_slot == "fuel"

    # EXACTLY ONE LANE: not double-rendered in T3 or the routine
    # complement (the gap-2 sibling test is this assertion's positive
    # control — the same fixture demonstrably CAN reach T3).
    assert "Walk Fergus" not in _names(view.t3)
    assert "Walk Fergus" not in [r.text for r in view.routine_today]

    # And on the BOARD projection: the row lands in the Duty group, not
    # Fuel — "T1-in-DUTY" asserted on the arrangement both renders read.
    plan = build_day_plan(view, rollover=(), is_done=lambda _e: False)
    by_slot = {g.slot: [r.name for r in g.rows] for g in plan.groups}
    assert "Walk Fergus" in by_slot[slots.SLOT_DUTY]
    assert "Walk Fergus" not in by_slot[slots.SLOT_FUEL]


def test_operator_scenario_gap2_stays_quiet_t3_in_fuel(tmp_path: Path) -> None:
    """Gap 2, same config → the quiet T3-in-fuel self (positive control)."""
    vault = _fergus_vault(tmp_path, last_done="2026-08-18")  # gap 2
    view = compute_today_view(vault, NOW, FUEL_DEFAULTS)

    assert "Walk Fergus" not in _names(view.t1)
    assert "Walk Fergus" in _names(view.t3)
    entry = _entry(view, "Walk Fergus")
    assert entry.gap_escalated is False
    assert entry.slot == slots.SLOT_FUEL
    assert entry.slot_rule == slots.RULE_EXPLICIT
    assert entry.source == "auto-cadence-routine"

    plan = build_day_plan(view, rollover=(), is_done=lambda _e: False)
    by_slot = {g.slot: [r.name for r in g.rows] for g in plan.groups}
    assert "Walk Fergus" in by_slot[slots.SLOT_FUEL]
    assert "Walk Fergus" not in by_slot[slots.SLOT_DUTY]


# ---------------------------------------------------------------------------
# Config default scoping — other-instance control, fuel-only reach, override.
# ---------------------------------------------------------------------------


def test_no_config_means_no_escalation(tmp_path: Path) -> None:
    """Absent config (tier_defaults=None) → gap 3 stays T3. The
    other-instance control: an instance that never configures the key
    sees zero behaviour change."""
    vault = _fergus_vault(tmp_path, last_done="2026-08-17")  # gap 3
    view = compute_today_view(vault, NOW, None)
    assert "Walk Fergus" not in _names(view.t1)
    assert "Walk Fergus" in _names(view.t3)  # positive control
    assert _entry(view, "Walk Fergus").slot == slots.SLOT_FUEL


def test_config_default_reaches_only_explicit_fuel(tmp_path: Path) -> None:
    """A non-fuel explicit slot never inherits the fuel default — even at
    a gap far past it."""
    vault = _fergus_vault(
        tmp_path, last_done="2026-08-12", slot="rhythm",  # gap 8
    )
    view = compute_today_view(vault, NOW, FUEL_DEFAULTS)
    assert "Walk Fergus" not in _names(view.t1)
    assert "Walk Fergus" in _names(view.t3)  # positive control
    assert _entry(view, "Walk Fergus").slot == slots.SLOT_RHYTHM


def test_per_item_override_wins_over_config_default(tmp_path: Path) -> None:
    """Item ``escalate_after_gap_days: 5`` + config default 3: gap 3 stays
    quiet, gap 5 escalates — the override owns the threshold entirely."""
    item_field = "    escalate_after_gap_days: 5\n"
    at_gap3 = _fergus_vault(
        tmp_path / "a", last_done="2026-08-17", extra_item_yaml=item_field,
    )
    view3 = compute_today_view(at_gap3, NOW, FUEL_DEFAULTS)
    assert "Walk Fergus" not in _names(view3.t1)
    assert "Walk Fergus" in _names(view3.t3)

    at_gap5 = _fergus_vault(
        tmp_path / "b", last_done="2026-08-15", extra_item_yaml=item_field,
    )
    view5 = compute_today_view(at_gap5, NOW, FUEL_DEFAULTS)
    entry = _entry(view5, "Walk Fergus")
    assert entry.tier == 1
    assert entry.gap_escalated is True
    assert entry.surface_reason == "neglected 5d (escalates at 5d gap)"


def test_per_item_field_needs_no_config(tmp_path: Path) -> None:
    """The per-item field is self-sufficient: no tier_defaults at all,
    ``escalate_after_gap_days: 2`` at gap 2 → T1 Duty."""
    vault = _fergus_vault(
        tmp_path, last_done="2026-08-18",  # gap 2
        extra_item_yaml="    escalate_after_gap_days: 2\n",
    )
    view = compute_today_view(vault, NOW, None)
    entry = _entry(view, "Walk Fergus")
    assert entry.tier == 1
    assert entry.slot == slots.SLOT_DUTY


# ---------------------------------------------------------------------------
# Decided edges: never-completed, aspirational.
# ---------------------------------------------------------------------------


def test_never_completed_never_escalates(tmp_path: Path) -> None:
    """No completion history → no measurable gap → no Duty debut. The
    cadence branch still surfaces it (max overdue) in T3."""
    vault = _vault(tmp_path)
    _write_routine(
        vault,
        "For Self Health",
        "cadence:\n"
        "  type: daily\n"
        "items:\n"
        "  - text: Walk Fergus\n"
        "    priority: aspirational\n"
        "    slot: fuel\n"
        "    target_cadence_days: 1\n",
    )
    view = compute_today_view(vault, NOW, FUEL_DEFAULTS)
    assert "Walk Fergus" not in _names(view.t1)
    assert "Walk Fergus" in _names(view.t3)


def test_aspirational_priority_does_not_block_escalation(tmp_path: Path) -> None:
    """The aspirational skip is about DEADLINE pressure. Fuel items are
    commonly aspirational — the fixture already is (``_fergus_vault``
    default) — so this pins the non-gating explicitly on a second
    priority too."""
    for prio in ("aspirational", "tracked"):
        vault = _fergus_vault(
            tmp_path / prio, last_done="2026-08-17", priority=prio,
        )
        view = compute_today_view(vault, NOW, FUEL_DEFAULTS)
        entry = _entry(view, "Walk Fergus")
        assert entry.tier == 1, f"priority={prio}"
        assert entry.gap_escalated is True, f"priority={prio}"


# ---------------------------------------------------------------------------
# Self-care floor interplay.
# ---------------------------------------------------------------------------


def test_self_care_fuel_item_escalates_past_the_floor(tmp_path: Path) -> None:
    """A ``self_care`` no-cadence fuel item: gap 1 → the T3 daily floor
    (positive control); gap 3 → T1 Duty, and the self-care surface
    excludes it (exactly-one-lane, self-care edition)."""
    def _sc_vault(base: Path, last_done: str) -> Path:
        vault = _vault(base)
        _write_routine(
            vault,
            "For Self Health",
            "cadence:\n"
            "  type: daily\n"
            "items:\n"
            "  - text: Guitar\n"
            "    priority: aspirational\n"
            "    slot: fuel\n"
            "    self_care: true\n"
            "completion_log:\n"
            "  Guitar:\n"
            f"    - {last_done}\n",
        )
        return vault

    floor = _sc_vault(tmp_path / "floor", "2026-08-19")  # gap 1, not today
    view = compute_today_view(floor, NOW, FUEL_DEFAULTS)
    entry = _entry(view, "Guitar")
    assert entry.tier == 3
    assert entry.surface_reason == "self-care"
    assert entry.slot == slots.SLOT_FUEL

    neglected = _sc_vault(tmp_path / "neg", "2026-08-17")  # gap 3
    view2 = compute_today_view(neglected, NOW, FUEL_DEFAULTS)
    entry2 = _entry(view2, "Guitar")
    assert entry2.tier == 1
    assert entry2.gap_escalated is True
    assert entry2.slot == slots.SLOT_DUTY
    assert "Guitar" not in _names(view2.t3)
    # The self-care compute surface itself agrees (same predicate).
    sc = compute_self_care_candidates(neglected, NOW, FUEL_DEFAULTS)
    assert "Guitar" not in [c.name for c in sc]
    sc_floor = compute_self_care_candidates(floor, NOW, FUEL_DEFAULTS)
    assert "Guitar" in [c.name for c in sc_floor]  # positive control


# ---------------------------------------------------------------------------
# The T3 cadence surface sees the SAME decision (exactly-one-lane).
# ---------------------------------------------------------------------------


def test_t3_surface_excludes_escalated_item(tmp_path: Path) -> None:
    escalated = _fergus_vault(tmp_path / "a", last_done="2026-08-17")
    quiet = _fergus_vault(tmp_path / "b", last_done="2026-08-18")
    gone = compute_auto_t3_candidates(escalated, NOW, FUEL_DEFAULTS)
    assert "Walk Fergus" not in [c.item_text for c in gone]
    there = compute_auto_t3_candidates(quiet, NOW, FUEL_DEFAULTS)
    assert "Walk Fergus" in [c.item_text for c in there]  # positive control


# ---------------------------------------------------------------------------
# warn_after_gap_days independence (aggregator render path).
# ---------------------------------------------------------------------------


def test_warn_and_escalate_are_independent_axes(tmp_path: Path) -> None:
    """``warn_after_gap_days`` warns without escalating;
    ``escalate_after_gap_days`` escalates without needing the warn field.

    Driven through the aggregator's production collector — the layer that
    both renders the annotation AND suppresses handed-off items."""
    def _stretch_vault(base: Path, last_done: str, warn: bool) -> Path:
        vault = _vault(base)
        warn_yaml = "    warn_after_gap_days: 2\n" if warn else ""
        _write_routine(
            vault,
            "Core Daily",
            "cadence:\n"
            "  type: daily\n"
            "items:\n"
            "  - text: Stretch Routine\n"
            "    priority: tracked\n"
            f"{warn_yaml}"
            "    escalate_after_gap_days: 5\n"
            "completion_log:\n"
            "  Stretch Routine:\n"
            f"    - {last_done}\n",
        )
        return vault

    # Gap 3: warn threshold (2) crossed, escalate threshold (5) not —
    # the item RENDERS, warning, unescalated.
    vault = _stretch_vault(tmp_path / "warned", "2026-08-17", warn=True)
    items, _, _ = _collect_items_for_today(_iter_routine_records(vault), TODAY)
    by_text = {i["text"]: i for i in items}
    assert "Stretch Routine" in by_text
    assert by_text["Stretch Routine"]["annotation"] == (
        "*(last: 3 days ago — past 2-day threshold)*"
    )

    # Gap 5: escalates (suppressed from the routine render) — with AND
    # without the warn field, proving the axes don't touch.
    for tag, warn in (("w", True), ("nw", False)):
        vault5 = _stretch_vault(tmp_path / f"esc-{tag}", "2026-08-15", warn=warn)
        items5, _, _ = _collect_items_for_today(
            _iter_routine_records(vault5), TODAY,
        )
        assert "Stretch Routine" not in {i["text"] for i in items5}, tag


# ---------------------------------------------------------------------------
# escalate_at_days (due-window) semantics UNTOUCHED — regression pins.
# ---------------------------------------------------------------------------

GARBAGE = {"type": "weekly", "day": "thu"}
RENT = {"type": "monthly", "day": 1}


@pytest.mark.parametrize(
    ("pattern", "esc", "surf", "today", "log_dates", "want"),
    [
        # Garbage-Day shape (config.py:32-47): escalate_at_days=1, weekly.
        (GARBAGE, 1, None, date(2026, 8, 20), ["2026-08-13"], (1, "due today")),
        (GARBAGE, 1, None, date(2026, 8, 19), ["2026-08-13"], (1, "due tomorrow")),
        (GARBAGE, 1, None, date(2026, 8, 18), ["2026-08-13"], (None, None)),
        # Pay-Clinic-Rental shape: surface_at_days=5, escalate_at_days=0.
        (RENT, 0, 5, date(2026, 9, 1), ["2026-08-01"], (1, "due today")),
        (RENT, 0, 5, date(2026, 8, 27), ["2026-08-01"],
         (2, "surface window (5d before due)")),
        (RENT, 0, 5, date(2026, 8, 26), ["2026-08-01"], (None, None)),
    ],
)
def test_due_window_semantics_untouched(
    pattern, esc, surf, today, log_dates, want,
) -> None:
    """The config.py:32-47 window math, byte-for-byte — run bare AND with
    the full gap-escalation surface armed (fuel slot + config default +
    a stale completion gap that WOULD escalate a no-deadline item). A
    deadline item must answer identically in both worlds."""
    due_pattern = DuePattern.from_dict(pattern)
    log = {"X": list(log_dates)}

    bare = classify_routine_item(
        priority=None, due_pattern=due_pattern, surface_at_days=surf,
        escalate_at_days=esc, target_cadence_days=None,
        completion_log=log, item_text="X", today=today,
    )
    assert (bare.tier, bare.reason) == want

    armed = classify_routine_item(
        priority=None, due_pattern=due_pattern, surface_at_days=surf,
        escalate_at_days=esc, target_cadence_days=None,
        completion_log=log, item_text="X", today=today,
        explicit_slot="fuel",
        default_fuel_escalate_after_gap_days=3,
    )
    assert (armed.tier, armed.reason) == want
    assert armed.gap_escalated is False


def test_gap_field_on_deadline_item_is_ignored_and_flagged() -> None:
    """The invalid combo: ``due_pattern`` + ``escalate_after_gap_days``.
    Due-window answer unchanged; conflict flag raised for the aggregator
    to voice."""
    due_pattern = DuePattern.from_dict(GARBAGE)
    c = classify_routine_item(
        priority=None, due_pattern=due_pattern, surface_at_days=None,
        escalate_at_days=1, target_cadence_days=None,
        completion_log={"X": ["2026-08-01"]}, item_text="X",
        today=date(2026, 8, 20),
        escalate_after_gap_days=3,
    )
    assert (c.tier, c.reason) == (1, "due today")
    assert c.gap_escalated is False
    assert c.gap_escalation_conflict is True


def test_aggregator_voices_gap_conflict_warn_once(tmp_path: Path) -> None:
    """The warn lives at the aggregate pass (the no-spam layer), named
    ``routine.item_gap_escalation_with_due_pattern``, with the fields an
    operator greps by."""
    vault = _vault(tmp_path)
    _write_routine(
        vault,
        "Core Daily",
        "cadence:\n"
        "  type: daily\n"
        "items:\n"
        "  - text: Garbage Day\n"
        "    priority: tracked\n"
        "    due_pattern:\n"
        "      type: weekly\n"
        "      day: thu\n"
        "    escalate_at_days: 1\n"
        "    escalate_after_gap_days: 3\n",
    )
    with capture_logs() as captured:
        _collect_items_for_today(_iter_routine_records(vault), TODAY)
    warns = [
        c for c in captured
        if c.get("event") == "routine.item_gap_escalation_with_due_pattern"
    ]
    assert len(warns) == 1
    assert warns[0]["routine_record"] == "Core Daily"
    assert warns[0]["item_text"] == "Garbage Day"
    assert warns[0]["escalate_after_gap_days"] == 3
    # And the due window still decided the day: due-today → handed off T1.
    handoffs = [
        c for c in captured
        if c.get("event") == "routine.aggregator.handed_off_to_tier"
    ]
    assert len(handoffs) == 1 and handoffs[0]["tier"] == 1


# ---------------------------------------------------------------------------
# No-logs invariant — the classifier stays a pure predicate.
# ---------------------------------------------------------------------------


def test_classifier_emits_no_logs_including_gap_paths() -> None:
    """The new branch keeps the contract: silence when escalating, silence
    on the quiet side, and silence for an unrecognised slot value with
    the default armed (the quiet-normalisation path)."""
    with capture_logs() as captured:
        classify_routine_item(
            priority=None, due_pattern=None, surface_at_days=None,
            escalate_at_days=None, target_cadence_days=1,
            completion_log={"X": ["2026-08-17"]}, item_text="X",
            today=TODAY, explicit_slot="fuel",
            default_fuel_escalate_after_gap_days=3,
        )
        classify_routine_item(
            priority=None, due_pattern=None, surface_at_days=None,
            escalate_at_days=None, target_cadence_days=1,
            completion_log={"X": ["2026-08-17"]}, item_text="X",
            today=TODAY, explicit_slot="fuell",  # typo — must stay quiet
            default_fuel_escalate_after_gap_days=3,
        )
    assert captured == []


# ---------------------------------------------------------------------------
# Feed producer — the board card carries the visit.
# ---------------------------------------------------------------------------


def test_feed_card_carries_duty_visit(tmp_path: Path) -> None:
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _fergus_vault(tmp_path, last_done="2026-08-17")  # gap 3
    items = slot_suggestion_feed_items(
        vault, NOW, FUEL_DEFAULTS, instance="salem",
    )
    assert items is not None
    ours = [it for it in items if it.evidence.get("name") == "Walk Fergus"]
    assert len(ours) == 1
    ev = ours[0].evidence
    assert ev["tier"] == 1
    assert ev["slot"] == slots.SLOT_DUTY
    assert ev["slot_rule"] == slots.RULE_GAP_ESCALATED
    assert ev["source"] == "auto-gap-escalated"
    assert ev["candidate"] is True  # auto-surfaced — operator still accepts
    assert ev["confirmed"] is False
    assert ev["backdate_limit_days"] == 0  # no due_pattern → no backdate
    assert ours[0].title == f"T1: Walk Fergus — {REASON_3D}"


# ---------------------------------------------------------------------------
# Accepted-escalation continuity — the curated copy keeps the visit.
# ---------------------------------------------------------------------------


def _write_daily_with_curation(vault: Path) -> None:
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    (vault / "daily" / "2026-08-20.md").write_text(
        "---\n"
        "type: daily\n"
        "date: 2026-08-20\n"
        "tier_curation:\n"
        "  t1:\n"
        "    - routine_item:\n"
        "        record: For Self Health\n"
        "        text: Walk Fergus\n"
        "      source: auto-gap-escalated\n"
        "      confirmed: true\n"
        "  t2: []\n"
        "  t3: []\n"
        "---\n\n# 2026-08-20\n",
        encoding="utf-8",
    )


def test_accepted_escalation_stays_in_duty_while_gap_persists(
    tmp_path: Path,
) -> None:
    """An ACCEPTED escalated item re-projects as curated; the view stamps
    the gap fact onto the curated copy (the backdate-stamp precedent), so
    it stays in Duty mid-neglect instead of snapping home on accept."""
    vault = _fergus_vault(tmp_path, last_done="2026-08-17")  # gap 3
    _write_daily_with_curation(vault)
    view = compute_today_view(vault, NOW, FUEL_DEFAULTS)
    entry = _entry(view, "Walk Fergus")
    assert entry.source == "auto-gap-escalated"  # curated copy won dedup
    assert entry.confirmed is True
    assert entry.gap_escalated is True
    assert entry.slot == slots.SLOT_DUTY
    assert entry.slot_rule == slots.RULE_GAP_ESCALATED
    # One lane only, still.
    assert [e.name for e in view.t1].count("Walk Fergus") == 1
    assert "Walk Fergus" not in _names(view.t3)


def test_accepted_escalation_goes_home_when_gap_closes(tmp_path: Path) -> None:
    """Completion today closes the gap: the curated copy loses the visit
    (``gap_escalated`` restamped False) and goes HOME to Fuel — the slot
    its own record names.

    FLIPPED 2026-08-21, premise reversed rather than deleted. This pin
    previously asserted ``UNSLOTTED`` and said so in its own docstring:
    "the pre-existing ``_hydrate_curated_entries`` gap (task-origin-only
    hydration) ... pinned here as the CURRENT contract, not endorsed as
    the right one." That limitation is now closed — the hydrator reads a
    curated routine row's item out of its record's ``items`` list — so the
    assertion that recorded it would otherwise have the suite DEFENDING
    the bug. The reversed premise is the whole point of keeping the test:
    "goes home when the gap closes" is only a meaningful claim once there
    is a home to go to."""
    vault = _fergus_vault(tmp_path, last_done="2026-08-20")  # done today
    _write_daily_with_curation(vault)
    view = compute_today_view(vault, NOW, FUEL_DEFAULTS)
    entry = _entry(view, "Walk Fergus")
    assert entry.confirmed is True
    assert entry.gap_escalated is False
    assert entry.slot != slots.SLOT_DUTY  # the visit is over
    assert entry.slot == slots.SLOT_FUEL  # ...and it went home, not nowhere
    assert entry.slot_rule == slots.RULE_EXPLICIT
    # Positive control for every "not in routine_today" above: the
    # complement path CAN see this record — done-today, unescalated,
    # the item renders as a routine line (in Fuel, by its own word).
    lines = {r.text: r for r in view.routine_today}
    assert "Walk Fergus" in lines
    assert lines["Walk Fergus"].slot == slots.SLOT_FUEL
