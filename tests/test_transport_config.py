"""Smoke tests for ``alfred.transport.config``.

Mirrors ``tests/test_instructor_config.py``: defaults round-trip,
overrides apply, ``${VAR}`` substitution fires before dataclass
construction, and the per-peer ``auth.tokens`` schema round-trips so
Stage 3.5 can extend in place.
"""

from __future__ import annotations

import pytest

from alfred.transport.config import (
    AuthTokenEntry,
    TransportConfig,
    load_from_unified,
)


def test_load_from_empty_unified_returns_defaults() -> None:
    """An empty unified config produces a fully-defaulted TransportConfig."""
    cfg = load_from_unified({})
    assert isinstance(cfg, TransportConfig)

    # Server defaults — localhost fixed port per ratified recommendation 1.
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8891

    # Scheduler knobs match ratified recs 7 + 8.
    assert cfg.scheduler.poll_interval_seconds == 30
    assert cfg.scheduler.stale_reminder_max_minutes == 180

    # Auth dict starts empty — deliberate: a missing config is a
    # mis-deploy and the server's auth middleware fails closed.
    assert cfg.auth.tokens == {}

    # State defaults.
    assert cfg.state.path == "./data/transport_state.json"
    assert cfg.state.dead_letter_max_age_days == 30


def test_load_from_unified_applies_section_overrides() -> None:
    """Values under ``transport:`` override dataclass defaults."""
    raw = {
        "transport": {
            "server": {"host": "0.0.0.0", "port": 9999},
            "scheduler": {
                "poll_interval_seconds": 15,
                "stale_reminder_max_minutes": 60,
            },
            "auth": {
                "tokens": {
                    "local": {
                        "token": "DUMMY_TRANSPORT_TEST_TOKEN",
                        "allowed_clients": ["scheduler", "brief"],
                    },
                    "kal-le": {
                        "token": "DUMMY_KAL_LE_TEST_TOKEN",
                        "allowed_clients": ["kal-le"],
                    },
                },
            },
            "state": {
                "path": "./custom/transport.json",
                "dead_letter_max_age_days": 7,
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9999
    assert cfg.scheduler.poll_interval_seconds == 15
    assert cfg.scheduler.stale_reminder_max_minutes == 60

    # Per-peer tokens dict — Stage 3.5 pre-commit: more than one entry
    # populates the dict without any schema rewrite.
    assert set(cfg.auth.tokens.keys()) == {"local", "kal-le"}
    local = cfg.auth.tokens["local"]
    assert isinstance(local, AuthTokenEntry)
    assert local.token == "DUMMY_TRANSPORT_TEST_TOKEN"
    assert local.allowed_clients == ["scheduler", "brief"]

    kal = cfg.auth.tokens["kal-le"]
    assert kal.token == "DUMMY_KAL_LE_TEST_TOKEN"
    assert kal.allowed_clients == ["kal-le"]

    assert cfg.state.path == "./custom/transport.json"
    assert cfg.state.dead_letter_max_age_days == 7


def test_load_from_unified_substitutes_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${VAR}`` placeholders resolve against the process environment."""
    monkeypatch.setenv("TEST_TRANSPORT_TOKEN", "DUMMY_TRANSPORT_TEST_TOKEN")
    raw = {
        "transport": {
            "auth": {
                "tokens": {
                    "local": {
                        "token": "${TEST_TRANSPORT_TOKEN}",
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.auth.tokens["local"].token == "DUMMY_TRANSPORT_TEST_TOKEN"


def test_load_from_unified_leaves_placeholder_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env vars leave the literal placeholder intact.

    Matches every other tool's config policy — downstream callers
    (server auth middleware) treat the placeholder as "unset".
    """
    monkeypatch.delenv("ABSENT_TRANSPORT_TOKEN", raising=False)
    raw = {
        "transport": {
            "auth": {
                "tokens": {
                    "local": {
                        "token": "${ABSENT_TRANSPORT_TOKEN}",
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.auth.tokens["local"].token == "${ABSENT_TRANSPORT_TOKEN}"


def test_load_from_unified_ignores_unknown_scalar_keys() -> None:
    """Unknown keys under nested sections are dropped, not raised.

    Keeps forward-compat room for Stage 3.5 additions.
    """
    raw = {
        "transport": {
            "server": {"host": "127.0.0.1", "future_field": 42},
            "auth": {
                "tokens": {
                    "local": {
                        "token": "DUMMY_TRANSPORT_TEST_TOKEN",
                        "allowed_clients": ["local"],
                        "future_permission_list": ["read"],
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.auth.tokens["local"].token == "DUMMY_TRANSPORT_TEST_TOKEN"


def test_example_config_transport_section_loads() -> None:
    """The ``transport:`` section in the example config loads cleanly.

    Catches a common break: the example drifts from the dataclass
    shape and every fresh install loads a broken config.
    """
    import yaml
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "config.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert "transport" in raw, "config.yaml.example must ship a transport section"
    cfg = load_from_unified(raw)
    assert cfg.server.port == 8891
    assert "local" in cfg.auth.tokens
    assert cfg.auth.tokens["local"].allowed_clients
