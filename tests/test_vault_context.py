"""Tests for the :class:`VaultContext` typed dispatcher-state container.

V1 of the env-var → function-arg refactor (see
``src/alfred/vault/context.py`` module docstring for the full design
rationale + backward-compat tier). These pins lock the contract:

  - Direct construction sets fields verbatim, defaults to all-None.
  - ``for_testing()`` coerces Path inputs to str.
  - ``from_env()`` reads the 4 vault env vars; emits the structured
    fallback log per ``feedback_intentionally_left_blank.md``.
  - ``from_env(log_fallback=False)`` skips the fallback log for
    legitimate-env-read paths (subprocess agents).
  - Frozen dataclass — mutation raises ``FrozenInstanceError``.
  - ``as_subprocess_env()`` round-trip produces a dict suitable for
    ``subprocess.Popen(env=...)`` injection.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``. No optional-dep skips.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import structlog

from alfred.vault.context import (
    ENV_VAULT_AUDIT_LOG,
    ENV_VAULT_PATH,
    ENV_VAULT_SCOPE,
    ENV_VAULT_SESSION,
    VaultContext,
)


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


class TestConstruction:
    """Direct ``VaultContext(...)`` construction shapes."""

    def test_default_all_none(self):
        """No-arg construction yields all-None fields.

        Caller decides what's required — the dataclass doesn't enforce
        any combination so the same shape works for "full agent
        context" (all 4 fields) and "CLI-only audit context"
        (audit_log_path only).
        """
        ctx = VaultContext()
        assert ctx.vault_path is None
        assert ctx.scope is None
        assert ctx.session_path is None
        assert ctx.audit_log_path is None

    def test_explicit_fields_pass_through(self, tmp_path: Path):
        """Constructor stores values verbatim, no coercion."""
        vault = str(tmp_path / "vault")
        ctx = VaultContext(
            vault_path=vault,
            scope="curator",
            session_path="/tmp/session-abc.json",
            audit_log_path="/var/log/vault_audit.log",
        )
        assert ctx.vault_path == vault
        assert ctx.scope == "curator"
        assert ctx.session_path == "/tmp/session-abc.json"
        assert ctx.audit_log_path == "/var/log/vault_audit.log"

    def test_frozen(self):
        """Dataclass is frozen — mutation raises ``FrozenInstanceError``.

        Frozen because the dispatcher resolves once at top of call chain;
        downstream handlers should treat it as read-only. If a downstream
        needs to "update" a field, it builds a new ``VaultContext``
        rather than mutating in place.
        """
        ctx = VaultContext(vault_path="/tmp/v")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.vault_path = "/tmp/other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# for_testing() factory
# ---------------------------------------------------------------------------


class TestForTesting:
    """The ``for_testing()`` test-fixture factory shape."""

    def test_path_coercion_to_str(self, tmp_path: Path):
        """Path inputs coerce to str (matches the env-var contract).

        Tests typically build ``tmp_path / "vault"`` paths; coercing
        them to str via the factory matches what an env-var read would
        produce.
        """
        vault = tmp_path / "vault"
        session = tmp_path / "session-x.json"
        audit = tmp_path / "audit.log"

        ctx = VaultContext.for_testing(
            vault_path=vault,
            scope="janitor",
            session_path=session,
            audit_log_path=audit,
        )

        assert ctx.vault_path == str(vault)
        assert isinstance(ctx.vault_path, str)
        assert ctx.scope == "janitor"
        assert ctx.session_path == str(session)
        assert isinstance(ctx.session_path, str)
        assert ctx.audit_log_path == str(audit)
        assert isinstance(ctx.audit_log_path, str)

    def test_str_inputs_pass_through(self):
        """String inputs aren't double-wrapped — pass through verbatim."""
        ctx = VaultContext.for_testing(
            vault_path="/tmp/v",
            audit_log_path="/tmp/a.log",
        )
        assert ctx.vault_path == "/tmp/v"
        assert ctx.audit_log_path == "/tmp/a.log"

    def test_none_inputs_preserved(self):
        """None inputs stay None — fixture can build partial contexts."""
        ctx = VaultContext.for_testing(audit_log_path="/tmp/a.log")
        assert ctx.vault_path is None
        assert ctx.scope is None
        assert ctx.session_path is None
        assert ctx.audit_log_path == "/tmp/a.log"

    def test_no_env_fallback_log_emitted(self):
        """``for_testing()`` does NOT emit the ``env_fallback`` log line.

        Distinct from ``from_env()`` — fixture-built contexts are
        intentional construction, not env-reads, so the fallback log
        would be noise. Pin via ``capture_logs`` to lock the behavior.
        """
        with structlog.testing.capture_logs() as captured:
            VaultContext.for_testing(vault_path="/tmp/v")
        fallback_events = [
            c for c in captured if c.get("event") == "vault_context.env_fallback"
        ]
        assert fallback_events == []


# ---------------------------------------------------------------------------
# from_env() factory + structured fallback log
# ---------------------------------------------------------------------------


class TestFromEnv:
    """``VaultContext.from_env()`` reads the 4 vault env vars."""

    def test_reads_all_four_vars(self, monkeypatch):
        """All 4 vault env vars populate the matching field."""
        monkeypatch.setenv(ENV_VAULT_PATH, "/tmp/vault-env")
        monkeypatch.setenv(ENV_VAULT_SCOPE, "distiller")
        monkeypatch.setenv(ENV_VAULT_SESSION, "/tmp/session-env.json")
        monkeypatch.setenv(ENV_VAULT_AUDIT_LOG, "/tmp/audit-env.log")

        ctx = VaultContext.from_env(log_fallback=False)
        assert ctx.vault_path == "/tmp/vault-env"
        assert ctx.scope == "distiller"
        assert ctx.session_path == "/tmp/session-env.json"
        assert ctx.audit_log_path == "/tmp/audit-env.log"

    def test_unset_vars_yield_none(self, monkeypatch):
        """Missing env vars resolve to None, not empty string."""
        monkeypatch.delenv(ENV_VAULT_PATH, raising=False)
        monkeypatch.delenv(ENV_VAULT_SCOPE, raising=False)
        monkeypatch.delenv(ENV_VAULT_SESSION, raising=False)
        monkeypatch.delenv(ENV_VAULT_AUDIT_LOG, raising=False)

        ctx = VaultContext.from_env(log_fallback=False)
        assert ctx.vault_path is None
        assert ctx.scope is None
        assert ctx.session_path is None
        assert ctx.audit_log_path is None

    def test_empty_string_env_treated_as_none(self, monkeypatch):
        """Env var set to "" resolves to None (matches legacy ``_env`` helper).

        The pre-V1 vault/cli.py ``_env(name) or None`` pattern treated
        empty string as unset. We preserve that semantic so the
        env-fallback path is bug-compatible with what the in-process
        consumers had been reading.
        """
        monkeypatch.setenv(ENV_VAULT_PATH, "")
        monkeypatch.setenv(ENV_VAULT_SCOPE, "")
        ctx = VaultContext.from_env(log_fallback=False)
        assert ctx.vault_path is None
        assert ctx.scope is None

    def test_partial_set_some_none(self, monkeypatch):
        """Only-some-set env vars resolve mixed (None + populated)."""
        monkeypatch.setenv(ENV_VAULT_PATH, "/tmp/v")
        monkeypatch.delenv(ENV_VAULT_SCOPE, raising=False)
        monkeypatch.delenv(ENV_VAULT_SESSION, raising=False)
        monkeypatch.setenv(ENV_VAULT_AUDIT_LOG, "/tmp/a.log")

        ctx = VaultContext.from_env(log_fallback=False)
        assert ctx.vault_path == "/tmp/v"
        assert ctx.scope is None
        assert ctx.session_path is None
        assert ctx.audit_log_path == "/tmp/a.log"


class TestFromEnvFallbackLog:
    """Structured-log emission contract for ``from_env``.

    Per ``feedback_intentionally_left_blank.md``: env-fallback emits an
    explicit signal so V2 migration can grep production logs for
    remaining env-only consumers. Per ``feedback_log_emission_test_
    pattern.md``: pin the log emission with ``capture_logs`` so the
    observability doesn't silently degrade across refactors.
    """

    def test_fallback_log_emitted_by_default(self, monkeypatch):
        """``from_env()`` (no args) emits ``vault_context.env_fallback``."""
        monkeypatch.setenv(ENV_VAULT_PATH, "/tmp/v")
        monkeypatch.delenv(ENV_VAULT_SCOPE, raising=False)
        monkeypatch.delenv(ENV_VAULT_SESSION, raising=False)
        monkeypatch.delenv(ENV_VAULT_AUDIT_LOG, raising=False)

        with structlog.testing.capture_logs() as captured:
            VaultContext.from_env()

        fallback = [
            c for c in captured if c.get("event") == "vault_context.env_fallback"
        ]
        assert len(fallback) == 1
        entry = fallback[0]
        assert entry["vault_path_set"] is True
        assert entry["scope_set"] is False
        assert entry["session_set"] is False
        assert entry["audit_log_set"] is False
        # caller defaults to "(unknown)" when not supplied
        assert entry["caller"] == "(unknown)"

    def test_caller_field_lands_in_log(self, monkeypatch):
        """Explicit ``caller=`` kwarg lands in the structured log."""
        monkeypatch.delenv(ENV_VAULT_PATH, raising=False)
        monkeypatch.delenv(ENV_VAULT_SCOPE, raising=False)
        monkeypatch.delenv(ENV_VAULT_SESSION, raising=False)
        monkeypatch.delenv(ENV_VAULT_AUDIT_LOG, raising=False)

        with structlog.testing.capture_logs() as captured:
            VaultContext.from_env(caller="test_suite.example_call")

        fallback = [
            c for c in captured if c.get("event") == "vault_context.env_fallback"
        ]
        assert len(fallback) == 1
        assert fallback[0]["caller"] == "test_suite.example_call"

    def test_log_fallback_false_suppresses_log(self, monkeypatch):
        """``log_fallback=False`` does NOT emit the fallback log.

        Suppression path is for subprocess-side reads (curator/janitor
        agent processes legitimately reading their own env) where the
        fallback log would be noise rather than signal.
        """
        monkeypatch.setenv(ENV_VAULT_PATH, "/tmp/v")
        monkeypatch.setenv(ENV_VAULT_SCOPE, "curator")

        with structlog.testing.capture_logs() as captured:
            VaultContext.from_env(log_fallback=False)

        fallback = [
            c for c in captured if c.get("event") == "vault_context.env_fallback"
        ]
        assert fallback == []

    def test_log_emitted_even_when_all_unset(self, monkeypatch):
        """All-None env still emits the fallback log.

        Per the docstring rationale: "env_fallback fired with all-None"
        is itself a legitimate signal (something tried to resolve vault
        context outside any dispatch). The field-set booleans let
        operator distinguish.
        """
        monkeypatch.delenv(ENV_VAULT_PATH, raising=False)
        monkeypatch.delenv(ENV_VAULT_SCOPE, raising=False)
        monkeypatch.delenv(ENV_VAULT_SESSION, raising=False)
        monkeypatch.delenv(ENV_VAULT_AUDIT_LOG, raising=False)

        with structlog.testing.capture_logs() as captured:
            VaultContext.from_env(caller="all-unset-test")

        fallback = [
            c for c in captured if c.get("event") == "vault_context.env_fallback"
        ]
        assert len(fallback) == 1
        entry = fallback[0]
        assert entry["vault_path_set"] is False
        assert entry["scope_set"] is False
        assert entry["session_set"] is False
        assert entry["audit_log_set"] is False


# ---------------------------------------------------------------------------
# as_subprocess_env() — cross-process boundary rendering
# ---------------------------------------------------------------------------


class TestAsSubprocessEnv:
    """``as_subprocess_env()`` rendering for ``subprocess.Popen(env=...)``.

    V1 preserves subprocess-env injection (agent backends cross
    process boundaries; the cross-process contract stays env vars).
    This factory bridges the two surfaces — a typed in-process
    ``VaultContext`` renders cleanly to the env-dict needed for
    ``subprocess.Popen(env={**os.environ, **ctx.as_subprocess_env()})``.
    """

    def test_full_context_yields_all_four(self):
        """Fully-populated context produces all 4 env-var keys."""
        ctx = VaultContext(
            vault_path="/tmp/v",
            scope="curator",
            session_path="/tmp/s.json",
            audit_log_path="/tmp/a.log",
        )
        env = ctx.as_subprocess_env()
        assert env == {
            ENV_VAULT_PATH: "/tmp/v",
            ENV_VAULT_SCOPE: "curator",
            ENV_VAULT_SESSION: "/tmp/s.json",
            ENV_VAULT_AUDIT_LOG: "/tmp/a.log",
        }

    def test_none_fields_omitted(self):
        """None fields are OMITTED, not rendered as empty strings.

        Critical: ``env={**os.environ, "FOO": ""}`` sets ``FOO`` to
        empty in the subprocess (which the legacy ``_env(name) or
        None`` consumer treats as unset, but renders as "" in
        ``os.environ.get(name)`` — fragile). Omitting the key
        entirely is the safe semantic.
        """
        ctx = VaultContext(vault_path="/tmp/v", audit_log_path="/tmp/a.log")
        env = ctx.as_subprocess_env()
        assert env == {
            ENV_VAULT_PATH: "/tmp/v",
            ENV_VAULT_AUDIT_LOG: "/tmp/a.log",
        }
        assert ENV_VAULT_SCOPE not in env
        assert ENV_VAULT_SESSION not in env

    def test_empty_context_yields_empty_dict(self):
        """All-None context yields empty dict (caller merges with os.environ)."""
        ctx = VaultContext()
        assert ctx.as_subprocess_env() == {}

    def test_round_trip_via_env(self, monkeypatch):
        """``as_subprocess_env`` → set env → ``from_env`` round-trips fields.

        Validates the cross-boundary contract: an in-process context
        rendered to env-vars and then re-read by a subprocess (or by a
        legacy in-process consumer) reconstructs the same context.
        """
        # Clear any pre-existing env first.
        for var in (
            ENV_VAULT_PATH,
            ENV_VAULT_SCOPE,
            ENV_VAULT_SESSION,
            ENV_VAULT_AUDIT_LOG,
        ):
            monkeypatch.delenv(var, raising=False)

        original = VaultContext(
            vault_path="/tmp/v-rt",
            scope="janitor",
            session_path="/tmp/s-rt.json",
            audit_log_path="/tmp/a-rt.log",
        )
        for k, v in original.as_subprocess_env().items():
            monkeypatch.setenv(k, v)

        reconstructed = VaultContext.from_env(log_fallback=False)
        assert reconstructed == original


# ---------------------------------------------------------------------------
# Env-var-name constants
# ---------------------------------------------------------------------------


class TestEnvVarNameConstants:
    """The 4 env-var-name constants are the single source of truth.

    Pin the literal string values so a rename (which would be
    operator-visible breaking change) requires an intentional test
    update. Per rename-grep-discipline checklist item.
    """

    def test_vault_path_literal(self):
        assert ENV_VAULT_PATH == "ALFRED_VAULT_PATH"

    def test_scope_literal(self):
        assert ENV_VAULT_SCOPE == "ALFRED_VAULT_SCOPE"

    def test_session_literal(self):
        assert ENV_VAULT_SESSION == "ALFRED_VAULT_SESSION"

    def test_audit_log_literal(self):
        assert ENV_VAULT_AUDIT_LOG == "ALFRED_VAULT_AUDIT_LOG"
