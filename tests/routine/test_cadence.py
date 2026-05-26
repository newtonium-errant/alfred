"""Cadence dispatcher tests — all six shapes + edge cases.

The cadence engine is the load-bearing primitive: a routine fires today
iff ``cadence.is_due(record["cadence"], today)`` returns True. If this
returns the wrong answer the operator sees the wrong daily plan, so the
test surface here is deliberately exhaustive.

Edge cases pinned (per dispatch):
  - Feb 29 anchor + non-leap target year (every_n_months).
  - ``monthly day: 31`` in February (clamps to last day).
  - ``monthly day: 'last'`` (string sentinel).
  - ``nth_weekday: [-1, 'Fri']`` (count from end of month).
  - ``every_n_days`` with anchor in future (NOT due).
  - ``every_n_days`` with anchor == today (DUE — anchors are inclusive).
  - DST boundary days don't shift ``every_n_days`` arithmetic (plain
    calendar-day count, no clock-time at this layer).
"""

from __future__ import annotations

from datetime import date

import pytest

from alfred.routine.cadence import CadenceError, is_due


# ---------------------------------------------------------------------------
# Shape 1: daily
# ---------------------------------------------------------------------------


def test_daily_fires_every_day() -> None:
    assert is_due({"type": "daily"}, date(2026, 5, 26)) is True
    assert is_due({"type": "daily"}, date(2024, 2, 29)) is True


def test_daily_extra_keys_ignored() -> None:
    # Forward-compatible: unknown extra keys don't trip the check.
    assert is_due({"type": "daily", "comment": "ignored"}, date(2026, 5, 26)) is True


# ---------------------------------------------------------------------------
# Shape 2: weekly
# ---------------------------------------------------------------------------


def test_weekly_fires_on_listed_days() -> None:
    # 2026-05-26 is a Tuesday.
    cadence = {"type": "weekly", "days": ["Tue", "Thu"]}
    assert is_due(cadence, date(2026, 5, 26)) is True   # Tuesday
    assert is_due(cadence, date(2026, 5, 28)) is True   # Thursday
    assert is_due(cadence, date(2026, 5, 27)) is False  # Wednesday


def test_weekly_case_insensitive_and_full_names() -> None:
    assert is_due({"type": "weekly", "days": ["monday"]}, date(2026, 5, 25)) is True
    assert is_due({"type": "weekly", "days": ["MON"]}, date(2026, 5, 25)) is True
    assert is_due({"type": "weekly", "days": ["mon"]}, date(2026, 5, 26)) is False


def test_weekly_empty_days_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "weekly", "days": []}, date(2026, 5, 26))


def test_weekly_missing_days_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "weekly"}, date(2026, 5, 26))


def test_weekly_unknown_weekday_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "weekly", "days": ["Funday"]}, date(2026, 5, 26))


# ---------------------------------------------------------------------------
# Shape 3: every_n_days
# ---------------------------------------------------------------------------


def test_every_n_days_anchor_today_fires() -> None:
    # Inclusive lower bound: anchor == today ⇒ due.
    cadence = {"type": "every_n_days", "n": 14, "anchor": "2026-05-26"}
    assert is_due(cadence, date(2026, 5, 26)) is True


def test_every_n_days_anchor_plus_n_fires() -> None:
    cadence = {"type": "every_n_days", "n": 14, "anchor": "2026-01-06"}
    # 2026-01-06 + 14 days = 2026-01-20.
    assert is_due(cadence, date(2026, 1, 20)) is True
    assert is_due(cadence, date(2026, 2, 3)) is True   # +28
    assert is_due(cadence, date(2026, 1, 21)) is False


def test_every_n_days_anchor_in_future_not_due() -> None:
    cadence = {"type": "every_n_days", "n": 7, "anchor": "2026-12-31"}
    assert is_due(cadence, date(2026, 5, 26)) is False


def test_every_n_days_dst_spring_forward_unaffected() -> None:
    # 2026 DST spring-forward in Halifax: 2026-03-08. Day-arithmetic
    # should NOT skip a day across the boundary — same N-day cadence
    # before and after.
    cadence = {"type": "every_n_days", "n": 3, "anchor": "2026-03-05"}
    assert is_due(cadence, date(2026, 3, 5)) is True
    assert is_due(cadence, date(2026, 3, 8)) is True   # spring-forward day
    assert is_due(cadence, date(2026, 3, 11)) is True
    assert is_due(cadence, date(2026, 3, 9)) is False


def test_every_n_days_dst_fall_back_unaffected() -> None:
    # 2026 DST fall-back in Halifax: 2026-11-01. Same drill.
    cadence = {"type": "every_n_days", "n": 2, "anchor": "2026-10-30"}
    assert is_due(cadence, date(2026, 10, 30)) is True
    assert is_due(cadence, date(2026, 11, 1)) is True   # fall-back day
    assert is_due(cadence, date(2026, 11, 3)) is True
    assert is_due(cadence, date(2026, 10, 31)) is False


def test_every_n_days_invalid_n_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "every_n_days", "n": 0, "anchor": "2026-01-01"}, date(2026, 5, 26))
    with pytest.raises(CadenceError):
        is_due({"type": "every_n_days", "n": -3, "anchor": "2026-01-01"}, date(2026, 5, 26))


def test_every_n_days_missing_anchor_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "every_n_days", "n": 7}, date(2026, 5, 26))


def test_every_n_days_anchor_as_date_object() -> None:
    # YAML parses ``anchor: 2026-01-06`` as a Python ``date`` directly.
    cadence = {"type": "every_n_days", "n": 7, "anchor": date(2026, 1, 6)}
    assert is_due(cadence, date(2026, 1, 13)) is True


# ---------------------------------------------------------------------------
# Shape 4a: monthly (by day-of-month)
# ---------------------------------------------------------------------------


def test_monthly_day_specific() -> None:
    cadence = {"type": "monthly", "day": 15}
    assert is_due(cadence, date(2026, 5, 15)) is True
    assert is_due(cadence, date(2026, 5, 14)) is False
    assert is_due(cadence, date(2026, 5, 16)) is False


def test_monthly_day_last_string_sentinel() -> None:
    cadence = {"type": "monthly", "day": "last"}
    assert is_due(cadence, date(2026, 5, 31)) is True
    assert is_due(cadence, date(2026, 4, 30)) is True   # April has 30 days
    assert is_due(cadence, date(2026, 2, 28)) is True   # Feb 2026 (non-leap)
    assert is_due(cadence, date(2024, 2, 29)) is True   # Feb 2024 (leap)
    assert is_due(cadence, date(2026, 5, 30)) is False


def test_monthly_day_31_clamps_to_february_last() -> None:
    # Operator wrote ``day: 31`` — in Feb 2026 (non-leap, 28 days) the
    # routine fires on the 28th, NOT on March 3rd, NOT silently skipped.
    cadence = {"type": "monthly", "day": 31}
    assert is_due(cadence, date(2026, 2, 28)) is True   # clamped
    assert is_due(cadence, date(2026, 3, 31)) is True   # March has 31
    assert is_due(cadence, date(2024, 2, 29)) is True   # leap-year Feb
    assert is_due(cadence, date(2026, 2, 27)) is False


def test_monthly_day_31_clamps_to_april_last() -> None:
    # April has 30. Day=31 fires on the 30th.
    cadence = {"type": "monthly", "day": 31}
    assert is_due(cadence, date(2026, 4, 30)) is True


def test_monthly_invalid_day_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "monthly", "day": 32}, date(2026, 5, 26))
    with pytest.raises(CadenceError):
        is_due({"type": "monthly", "day": 0}, date(2026, 5, 26))
    with pytest.raises(CadenceError):
        is_due({"type": "monthly"}, date(2026, 5, 26))   # no day, no nth_weekday


# ---------------------------------------------------------------------------
# Shape 4b: monthly (by nth_weekday)
# ---------------------------------------------------------------------------


def test_monthly_nth_weekday_first_monday() -> None:
    cadence = {"type": "monthly", "nth_weekday": [1, "Mon"]}
    # First Monday of May 2026 = May 4.
    assert is_due(cadence, date(2026, 5, 4)) is True
    assert is_due(cadence, date(2026, 5, 11)) is False


def test_monthly_nth_weekday_last_friday() -> None:
    cadence = {"type": "monthly", "nth_weekday": [-1, "Fri"]}
    # Last Friday of May 2026 = May 29.
    assert is_due(cadence, date(2026, 5, 29)) is True
    assert is_due(cadence, date(2026, 5, 22)) is False
    # Last Friday of April 2026 = April 24.
    assert is_due(cadence, date(2026, 4, 24)) is True


def test_monthly_nth_weekday_second_to_last() -> None:
    cadence = {"type": "monthly", "nth_weekday": [-2, "Sat"]}
    # May 2026 Saturdays: 2, 9, 16, 23, 30 → second-to-last = May 23.
    assert is_due(cadence, date(2026, 5, 23)) is True
    assert is_due(cadence, date(2026, 5, 30)) is False


def test_monthly_nth_weekday_n_zero_raises() -> None:
    with pytest.raises(CadenceError):
        is_due(
            {"type": "monthly", "nth_weekday": [0, "Mon"]},
            date(2026, 5, 26),
        )


def test_monthly_nth_weekday_out_of_range_raises() -> None:
    # February 2026 has only 4 Mondays — asking for the 5th is unresolvable.
    cadence = {"type": "monthly", "nth_weekday": [5, "Mon"]}
    with pytest.raises(CadenceError):
        is_due(cadence, date(2026, 2, 28))


def test_monthly_nth_weekday_malformed_pair_raises() -> None:
    with pytest.raises(CadenceError):
        is_due(
            {"type": "monthly", "nth_weekday": [1]},
            date(2026, 5, 26),
        )


# ---------------------------------------------------------------------------
# Shape 5: every_n_months
# ---------------------------------------------------------------------------


def test_every_n_months_basic() -> None:
    cadence = {"type": "every_n_months", "n": 2, "day": 15, "anchor": "2026-01-15"}
    assert is_due(cadence, date(2026, 1, 15)) is True
    assert is_due(cadence, date(2026, 3, 15)) is True
    assert is_due(cadence, date(2026, 5, 15)) is True
    assert is_due(cadence, date(2026, 2, 15)) is False   # off-cycle month
    assert is_due(cadence, date(2026, 3, 14)) is False
    assert is_due(cadence, date(2026, 3, 16)) is False


def test_every_n_months_anchor_in_future_not_due() -> None:
    cadence = {"type": "every_n_months", "n": 6, "day": 1, "anchor": "2027-01-01"}
    assert is_due(cadence, date(2026, 5, 26)) is False


def test_every_n_months_anchor_today_is_due() -> None:
    cadence = {"type": "every_n_months", "n": 3, "day": 26, "anchor": "2026-05-26"}
    assert is_due(cadence, date(2026, 5, 26)) is True


def test_every_n_months_feb29_anchor_non_leap_target_clamps() -> None:
    # Anchor 2024-02-29 (leap), n=12: the next firing in 2026 should clamp
    # to 2026-02-28 (Feb 2026 has 28 days). 2026 is non-leap.
    cadence = {"type": "every_n_months", "n": 12, "day": 29, "anchor": "2024-02-29"}
    assert is_due(cadence, date(2026, 2, 28)) is True
    assert is_due(cadence, date(2026, 3, 1)) is False
    # And in 2028 (leap year), it fires on the 29th proper.
    assert is_due(cadence, date(2028, 2, 29)) is True


def test_every_n_months_day_last_sentinel() -> None:
    cadence = {"type": "every_n_months", "n": 2, "day": "last", "anchor": "2026-01-31"}
    assert is_due(cadence, date(2026, 1, 31)) is True
    # +2 months from Jan = March → 2026-03-31.
    assert is_due(cadence, date(2026, 3, 31)) is True
    # +4 months = May → 2026-05-31.
    assert is_due(cadence, date(2026, 5, 31)) is True
    # February (off-cycle) should NOT fire.
    assert is_due(cadence, date(2026, 2, 28)) is False


def test_every_n_months_invalid_n_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "every_n_months", "n": 0, "day": 1, "anchor": "2026-01-01"}, date(2026, 5, 26))


def test_every_n_months_missing_anchor_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "every_n_months", "n": 2, "day": 1}, date(2026, 5, 26))


# ---------------------------------------------------------------------------
# Negative cases (shape-level)
# ---------------------------------------------------------------------------


def test_unknown_type_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"type": "fortnightly"}, date(2026, 5, 26))


def test_missing_type_raises() -> None:
    with pytest.raises(CadenceError):
        is_due({"days": ["Mon"]}, date(2026, 5, 26))


def test_non_dict_cadence_raises() -> None:
    with pytest.raises(CadenceError):
        is_due("daily", date(2026, 5, 26))     # type: ignore[arg-type]
    with pytest.raises(CadenceError):
        is_due(None, date(2026, 5, 26))         # type: ignore[arg-type]
    with pytest.raises(CadenceError):
        is_due([], date(2026, 5, 26))           # type: ignore[arg-type]


def test_anchor_bad_string_raises() -> None:
    with pytest.raises(CadenceError):
        is_due(
            {"type": "every_n_days", "n": 7, "anchor": "not-a-date"},
            date(2026, 5, 26),
        )
