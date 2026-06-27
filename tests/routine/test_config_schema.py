"""Tests for the Item + DuePattern dataclasses (Phase 2A Ship A).

Pins the parsing contract from YAML-loaded dicts into the typed
dataclasses. Schema-tolerance per the CLAUDE.md load() contract:
unknown keys silently dropped; missing optional fields default to
None / sensible fallback; malformed input returns None rather than
raising (so a single bad item doesn't taint the whole record parse).
"""

from __future__ import annotations

from alfred.routine.config import (
    DUE_PATTERN_TYPES,
    DuePattern,
    Item,
    TierDefaultsConfig,
    load_from_unified,
)


# ---------------------------------------------------------------------------
# DUE_PATTERN_TYPES — frozen contract
# ---------------------------------------------------------------------------


def test_due_pattern_types_pinned() -> None:
    """Ship D SKILL quotes these verbatim — a rename here = lockstep
    update required in the SKILL."""
    assert DUE_PATTERN_TYPES == frozenset({
        "weekly",
        "biweekly",
        "monthly",
        "every_n_days",
        "monthly_nth_weekday",
        "weekly_soft",
    })


# ---------------------------------------------------------------------------
# DuePattern.from_dict
# ---------------------------------------------------------------------------


def test_due_pattern_weekly_minimal() -> None:
    """``type: weekly, day: thu`` → parsed."""
    p = DuePattern.from_dict({"type": "weekly", "day": "thu"})
    assert p is not None
    assert p.type == "weekly"
    assert p.day == "thu"
    assert p.anchor is None
    assert p.soft is False


def test_due_pattern_biweekly_full() -> None:
    """``type: biweekly, day: thu, anchor: 2026-05-28`` → parsed."""
    p = DuePattern.from_dict({
        "type": "biweekly", "day": "thu", "anchor": "2026-05-28",
    })
    assert p is not None
    assert p.type == "biweekly"
    assert p.anchor == "2026-05-28"


def test_due_pattern_monthly_day_int() -> None:
    """``day`` may be int."""
    p = DuePattern.from_dict({"type": "monthly", "day": 1})
    assert p is not None
    assert p.day == 1


def test_due_pattern_monthly_day_last_str() -> None:
    """``day: last`` (string) preserved."""
    p = DuePattern.from_dict({"type": "monthly", "day": "last"})
    assert p is not None
    assert p.day == "last"


def test_due_pattern_every_n_days() -> None:
    """``type: every_n_days, n: 14, anchor: 2026-05-01``."""
    p = DuePattern.from_dict({
        "type": "every_n_days", "n": 14, "anchor": "2026-05-01",
    })
    assert p is not None
    assert p.n == 14
    assert p.anchor == "2026-05-01"


def test_due_pattern_monthly_nth_weekday() -> None:
    """``type: monthly_nth_weekday, n: 1, weekday: tue``."""
    p = DuePattern.from_dict({
        "type": "monthly_nth_weekday", "n": 1, "weekday": "tue",
    })
    assert p is not None
    assert p.n == 1
    assert p.weekday == "tue"


def test_due_pattern_weekly_soft_no_day_needed() -> None:
    """``type: weekly_soft`` — no day required."""
    p = DuePattern.from_dict({"type": "weekly_soft"})
    assert p is not None
    assert p.type == "weekly_soft"
    assert p.day is None


def test_due_pattern_soft_flag_on_weekly() -> None:
    """Backward-compat: ``type: weekly, soft: true`` parses ``soft``
    field. Resolution treats it as weekly_soft."""
    p = DuePattern.from_dict({"type": "weekly", "day": "thu", "soft": True})
    assert p is not None
    assert p.type == "weekly"
    assert p.soft is True


def test_due_pattern_unknown_type_returns_none() -> None:
    """Unknown ``type`` → None (silent drop, defensive)."""
    assert DuePattern.from_dict({"type": "fortnightly"}) is None


def test_due_pattern_missing_type_returns_none() -> None:
    """No ``type`` key → None."""
    assert DuePattern.from_dict({"day": "thu"}) is None


def test_due_pattern_non_dict_returns_none() -> None:
    """``due_pattern: weekly`` (string) → None (defensive)."""
    assert DuePattern.from_dict("weekly") is None
    assert DuePattern.from_dict(None) is None
    assert DuePattern.from_dict([1, 2, 3]) is None


def test_due_pattern_unknown_keys_dropped() -> None:
    """Schema tolerance: unknown keys silently dropped."""
    p = DuePattern.from_dict({
        "type": "weekly", "day": "thu",
        "future_key_from_ship_b": "tolerated",
    })
    assert p is not None
    assert p.type == "weekly"


# ---------------------------------------------------------------------------
# Item.from_dict
# ---------------------------------------------------------------------------


def test_item_minimal_text_and_priority() -> None:
    """Bare item: ``text`` + ``priority``."""
    item = Item.from_dict({"text": "Walk Fergus", "priority": "tracked"})
    assert item is not None
    assert item.text == "Walk Fergus"
    assert item.priority == "tracked"
    assert item.due_pattern is None
    assert item.escalate_at_days is None


def test_item_with_phase_1_fields() -> None:
    """Phase 1 fields (time + warn_after_gap_days) preserved."""
    item = Item.from_dict({
        "text": "Kiki Insulin",
        "priority": "critical",
        "time": "12:00",
        "warn_after_gap_days": 1,
    })
    assert item is not None
    assert item.time == "12:00"
    assert item.warn_after_gap_days == 1


def test_item_with_phase_2a_fields() -> None:
    """Phase 2A fields (due_pattern + surface_at_days + escalate_at_days)."""
    item = Item.from_dict({
        "text": "Pay Clinic Rental",
        "priority": "critical",
        "due_pattern": {"type": "monthly", "day": 1},
        "surface_at_days": 5,
        "escalate_at_days": 0,
    })
    assert item is not None
    assert item.due_pattern is not None
    assert item.due_pattern.type == "monthly"
    assert item.due_pattern.day == 1
    assert item.surface_at_days == 5
    assert item.escalate_at_days == 0


def test_item_self_care_defaults_false() -> None:
    """Q2 (2026-06-26): self_care defaults to False — existing records
    without the field parse unchanged (backward-compat)."""
    item = Item.from_dict({"text": "Pay Rent", "priority": "tracked"})
    assert item is not None
    assert item.self_care is False


def test_item_self_care_bool_true() -> None:
    """self_care: true (PyYAML bool) → True."""
    item = Item.from_dict({
        "text": "Meditate", "priority": "aspirational", "self_care": True,
    })
    assert item is not None
    assert item.self_care is True


def test_item_self_care_string_coercion() -> None:
    """A quoted string form (operator hand-edit) coerces correctly —
    bool('false') is True in Python, so the string path must be explicit.
    """
    assert Item.from_dict(
        {"text": "X", "priority": "tracked", "self_care": "true"}
    ).self_care is True
    assert Item.from_dict(
        {"text": "X", "priority": "tracked", "self_care": "yes"}
    ).self_care is True
    assert Item.from_dict(
        {"text": "X", "priority": "tracked", "self_care": "false"}
    ).self_care is False
    assert Item.from_dict(
        {"text": "X", "priority": "tracked", "self_care": "no"}
    ).self_care is False


def test_item_missing_text_returns_none() -> None:
    """Item without ``text`` → None (can't render or track)."""
    assert Item.from_dict({"priority": "tracked"}) is None
    assert Item.from_dict({"text": "", "priority": "tracked"}) is None
    assert Item.from_dict({"text": "   ", "priority": "tracked"}) is None


def test_item_priority_defaults_to_tracked() -> None:
    """Missing priority → 'tracked' (aggregator's existing fallback)."""
    item = Item.from_dict({"text": "Walk Fergus"})
    assert item is not None
    assert item.priority == "tracked"


def test_item_malformed_due_pattern_silently_dropped() -> None:
    """A malformed ``due_pattern`` becomes None — the rest of the
    item still parses. A single bad pattern doesn't taint the parse."""
    item = Item.from_dict({
        "text": "Pay Clinic Rental",
        "due_pattern": "monthly",  # string instead of dict
        "escalate_at_days": 0,
    })
    assert item is not None
    assert item.due_pattern is None
    assert item.escalate_at_days == 0


def test_item_non_dict_returns_none() -> None:
    """Non-dict input → None (defensive)."""
    assert Item.from_dict(None) is None
    assert Item.from_dict("text") is None
    assert Item.from_dict(123) is None


def test_item_unknown_keys_dropped() -> None:
    """Schema tolerance: future fields silently dropped."""
    item = Item.from_dict({
        "text": "X",
        "priority": "tracked",
        "future_field": "tolerated",
    })
    assert item is not None
    assert item.text == "X"


# ---------------------------------------------------------------------------
# Q3 Option A — TierDefaultsConfig (global tier-window defaults, 2026-06-26)
# ---------------------------------------------------------------------------


def test_tier_defaults_absent_all_none() -> None:
    """No routine.tier_defaults block → all-None (opt-out unchanged)."""
    td = TierDefaultsConfig.from_raw(None)
    assert td.escalate_at_days is None
    assert td.surface_at_days is None


def test_tier_defaults_from_raw_parses_ints() -> None:
    td = TierDefaultsConfig.from_raw(
        {"escalate_at_days": 3, "surface_at_days": 5},
    )
    assert td.escalate_at_days == 3
    assert td.surface_at_days == 5


def test_tier_defaults_from_raw_coerces_string_ints() -> None:
    """Operator hand-edit may pass strings; coerce defensively."""
    td = TierDefaultsConfig.from_raw(
        {"escalate_at_days": "3", "surface_at_days": "5"},
    )
    assert td.escalate_at_days == 3
    assert td.surface_at_days == 5


def test_tier_defaults_from_raw_malformed_drops_to_none() -> None:
    td = TierDefaultsConfig.from_raw(
        {"escalate_at_days": "abc", "surface_at_days": None},
    )
    assert td.escalate_at_days is None
    assert td.surface_at_days is None


def test_routine_config_carries_tier_defaults() -> None:
    """load_from_unified wires routine.tier_defaults onto RoutineConfig."""
    cfg = load_from_unified({
        "vault": {"path": "./vault"},
        "telegram": {"instance": {"name": "Salem"}},
        "routine": {"tier_defaults": {"escalate_at_days": 2}},
    })
    assert cfg.tier_defaults.escalate_at_days == 2
    assert cfg.tier_defaults.surface_at_days is None


def test_routine_config_tier_defaults_default_empty() -> None:
    """Omitted routine block → tier_defaults all-None (no behavior
    change for existing instance configs)."""
    cfg = load_from_unified({
        "vault": {"path": "./vault"},
        "telegram": {"instance": {"name": "Salem"}},
    })
    assert cfg.tier_defaults.escalate_at_days is None
    assert cfg.tier_defaults.surface_at_days is None
