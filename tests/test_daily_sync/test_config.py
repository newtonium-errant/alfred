"""Tests for the Daily Sync config loader.

Covers:
- Block absent → enabled=False default config.
- Block present with enabled: true → enabled flag set, schedule honoured.
- Confidence flags load with defaults all False.
- Env var substitution in nested string values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.daily_sync.config import DailySyncConfig, load_config, load_from_unified


def test_block_absent_returns_disabled():
    cfg = load_from_unified({"vault": {"path": "/tmp"}})
    assert cfg.enabled is False
    assert isinstance(cfg, DailySyncConfig)


def test_block_present_enables():
    raw = {
        "daily_sync": {
            "enabled": True,
            "schedule": {"time": "09:30", "timezone": "America/Halifax"},
            "batch_size": 7,
            "corpus": {"path": "./data/x.jsonl"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.enabled is True
    assert cfg.schedule.time == "09:30"
    assert cfg.batch_size == 7
    assert cfg.corpus.path == "./data/x.jsonl"


def test_confidence_block_loads():
    raw = {
        "daily_sync": {
            "enabled": True,
            "confidence": {"high": True, "spam": True},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.confidence.high is True
    assert cfg.confidence.spam is True
    assert cfg.confidence.medium is False  # default
    assert cfg.confidence.low is False  # default


def test_env_substitution(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MY_DS_PATH", "/tmp/test.jsonl")
    raw = {
        "daily_sync": {
            "enabled": True,
            "corpus": {"path": "${MY_DS_PATH}"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.corpus.path == "/tmp/test.jsonl"


def test_unknown_keys_silently_ignored():
    raw = {
        "daily_sync": {
            "enabled": True,
            "future_key": "future_value",  # not a real field
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.enabled is True


def test_defaults_when_block_present_but_minimal():
    cfg = load_from_unified({"daily_sync": {"enabled": True}})
    assert cfg.enabled is True
    assert cfg.batch_size == 5
    assert cfg.schedule.time == "09:00"
    assert cfg.schedule.timezone == "America/Halifax"


# --- config_path threading (c8) ----------------------------------------
#
# Mirror of TalkerConfig.config_path tests in
# tests/telegram/test_conversation_peer_tools.py — see commit 420364b.
# The synthetic ``_config_path`` key is set by the CLI in
# ``_load_unified_config`` and rides along with the raw dict through
# multiprocessing pickling to subprocess daemons. The canonical-proposals
# queue-path helpers read ``DailySyncConfig.config_path`` to re-load
# the SAME file (not Salem's default ``config.yaml``).


def test_config_path_default_none():
    """Without ``_config_path`` in raw, ``config_path`` stays None —
    backward compat for existing test fixtures that build raw dicts
    manually."""
    cfg = load_from_unified({"daily_sync": {"enabled": True}})
    assert cfg.config_path is None


def test_load_from_unified_picks_up_synthetic_path():
    """``load_from_unified`` reads ``_config_path`` from the raw dict —
    set by the CLI's ``_load_unified_config`` before handing raw to the
    orchestrator. Tests the multiprocessing-pickle path where the path
    can't be a function arg."""
    raw = {
        "_config_path": "/etc/alfred/config.hypatia.yaml",
        "daily_sync": {"enabled": True},
    }
    cfg = load_from_unified(raw)
    assert cfg.config_path == "/etc/alfred/config.hypatia.yaml"


def test_load_from_unified_picks_up_synthetic_path_when_block_absent():
    """Even with no ``daily_sync`` block, the synthetic ``_config_path``
    still flows through onto the disabled-default config. Otherwise a
    Hypatia config that just omits the daily_sync block would lose the
    path on its way through ``load_from_unified``."""
    raw = {
        "_config_path": "/etc/alfred/config.hypatia.yaml",
        "vault": {"path": "/tmp"},
    }
    cfg = load_from_unified(raw)
    assert cfg.enabled is False
    assert cfg.config_path == "/etc/alfred/config.hypatia.yaml"


def test_load_from_unified_ignores_non_string_synthetic_path():
    """Defensive: if the synthetic key gets corrupted (None, list, int,
    empty string), we don't crash and we don't set ``config_path``."""
    for bad in (None, [], 42, ""):
        raw = {
            "_config_path": bad,
            "daily_sync": {"enabled": True},
        }
        cfg = load_from_unified(raw)
        assert cfg.config_path is None, f"unexpected for {bad!r}"


def test_load_config_stamps_resolved_path(tmp_path: Path):
    """``load_config(path)`` populates ``DailySyncConfig.config_path``
    with the resolved absolute path. Tests the load-side half of the
    fix — when the path is known directly, no synthetic-key dance is
    needed."""
    config_file = tmp_path / "config.test.yaml"
    config_file.write_text(
        "daily_sync:\n"
        "  enabled: true\n"
        "vault:\n"
        "  path: /tmp/v\n",
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.config_path == str(config_file.resolve())
    assert cfg.enabled is True
