"""Tests for ``orchestrator._inject_transport_env_vars`` —
particularly the multi-instance ${VARNAME} resolution + override path.

Bug class (P0 from QA 2026-05-05):
  KAL-LE Daily Sync at 09:00 ADT failed with 401 invalid_token. Root
  cause: shared ``.env`` defines ``ALFRED_TRANSPORT_TOKEN=<salem>``
  AND ``ALFRED_KALLE_TRANSPORT_TOKEN=<kalle>``. KAL-LE's orchestrator
  starts with ``ALFRED_TRANSPORT_TOKEN`` already populated (Salem's
  value, inherited from .env). Pre-fix, ``_inject_transport_env_vars``:
    (a) read ``local.token`` from un-substituted raw → got literal
        ``${ALFRED_KALLE_TRANSPORT_TOKEN}``,
    (b) saw ``startswith("${")`` → declined to inject,
    (c) preserved the inherited Salem token,
    (d) Daily Sync's subprocess sent Salem's token to KAL-LE's own
        transport server → no peer match → 401.

Post-fix:
  * Resolve ``${VARNAME}`` placeholders against ``os.environ`` AT
    INJECTION TIME (via the new ``_resolve_env_placeholders`` helper).
  * OVERRIDE any prior ``ALFRED_TRANSPORT_TOKEN`` value — the
    orchestrator's intent is "this instance's daemons must use THIS
    instance's token", not "preserve inherited env".
  * Defensive: if a placeholder fails to resolve (env var missing),
    keep the literal ``${VARNAME}`` in value and decline to inject;
    transport client's ``_resolve_token`` raises with a clear
    message rather than leaking a literal placeholder.
"""

from __future__ import annotations

import os

import pytest

from alfred.orchestrator import (
    _inject_transport_env_vars,
    _resolve_env_placeholders,
)


# ---------------------------------------------------------------------------
# _resolve_env_placeholders — pure-function unit tests
# ---------------------------------------------------------------------------


class TestResolveEnvPlaceholders:
    def test_no_placeholders_returns_input_unchanged(self):
        assert _resolve_env_placeholders("plain-value") == "plain-value"
        assert _resolve_env_placeholders("") == ""

    def test_resolves_single_placeholder(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "resolved-value")
        assert _resolve_env_placeholders("${MY_TEST_VAR}") == "resolved-value"

    def test_resolves_placeholder_inside_string(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "abc123")
        assert (
            _resolve_env_placeholders("Bearer ${MY_TOKEN}")
            == "Bearer abc123"
        )

    def test_unresolved_placeholder_stays_literal(self, monkeypatch):
        """Defensive: env var missing → ``${VARNAME}`` literal stays
        so callers can detect "still unresolved" via startswith."""
        monkeypatch.delenv("DEFINITELY_NOT_SET_VAR", raising=False)
        assert (
            _resolve_env_placeholders("${DEFINITELY_NOT_SET_VAR}")
            == "${DEFINITELY_NOT_SET_VAR}"
        )

    def test_partial_resolution_keeps_unresolved_literal(self, monkeypatch):
        """Mixed string with one resolved + one unresolved placeholder:
        the resolved one substitutes, the unresolved one stays."""
        monkeypatch.setenv("RESOLVED_VAR", "ok")
        monkeypatch.delenv("MISSING_VAR", raising=False)
        result = _resolve_env_placeholders("${RESOLVED_VAR}/${MISSING_VAR}")
        assert result == "ok/${MISSING_VAR}"


# ---------------------------------------------------------------------------
# _inject_transport_env_vars — multi-instance behaviour
# ---------------------------------------------------------------------------


class TestInjectTransportEnvVars:
    def test_resolves_placeholder_and_sets_env(self, monkeypatch):
        """Per-instance config carries ``${ALFRED_KALLE_TRANSPORT_TOKEN}``
        as the local.token. The injector must resolve against
        os.environ + set ALFRED_TRANSPORT_TOKEN to the resolved value."""
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        monkeypatch.setenv(
            "ALFRED_KALLE_TRANSPORT_TOKEN",
            "kalle-resolved-token-value",
        )
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "${ALFRED_KALLE_TRANSPORT_TOKEN}",
                        },
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        assert (
            os.environ.get("ALFRED_TRANSPORT_TOKEN")
            == "kalle-resolved-token-value"
        )

    def test_OVERRIDES_inherited_env_var(self, monkeypatch):
        """The headline P0 fix: when ALFRED_TRANSPORT_TOKEN is already
        set in env (e.g. Salem started first + populated it via shared
        .env), KAL-LE's orchestrator MUST override with the
        per-instance resolved token. Pre-fix: inherited Salem token
        survived → 401 invalid_token at KAL-LE's own transport."""
        monkeypatch.setenv(
            "ALFRED_TRANSPORT_TOKEN", "salem-token-from-prior-startup",
        )
        monkeypatch.setenv(
            "ALFRED_KALLE_TRANSPORT_TOKEN", "kalle-resolved-token",
        )
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "${ALFRED_KALLE_TRANSPORT_TOKEN}",
                        },
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        # OVERRIDE — KAL-LE's value wins, not Salem's inherited value.
        assert (
            os.environ.get("ALFRED_TRANSPORT_TOKEN")
            == "kalle-resolved-token"
        )

    def test_unresolved_placeholder_does_not_inject(self, monkeypatch):
        """Defensive: placeholder failed to resolve (env var actually
        missing) → don't inject. Let transport client raise a clear
        ``ALFRED_TRANSPORT_TOKEN is not set`` rather than leak a
        literal ``${VARNAME}`` into bearer auth headers."""
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        monkeypatch.delenv(
            "ALFRED_PHANTOM_TOKEN_VAR", raising=False,
        )
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "${ALFRED_PHANTOM_TOKEN_VAR}",
                        },
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        # Token NOT injected — placeholder unresolved.
        assert "ALFRED_TRANSPORT_TOKEN" not in os.environ

    def test_unresolved_placeholder_does_not_clobber_inherited(
        self, monkeypatch,
    ):
        """If the per-instance placeholder fails to resolve BUT a prior
        ALFRED_TRANSPORT_TOKEN is in env, leave the inherited value
        alone. Pre-fix already had this behaviour but for a different
        reason (startswith("${") guard); post-fix preserves it via
        the resolved-still-starts-with-${ check."""
        monkeypatch.setenv(
            "ALFRED_TRANSPORT_TOKEN", "inherited-legacy-token",
        )
        monkeypatch.delenv("ALFRED_MISSING_VAR", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "${ALFRED_MISSING_VAR}",
                        },
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        # Inherited value survives — we declined to override.
        assert (
            os.environ.get("ALFRED_TRANSPORT_TOKEN")
            == "inherited-legacy-token"
        )

    def test_literal_token_in_config_used_directly(self, monkeypatch):
        """If the operator hardcoded a literal token in config (no
        ``${...}`` wrapping), use it as-is. Same OVERRIDE semantics."""
        monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "old-value")
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "literal-hardcoded-token",
                        },
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        assert (
            os.environ.get("ALFRED_TRANSPORT_TOKEN")
            == "literal-hardcoded-token"
        )

    def test_empty_token_does_not_inject(self, monkeypatch):
        monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "preserved")
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": ""},
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        # Empty token doesn't override.
        assert os.environ.get("ALFRED_TRANSPORT_TOKEN") == "preserved"

    def test_missing_local_block_does_not_crash(self, monkeypatch):
        """Defensive: config might omit transport entirely (no daemons
        that use the outbound client). The function should be a no-op
        without raising."""
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        _inject_transport_env_vars({})
        _inject_transport_env_vars({"transport": {}})
        _inject_transport_env_vars({"transport": {"auth": {}}})
        _inject_transport_env_vars({
            "transport": {"auth": {"tokens": {}}},
        })
        # No injection happened; no crash.
        assert "ALFRED_TRANSPORT_TOKEN" not in os.environ

    def test_host_and_port_still_injected(self, monkeypatch):
        """Regression: HOST + PORT injection unchanged (preserves
        prior env to allow .env override per the existing comment)."""
        monkeypatch.delenv("ALFRED_TRANSPORT_HOST", raising=False)
        monkeypatch.delenv("ALFRED_TRANSPORT_PORT", raising=False)
        raw = {
            "transport": {
                "server": {"host": "127.0.0.1", "port": 8892},
            },
        }
        _inject_transport_env_vars(raw)
        assert os.environ["ALFRED_TRANSPORT_HOST"] == "127.0.0.1"
        assert os.environ["ALFRED_TRANSPORT_PORT"] == "8892"

    def test_kalle_after_salem_scenario_end_to_end(self, monkeypatch):
        """End-to-end repro of the QA finding: Salem starts first
        (sets ALFRED_TRANSPORT_TOKEN to its value via .env). KAL-LE
        starts second; its config carries
        ``${ALFRED_KALLE_TRANSPORT_TOKEN}``. After injection, the env
        carries KAL-LE's token, NOT Salem's. KAL-LE's Daily Sync
        subprocess sends KAL-LE's token to KAL-LE's transport →
        match."""
        # Salem started first — .env populated this.
        monkeypatch.setenv(
            "ALFRED_TRANSPORT_TOKEN",
            "b784e394712b5fdf70374e557b2e80e10c86436c4da085df1e0dc5799354a310",
        )
        # KAL-LE's per-instance token also in .env.
        monkeypatch.setenv(
            "ALFRED_KALLE_TRANSPORT_TOKEN",
            "1c8c04e784737f91059fef042256c64b1cee9dc158d99465a2dc0d054b50b103",
        )
        # KAL-LE's config (mirrors live config.kalle.yaml shape).
        raw = {
            "transport": {
                "server": {"host": "127.0.0.1", "port": 8892},
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "${ALFRED_KALLE_TRANSPORT_TOKEN}",
                            "allowed_clients": [
                                "scheduler", "brief", "janitor",
                                "curator", "talker", "daily_sync",
                            ],
                        },
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        # KAL-LE's token wins after injection.
        assert (
            os.environ["ALFRED_TRANSPORT_TOKEN"]
            == "1c8c04e784737f91059fef042256c64b1cee9dc158d99465a2dc0d054b50b103"
        )
