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
    INJECTION TIME (via the canonical ``alfred._env.resolve_env_placeholders``
    helper — see ``tests/test_env_helpers.py`` for the helper's own unit
    tests; this file covers the ORCHESTRATOR-side integration).
  * OVERRIDE any prior ``ALFRED_TRANSPORT_TOKEN`` value — the
    orchestrator's intent is "this instance's daemons must use THIS
    instance's token", not "preserve inherited env".
  * Defensive: if a placeholder fails to resolve (env var missing OR
    set-to-empty-string per the helper's coalesce semantics), keep
    the literal ``${VARNAME}`` in value and decline to inject;
    transport client's ``_resolve_token`` raises with a clear
    message rather than leaking a literal placeholder.
  * Per ``feedback_intentionally_left_blank.md``: emit one structured
    info log per call so operator can confirm "KAL-LE booted with
    KAL-LE's token, not Salem's" from orchestrator log alone.
"""

from __future__ import annotations

import os

import pytest
import structlog

from alfred.orchestrator import _inject_transport_env_vars


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


# ---------------------------------------------------------------------------
# Observability — orchestrator.transport_token.injected log event
# ---------------------------------------------------------------------------
#
# Per ``feedback_intentionally_left_blank.md``: the load-bearing
# override path MUST be observable. Without a per-call log, the
# operator can't tell from ``data/orchestrator.log`` whether KAL-LE
# booted with KAL-LE's token, fell into the placeholder-unresolved
# branch, or had an empty config token. Silent-good is silent-broken's
# cousin (review-block B from QA 2026-05-05).


class TestObservabilityLog:
    def test_log_fires_on_placeholder_resolved_path(self, monkeypatch):
        monkeypatch.setenv(
            "ALFRED_KALLE_TRANSPORT_TOKEN", "kalle-resolved-token-value",
        )
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_KALLE_TRANSPORT_TOKEN}"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        e = events[0]
        assert e["source"] == "placeholder_resolved"
        assert e["overrode_inherited"] is False
        # Fingerprint = first 8 chars + "..." — enough for operator
        # to disambiguate Salem vs KAL-LE without leaking the secret.
        assert e["token_fingerprint"] == "kalle-re..."

    def test_log_fires_on_literal_token_path(self, monkeypatch):
        """Operator hardcoded a literal token in config (no ${...})
        — log says ``source=literal``."""
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "literal-hardcoded-token"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        assert events[0]["source"] == "literal"
        assert events[0]["token_fingerprint"] == "literal-..."

    def test_log_fires_on_skipped_unresolved_path_no_prior_env(
        self, monkeypatch,
    ):
        """Placeholder failed to resolve + ALFRED_TRANSPORT_TOKEN was
        NOT set in env — log says ``source=skipped_unresolved``,
        ``had_prior_env=False``, includes placeholder for operator
        to fix."""
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        monkeypatch.delenv("ALFRED_PHANTOM_VAR", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_PHANTOM_VAR}"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        assert events[0]["source"] == "skipped_unresolved"
        assert events[0]["placeholder"] == "${ALFRED_PHANTOM_VAR}"
        assert events[0]["had_prior_env"] is False
        # No fingerprint on the skipped path — nothing was injected.
        assert "token_fingerprint" not in events[0]

    def test_log_fires_on_skipped_unresolved_path_with_prior_env(
        self, monkeypatch,
    ):
        """Placeholder failed to resolve BUT ALFRED_TRANSPORT_TOKEN
        was set in env (e.g. inherited from sibling-instance startup
        or .env). Log MUST report ``had_prior_env=True`` so the
        operator's grep-for-stale-inherit catches the silent-survival
        case — config wanted to override but couldn't, prior env
        kept serving requests with the wrong token. Without this
        assertion, a regression where ``had_prior_env`` returns
        False despite a prior env being present would slip through."""
        monkeypatch.setenv(
            "ALFRED_TRANSPORT_TOKEN", "stale-inherited-from-elsewhere",
        )
        monkeypatch.delenv("ALFRED_PHANTOM_VAR", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_PHANTOM_VAR}"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        assert events[0]["source"] == "skipped_unresolved"
        assert events[0]["had_prior_env"] is True
        assert events[0]["placeholder"] == "${ALFRED_PHANTOM_VAR}"
        # Decline-to-inject preserves the inherited value — defensive
        # path AND the operator-visibility signal that says "this
        # instance is running with a token from somewhere else."
        assert (
            os.environ["ALFRED_TRANSPORT_TOKEN"]
            == "stale-inherited-from-elsewhere"
        )

    def test_log_fires_on_empty_config_token_path(self, monkeypatch):
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {"local": {"token": ""}},
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        assert events[0]["source"] == "empty_config_token"
        assert events[0]["had_prior_env"] is False

    def test_log_fires_with_overrode_inherited_true(self, monkeypatch):
        """Headline observability case: KAL-LE-after-Salem. Prior env
        token differs from resolved token → ``overrode_inherited=True``
        so operator can grep for "did we silently override an
        inherited value?" — the question that surfaced today's bug."""
        monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "salem-stale-token")
        monkeypatch.setenv(
            "ALFRED_KALLE_TRANSPORT_TOKEN", "kalle-fresh-token",
        )
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_KALLE_TRANSPORT_TOKEN}"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        assert events[0]["overrode_inherited"] is True
        assert events[0]["token_fingerprint"] == "kalle-fr..."

    def test_log_overrode_false_when_prior_env_matches_resolved(
        self, monkeypatch,
    ):
        """Salem-shape no-op: prior env value equals resolved value
        (Salem's ${ALFRED_TRANSPORT_TOKEN} resolves to the same
        ALFRED_TRANSPORT_TOKEN that's already in env). Override
        executes but is identity → ``overrode_inherited=False``."""
        monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "salem-token-value")
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_TRANSPORT_TOKEN}"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
        ]
        assert len(events) == 1
        assert events[0]["source"] == "placeholder_resolved"
        assert events[0]["overrode_inherited"] is False


# ---------------------------------------------------------------------------
# Empty-string env coalesce — review-block A
# ---------------------------------------------------------------------------
#
# An operator who intentionally empties a token via
# ``ALFRED_KALLE_TRANSPORT_TOKEN=""`` (testing auth-failure paths)
# should hit the same defensive guard as one who never set the var.
# Pre-fix used ``os.environ.get(name, fallback)`` which returns ""
# for the empty case — the empty string would propagate as
# ``Bearer `` (empty) to subprocesses → still 401, but a different
# shape than "placeholder unresolved." Canonical helper coalesces
# both via ``or``.


class TestEmptyStringEnvCoalesce:
    def test_empty_env_var_treated_as_unresolved(self, monkeypatch):
        """``ALFRED_KALLE_TRANSPORT_TOKEN=""`` (set, but empty) →
        placeholder stays literal → injector declines to inject."""
        monkeypatch.setenv("ALFRED_KALLE_TRANSPORT_TOKEN", "")
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_KALLE_TRANSPORT_TOKEN}"},
                    },
                },
            },
        }
        _inject_transport_env_vars(raw)
        # Token NOT injected — empty env var coalesced to literal.
        assert "ALFRED_TRANSPORT_TOKEN" not in os.environ

    def test_empty_env_var_emits_skipped_unresolved_log(self, monkeypatch):
        """The decline-to-inject path on empty env var must emit the
        same diagnostic shape as the never-set case so operator's grep
        catches both."""
        monkeypatch.setenv("ALFRED_KALLE_TRANSPORT_TOKEN", "")
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
        raw = {
            "transport": {
                "auth": {
                    "tokens": {
                        "local": {"token": "${ALFRED_KALLE_TRANSPORT_TOKEN}"},
                    },
                },
            },
        }
        with structlog.testing.capture_logs() as captured:
            _inject_transport_env_vars(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.transport_token.injected"
            and c.get("source") == "skipped_unresolved"
        ]
        assert len(events) == 1
        assert events[0]["placeholder"] == "${ALFRED_KALLE_TRANSPORT_TOKEN}"


# ---------------------------------------------------------------------------
# _auto_load_dotenv_for_config — operator-gotcha closer (P1 from QA 2026-05-05)
# ---------------------------------------------------------------------------
#
# Companion path to ``_inject_transport_env_vars``. The injector resolves
# ``${VARNAME}`` placeholders against os.environ; the auto-loader makes
# sure os.environ actually HAS the right per-instance vars in the first
# place, even when the operator forgot ``set -a; source .env``.
#
# Invariants under test:
#   1. .env present + var-not-in-environ → loaded into environ,
#      ``_inject_transport_env_vars`` then resolves the placeholder.
#   2. .env present + var-in-environ already → existing value WINS
#      (override=False semantics — manual debugging stays predictable).
#   3. .env absent → no-op + ``orchestrator.dotenv_missing`` info log.
#   4. .env present but parses zero KEY=value lines (empty / all comments)
#      → no-op + ``orchestrator.dotenv_empty`` info log.


class TestAutoLoadDotenvForConfig:
    """Orchestrator-side integration. Wires ``_config_path`` synthetic
    key on raw → resolves to sibling .env path → invokes
    ``_env.auto_load_dotenv``. Covers the four spec cases."""

    def test_present_var_not_in_environ_loads(self, tmp_path, monkeypatch):
        """Headline case: operator ran ``alfred up`` from a fresh shell.
        The per-instance var (``ALFRED_KALLE_TRANSPORT_TOKEN``) isn't in
        environ. .env has it. Auto-loader fills the gap; the injector
        then resolves the placeholder cleanly."""
        from alfred.orchestrator import (
            _auto_load_dotenv_for_config,
            _inject_transport_env_vars,
        )

        monkeypatch.delenv("ALFRED_KALLE_TRANSPORT_TOKEN", raising=False)
        monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)

        config = tmp_path / "config.kalle.yaml"
        config.write_text("# kalle config", encoding="utf-8")
        env = tmp_path / ".env"
        env.write_text(
            "ALFRED_KALLE_TRANSPORT_TOKEN=kalle-loaded-from-dotenv\n",
            encoding="utf-8",
        )

        raw = {
            "_config_path": str(config),
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
        _auto_load_dotenv_for_config(raw)
        # .env loaded → per-instance token now in environ.
        assert (
            os.environ["ALFRED_KALLE_TRANSPORT_TOKEN"]
            == "kalle-loaded-from-dotenv"
        )
        # And the downstream injector resolves cleanly.
        _inject_transport_env_vars(raw)
        assert (
            os.environ.get("ALFRED_TRANSPORT_TOKEN")
            == "kalle-loaded-from-dotenv"
        )

    def test_present_var_already_in_environ_existing_wins(
        self, tmp_path, monkeypatch,
    ):
        """``override=False`` contract: an explicit
        ``export ALFRED_KALLE_TRANSPORT_TOKEN=...`` in the parent shell
        survives. .env only fills gaps; manual-debug overrides stay
        predictable."""
        from alfred.orchestrator import _auto_load_dotenv_for_config

        monkeypatch.setenv(
            "ALFRED_KALLE_TRANSPORT_TOKEN", "from-parent-shell-export",
        )

        config = tmp_path / "config.kalle.yaml"
        config.write_text("# kalle config", encoding="utf-8")
        env = tmp_path / ".env"
        env.write_text(
            "ALFRED_KALLE_TRANSPORT_TOKEN=stale-value-in-dotenv\n",
            encoding="utf-8",
        )

        raw = {"_config_path": str(config)}
        _auto_load_dotenv_for_config(raw)
        # Parent-shell value preserved; .env did NOT clobber.
        assert (
            os.environ["ALFRED_KALLE_TRANSPORT_TOKEN"]
            == "from-parent-shell-export"
        )

    def test_absent_dotenv_no_op_with_missing_log(self, tmp_path):
        """Production deploys (systemd, k8s) set env directly; .env
        absence is the common case there. Must be a no-op + emit
        ``orchestrator.dotenv_missing`` info log so operator can
        confirm the auto-loader fired (not an unrelated failure)."""
        from alfred.orchestrator import _auto_load_dotenv_for_config

        config = tmp_path / "config.kalle.yaml"
        config.write_text("# kalle config", encoding="utf-8")
        # NOTE: no .env created.
        raw = {"_config_path": str(config)}

        with structlog.testing.capture_logs() as captured:
            _auto_load_dotenv_for_config(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.dotenv_missing"
        ]
        assert len(events) == 1
        assert events[0]["path"] == str(tmp_path / ".env")

    def test_empty_dotenv_no_op_with_empty_log(self, tmp_path):
        """File present but parses zero KEY=value lines (empty file
        OR all-comments OR all-malformed). Distinct log shape from the
        ``missing`` case — operator can grep
        ``orchestrator.dotenv_empty`` to spot a .env that exists but
        isn't doing anything (likely misformatted)."""
        from alfred.orchestrator import _auto_load_dotenv_for_config

        config = tmp_path / "config.kalle.yaml"
        config.write_text("# kalle config", encoding="utf-8")
        env = tmp_path / ".env"
        env.write_text("# all comments\n# nothing else\n", encoding="utf-8")
        raw = {"_config_path": str(config)}

        with structlog.testing.capture_logs() as captured:
            _auto_load_dotenv_for_config(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.dotenv_empty"
        ]
        assert len(events) == 1
        assert events[0]["path"] == str(env)

    def test_loaded_log_emits_counts(self, tmp_path, monkeypatch):
        """The ``loaded`` log path must report
        ``vars_loaded`` + ``vars_skipped_existing`` counts (NOT key
        names — secret-shaped values land in .env). Operator can read
        ``loaded=2 skipped=1`` from logs alone to confirm the .env
        contributed AND that some env vars were preserved."""
        from alfred.orchestrator import _auto_load_dotenv_for_config

        monkeypatch.delenv("DOTENV_INTEG_FRESH_A", raising=False)
        monkeypatch.delenv("DOTENV_INTEG_FRESH_B", raising=False)
        monkeypatch.setenv("DOTENV_INTEG_PRESET", "preset-value")

        config = tmp_path / "config.kalle.yaml"
        config.write_text("# kalle config", encoding="utf-8")
        env = tmp_path / ".env"
        env.write_text(
            "DOTENV_INTEG_FRESH_A=a\n"
            "DOTENV_INTEG_FRESH_B=b\n"
            "DOTENV_INTEG_PRESET=ignored\n",
            encoding="utf-8",
        )
        raw = {"_config_path": str(config)}

        with structlog.testing.capture_logs() as captured:
            _auto_load_dotenv_for_config(raw)

        events = [
            c for c in captured
            if c.get("event") == "orchestrator.dotenv_loaded"
        ]
        assert len(events) == 1
        ev = events[0]
        assert ev["vars_loaded"] == 2
        assert ev["vars_skipped_existing"] == 1
        # Secret hygiene: counts only, never key names.
        assert "key" not in ev
        assert "keys" not in ev

    def test_no_config_path_falls_back_to_cwd(self, tmp_path, monkeypatch):
        """Legacy callers / tests that build raw inline without going
        through ``_load_unified_config`` won't have ``_config_path``
        on raw. Auto-loader falls back to CWD-relative ``.env``."""
        from alfred.orchestrator import _auto_load_dotenv_for_config

        monkeypatch.chdir(tmp_path)
        env = tmp_path / ".env"
        env.write_text("DOTENV_CWD_FALLBACK=cwd-found\n", encoding="utf-8")
        monkeypatch.delenv("DOTENV_CWD_FALLBACK", raising=False)

        raw: dict = {}  # No _config_path key.
        _auto_load_dotenv_for_config(raw)
        assert os.environ.get("DOTENV_CWD_FALLBACK") == "cwd-found"
