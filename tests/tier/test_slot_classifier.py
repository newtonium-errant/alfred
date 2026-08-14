"""Slot classifier — Duty / Rhythm / Fuel (#18 slice 1: classify + observe).

Every classification pin here drives ``compute_today_view`` and asserts the
STAMPED slot on the resulting entry. None of them call ``classify_slot``
directly with a hand-built object.

That is not stylistic. A pin that constructs its own input is testing its own
copy: it stays green through exactly the change it exists to catch, because the
assertion is fine and the subject is wrong. This arc has already paid for that
lesson twice — a hand-built ``Glossary`` probe that passed while the real loader
carried a stale alias, and a lock-path pin that hand-mirrored production's
composition (with a "verbatim" comment) and stayed green through the very
convergence it was built to detect. The plumbing between a routine record's
``self_care`` field and the classifier's input is the largest thing that can
break here, and only a producer-driven pin can see it.

Fixtures use the operator's REAL routine record names — ``For Self Health``,
``Recurring Bills + Admin`` — for the same reason: a fixture shaped for
convenience tests a vault that does not exist.

STAGE 1 SCOPE. ``balanced_day`` is deliberately unchanged (still tier-based) and
the rings still group by tier; the metric flip is stage 3. The pins assert that
non-change explicitly, because "the classifier ran" and "the classifier ran and
quietly moved the headline number" are the same green without it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog

from alfred.tier import slots
from alfred.tier.compute import compute_today_view

# Thursday 2026-05-28 13:00 UTC — the reference instant the sibling tier tests
# use, so a fixture that surfaces here surfaces there too.
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)
TODAY_ISO = "2026-05-28"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    return vault


def _write_routine(vault: Path, name: str, items_yaml: str) -> None:
    (vault / "routine" / f"{name}.md").write_text(
        f"---\ntype: routine\nname: {name}\nstatus: active\n"
        f"items:\n{items_yaml}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _write_task(vault: Path, name: str, fm_body: str) -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\ntype: task\nname: {name}\n{fm_body}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _entries(view) -> dict[str, object]:
    """Every lane's entries keyed by display name — slot is orthogonal to tier,
    so the pins must not care which lane an item landed in."""
    out = {}
    for lane in (view.t1, view.t2, view.t3):
        for e in lane:
            out[e.name] = e
    return out


def _slot_of(view, name: str) -> str:
    entry = _entries(view).get(name)
    assert entry is not None, (
        f"{name!r} never surfaced; visible: {sorted(_entries(view))}"
    )
    return entry.slot


# ---------------------------------------------------------------------------
# The canonical set — fixed keys, per-user labels
# ---------------------------------------------------------------------------


def test_canonical_slots_are_exactly_three_and_exclude_unslotted() -> None:
    """The set is FIXED; per-user vocabulary renames LABELS only. And
    ``CANONICAL_SLOTS`` is the balanced-day denominator, so ``unslotted``
    being absent from it IS the design's exclusion rule — expressed as set
    membership rather than a filter someone has to remember to apply."""
    assert slots.CANONICAL_SLOTS == ("duty", "rhythm", "fuel")
    assert slots.SLOT_UNSLOTTED not in slots.CANONICAL_SLOTS
    assert slots.SLOT_UNSLOTTED in slots.ALL_SLOT_VALUES


def test_learned_seam_is_required_not_defaulted() -> None:
    """The optional-gate trap, pre-closed. Rule 2's store lands in slice 2; if
    ``learned`` were defaulted, slice 2 could add the store, thread it in tests,
    miss a production call site, and every pin would stay green while the
    learned overrides were never consulted. That is precisely how R3's snooze
    suppression shipped write-live/read-dead."""
    from tests._required_kwarg import (
        MISSING_KWARG_RE,
        assert_required_keyword_only,
    )

    assert_required_keyword_only(slots.classify_slot, "learned")
    with pytest.raises(TypeError, match=MISSING_KWARG_RE):
        slots.classify_slot(object())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Deterministic rules, driven through the producer
# ---------------------------------------------------------------------------


def test_self_care_routine_item_is_fuel(tmp_path: Path) -> None:
    """Rule 3 on the operator's real self-care record."""
    vault = _vault(tmp_path)
    _write_routine(vault, "For Self Health", "  - text: Guitar\n    priority: aspirational\n    self_care: true\n")
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Guitar") == slots.SLOT_FUEL


def test_due_pattern_routine_item_is_duty(tmp_path: Path) -> None:
    """Rule 4 — a recurring hard deadline is a scheduled obligation."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Recurring Bills + Admin",
        "  - text: Pay the power bill\n    priority: critical\n"
        "    due_pattern: {type: weekly, day: thu}\n"
        "    surface_at_days: 3\n    escalate_at_days: 1\n",
    )
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Pay the power bill") == slots.SLOT_DUTY


def test_cadence_routine_item_is_rhythm(tmp_path: Path) -> None:
    """Rule 5 — "at least every N days" is the definition of a practice."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Standing Practices",
        "  - text: Walk the dog\n    priority: aspirational\n"
        "    target_cadence_days: 1\n",
    )
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Walk the dog") == slots.SLOT_RHYTHM


def test_dated_task_is_duty(tmp_path: Path) -> None:
    """Rule 6 — a dated task record is an obligation."""
    vault = _vault(tmp_path)
    _write_task(vault, "File the return", f"status: todo\ndue: {TODAY_ISO}\n")
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "File the return") == slots.SLOT_DUTY


# ---------------------------------------------------------------------------
# Precedence — the bugs that are silent by nature
# ---------------------------------------------------------------------------


def test_self_care_beats_cadence_THE_plumbing_pin(tmp_path: Path) -> None:
    """THE pin for this slice. A self-care item that ALSO carries a cadence
    target must be Fuel, not Rhythm.

    ``compute_auto_t3_candidates`` reads ``item.self_care`` (it passes it to
    ``classify_routine_item``) and, before this slice, dropped it — so the item
    reached the classifier indistinguishable from a plain practice. That is the
    one gap in the arc that yields a WRONG answer rather than an honest
    ``unslotted``, and it lands on exactly the category the feature exists to
    protect. It is invisible to every per-layer unit pin: the rule table is
    right, the classifier is right, and the answer is still wrong because the
    field never arrived.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "  - text: Guitar\n    priority: aspirational\n"
        "    self_care: true\n    target_cadence_days: 3\n",
    )
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Guitar") == slots.SLOT_FUEL


def test_self_care_beats_dated_task(tmp_path: Path) -> None:
    """Rule 3 over rule 6 — a dated self-care task is Fuel that happens to be
    scheduled, which is the whole reason slot is orthogonal to tier."""
    vault = _vault(tmp_path)
    _write_task(
        vault, "Massage appointment",
        f"status: todo\ndue: {TODAY_ISO}\nself_care: true\n",
    )
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Massage appointment") == slots.SLOT_FUEL


def test_explicit_slot_beats_every_structural_rule(tmp_path: Path) -> None:
    """Rule 1 — the operator's own word is final. The item below would classify
    Fuel on structure (self_care) and Duty on its due_pattern; the operator
    says Rhythm, so it is Rhythm."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "  - text: Physio exercises\n    priority: aspirational\n"
        "    self_care: true\n    target_cadence_days: 2\n"
        "    slot: rhythm\n",
    )
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Physio exercises") == slots.SLOT_RHYTHM


def test_unrecognised_explicit_slot_falls_through_not_invents(
    tmp_path: Path,
) -> None:
    """A typo must degrade to the next rule, never become a fourth category.
    Inventing a slot would break the ring geometry and the balanced-day rollup,
    both of which assume exactly three."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "  - text: Guitar\n    priority: aspirational\n"
        "    self_care: true\n    slot: Restorative\n",
    )
    view = compute_today_view(vault, NOW)
    # Falls through rule 1 to rule 3.
    assert _slot_of(view, "Guitar") == slots.SLOT_FUEL


def test_explicit_slot_is_case_and_space_insensitive(tmp_path: Path) -> None:
    """It reads hand-edited frontmatter."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Standing Practices",
        "  - text: Weekly review\n    priority: aspirational\n"
        "    target_cadence_days: 7\n    slot: '  DUTY '\n",
    )
    view = compute_today_view(vault, NOW)
    assert _slot_of(view, "Weekly review") == slots.SLOT_DUTY


# ---------------------------------------------------------------------------
# unslotted — a first-class answer, positively pinned
# ---------------------------------------------------------------------------


def _write_daily_curation(vault: Path, iso: str, fm_body: str) -> None:
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    (vault / "daily" / f"{iso}.md").write_text(
        f"---\n{fm_body}---\n\n# daily\n", encoding="utf-8",
    )


def test_no_signal_item_is_unslotted_not_defaulted(tmp_path: Path) -> None:
    """The honest-residue pin, on the residue's most realistic shape.

    An ad-hoc free-text entry the operator typed into today's plan ("Read for
    an hour") has no record behind it — no ``self_care``, no cadence, no
    ``due_pattern``, no explicit ``slot``. There is genuinely nothing to read,
    which is precisely when a classifier is tempted to guess.

    Asserting ``== "unslotted"`` rather than ``!= "duty"`` is the whole point:
    an item DEFAULTED to Duty and an item correctly classified Duty are
    indistinguishable without a positive assertion, and defaulting is the
    "lying DONE button" disease — a surface showing a confident answer it does
    not have. These entries are also the highest-value correction targets, so
    getting them honestly labelled is what makes the correction loop worth
    building in slice 2.
    """
    vault = _vault(tmp_path)
    _write_daily_curation(
        vault, TODAY_ISO,
        f"type: daily\ndate: '{TODAY_ISO}'\n"
        "tier_curation:\n"
        "  t1: []\n  t2: []\n"
        "  t3:\n"
        "  - item: Read for an hour\n    source: operator-adhoc\n"
        f"  curated_at: '{TODAY_ISO}T07:00:00-03:00'\n",
    )
    view = compute_today_view(vault, NOW)
    entry = _entries(view).get("Read for an hour")
    assert entry is not None, "fixture surfaced nothing — the pin would be vacuous"
    assert entry.slot == slots.SLOT_UNSLOTTED
    assert entry.slot_rule == slots.RULE_NONE
    # And it is reported as residue, not quietly counted as covered.
    assert view.slot_coverage.unslotted >= 1
    assert view.slot_coverage.coverage_pct < 100.0


def test_unslotted_is_excluded_from_the_balanced_day_denominator() -> None:
    """The exclusion is structural, not a filter at the call site.

    Stage 3 flips ``balanced_day`` to one-done-per-slot over
    ``CANONICAL_SLOTS``. Because ``unslotted`` is not a member, an item the
    classifier could not answer for can never make the daily goal unreachable —
    which is the rollout hazard the staging exists to avoid (a silently
    unreachable goal reads as personal failure, not as a migration artifact).
    """
    assert slots.SLOT_UNSLOTTED not in slots.CANONICAL_SLOTS
    verdict = slots.SlotVerdict(slots.SLOT_UNSLOTTED, slots.RULE_NONE)
    assert verdict.is_slotted is False
    for real in slots.CANONICAL_SLOTS:
        assert slots.SlotVerdict(real, slots.RULE_EXPLICIT).is_slotted is True


# ---------------------------------------------------------------------------
# Stage-1 contract — what must NOT have changed
# ---------------------------------------------------------------------------


def test_stage1_does_not_touch_balanced_day(tmp_path: Path) -> None:
    """The staged rollout's whole safety property. Slot is computed and stamped;
    ``balanced_day`` stays the tier-based definition until stage 3.

    Built so the two definitions DISAGREE: one done T1 + one done T2 and nothing
    restorative is ``balanced_day=True`` under tiers and would be False under
    slots. If this ever flips green-to-red on its own, stage 3 landed early.
    """
    vault = _vault(tmp_path)
    _write_task(vault, "File the return", f"status: todo\ndue: {TODAY_ISO}\n")
    _write_routine(
        vault, "Recurring Bills + Admin",
        "  - text: Pay the power bill\n    priority: critical\n"
        "    due_pattern: {type: weekly, day: thu}\n"
        "    surface_at_days: 3\n    escalate_at_days: 1\n",
    )
    view = compute_today_view(vault, NOW)
    goal = view.daily_goal
    # Tier-based fields still present and still driving the metric.
    assert hasattr(goal, "t1_done") and hasattr(goal, "balanced_day")
    assert goal.balanced_day == (
        goal.t1_done >= 1 and goal.t2_done >= 1 and goal.t3_done >= 1
    )


def test_every_stamped_slot_is_a_known_value(tmp_path: Path) -> None:
    """No entry may carry a value outside the vocabulary — the FE's label map
    and the ring geometry both index on it."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "  - text: Guitar\n    priority: aspirational\n    self_care: true\n",
    )
    _write_routine(
        vault, "Standing Practices",
        "  - text: Walk the dog\n    priority: aspirational\n"
        "    target_cadence_days: 1\n",
    )
    _write_task(vault, "File the return", f"status: todo\ndue: {TODAY_ISO}\n")
    view = compute_today_view(vault, NOW)
    entries = list(_entries(view).values())
    assert entries, "fixture surfaced nothing — the pin would be vacuous"
    for e in entries:
        assert e.slot in slots.ALL_SLOT_VALUES
        assert isinstance(e.slot_rule, str) and e.slot_rule


# ---------------------------------------------------------------------------
# Coverage observability — the stage-2 gate number
# ---------------------------------------------------------------------------


def test_coverage_is_logged_even_on_an_empty_day(tmp_path: Path) -> None:
    """Intentionally-left-blank. A projection that classified nothing and a
    classifier that stopped running are indistinguishable without this line —
    and the stage-2 threshold decision is made from exactly this number, so its
    absence would be silent."""
    vault = _vault(tmp_path)
    with structlog.testing.capture_logs() as cap:
        view = compute_today_view(vault, NOW)
    events = [c for c in cap if c.get("event") == "tier.slots.coverage"]
    assert len(events) == 1
    assert events[0]["total"] == 0
    assert events[0]["unslotted"] == 0
    # Empty day is 100%, not 0% — nothing was left unanswered. 0.0 would read
    # as total classifier failure on a day that simply had no items.
    assert events[0]["coverage_pct"] == 100.0
    assert view.slot_coverage.total == 0


def test_coverage_counts_and_reports_the_rule_breakdown(tmp_path: Path) -> None:
    """The per-rule split is what makes the number actionable: "Duty because the
    operator said so" and "Duty because it's a dated task" are different claims
    about how much the classifier is actually inferring."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "  - text: Guitar\n    priority: aspirational\n    self_care: true\n",
    )
    _write_task(vault, "File the return", f"status: todo\ndue: {TODAY_ISO}\n")

    with structlog.testing.capture_logs() as cap:
        view = compute_today_view(vault, NOW)

    cov = view.slot_coverage
    assert cov.total == cov.slotted + cov.unslotted
    assert cov.slotted >= 2
    assert cov.by_slot.get(slots.SLOT_FUEL, 0) >= 1
    assert cov.by_rule.get(slots.RULE_SELF_CARE, 0) >= 1
    assert cov.by_rule.get(slots.RULE_DATED_TASK, 0) >= 1
    events = [c for c in cap if c.get("event") == "tier.slots.coverage"]
    assert len(events) == 1
    assert events[0]["total"] == cov.total
    assert events[0]["coverage_pct"] == cov.coverage_pct


# ---------------------------------------------------------------------------
# The producer contract — the FE's input
# ---------------------------------------------------------------------------


def test_feed_evidence_carries_slot_alongside_tier(tmp_path: Path) -> None:
    """Stage 2 swaps the rings' grouping key to slot. Emitting it from stage 1
    means that swap is an FE-only change — a contract extension that arrives
    with its consumer is one nobody can forget to thread.

    ``tier`` must STILL be present: it is not removed, and the FE's
    completion-semantics matrix still keys on ``tier === 3``.
    """
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "  - text: Guitar\n    priority: aspirational\n    self_care: true\n",
    )
    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem")
    assert items, "no feed items produced — the pin would be vacuous"
    ev = items[0].evidence
    assert ev["slot"] in slots.ALL_SLOT_VALUES
    assert ev["slot_rule"]
    assert "tier" in ev, "tier must survive — slot rides alongside it"


# ---------------------------------------------------------------------------
# Phase 2c+h — operator-vocabulary aliases
# ---------------------------------------------------------------------------


def test_normalize_slot_accepts_routine_as_alias_for_rhythm() -> None:
    """The operator says "routine"; the classifier speaks "rhythm".

    His 2026-08-14 rulings used it as a slot name ("surface as ROUTINE
    slot; ESCALATE to DUTY") and the Phase-1 pass wrote
    ``return_slot: routine`` onto two live records. Without the alias
    those returns land unslotted.
    """
    assert slots.normalize_slot("routine") == slots.SLOT_RHYTHM
    assert slots.normalize_slot("  ROUTINE  ") == slots.SLOT_RHYTHM


def test_normalize_slot_canonical_values_still_win() -> None:
    """Positive control — the alias must not disturb the real keys."""
    assert slots.normalize_slot("duty") == slots.SLOT_DUTY
    assert slots.normalize_slot("rhythm") == slots.SLOT_RHYTHM
    assert slots.normalize_slot("fuel") == slots.SLOT_FUEL


def test_normalize_slot_still_refuses_unslotted_and_typos() -> None:
    """The alias table is not a general escape hatch."""
    assert slots.normalize_slot("unslotted") is None
    assert slots.normalize_slot("rythm") is None
    assert slots.normalize_slot("duti") is None


def test_normalize_slot_logs_unrecognized_value() -> None:
    """A typo must not be indistinguishable from an unset field.

    This function now decides where a returning snooze lands, so a
    silent fall-through would present as "the operator's ruling quietly
    did nothing".
    """
    import structlog

    with structlog.testing.capture_logs() as captured:
        assert slots.normalize_slot("rythm") is None

    matches = [
        c for c in captured
        if c.get("event") == "tier.slots.unrecognized_slot_value"
    ]
    assert len(matches) == 1
    assert matches[0]["value"] == "rythm"
    assert slots.SLOT_RHYTHM in matches[0]["canonical"]


def test_normalize_slot_does_not_log_for_absent_or_blank() -> None:
    """Negative control — "not set" is ordinary, not a warning.

    Without this the log would fill with noise for every unslotted
    record and the real typo signal would be buried.
    """
    import structlog

    with structlog.testing.capture_logs() as captured:
        assert slots.normalize_slot(None) is None
        assert slots.normalize_slot("") is None
        assert slots.normalize_slot("   ") is None
        assert slots.normalize_slot(42) is None

    assert [
        c for c in captured
        if c.get("event") == "tier.slots.unrecognized_slot_value"
    ] == []


# ---------------------------------------------------------------------------
# Phase 2c+h — chase vocabulary, one spelling
# ---------------------------------------------------------------------------


def test_chase_phrase_wording() -> None:
    assert slots.chase_phrase("Carfax") == "chase Carfax"
    assert slots.chase_phrase("  Duncan (Cleveland Insurance)  ") == (
        "chase Duncan (Cleveland Insurance)"
    )


def test_chase_phrase_none_when_nobody_named() -> None:
    """Absent and blank both mean "not waiting on anyone" — neither may
    produce a chase against nobody."""
    assert slots.chase_phrase(None) is None
    assert slots.chase_phrase("") is None
    assert slots.chase_phrase("   ") is None
    assert slots.chase_phrase(42) is None
