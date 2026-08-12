"""Pins for §1's glance stat (Phase C, item 5) — "X/Y green" leads the brief.

The ratified §1 shape is stat → list → why. What is pinned here is the part
that is easy to get wrong and expensive when wrong: the DENOMINATOR.

``skip`` means "this check did not apply on this instance", not "this check
was not green". Counting skips into the denominator prints a false alarm at the
very top of the operator's morning brief, every morning, on an instance where
nothing is wrong — the same skip-blindness that made the narration say
"7 tools need a look" daily. KAL-LE's measured BIT is the fixture for exactly
that, because it is the instance the bug was found on.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

from alfred.brief.health_section import (
    _render_from_frontmatter,
    health_glance_stat,
)

#: KAL-LE's MEASURED BIT tool counts (recorded 2026-08-03/04, the shape that
#: surfaced the skip-blindness class across three surfaces in one day). Pinned
#: as a named constant so a future edit can see which instance it describes.
KALLE_MEASURED_TOOL_COUNTS = {"ok": 5, "warn": 0, "fail": 0, "skip": 7}


def test_skips_are_excluded_from_the_denominator() -> None:
    """THE pin. Over the raw total this reads "5/12 green" — a false alarm at
    the top of the brief on an instance where nothing is wrong."""
    stat = health_glance_stat(KALLE_MEASURED_TOOL_COUNTS)
    assert "5/5 green" in stat
    assert "5/12" not in stat
    # The skipped checks are not hidden either — they are reported as what
    # they are, so "why are only 5 things checked" has a visible answer.
    assert "7 not applicable" in stat


def test_all_skipped_does_not_claim_green() -> None:
    """Zero applicable checks is not "0/0 green" and definitely not green:
    claiming health on the strength of no evidence is the one direction this
    must not take (mirrors narration._health_text)."""
    stat = health_glance_stat({"skip": 7})
    assert "green" not in stat
    assert "No checks ran" in stat
    assert "7 skipped" in stat


def test_warn_and_fail_count_against_green() -> None:
    """The positive control for the exclusion pins above — without it they
    would pass identically against a stat that counted nothing at all."""
    assert "3/5 green" in health_glance_stat({"ok": 3, "warn": 1, "fail": 1})


def test_unknown_status_fails_open_into_the_denominator() -> None:
    """An unrecognised status is NOT green and NOT a skip. It counts as
    applicable-and-not-green, so a future 5th Status value (or a hand-edited
    record) drags the stat down rather than being laundered into green —
    the same denylist direction as ``QUIET_HEALTH_STATUSES``."""
    assert "2/3 green" in health_glance_stat({"ok": 2, "something_new": 1})


def test_no_tools_is_its_own_answer() -> None:
    assert health_glance_stat({}) == "**No tools checked**"


def test_zero_and_malformed_counts_are_ignored_not_crashed() -> None:
    """BIT records are files on disk; a hand-edited or older record can carry
    a null or a string. Degrade, never crash the whole brief."""
    stat = health_glance_stat({"ok": 3, "warn": 0, "fail": None, "skip": "x"})
    assert "3/3 green" in stat


def test_section_leads_with_the_stat_then_lists_then_says_why() -> None:
    """The ratified §1 ORDER, driven through the real render.

    Previously the section led with "**Overall:** ok (last run …)", which makes
    the reader parse a sentence to learn a number. The run metadata is still
    there — it is the "says who, and how fresh" — but it follows the glance and
    the list rather than heading them.
    """
    body = _render_from_frontmatter(
        {
            "overall_status": "warn", "mode": "full", "created": "2026-08-12",
            "started": "05:00", "name": "Alfred BIT 2026-08-12",
            "tool_counts": KALLE_MEASURED_TOOL_COUNTS,
        },
        "", "2026-08-12", record_dir="run",
    )
    assert body.startswith("**5/5 green**")
    assert body.index("5/5 green") < body.index("tool summary")
    assert body.index("tool summary") < body.index("**Overall:**")
    # The why did not get dropped on the way down the page.
    assert "warn (last run 05:00, full mode)" in body


def test_stat_prefers_the_body_lines_over_tool_counts() -> None:
    """When the per-tool body parsed, the stat is tallied from the SAME lines
    the section goes on to list. A stat that disagreed with the list directly
    beneath it is worse than no stat at all."""
    body_md = (
        "## Summary\n"
        "[OK] curator (12 ms) — fine\n"
        "[FAIL] janitor (8 ms) — broken\n"
    )
    rendered = _render_from_frontmatter(
        {
            "overall_status": "fail", "mode": "full", "created": "2026-08-12",
            "started": "05:00", "name": "BIT",
            # Deliberately DISAGREES with the body above — if the stat were
            # read from here it would say 9/9 green.
            "tool_counts": {"ok": 9},
        },
        body_md, "2026-08-12", record_dir="run",
    )
    assert "9/9" not in rendered
    assert rendered.startswith("**1/2 green**")
