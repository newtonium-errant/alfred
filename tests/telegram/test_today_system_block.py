"""Tests for the today's-date system block — Phase A 2026-05-06.

Closes the day-of-week date-math gap surfaced 2026-05-05 in
conversation ``716f5b24``. Andrew said "Massage Thursday 10am-12pm"
on Tue 2026-05-05; Salem computed "Thursday is 2026-05-08" (added 3
days, got Friday's date). The SKILL's confirm-with-absolute-date
discipline (commit ``1c56966``) caught it but eats friction-turns
on every relative-time phrase. Source-side fix: inject today's date
+ day-of-week + tz into every conversation's system context as the
LAST block.

Coverage:

* Pure helper ``_build_today_block_text``:
    - YYYY-MM-DD format
    - Day-of-week mapping correct (parametrized across all 7 days)
    - Timezone label (instance_timezone IANA name + tz_short like ADT)
    - UTC offset format ``UTC±HH:MM``
    - Anchor-instructions text present (helps LLM use the block)

* Integration with ``_build_system_blocks``:
    - Today-block always present as the LAST element (even when only
      system_prompt is supplied)
    - NO ``cache_control`` on the today-block (changes daily; cache
      TTL is 5min; ephemeral breakpoint here would churn the cache
      pointlessly)
    - Default ``now`` produces a tz-aware datetime in
      ``America/Halifax`` (or whatever ``instance_timezone`` is)
    - ``now=`` injection works for deterministic tests (mirrors the
      dangling-tool_use detector pattern)
    - Custom ``instance_timezone`` honoured

Per ``feedback_intentionally_left_blank.md``: the today-block fires
unconditionally so a future contributor who removes it (or fails to
populate ``now``) can't silently disable Salem's date anchor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from alfred.telegram import conversation


# --- Pure helper: _build_today_block_text ---------------------------------


def test_today_block_yyyy_mm_dd_day_format():
    """Date renders in ``YYYY-MM-DD (DayName)`` format."""
    now = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    text = conversation._build_today_block_text(now, "America/Halifax")
    # 2026-05-05 13:00 UTC = 10:00 ADT (UTC-3) on Tuesday.
    assert "2026-05-05 (Tuesday)" in text


def test_today_block_includes_timezone_label():
    """IANA timezone name + short tz label both present."""
    now = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    text = conversation._build_today_block_text(now, "America/Halifax")
    assert "America/Halifax" in text
    # ADT — Atlantic Daylight Time, the May offset for Halifax.
    assert "(ADT)" in text


def test_today_block_includes_utc_offset_in_canonical_format():
    """UTC offset rendered as ``UTC±HH:MM`` (not ``±HHMM``)."""
    now = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    text = conversation._build_today_block_text(now, "America/Halifax")
    assert "UTC-03:00" in text


def test_today_block_includes_anchor_instructions():
    """The body text instructs the LLM to use this date as the anchor
    for relative time phrases. Without the instructions the LLM might
    treat the date as decorative; the instructions make it actionable."""
    now = datetime(2026, 5, 5, tzinfo=timezone.utc)
    text = conversation._build_today_block_text(now, "America/Halifax")
    assert "anchor" in text.lower()
    # Examples should name the relative-time phrase shapes.
    assert "Thursday" in text
    assert "tomorrow" in text


def test_today_block_starts_with_today_heading():
    """## Today heading at the top so the LLM sees a section, not a
    prose blob — easier to anchor on in the model's attention."""
    now = datetime(2026, 5, 5, tzinfo=timezone.utc)
    text = conversation._build_today_block_text(now, "America/Halifax")
    assert text.startswith("## Today")


@pytest.mark.parametrize(
    "iso_date, expected_day",
    [
        ("2026-05-04", "Monday"),
        ("2026-05-05", "Tuesday"),
        ("2026-05-06", "Wednesday"),
        ("2026-05-07", "Thursday"),
        ("2026-05-08", "Friday"),
        ("2026-05-09", "Saturday"),
        ("2026-05-10", "Sunday"),
    ],
)
def test_today_block_day_of_week_mapping_all_seven_days(
    iso_date: str, expected_day: str,
):
    """Day-of-week mapping correct across the full week. The bug from
    conversation ``716f5b24`` was specifically a day-of-week miscount;
    pin every weekday so a future formatter swap (e.g. ``%a`` for
    abbreviated names) gets caught explicitly."""
    # Use noon ADT so the date doesn't roll over on the UTC conversion.
    halifax = ZoneInfo("America/Halifax")
    year, month, day = (int(p) for p in iso_date.split("-"))
    now = datetime(year, month, day, 12, 0, tzinfo=halifax)
    text = conversation._build_today_block_text(now, "America/Halifax")
    assert f"{iso_date} ({expected_day})" in text


def test_today_block_respects_non_default_timezone():
    """Different IANA timezone → different date rendering. The block
    is operator-local-time, not UTC."""
    # 2026-05-05 02:00 UTC = 2026-05-04 22:00 EDT (UTC-4 in May).
    now = datetime(2026, 5, 5, 2, 0, tzinfo=timezone.utc)
    text_halifax = conversation._build_today_block_text(now, "America/Halifax")
    text_ny = conversation._build_today_block_text(now, "America/New_York")
    # Halifax (UTC-3) sees 2026-05-04 23:00, still Monday.
    assert "2026-05-04 (Monday)" in text_halifax
    # New York (UTC-4) sees 2026-05-04 22:00, also Monday.
    assert "2026-05-04 (Monday)" in text_ny
    # But the tz labels are distinct.
    assert "America/New_York" in text_ny
    assert "America/Halifax" in text_halifax


# --- Integration: _build_system_blocks always tails with the today-block ---


def test_build_system_blocks_appends_today_as_last_block():
    """Even with ONLY system_prompt + vault_context_str supplied, the
    today-block tails. The date anchor is unconditional — no opt-out
    via skipping calibration / pushback can remove it."""
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
    )
    assert len(blocks) == 3
    assert blocks[-1]["text"].startswith("## Today")


def test_build_system_blocks_today_block_has_no_cache_control():
    """The today-block must NOT carry ``cache_control``. It changes
    daily; cache TTL is 5min; an ephemeral breakpoint here would
    churn the cache pointlessly. Pin the dict shape so a "consistency"
    refactor that adds cache_control to every block fails this test."""
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
    )
    today_block = blocks[-1]
    assert today_block["type"] == "text"
    assert "cache_control" not in today_block


def test_build_system_blocks_other_blocks_keep_cache_control():
    """Cross-check: every block EXCEPT the today-block keeps its
    ephemeral cache breakpoint. Catches a regression that removes
    cache_control from the wrong block."""
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str="CAL",
        pushback_level=2,
    )
    # First 4 blocks (system / vault / calibration / pushback) cached;
    # tail (today) NOT cached.
    for block in blocks[:-1]:
        assert block["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[-1]


def test_build_system_blocks_now_kwarg_threads_to_today_block():
    """``now=`` kwarg lets tests pin the rendered date deterministically.
    Mirrors the dangling-tool_use detector's ``now=`` pattern at
    ``conversation.py``'s ``detect_dangling_tool_use_at_startup``."""
    fixed_now = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        now=fixed_now,
    )
    assert "2026-05-05 (Tuesday)" in blocks[-1]["text"]


def test_build_system_blocks_default_now_is_recent():
    """Default ``now`` resolves to ``datetime.now(timezone.utc)``. Test
    via lower-bound assertion — the today-block's date string parses
    back to a date within the last 5 minutes."""
    before = datetime.now(timezone.utc)
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
    )
    after = datetime.now(timezone.utc)
    text = blocks[-1]["text"]
    # Pull the YYYY-MM-DD substring out of the rendered text.
    halifax = ZoneInfo("America/Halifax")
    expected_dates = {
        before.astimezone(halifax).strftime("%Y-%m-%d"),
        after.astimezone(halifax).strftime("%Y-%m-%d"),
    }
    # Today-block text must contain at least one of the expected dates
    # (the only ambiguity is a midnight-rollover race, so we accept
    # either ``before``'s date or ``after``'s date).
    assert any(d in text for d in expected_dates), (
        f"today-block date didn't match before={before} / after={after}; "
        f"text was: {text!r}"
    )


def test_build_system_blocks_custom_instance_timezone():
    """``instance_timezone=`` kwarg honoured — non-Halifax callers can
    pass their own IANA name. Future per-instance config plumbing
    will use this knob."""
    fixed_now = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        now=fixed_now,
        instance_timezone="America/New_York",
    )
    assert "America/New_York" in blocks[-1]["text"]


# --- Cache-ordering doctrine pin -------------------------------------------


def test_today_block_is_strictly_last_in_full_layout():
    """Cache-ordering invariant: today is THE most-volatile block,
    so it MUST tail. A cacheable block placed after it would
    invalidate the cache prefix on every date rollover (midnight ADT
    — 04:00 UTC for half the year). Pin position so a future addition
    can't accidentally move today off the tail."""
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str="CAL",
        pushback_level=4,
    )
    # All blocks present; today is at the end.
    assert blocks[-1]["text"].startswith("## Today")
    # Sanity: the OTHER block contents are NOT in the last position.
    assert "SYS" not in blocks[-1]["text"]
    assert "VAULT" not in blocks[-1]["text"]
    assert "Alfred's calibration" not in blocks[-1]["text"]
    assert "Session pushback directive" not in blocks[-1]["text"]
