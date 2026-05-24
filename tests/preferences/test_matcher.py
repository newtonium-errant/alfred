"""Tests for ``alfred.preferences.matchers`` — V1 dispatch + edge cases."""
from __future__ import annotations

import pytest

from alfred.preferences.matchers import KNOWN_RULES, evaluate


# ---------------------------------------------------------------------------
# Per-rule behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule", ["skip_event_if", "skip_brief_event_if", "skip_brief_task_if"],
)
def test_title_regex_case_insensitive_match(rule: str) -> None:
    """All three V1 rules match case-insensitively via the standard
    ``(?i)`` regex prefix the operator writes into args."""
    args = {"title_regex": "(?i)\\bopen house\\b"}
    candidate = {"name": "Open House at 123 Main Street"}
    result = evaluate(rule, args, candidate)
    assert result.skip is True
    assert rule in result.reason
    # Reason mentions both title + pattern for grep.
    assert "Open House" in result.reason


@pytest.mark.parametrize(
    "rule", ["skip_event_if", "skip_brief_event_if", "skip_brief_task_if"],
)
def test_title_regex_no_match(rule: str) -> None:
    args = {"title_regex": "(?i)\\bopen house\\b"}
    candidate = {"name": "Dentist appointment"}
    result = evaluate(rule, args, candidate)
    assert result.skip is False
    assert "does not match" in result.reason


def test_title_regex_capitalisation_variants() -> None:
    """OPEN HOUSE / Open House / open house all match (case-insensitive)."""
    args = {"title_regex": "(?i)\\bopen house\\b"}
    for title in ("OPEN HOUSE", "Open House", "open house", "OpEn HoUsE"):
        result = evaluate("skip_event_if", args, {"name": title})
        assert result.skip is True, f"{title!r} should match"


# ---------------------------------------------------------------------------
# Defensive: missing args, missing title, invalid regex
# ---------------------------------------------------------------------------


def test_missing_title_regex_arg_returns_skip_false() -> None:
    """Missing required arg → skip=False with a grep-able reason.

    Fail-open: a malformed preference must NOT silently skip every
    candidate (which would happen if missing-arg defaulted to skip=True).
    """
    result = evaluate("skip_event_if", {}, {"name": "Anything"})
    assert result.skip is False
    assert "missing required arg" in result.reason
    assert "title_regex" in result.reason


def test_missing_candidate_title_returns_skip_false() -> None:
    """Candidate with no title → skip=False (rule cannot fire)."""
    result = evaluate(
        "skip_event_if",
        {"title_regex": "test"},
        {},
    )
    assert result.skip is False
    assert "no title" in result.reason


def test_invalid_regex_returns_skip_false_with_reason() -> None:
    """A regex that can't compile → skip=False + reason naming the error.

    Same fail-open discipline: a corrupted preference must NOT take
    down the consumer.
    """
    result = evaluate(
        "skip_event_if",
        {"title_regex": "[invalid"},  # unclosed bracket
        {"name": "Anything"},
    )
    assert result.skip is False
    assert "invalid regex" in result.reason


def test_unknown_rule_returns_skip_false() -> None:
    """Unknown rule name → fail-open, NOT skip-all."""
    result = evaluate("rule_that_does_not_exist", {}, {"name": "x"})
    assert result.skip is False
    assert "unknown rule" in result.reason


def test_args_none_tolerated() -> None:
    """Passing args=None is tolerated (treated as empty dict)."""
    result = evaluate("skip_event_if", None, {"name": "x"})
    assert result.skip is False
    # Falls through to missing-arg reason.
    assert "missing required arg" in result.reason


def test_args_non_dict_tolerated() -> None:
    """Passing args as a non-dict (e.g. None-equivalent shapes) is safe."""
    result = evaluate("skip_event_if", "not a dict", {"name": "x"})  # type: ignore[arg-type]
    assert result.skip is False


# ---------------------------------------------------------------------------
# Title field priority + coercion
# ---------------------------------------------------------------------------


def test_title_falls_through_name_to_title_field() -> None:
    """``name`` is preferred; ``title`` is the fallback."""
    args = {"title_regex": "(?i)matchme"}
    # Only `title` set, no `name`.
    result = evaluate("skip_event_if", args, {"title": "MatchMe Today"})
    assert result.skip is True


def test_title_non_string_coerced_safely() -> None:
    """A non-string title (e.g. malformed frontmatter) is coerced via str().

    Don't crash; if the coerced form matches the regex, do skip.
    """
    args = {"title_regex": r"\d{4}"}
    result = evaluate("skip_event_if", args, {"name": 2026})  # int
    assert result.skip is True


# ---------------------------------------------------------------------------
# Public registry
# ---------------------------------------------------------------------------


def test_known_rules_registry() -> None:
    """V1 ships exactly three rules — pin the contract."""
    assert KNOWN_RULES == frozenset({
        "skip_event_if",
        "skip_brief_event_if",
        "skip_brief_task_if",
    })
