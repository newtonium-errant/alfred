"""Smoke tests for ``alfred.instructor.config``.

Verifies defaults round-trip through ``load_from_unified`` the same way
every other tool's config does: missing sections produce default values,
explicit values override, and environment-variable substitution fires
before dataclass construction.
"""

from __future__ import annotations

import os

import pytest

from alfred.instructor.config import (
    InstructorConfig,
    load_from_unified,
)


def test_load_from_empty_unified_returns_defaults() -> None:
    """An empty unified config produces a fully-defaulted InstructorConfig."""
    cfg = load_from_unified({})
    assert isinstance(cfg, InstructorConfig)

    # Core scalar defaults.
    assert cfg.poll_interval_seconds == 60
    assert cfg.max_retries == 3
    assert cfg.audit_window_size == 5

    # Destructive-keyword gate — must be a tuple (immutable) with the
    # documented set of keywords.
    assert isinstance(cfg.destructive_keywords, tuple)
    for kw in ("delete", "remove", "drop", "purge", "wipe", "clear all"):
        assert kw in cfg.destructive_keywords

    # Nested dataclasses.
    assert cfg.anthropic.model == "claude-sonnet-4-6"
    assert cfg.anthropic.max_tokens == 4096
    assert cfg.state.path == "./data/instructor_state.json"
    assert cfg.logging.level == "INFO"


def test_load_from_unified_applies_instructor_section_overrides() -> None:
    """Values under ``instructor:`` override dataclass defaults."""
    raw = {
        "instructor": {
            "poll_interval_seconds": 30,
            "max_retries": 5,
            "audit_window_size": 7,
            "anthropic": {
                "api_key": "DUMMY_ANTHROPIC_TEST_KEY",
                "model": "claude-sonnet-4-7",
                "max_tokens": 8192,
            },
            "state": {"path": "./custom/path.json"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.poll_interval_seconds == 30
    assert cfg.max_retries == 5
    assert cfg.audit_window_size == 7
    assert cfg.anthropic.api_key == "DUMMY_ANTHROPIC_TEST_KEY"
    assert cfg.anthropic.model == "claude-sonnet-4-7"
    assert cfg.anthropic.max_tokens == 8192
    assert cfg.state.path == "./custom/path.json"


def test_load_from_unified_substitutes_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${VAR}`` placeholders resolve against the process environment."""
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "DUMMY_ANTHROPIC_TEST_KEY")
    raw = {
        "instructor": {
            "anthropic": {"api_key": "${TEST_ANTHROPIC_KEY}"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.anthropic.api_key == "DUMMY_ANTHROPIC_TEST_KEY"


def test_load_from_unified_leaves_placeholder_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env vars leave the ``${VAR}`` placeholder intact.

    Matches every other tool's config policy — downstream callers treat
    the literal placeholder as "unset" rather than raising at load time,
    so the health check (not the config loader) is the chokepoint for
    API-key presence.
    """
    monkeypatch.delenv("ABSENT_INSTRUCTOR_KEY", raising=False)
    raw = {
        "instructor": {
            "anthropic": {"api_key": "${ABSENT_INSTRUCTOR_KEY}"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.anthropic.api_key == "${ABSENT_INSTRUCTOR_KEY}"


def test_load_from_unified_maps_logging_dir_to_file() -> None:
    """The shared ``logging.dir`` key produces a per-tool log file path."""
    raw = {"logging": {"dir": "./tmp_data", "level": "DEBUG"}}
    cfg = load_from_unified(raw)
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.file == "./tmp_data/instructor.log"


def test_load_from_unified_accepts_destructive_keywords_list() -> None:
    """YAML lists for destructive_keywords are coerced to tuple.

    YAML has no tuple type; users configure the keyword list as a list
    and we convert to tuple inside ``_build`` to preserve the
    immutability contract on the dataclass field.
    """
    raw = {
        "instructor": {
            "destructive_keywords": ["wipe", "nuke", "trash"],
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.destructive_keywords == ("wipe", "nuke", "trash")
    assert isinstance(cfg.destructive_keywords, tuple)
