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
    """V1 ships exactly four rules — pin the contract.

    P10 / Ship 3 added ``skip_inbox_if_sender_matches`` (2026-06-07).
    The contract-pin shape is unchanged: any future rule addition
    forces a deliberate test update so the same commit can ratify the
    matcher.py / KNOWN_RULES / test set together.
    """
    assert KNOWN_RULES == frozenset({
        "skip_event_if",
        "skip_brief_event_if",
        "skip_brief_task_if",
        "skip_inbox_if_sender_matches",
    })


# ---------------------------------------------------------------------------
# skip_inbox_if_sender_matches (P10 / Ship 3 — 2026-06-07)
# ---------------------------------------------------------------------------


_INBOX_RULE = "skip_inbox_if_sender_matches"


def test_inbox_sender_matches_substack_com_pattern() -> None:
    """``*@substack.com`` glob matches the bare substack.com sender."""
    args = {"sender_patterns": ["*@substack.com"]}
    candidate = {"sender": "writer@substack.com"}
    result = evaluate(_INBOX_RULE, args, candidate)
    assert result.skip is True
    assert "substack.com" in result.reason


def test_inbox_sender_matches_substack_subdomain_pattern() -> None:
    """``*@*.substack.com`` glob matches subdomain senders."""
    args = {"sender_patterns": ["*@*.substack.com"]}
    candidate = {"sender": "newsletter@somewriter.substack.com"}
    result = evaluate(_INBOX_RULE, args, candidate)
    assert result.skip is True
    assert "somewriter.substack.com" in result.reason


def test_inbox_sender_passes_through_when_no_pattern_matches() -> None:
    """Non-Substack sender (Tim Denning's actual address) passes through."""
    args = {
        "sender_patterns": ["*@substack.com", "*@*.substack.com"],
    }
    candidate = {"sender": "tim@timdenning.com"}
    result = evaluate(_INBOX_RULE, args, candidate)
    assert result.skip is False
    assert "tim@timdenning.com" in result.reason
    assert "does not match" in result.reason


def test_inbox_sender_first_match_wins() -> None:
    """First pattern that matches stops the iteration — reason names it.

    The matcher names ONE pattern, not all candidates. Operator-grep
    on the reason line should show exactly which entry of the list
    caused the drop, not "either pattern A or pattern B."
    """
    args = {
        "sender_patterns": [
            "*@*.substack.com",  # broader; matches first
            "*@*",                # universal; would also match
        ],
    }
    candidate = {"sender": "newsletter@somewriter.substack.com"}
    result = evaluate(_INBOX_RULE, args, candidate)
    assert result.skip is True
    # The FIRST pattern is named in the reason, not the second.
    assert "*@*.substack.com" in result.reason
    assert "'*@*'" not in result.reason


def test_inbox_sender_case_insensitive_match() -> None:
    """Upper / mixed case in either sender or pattern still matches."""
    args = {"sender_patterns": ["*@SUBSTACK.COM"]}
    candidate = {"sender": "Tim@Substack.com"}
    result = evaluate(_INBOX_RULE, args, candidate)
    assert result.skip is True


def test_inbox_sender_missing_sender_no_op() -> None:
    """Candidate with no sender → skip=False with grep-able reason."""
    args = {"sender_patterns": ["*@substack.com"]}
    result = evaluate(_INBOX_RULE, args, {"sender": ""})
    assert result.skip is False
    assert "no sender" in result.reason

    result = evaluate(_INBOX_RULE, args, {})
    assert result.skip is False


def test_inbox_sender_missing_patterns_arg_no_op() -> None:
    """Missing ``sender_patterns`` arg → fail-open with grep-able reason."""
    result = evaluate(
        _INBOX_RULE, {}, {"sender": "tim@substack.com"},
    )
    assert result.skip is False
    assert "missing or non-list" in result.reason


def test_inbox_sender_non_list_patterns_arg_no_op() -> None:
    """``sender_patterns`` as dict / str / int → fail-open."""
    for bad in ({"a": "b"}, "*@substack.com", 42):
        result = evaluate(
            _INBOX_RULE,
            {"sender_patterns": bad},
            {"sender": "any@thing.com"},
        )
        assert result.skip is False, f"non-list {bad!r} should fail-open"
        assert "missing or non-list" in result.reason


def test_inbox_sender_empty_patterns_list_no_op() -> None:
    """Empty ``sender_patterns: []`` → fail-open with explicit reason."""
    result = evaluate(
        _INBOX_RULE,
        {"sender_patterns": []},
        {"sender": "any@thing.com"},
    )
    assert result.skip is False
    assert "empty sender_patterns" in result.reason


def test_inbox_sender_non_string_pattern_entries_silently_skipped() -> None:
    """Defensive: int / dict pattern entries don't break list iteration.

    An operator-yaml accident (stray int in a list of strings)
    shouldn't break gate dispatch for the rest of the list.
    """
    args = {
        "sender_patterns": [42, None, {}, "*@substack.com"],
    }
    candidate = {"sender": "writer@substack.com"}
    result = evaluate(_INBOX_RULE, args, candidate)
    assert result.skip is True


def test_inbox_sender_args_none_tolerated() -> None:
    """``args=None`` (rather than {}) at the top-level dispatch is safe."""
    result = evaluate(_INBOX_RULE, None, {"sender": "x@substack.com"})
    assert result.skip is False  # treated as empty args → missing patterns
    assert "missing or non-list" in result.reason
