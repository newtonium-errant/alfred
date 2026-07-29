"""Unit tests for the shared agent-failure classify + summary helper.

Covers ``alfred.health.agent_failure`` — the ONE implementation the three
CLI backends (curator, janitor, distiller) share so a failed ``claude -p``
call produces the same diagnostic summary + closed-set ``kind`` everywhere.

The 2026-07-29 weekly-limit incident is pinned directly: returncode 1,
stdout = the quota banner, stderr empty → summary CONTAINS the banner (not
the content-free "Exit code 1: " that stderr-only summaries produced), and
kind == quota_limited.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from alfred.health.agent_failure import (
    AUTH,
    KNOWN_FAILURE_KINDS,
    OTHER,
    QUOTA_LIMITED,
    build_failure_summary,
    classify_agent_failure,
)

# The exact stdout ``claude -p`` printed during the outage.
INCIDENT_STDOUT = "You've hit your weekly limit · resets 4am (UTC)"


# ---------------------------------------------------------------------------
# build_failure_summary
# ---------------------------------------------------------------------------


def test_summary_incident_contains_stdout_message() -> None:
    """THE incident: quota banner on stdout, stderr empty → summary carries it."""
    summary = build_failure_summary(1, INCIDENT_STDOUT, "")
    assert "You've hit your weekly limit" in summary
    assert summary.startswith("Exit code 1:")
    # NOT the content-free stderr-only summary the outage logged.
    assert summary != "Exit code 1: "
    assert summary.strip() != "Exit code 1:"


def test_summary_folds_both_streams() -> None:
    summary = build_failure_summary(2, "out-content", "err-content")
    assert "stdout: out-content" in summary
    assert "stderr: err-content" in summary
    assert summary.startswith("Exit code 2:")


def test_summary_both_empty_is_no_output_not_bare_colon() -> None:
    summary = build_failure_summary(1, "", "")
    assert summary == "Exit code 1: (no output)"
    # The bare-colon sentinel must never be the whole summary.
    assert not summary.endswith(": ")


def test_summary_none_streams_is_no_output() -> None:
    assert build_failure_summary(1, None, None) == "Exit code 1: (no output)"


def test_summary_is_bounded() -> None:
    """Long streams are capped so the log field stays grep-friendly."""
    summary = build_failure_summary(1, "x" * 5000, "y" * 5000)
    assert len(summary) <= 300
    # Both streams still represented (tails), not one clobbering the other.
    assert "stdout:" in summary and "stderr:" in summary


def test_summary_strips_ansi_and_collapses_newlines() -> None:
    colored = "\x1b[31mYou've hit your weekly limit\x1b[0m\n\nresets 4am"
    summary = build_failure_summary(1, colored, "")
    assert "\x1b" not in summary
    assert "\n" not in summary
    assert "You've hit your weekly limit" in summary
    assert "resets 4am" in summary


def test_summary_single_line() -> None:
    summary = build_failure_summary(1, "line1\nline2\nline3", "e1\ne2")
    assert "\n" not in summary


# ---------------------------------------------------------------------------
# classify_agent_failure
# ---------------------------------------------------------------------------


def test_classify_incident_is_quota() -> None:
    assert classify_agent_failure(INCIDENT_STDOUT, "") == QUOTA_LIMITED


def test_classify_quota_variants() -> None:
    for text in (
        "You've hit your weekly limit",
        "rate limit exceeded",
        "usage limit reached",
        "You have exceeded your monthly limit, resets at midnight",
        "429 too many requests",
        "quota exceeded for this account",
        "hit the limit — upgrade your plan",
    ):
        assert classify_agent_failure(text, "") == QUOTA_LIMITED, text


def test_classify_quota_on_stderr_stream_too() -> None:
    # Some CLI variants may print to stderr — classification scans both.
    assert classify_agent_failure("", "rate limit exceeded") == QUOTA_LIMITED


def test_classify_quota_ansi_colored() -> None:
    colored = "\x1b[1;31mYou've hit your weekly limit · resets 4am (UTC)\x1b[0m"
    assert classify_agent_failure(colored, "") == QUOTA_LIMITED


def test_classify_auth_variants() -> None:
    for text in (
        "Not logged in. Please run /login",
        "please run /login to authenticate",
        "invalid API key provided",
        "Unauthorized",
        "not authenticated",
        "login required",
    ):
        assert classify_agent_failure(text, "") == AUTH, text


def test_classify_other_on_unrecognized() -> None:
    for text in (
        "connection reset by peer",
        "segmentation fault",
        "unexpected end of JSON input",
        "some random failure",
    ):
        assert classify_agent_failure(text, "") == OTHER, text


def test_classify_both_empty_is_other() -> None:
    assert classify_agent_failure("", "") == OTHER
    assert classify_agent_failure(None, None) == OTHER


def test_classify_quota_wins_over_auth_when_both_present() -> None:
    # A quota banner that also nudges "/login" (upgrade link) is a quota event.
    text = "You've hit your weekly limit — upgrade at /login"
    assert classify_agent_failure(text, "") == QUOTA_LIMITED


def test_classify_weak_resets_alone_is_not_quota() -> None:
    # "resets" without "limit" must not false-fire quota (compound guard).
    assert classify_agent_failure("the daemon resets the cache nightly", "") == OTHER


def test_closed_set_is_exactly_three_kinds() -> None:
    assert KNOWN_FAILURE_KINDS == {QUOTA_LIMITED, AUTH, OTHER}
    # Every classifier return is inside the closed set.
    for out, err in [(INCIDENT_STDOUT, ""), ("not logged in", ""), ("boom", ""), ("", "")]:
        assert classify_agent_failure(out, err) in KNOWN_FAILURE_KINDS
