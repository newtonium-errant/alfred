"""Tests for ``alfred.cloudflared.config.load_from_unified``.

Pinning behavior:

- Empty / absent section → disabled config + no crash
- All fields resolve correctly when present
- ``~`` expansion happens at load time (not deferred to daemon runtime)
- Default ``binary_path`` is the standard install location
- Default ``log_path`` derives from ``logging.dir`` when unset
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alfred.cloudflared.config import (
    DEFAULT_BINARY_PATH,
    CloudflaredConfig,
    load_from_unified,
)


def test_absent_section_returns_disabled_config() -> None:
    """No ``cloudflared:`` block → enabled=False, everything default.

    KAL-LE / Hypatia / instances that don't need a tunnel leave the
    block out entirely. The loader must produce a disabled config so
    the orchestrator's auto-start gate filters it out cleanly.
    """
    raw = {"logging": {"dir": "./data"}}
    config = load_from_unified(raw)
    assert config.enabled is False
    assert config.tunnel_id == ""
    assert config.binary_path == DEFAULT_BINARY_PATH


def test_null_section_treated_as_absent() -> None:
    """``cloudflared: null`` in YAML (commented-out-then-uncommented) → disabled.

    YAML's ``null`` parses to Python None. We tolerate this rather
    than crashing — the operator's intent is "block effectively
    absent."
    """
    raw = {"cloudflared": None}
    config = load_from_unified(raw)
    assert config.enabled is False


def test_enabled_block_fully_populated() -> None:
    """All fields resolve when present."""
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "5e44e541-b24c-4caa-8246-105559dd8744",
            "config_path": "/etc/cloudflared/config.yml",
            "binary_path": "/opt/cloudflared/cloudflared",
            "log_path": "./data/tunnel.log",
        }
    }
    config = load_from_unified(raw)
    assert config.enabled is True
    assert config.tunnel_id == "5e44e541-b24c-4caa-8246-105559dd8744"
    assert config.config_path == "/etc/cloudflared/config.yml"
    assert config.binary_path == "/opt/cloudflared/cloudflared"
    assert config.log_path == "./data/tunnel.log"


def test_tilde_expansion_happens_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """``~/.cloudflared/config.yml`` resolves at load time, not runtime.

    Reason: the daemon's child subprocess inherits the parent's CWD
    but NOT the parent's shell-expansion. ``~`` left literal in
    ``proc.Popen([...])`` would be passed as the string ``"~"`` to
    cloudflared — wrong. Expanding at load time means the daemon sees
    the absolute path.
    """
    monkeypatch.setenv("HOME", "/home/test-user")
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "config_path": "~/.cloudflared/config.yml",
        }
    }
    config = load_from_unified(raw)
    assert config.config_path == "/home/test-user/.cloudflared/config.yml"


def test_default_log_path_derives_from_logging_dir() -> None:
    """Empty ``log_path`` → ``<logging.dir>/cloudflared.log``.

    Matches the other daemons' default log locations so operators
    can find all daemon logs in one directory.
    """
    raw = {
        "logging": {"dir": "/var/lib/alfred/data"},
        "cloudflared": {"enabled": True, "tunnel_id": "abc"},
    }
    config = load_from_unified(raw)
    assert config.log_path == "/var/lib/alfred/data/cloudflared.log"


def test_default_log_path_fallback_when_logging_dir_missing() -> None:
    """Empty logging block → ``./data/cloudflared.log`` (bare default)."""
    raw = {"cloudflared": {"enabled": True, "tunnel_id": "abc"}}
    config = load_from_unified(raw)
    assert config.log_path == "data/cloudflared.log" or config.log_path == "./data/cloudflared.log"


def test_default_binary_path_is_standard_install_location() -> None:
    """Empty ``binary_path`` → ``/usr/local/bin/cloudflared``.

    Standard install location on Debian/Ubuntu/WSL — what the operator
    has when they install via the official install script.
    """
    raw = {
        "cloudflared": {"enabled": True, "tunnel_id": "abc"},
    }
    config = load_from_unified(raw)
    assert config.binary_path == "/usr/local/bin/cloudflared"


def test_empty_binary_path_falls_back_to_default() -> None:
    """Empty-string ``binary_path`` falls back to DEFAULT_BINARY_PATH.

    Reason: an operator might explicitly set ``binary_path: ""`` to
    "use the default" by analogy with bit.schedule.time. The loader
    should match that intent rather than passing the empty string
    through to the daemon (which would fail-fast with binary_missing,
    but with a confusing error message).
    """
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "binary_path": "",
        },
    }
    config = load_from_unified(raw)
    assert config.binary_path == DEFAULT_BINARY_PATH


def test_disabled_explicit_false() -> None:
    """``enabled: false`` is honored even with other fields set."""
    raw = {
        "cloudflared": {
            "enabled": False,
            "tunnel_id": "abc",
        },
    }
    config = load_from_unified(raw)
    assert config.enabled is False
    # Other fields still parse — useful for the orchestrator's log line.
    assert config.tunnel_id == "abc"


def test_schema_tolerance_ignores_unknown_fields() -> None:
    """Schema-tolerance contract: unknown fields don't crash the loader.

    Per CLAUDE.md "State persistence — load() schema-tolerance
    contract": the loader filters incoming keys against the dataclass's
    known fields before constructing instances. A config file with an
    extra key (forward-compat from a newer version, or operator typo)
    should load without raising ``TypeError`` on the unexpected keyword.
    """
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "future_unknown_field": "would-crash-without-filter",
            "another_typo_field": 12345,
        },
    }
    config = load_from_unified(raw)
    # Known fields still parsed correctly.
    assert config.enabled is True
    assert config.tunnel_id == "abc"
    # Unknown fields silently ignored — no attribute exposed.
    assert not hasattr(config, "future_unknown_field")
    assert not hasattr(config, "another_typo_field")


def test_metrics_port_default() -> None:
    """``metrics_port`` defaults to 20241 (cloudflared's own default)."""
    raw = {
        "cloudflared": {"enabled": True, "tunnel_id": "abc"},
    }
    config = load_from_unified(raw)
    assert config.metrics_port == 20241


def test_metrics_port_override() -> None:
    """Operator can override ``metrics_port`` if 20241 is taken."""
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "metrics_port": 30241,
        },
    }
    config = load_from_unified(raw)
    assert config.metrics_port == 30241


def test_metrics_port_string_coerced() -> None:
    """YAML-quoted ``"20241"`` coerces to int 20241.

    Reason: YAML 1.1 parses ``20241`` as int but quoted forms parse as
    str. The loader tolerates either form rather than crashing on type
    mismatch.
    """
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "metrics_port": "30241",
        },
    }
    config = load_from_unified(raw)
    assert config.metrics_port == 30241


def test_metrics_port_invalid_falls_back_to_default() -> None:
    """Garbage ``metrics_port`` falls back to default rather than crash.

    A misconfigured port surfaces at probe time (the health probe
    returns FAIL on "unreachable") rather than crashing the loader.
    """
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "metrics_port": "not-a-number",
        },
    }
    config = load_from_unified(raw)
    assert config.metrics_port == 20241


def test_metrics_url_property() -> None:
    """``metrics_url`` composes from ``metrics_port``."""
    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "metrics_port": 22222,
        },
    }
    config = load_from_unified(raw)
    assert config.metrics_url == "http://localhost:22222/metrics"
