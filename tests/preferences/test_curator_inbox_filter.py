"""Tests for curator inbox-stage preference filter (P10 / Ship 3 — 2026-06-07).

Covers :func:`alfred.curator.pipeline._apply_inbox_preference_filter` —
the helper that gates inbox files BEFORE any LLM call against
``skip_inbox_if_sender_matches`` action preferences. Filtered files
land in ``processed/`` with sidecar frontmatter (``status:
filtered_by_preference``); the consumer side of that contract lives
in ``test_inbox_filter_daemon.py``.

Operator motivation (per ``project_empty_body_email_arc.md``):
Salem's inbox is ~99% empty-body promotional with ~29% Substack-
platform-routed; dropping at the inbox stage avoids LLM cost +
manifest churn entirely.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog

from alfred.curator.pipeline import _apply_inbox_preference_filter
from alfred.preferences.loader import Preference


def _build_inbox_pref(
    slug: str,
    sender_patterns: list[str],
    *,
    domain: str = "curator",
    rule: str = "skip_inbox_if_sender_matches",
) -> Preference:
    """Build a Shape A preference for inbox filtering.

    Mirrors what the loader would return for a Shape A action record
    with a ``skip_inbox_if_sender_matches`` matcher. Direct
    construction (vs. write-to-disk + load) keeps the unit tests fast
    and independent of the loader's file scanning.
    """
    return Preference(
        slug=slug,
        name=slug,
        shape="action",
        scope="universal",
        applies_to_instance=None,
        applies_to_user=None,
        cites_canonical=None,
        source_quote="",
        source_session="",
        matcher={
            "domain": domain,
            "rule": rule,
            "args": {"sender_patterns": sender_patterns},
        },
        body="",
        path=Path("/tmp/test"),
        raw={},
    )


# ---------------------------------------------------------------------------
# Per-pattern matching
# ---------------------------------------------------------------------------


def test_substack_com_sender_matches_blocklist() -> None:
    """Exact ``*@substack.com`` glob fires; result names the pref."""
    pref = _build_inbox_pref("substack-block", ["*@substack.com"])
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "writer@substack.com", [pref],
    )
    assert should_skip is True
    assert matching is not None
    assert matching.slug == "substack-block"
    assert reason is not None
    assert "substack.com" in reason


def test_substack_subdomain_matches_blocklist() -> None:
    """``*@*.substack.com`` subdomain glob fires."""
    pref = _build_inbox_pref(
        "substack-subdomain-block", ["*@*.substack.com"],
    )
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "newsletter@bigwriter.substack.com", [pref],
    )
    assert should_skip is True
    assert matching is not None
    assert matching.slug == "substack-subdomain-block"


def test_non_substack_sender_passes_through() -> None:
    """Tim Denning's real address (the operator's keep-sender) passes."""
    pref = _build_inbox_pref(
        "substack-all",
        ["*@substack.com", "*@*.substack.com"],
    )
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "tim@timdenning.com", [pref],
    )
    assert should_skip is False
    assert matching is None
    assert reason is None


def test_first_drop_wins_when_multiple_prefs_match() -> None:
    """Two prefs both match; first one in the list reports the drop.

    Operator-grep on the drop log should show ONE matching slug, not
    a stack of "and also matched by pref X." First-skip-wins is the
    operator's mental model for what fires.
    """
    pref_a = _build_inbox_pref("substack-broad", ["*@*.substack.com"])
    pref_b = _build_inbox_pref("universal-catch", ["*@*"])
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "writer@somewriter.substack.com", [pref_a, pref_b],
    )
    assert should_skip is True
    assert matching is not None
    assert matching.slug == "substack-broad"  # NOT "universal-catch"


# ---------------------------------------------------------------------------
# No-op paths (per feedback_intentionally_left_blank.md)
# ---------------------------------------------------------------------------


def test_empty_sender_patterns_list_no_op() -> None:
    """Pref with ``sender_patterns: []`` → no drop, run log fires."""
    pref = _build_inbox_pref("empty-list", [])
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "any@thing.com", [pref],
    )
    assert should_skip is False
    assert matching is None


def test_no_active_preferences_no_op() -> None:
    """``prefs=[]`` → silent pass-through with the no-preferences log."""
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "writer@substack.com", [],
    )
    assert should_skip is False
    assert matching is None
    assert reason is None


def test_no_sender_in_inbox_no_op() -> None:
    """``sender_email=None`` (non-email file) → no drop, run log fires."""
    pref = _build_inbox_pref("substack", ["*@substack.com"])
    should_skip, reason, matching = _apply_inbox_preference_filter(
        None, [pref],
    )
    assert should_skip is False
    assert matching is None

    should_skip, reason, matching = _apply_inbox_preference_filter(
        "", [pref],
    )
    assert should_skip is False
    assert matching is None


# ---------------------------------------------------------------------------
# Cross-domain / cross-rule isolation
# ---------------------------------------------------------------------------


def test_pref_with_different_domain_ignored() -> None:
    """A pref with ``domain: brief`` doesn't fire against curator's inbox filter."""
    pref = _build_inbox_pref(
        "wrong-domain",
        ["*@substack.com"],
        domain="brief",
    )
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "writer@substack.com", [pref],
    )
    assert should_skip is False
    assert matching is None


def test_pref_with_different_rule_ignored() -> None:
    """A pref with ``rule: skip_event_if`` doesn't fire on inbox filter.

    Even though the pref is curator-domain, the rule doesn't match
    the inbox filter's dispatch — it's a manifest-stage rule, not an
    inbox-stage rule. The two stages must NOT cross-fire.
    """
    pref = Preference(
        slug="event-rule-wrong-stage",
        name="event-rule-wrong-stage",
        shape="action",
        scope="universal",
        applies_to_instance=None,
        applies_to_user=None,
        cites_canonical=None,
        source_quote="",
        source_session="",
        matcher={
            "domain": "curator",
            "rule": "skip_event_if",
            "args": {"title_regex": "(?i)open house"},
        },
        body="",
        path=Path("/tmp/test"),
        raw={},
    )
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "writer@substack.com", [pref],
    )
    assert should_skip is False
    assert matching is None


def test_pref_without_explicit_domain_treated_as_curator() -> None:
    """A pref with missing ``matcher.domain`` defaults to curator.

    Andrew's authored prefs may omit the domain field. The filter
    should treat missing-domain as "matches curator" so the prefs
    work without requiring the operator to type ``domain: curator``
    every time.
    """
    pref = Preference(
        slug="no-domain",
        name="no-domain",
        shape="action",
        scope="universal",
        applies_to_instance=None,
        applies_to_user=None,
        cites_canonical=None,
        source_quote="",
        source_session="",
        matcher={
            # No "domain" key.
            "rule": "skip_inbox_if_sender_matches",
            "args": {"sender_patterns": ["*@substack.com"]},
        },
        body="",
        path=Path("/tmp/test"),
        raw={},
    )
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "writer@substack.com", [pref],
    )
    assert should_skip is True
    assert matching is not None
    assert matching.slug == "no-domain"


def test_case_insensitive_sender_match() -> None:
    """Capitalised sender vs. lowercase pattern still matches."""
    pref = _build_inbox_pref("case", ["*@substack.com"])
    should_skip, reason, matching = _apply_inbox_preference_filter(
        "TIM@SUBSTACK.COM", [pref],
    )
    assert should_skip is True
    assert matching is not None


def test_log_event_carries_pref_slug_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pin the run-log shape: slug + result + sender all present.

    Per ``feedback_log_emission_test_pattern.md`` — when production
    code emits an observability log line, the test driving that path
    MUST also pin the log emission so a future refactor that drops
    the line is caught.
    """
    pref = _build_inbox_pref("logtest", ["*@substack.com"])
    with structlog.testing.capture_logs() as captured:
        should_skip, _, _ = _apply_inbox_preference_filter(
            "writer@substack.com", [pref],
        )
    assert should_skip is True
    # Find the run log + assert key fields.
    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_run"
    ]
    assert len(matches) == 1, (
        f"expected one curator.preference_filter_inbox_run event, "
        f"got {len(matches)} (captured={captured!r})"
    )
    run_event = matches[0]
    assert run_event["preferences_loaded"] == 1
    assert run_event["sender"] == "writer@substack.com"
    assert run_event["result"] == "match"
    assert run_event["preference_slug"] == "logtest"


def test_no_match_run_log_fires_with_no_match_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pass-through path still emits the run log with result=no_match."""
    pref = _build_inbox_pref("nope", ["*@substack.com"])
    with structlog.testing.capture_logs() as captured:
        _apply_inbox_preference_filter("tim@timdenning.com", [pref])
    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_run"
    ]
    assert len(matches) == 1
    assert matches[0]["result"] == "no_match"
    assert matches[0]["sender"] == "tim@timdenning.com"


def test_no_sender_run_log_fires_with_no_sender_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No-sender path emits the run log with result=no_sender."""
    pref = _build_inbox_pref("any", ["*@substack.com"])
    with structlog.testing.capture_logs() as captured:
        _apply_inbox_preference_filter(None, [pref])
    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_run"
    ]
    assert len(matches) == 1
    assert matches[0]["result"] == "no_sender"


def test_no_preferences_run_log_fires_with_no_preferences_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty prefs path emits the run log with result=no_preferences."""
    with structlog.testing.capture_logs() as captured:
        _apply_inbox_preference_filter("any@thing.com", [])
    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_run"
    ]
    assert len(matches) == 1
    assert matches[0]["result"] == "no_preferences"
    assert matches[0]["preferences_loaded"] == 0
