"""Tests for the Daily Sync config loader.

Covers:
- Block absent → enabled=False default config.
- Block present with enabled: true → enabled flag set, schedule honoured.
- Confidence flags load with defaults all False.
- Env var substitution in nested string values.
"""

from __future__ import annotations

import pytest

from alfred.daily_sync.config import DailySyncConfig, load_from_unified


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
