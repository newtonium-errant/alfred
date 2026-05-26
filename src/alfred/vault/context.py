"""Typed dispatcher-state container for vault env-var threading.

V1 of the refactor that replaces process-global ``os.environ`` injection
with explicit function-arg threading. See CLAUDE.md §"Dispatcher env-var
injection — test-hygiene contract" for the prior-art footgun this fixes.

Background
----------

Several top-level CLI dispatchers (:func:`alfred.cli.cmd_vault`,
:func:`alfred.cli.cmd_distiller`, :func:`alfred.cli.cmd_scaffold`)
historically injected vault env vars into ``os.environ`` before
delegating to downstream handlers:

  * ``ALFRED_VAULT_PATH``
  * ``ALFRED_VAULT_SCOPE``
  * ``ALFRED_VAULT_SESSION``
  * ``ALFRED_VAULT_AUDIT_LOG``

Downstream handlers read them back via ``_env(...)`` helpers. The
process-global mutation is a recurring source of test-bleed: every test
touching a dispatcher path must ``monkeypatch.delenv(<var>,
raising=False)`` at fixture setup or risk env-var carryover masking
validation. The 2026-05-11 issue #64 fix added a 3rd dispatcher mutating
``ALFRED_VAULT_AUDIT_LOG``; CLAUDE.md flagged the pattern as worth
refactoring once the mutation-site count crossed the threshold.

V1 contract
-----------

This module ships ``VaultContext`` — a frozen typed dataclass that
bundles the 4 vault env vars. Dispatchers build a ``VaultContext`` and
thread it as a kwarg to the downstream handler instead of (or in
addition to, during the transition) mutating ``os.environ``.

Downstream handlers accept ``vault_context: VaultContext | None = None``
as a kwarg. When provided, they read from the context object. When
``None`` (legacy entry point, in-tree consumer not yet migrated, etc.)
they fall back to :meth:`VaultContext.from_env` and emit a one-shot
deprecation log so the migration tail can be greped out for V2.

Backward-compat
---------------

The env vars stay functional during the transition. Three layers:

  1. **Explicit ``VaultContext`` kwarg** — preferred; what new code
     should pass.
  2. **Env-var fallback** via :meth:`from_env` — what legacy consumers
     get. Emits a ``vault_context.env_fallback`` log line per
     ``feedback_intentionally_left_blank.md`` (explicit signal vs silent
     fallback) so the V2 migration cycle can grep production logs to
     find remaining env-only call sites.
  3. **Subprocess-env injection** — preserved as-is. Agent backends
     (curator daemon, janitor daemon, distiller pipeline, ``cmd_exec``,
     temporal activities) cross process boundaries and must continue to
     pass env via ``subprocess.Popen(env=...)``. V1 does NOT touch this
     path; the env vars remain the cross-process contract.

V2 work (out of scope)
----------------------

  * Migrate remaining in-process consumers off ``from_env`` fallback.
  * Stop dispatcher writes to ``os.environ`` once all in-process
     consumers accept ``VaultContext`` directly.
  * Subprocess-env injection stays (cross-process contract).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


# Env-var names — single source of truth so future renames can be done in
# one place. Other modules grep for the literal strings today; the
# constants are exported so new code can import them.
ENV_VAULT_PATH = "ALFRED_VAULT_PATH"
ENV_VAULT_SCOPE = "ALFRED_VAULT_SCOPE"
ENV_VAULT_SESSION = "ALFRED_VAULT_SESSION"
ENV_VAULT_AUDIT_LOG = "ALFRED_VAULT_AUDIT_LOG"


@dataclass(frozen=True)
class VaultContext:
    """Bundled dispatcher-state for vault operations.

    Threads the 4 vault env vars as a single typed kwarg, replacing
    process-global ``os.environ`` injection. Frozen because the
    dispatcher resolves all values once at the top of the call chain;
    downstream handlers should treat it as read-only.

    All fields default to ``None`` — meaning "not configured at this
    layer." Downstream gates (e.g. :func:`vault.cli._vault_path`)
    decide whether ``None`` is a hard error or a silent no-op. The
    constructor doesn't enforce any combination; that's the
    consumer's job (e.g. ``vault_path`` is required for any vault
    op, ``audit_log_path`` is optional and triggers the "no audit
    context = silent no-op" branch when absent).
    """

    vault_path: str | None = None
    scope: str | None = None
    session_path: str | None = None
    audit_log_path: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        log_fallback: bool = True,
        caller: str | None = None,
    ) -> "VaultContext":
        """Build a context from the current process env vars.

        Used by legacy entry points and by handlers' backward-compat
        fallback when no explicit context kwarg is threaded through.

        When ``log_fallback`` is True (default), emits a
        ``vault_context.env_fallback`` log line so V2 migration cycles
        can grep production logs to find remaining env-only consumers.
        Pass ``log_fallback=False`` when you're intentionally building
        from env (e.g. inside a subprocess that legitimately reads its
        own env — the curator/janitor/distiller agent processes) and
        the fallback log would be noise rather than signal.

        ``caller`` is an optional string identifying the call site for
        the fallback log (e.g. ``"vault.cli._vault_path"``). When
        present it lands in the structured log so the V2 migration
        cycle can prioritize migration order.
        """
        path = os.environ.get(ENV_VAULT_PATH) or None
        scope = os.environ.get(ENV_VAULT_SCOPE) or None
        session = os.environ.get(ENV_VAULT_SESSION) or None
        audit = os.environ.get(ENV_VAULT_AUDIT_LOG) or None

        if log_fallback:
            # Per feedback_intentionally_left_blank.md: explicit signal
            # vs silent fallback. Always emit, regardless of whether
            # any var was set — "env_fallback fired with all-None" is
            # itself a legitimate signal (something tried to resolve
            # vault context outside any dispatch). Field set lets
            # operator distinguish.
            log.info(
                "vault_context.env_fallback",
                caller=caller or "(unknown)",
                vault_path_set=path is not None,
                scope_set=scope is not None,
                session_set=session is not None,
                audit_log_set=audit is not None,
            )

        return cls(
            vault_path=path,
            scope=scope,
            session_path=session,
            audit_log_path=audit,
        )

    @classmethod
    def for_testing(
        cls,
        *,
        vault_path: str | Path | None = None,
        scope: str | None = None,
        session_path: str | Path | None = None,
        audit_log_path: str | Path | None = None,
    ) -> "VaultContext":
        """Test-fixture factory.

        Same shape as the default constructor but coerces ``Path``
        inputs to strings (test fixtures typically build ``tmp_path /
        "vault"`` paths). No env-fallback log emission. Use this in
        pytest fixtures instead of constructing via ``__init__``
        directly so the intent ("this is test-context, NOT
        from-environment") is grep-able.
        """
        return cls(
            vault_path=str(vault_path) if vault_path is not None else None,
            scope=scope,
            session_path=str(session_path) if session_path is not None else None,
            audit_log_path=str(audit_log_path) if audit_log_path is not None else None,
        )

    def as_subprocess_env(self) -> dict[str, str]:
        """Render the context as a env-var dict for subprocess injection.

        Used by call sites that spawn an agent subprocess and need to
        pass the vault context across the process boundary (curator
        daemon, janitor daemon, distiller pipeline, ``cmd_exec``,
        temporal activities). Subprocess env injection is the
        intentional cross-process contract — V1 does NOT migrate this
        path off env vars, since the agent subprocess reads its own
        env to bootstrap context anyway.

        Only fields with non-None values land in the dict; the caller
        merges with ``os.environ`` (or the existing subprocess env)
        as appropriate.
        """
        out: dict[str, str] = {}
        if self.vault_path is not None:
            out[ENV_VAULT_PATH] = self.vault_path
        if self.scope is not None:
            out[ENV_VAULT_SCOPE] = self.scope
        if self.session_path is not None:
            out[ENV_VAULT_SESSION] = self.session_path
        if self.audit_log_path is not None:
            out[ENV_VAULT_AUDIT_LOG] = self.audit_log_path
        return out


__all__ = [
    "VaultContext",
    "ENV_VAULT_PATH",
    "ENV_VAULT_SCOPE",
    "ENV_VAULT_SESSION",
    "ENV_VAULT_AUDIT_LOG",
]
