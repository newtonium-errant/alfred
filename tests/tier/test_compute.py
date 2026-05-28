"""Tests for ``alfred.tier.compute`` — tier projection over task frontmatter.

Boundary cases per dispatch:
- No due date
- Due in the past (overdue — max escalation regardless of window)
- Exactly at the escalation window boundary
- ``escalate_to`` absent (default to ``max(1, base_tier - 1)``)
- ``escalate_at_days`` absent (opt-in — no escalation)
- ``base_tier`` absent — priority-derivation fallback
- Both absent — default to T3
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import structlog

from alfred.tier.compute import (
    DEFAULT_ESCALATION_GAP,
    OPEN_STATUSES,
    PRIORITY_TO_BASE_TIER,
    TierResult,
    compute_effective_tier,
    derive_base_tier_from_priority,
)


# Reference instant — 2026-05-28 13:00 UTC. Tests pass deterministic
# ``now`` to keep day-boundary math reproducible.
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# derive_base_tier_from_priority
# ---------------------------------------------------------------------------


def test_derive_priority_urgent_maps_to_t1() -> None:
    assert derive_base_tier_from_priority("urgent") == 1


def test_derive_priority_high_maps_to_t2() -> None:
    assert derive_base_tier_from_priority("high") == 2


def test_derive_priority_medium_maps_to_t2() -> None:
    assert derive_base_tier_from_priority("medium") == 2


def test_derive_priority_low_maps_to_t3() -> None:
    assert derive_base_tier_from_priority("low") == 3


def test_derive_priority_case_insensitive() -> None:
    assert derive_base_tier_from_priority("Urgent") == 1
    assert derive_base_tier_from_priority("HIGH") == 2


def test_derive_priority_unknown_returns_none() -> None:
    assert derive_base_tier_from_priority("critical") is None
    assert derive_base_tier_from_priority("") is None
    assert derive_base_tier_from_priority(None) is None


def test_derive_priority_non_string_returns_none() -> None:
    assert derive_base_tier_from_priority(1) is None
    assert derive_base_tier_from_priority(["urgent"]) is None


# ---------------------------------------------------------------------------
# compute_effective_tier — base_tier resolution
# ---------------------------------------------------------------------------


def test_base_tier_set_explicit_takes_precedence() -> None:
    """When ``base_tier`` is set, ``priority`` is ignored for derivation."""
    fm = {"base_tier": 2, "priority": "urgent"}
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 2
    assert result.effective_tier == 2
    assert "no due date" in result.reason


def test_base_tier_string_int_coerced() -> None:
    """Operators sometimes write ``base_tier: '2'`` — accept it."""
    fm = {"base_tier": "1"}
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 1


def test_base_tier_out_of_range_falls_back_to_priority() -> None:
    """``base_tier: 5`` is invalid — fall back to priority derivation."""
    fm = {"base_tier": 5, "priority": "low"}
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 3
    assert "from priority" in result.reason


def test_base_tier_missing_uses_priority_fallback() -> None:
    """No ``base_tier`` — derive from ``priority``."""
    fm = {"priority": "urgent"}
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 1
    assert "from priority" in result.reason


def test_base_tier_and_priority_missing_defaults_to_t3() -> None:
    """No ``base_tier``, no ``priority`` — default to T3."""
    fm: dict = {}
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 3
    assert "default" in result.reason


# ---------------------------------------------------------------------------
# compute_effective_tier — no due / no escalation
# ---------------------------------------------------------------------------


def test_no_due_returns_base_tier() -> None:
    fm = {"base_tier": 2}
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2
    assert "no due date" in result.reason


def test_due_set_but_no_escalate_at_days_returns_base() -> None:
    """Opt-in: ``escalate_at_days`` absent means no escalation fires."""
    fm = {"base_tier": 2, "due": "2026-05-30"}
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2
    assert "escalation not configured" in result.reason


# ---------------------------------------------------------------------------
# compute_effective_tier — escalation window
# ---------------------------------------------------------------------------


def test_inside_escalation_window_escalates() -> None:
    """Due in 2 days, window is 3 — escalation fires."""
    fm = {
        "base_tier": 2,
        "due": "2026-05-30",
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 2
    assert result.effective_tier == 1  # default escalate_to = base - 1
    assert "escalated" in result.reason
    assert "2d to due" in result.reason


def test_outside_escalation_window_stays_base() -> None:
    """Due in 10 days, window is 3 — no escalation yet."""
    fm = {
        "base_tier": 2,
        "due": "2026-06-07",
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2
    assert "to escalation window" in result.reason


def test_exactly_at_escalation_window_boundary_escalates() -> None:
    """Boundary case: ``days_to_due == escalate_at_days`` → escalates.

    The escalation predicate is ``days_to_due <= escalate_at_days``;
    pinning the boundary to "inclusive" matters for operator intent
    (escalate_at_days=3 means "starts escalating 3 days out, not 2").
    """
    fm = {
        "base_tier": 3,
        "due": "2026-05-31",  # 3 days from NOW
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2  # 3 - 1 = 2
    assert "3d to due" in result.reason


# ---------------------------------------------------------------------------
# compute_effective_tier — past-due (max escalation)
# ---------------------------------------------------------------------------


def test_past_due_always_escalates_regardless_of_window() -> None:
    """Past-due = max escalation. ``escalate_at_days`` is irrelevant."""
    fm = {
        "base_tier": 3,
        "due": "2026-05-27",  # 1 day in the past
        # No escalate_at_days — past-due bypasses the opt-in.
    }
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 3
    assert result.effective_tier == 2  # default escalate_to = base - 1
    assert "overdue" in result.reason
    assert "1d" in result.reason


def test_past_due_with_explicit_escalate_to() -> None:
    """Operator can set ``escalate_to: 1`` to force T1 on overdue from T3."""
    fm = {
        "base_tier": 3,
        "due": "2026-05-20",
        "escalate_to": 1,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1
    assert "overdue 8d" in result.reason


def test_past_due_with_t1_base_stays_t1() -> None:
    """T1 task past-due — escalate_to default is ``max(1, 0) = 1``."""
    fm = {
        "base_tier": 1,
        "due": "2026-05-27",
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


# ---------------------------------------------------------------------------
# compute_effective_tier — escalate_to defaults + clamping
# ---------------------------------------------------------------------------


def test_default_escalate_to_one_tier_up() -> None:
    """``escalate_to`` defaults to ``max(1, base_tier - 1)``."""
    fm = {
        "base_tier": 3,
        "due": "2026-05-28",  # today
        "escalate_at_days": 1,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2  # 3 - 1


def test_default_escalate_to_clamps_at_t1() -> None:
    """T1 task can't escalate higher — clamps to T1."""
    fm = {
        "base_tier": 1,
        "due": "2026-05-28",
        "escalate_at_days": 1,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


def test_explicit_escalate_to_overrides_default() -> None:
    """Operator can specify ``escalate_to: 1`` from T3 (skip T2)."""
    fm = {
        "base_tier": 3,
        "due": "2026-05-29",
        "escalate_at_days": 2,
        "escalate_to": 1,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


# ---------------------------------------------------------------------------
# compute_effective_tier — type coercion
# ---------------------------------------------------------------------------


def test_due_as_date_object() -> None:
    """PyYAML parses ``due: 2026-05-30`` as a ``date`` object."""
    fm = {
        "base_tier": 2,
        "due": date(2026, 5, 30),
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


def test_due_as_datetime_object() -> None:
    """If a datetime slips in, it should normalise to date."""
    fm = {
        "base_tier": 2,
        "due": datetime(2026, 5, 30, 15, 0, 0),
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


def test_due_as_iso_string() -> None:
    fm = {
        "base_tier": 2,
        "due": "2026-05-30",
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


def test_due_as_unparseable_string_treated_as_no_due() -> None:
    """Bad date string → no due → base tier holds."""
    fm = {
        "base_tier": 2,
        "due": "tomorrow",
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2
    assert "no due date" in result.reason


def test_escalate_at_days_as_string() -> None:
    fm = {
        "base_tier": 2,
        "due": "2026-05-29",
        "escalate_at_days": "3",
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


def test_escalate_at_days_unparseable_treated_as_absent() -> None:
    fm = {
        "base_tier": 2,
        "due": "2026-05-29",
        "escalate_at_days": "soonish",
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 2
    assert "escalation not configured" in result.reason


# ---------------------------------------------------------------------------
# Module-level constants — pin the contract surface
# ---------------------------------------------------------------------------


def test_default_escalation_gap_pinned() -> None:
    """Pin the default — if it ever changes from 1, dispatch needs to know."""
    assert DEFAULT_ESCALATION_GAP == 1


def test_open_statuses_includes_blocked() -> None:
    """Ratified 2026-05-28: blocked tasks surface in the queue."""
    assert "blocked" in OPEN_STATUSES
    assert "todo" in OPEN_STATUSES
    assert "active" in OPEN_STATUSES
    assert "done" not in OPEN_STATUSES
    assert "cancelled" not in OPEN_STATUSES


def test_priority_to_base_tier_mapping_pinned() -> None:
    """Pin the priority→tier mapping. Mirrored in talker SKILL via
    prompt-tuner — if this changes, SKILL must update in lockstep."""
    assert PRIORITY_TO_BASE_TIER == {
        "urgent": 1,
        "high": 2,
        "medium": 2,
        "low": 3,
    }


# ---------------------------------------------------------------------------
# TierResult contract
# ---------------------------------------------------------------------------


def test_tier_result_is_namedtuple() -> None:
    r = TierResult(base_tier=2, effective_tier=1, reason="escalated")
    assert r.base_tier == 2
    assert r.effective_tier == 1
    assert r.reason == "escalated"
    # Tuple-unpacking interface is part of the contract.
    base, eff, reason = r
    assert (base, eff, reason) == (2, 1, "escalated")


# ---------------------------------------------------------------------------
# Real-world fixtures from the vault (the cases that triggered the system)
# ---------------------------------------------------------------------------


def test_rrts_payroll_escalation_case() -> None:
    """RRTS Payroll, due 2026-05-28 — would be operator-configured T2
    with escalate_at_days=1 escalating to T1 on the due day."""
    fm = {
        "base_tier": 2,
        "due": "2026-05-28",
        "escalate_at_days": 1,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1
    assert "0d to due" in result.reason


def test_rrts_invoicing_escalation_case() -> None:
    """RRTS Invoicing — operator's brief example: base T2, escalate
    3d before due. Due 2026-05-30 means today (NOW = 2026-05-28) is
    inside the 3-day window."""
    fm = {
        "base_tier": 2,
        "due": "2026-05-30",
        "escalate_at_days": 3,
    }
    result = compute_effective_tier(fm, NOW)
    assert result.effective_tier == 1


def test_reading_standing_t3_no_due() -> None:
    """Reading.md has ``tier: 3`` (will become ``base_tier: 3``) and
    no due date — pure aspirational, never escalates."""
    fm = {"base_tier": 3}
    result = compute_effective_tier(fm, NOW)
    assert result.base_tier == 3
    assert result.effective_tier == 3
    assert "no due date" in result.reason


# ---------------------------------------------------------------------------
# Log emission pins (per builder.md rule #9)
# ---------------------------------------------------------------------------
#
# compute_effective_tier does NOT log — it's a pure function. The log
# emissions live in the brief.tier_section render layer (no_open_tasks,
# parse_failed, invalid_effective_tier). Those are pinned in
# test_brief_tier_section.py.
#
# Reserved here so a future reviewer doesn't add log lines to compute.py
# without a matching test — the contract is "compute is pure; logging
# happens at the IO boundary."


def test_compute_is_pure_no_logs() -> None:
    """Sanity: invoking ``compute_effective_tier`` over many cases
    produces no log emissions. Keeps the IO/pure boundary explicit."""
    with structlog.testing.capture_logs() as captured:
        for fm in [
            {},
            {"base_tier": 1},
            {"base_tier": 2, "due": "2026-05-29", "escalate_at_days": 2},
            {"due": "2026-05-20", "priority": "urgent"},
        ]:
            compute_effective_tier(fm, NOW)
    assert captured == []
